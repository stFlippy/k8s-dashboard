"""Explicit-root test runner with parent-owned timeout/crash evidence."""
import argparse
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import unittest
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quaber.processes import start_process, stop_process


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from flatten(item)
        else: yield item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--test', action='append', default=[])
    parser.add_argument('--module', action='append', default=[])
    parser.add_argument('--suite', choices=['full', 'inventory', 'selected'])
    parser.add_argument('--deadline', type=float, default=30)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--scope', choices=['project','external'], default='project')
    parser.add_argument('--test-file', type=Path)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output_dir.resolve()
    if not (root/'main.py').is_file(): parser.error('--root must be DesktopApp')
    if output.exists(): parser.error('output-dir already exists; evidence is immutable')
    output.mkdir(parents=True)
    os.chdir(root)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    sys.path[:0] = [str(root), str(root/'tests')]
    files = [*sorted((root/'quaber').glob('*.py')), *sorted((root/'tests').glob('*.py')),
             root/'main.py', root/'requirements.lock']
    (output/'manifest.json').write_text(json.dumps(dict(python=sys.version, executable=sys.executable,
        hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}),indent=2),encoding='utf-8')
    loader = unittest.defaultTestLoader
    external = {'test_windows_login': 'External Addons parity; excluded before import from project scope.'}
    def included(module): return (module in external) == (args.scope == 'external')
    manifest_scope = dict(scope=args.scope, excluded=external if args.scope=='project' else {})
    (output/'scope.json').write_text(json.dumps(manifest_scope,indent=2),encoding='utf-8')
    if args.test_file:
        args.test.extend(json.loads(args.test_file.read_text(encoding='utf-8')))
    if args.test or args.module:
        if any(not included(name.split('.')[0]) for name in args.test+args.module):
            parser.error('Explicit test/module is outside requested scope; nothing imported.')
        suite = loader.loadTestsFromNames(args.test + args.module)
    else:
        tests, aliases = [], []
        for path in sorted((root/'tests').glob('test_*.py')):
            if not included(path.stem): continue
            for test in flatten(loader.loadTestsFromName(path.stem)):
                if test.__class__.__module__ != path.stem and not isinstance(test, unittest.loader._FailedTest):
                    aliases.append(dict(discovered_in=path.stem, canonical=test.id()))
                else: tests.append(test)
        suite = unittest.TestSuite(tests)
        (output/'duplicate-ledger.json').write_text(json.dumps(aliases,indent=2),encoding='utf-8')
        if args.suite == 'selected':
            suite = unittest.TestSuite(t for t in flatten(suite) if t.id().split('.')[0].startswith(
                ('test_v3_', 'test_perf_', 'test_followup_', 'test_implementation_')))
    ids = [t.id() for t in flatten(suite)]
    (output/'test-ids.json').write_text(json.dumps(ids,indent=2),encoding='utf-8')
    (output/'plan-counts.json').write_text(json.dumps(dict(occurrences=len(ids), unique_ids=len(set(ids)),
        modules=len({i.split('.')[0] for i in ids}), duplicate_ids={k:v for k,v in Counter(ids).items() if v>1}),indent=2),encoding='utf-8')
    if args.child:
        errors, current = [], [None]
        occurrence = [0]
        fault = (output/'fault.log').open('w',encoding='utf-8')
        faulthandler.enable(file=fault,all_threads=True)
        events = (output/'progress.jsonl').open('w',encoding='utf-8')
        def event(stage, **values):
            events.write(json.dumps(dict(stage=stage,test=current[0],occurrence=occurrence[0],monotonic=time.monotonic(),**values))+'\n')
            events.flush()
        def hook(*exc):
            diagnostic = ''.join(traceback.format_exception(*exc))
            errors.append(diagnostic)
            event('callback_error', traceback=diagnostic, exception_type=exc[0].__name__)
            os.fsync(events.fileno())
            (output/'callback-errors.json').write_text(json.dumps(errors,indent=2),encoding='utf-8')
        sys.excepthook = hook
        from PyQt5.QtCore import QCoreApplication, QEvent, QTimer
        from PyQt5.QtWidgets import QApplication, QDialog
        app = QApplication.instance() or QApplication([])
        dump_timer = [None]
        def stop_dump_timer():
            timer, dump_timer[0] = dump_timer[0], None
            if timer is not None:
                timer.cancel()
                timer.join()
        modal_since = {}
        def modal_guard():
            now = time.monotonic()
            for widget in app.topLevelWidgets():
                if isinstance(widget, QDialog) and widget.isVisible() and widget.isModal():
                    first = modal_since.setdefault(id(widget), now)
                    if now-first > 2:
                        errors.append('Unexpected modal: '+type(widget).__name__+': '+widget.windowTitle())
                        event('unexpected_modal',dialog=type(widget).__name__,title=widget.windowTitle())
                        widget.reject()
        guard = QTimer()
        guard.timeout.connect(modal_guard)
        guard.start(100)
        class Result(unittest.TextTestResult):
            def startTest(self, test):
                occurrence[0] += 1
                current[0] = test.id()
                event('start')
                # The native watchdog reads Python frames without the GIL and
                # can race frame teardown (CPython gh-140815). Enter through a
                # Python thread instead; the parent still owns the hard timeout
                # even if a child cannot acquire the GIL to produce this dump.
                stop_dump_timer()
                timer = dump_timer[0] = threading.Timer(min(20,args.deadline*.7),
                    lambda: faulthandler.dump_traceback(file=fault,all_threads=True))
                timer.daemon = True
                timer.start()
                super().startTest(test)
            def stopTest(self, test):
                stop_dump_timer()
                QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
                app.processEvents()
                event('stop')
                super().stopTest(test)
                (output/'partial-result.json').write_text(json.dumps(dict(
                    executions=self.testsRun, failures=[(str(t),s) for t,s in self.failures],
                    errors=[(str(t),s) for t,s in self.errors], skipped=[(str(t),s) for t,s in self.skipped],
                    callback_errors=errors, successful=False),indent=2),encoding='utf-8')
        result = unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
        stop_dump_timer()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        app.processEvents()
        guard.stop()
        events.close()
        data = dict(executions=result.testsRun, failures=[(str(t),s) for t,s in result.failures],
                    errors=[(str(t),s) for t,s in result.errors], skipped=[(str(t),s) for t,s in result.skipped],
                    callback_errors=errors, successful=result.wasSuccessful() and not errors)
        (output/'result.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
        return 0 if data['successful'] else 1
    groups = [ids] if args.suite == 'full' else []
    if not groups:
        for module in dict.fromkeys(i.split('.')[0] for i in ids):
            groups.append([i for i in ids if i.split('.')[0] == module])
    records = []
    while groups:
        selected = groups.pop(0)
        target = output/f'child-{len(records):03d}'
        parse_errors = []
        def read_events(path):
            values = []
            if not path.exists(): return values
            for number, line in enumerate(path.read_text(encoding='utf-8',errors='replace').splitlines(),1):
                try: values.append(json.loads(line))
                except (ValueError, TypeError) as exc:
                    diagnostic = dict(path=str(path),line=number,error=str(exc))
                    if diagnostic not in parse_errors: parse_errors.append(diagnostic)
            return values
        def read_document(path):
            if not path.exists(): return None
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(value,dict): raise ValueError('result must be a JSON object')
                return value
            except (ValueError,UnicodeError) as exc:
                parse_errors.append(dict(path=str(path),error=str(exc)))
                return None
        command = [sys.executable,'-B',str(Path(__file__).resolve()),'--root',str(root),
                   '--output-dir',str(target),'--child','--deadline',str(args.deadline),'--scope',args.scope]
        test_file = output/(target.name+'-requested.json')
        test_file.write_text(json.dumps(selected),encoding='utf-8')
        command += ['--test-file',str(test_file)]
        with (output/(target.name+'.log')).open('w',encoding='utf-8') as log:
            try:
                process = start_process(command,cwd=root,stdout=log,stderr=subprocess.STDOUT)
            except OSError as exc:
                records.append(dict(command=command,pid=None,exit=None,status='CRASH',startup_error=str(exc)))
                (output/'summary.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
                continue
            started, timed_out = time.monotonic(), False
            last_events = []
            while process.poll() is None:
                path = target/'progress.jsonl'
                if path.exists():
                    last_events = read_events(path)
                lifecycle = [e for e in last_events if e['stage'] in ('start','stop')]
                last = lifecycle[-1] if lifecycle else None
                elapsed = time.monotonic() - (last['monotonic'] if last else started)
                if elapsed > args.deadline:
                    timed_out = True
                    stop_process(process)
                    break
                time.sleep(.1)
            exit_code = process.wait()
            stop_process(process)  # Release job and any pipe-holding descendants.
            # A fast crash can write startTest between the final poll and exit.
            # Re-read durable progress before deciding which IDs remain.
            path = target/'progress.jsonl'
            if path.exists():
                last_events = read_events(path)
        child_result = target/'result.json'
        final_data = read_document(child_result)
        completed = final_data is not None
        partial = target/'partial-result.json'
        evidence = child_result if completed else partial
        data = final_data if completed else read_document(evidence)
        status = 'TIMEOUT' if timed_out else 'CRASH' if not completed else 'PASS' if data['successful'] else 'FAIL'
        records.append(dict(command=command,pid=process.pid,exit=exit_code,status=status,
                            last_events=last_events[-4:],result=data,
                            parse_errors=parse_errors,
                            callback_errors=[e.get('traceback', e) for e in last_events if e['stage']=='callback_error']))
        if not completed:
            attempted = Counter(e['test'] for e in last_events if e['stage']=='start')
            remaining = []
            for test_id in selected:
                if attempted[test_id]: attempted[test_id] -= 1
                else: remaining.append(test_id)
            if last_events and remaining: groups.insert(0,remaining)
        (output/'summary.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
    return 0 if all(r['status']=='PASS' for r in records) else 1


if __name__ == '__main__': raise SystemExit(main())
