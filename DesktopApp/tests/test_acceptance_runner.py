"""Runner failures are injected only into synthetic child test modules."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RunnerContracts(unittest.TestCase):
    def test_callback_trace_survives_immediate_child_crash(self):
        runner = Path(__file__).with_name('run_acceptance.py')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'tests').mkdir(); (root/'quaber').mkdir()
            (root/'main.py').write_text(''); (root/'requirements.lock').write_text('')
            (root/'tests/test_callback_fixture.py').write_text(
                'import os,sys,unittest\nclass Cases(unittest.TestCase):\n'
                ' def test_a_crash(self):\n'
                '  try: raise RuntimeError("durable synthetic callback")\n'
                '  except RuntimeError: sys.excepthook(*sys.exc_info())\n'
                '  os._exit(9)\n'
                ' def test_b_ok(self): self.assertTrue(True)\n')
            output = root/'evidence'
            process = subprocess.run([sys.executable,'-B',str(runner),'--root',str(root),
                '--output-dir',str(output),'--module','test_callback_fixture','--scope','project'],
                capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,1)
            summary = json.loads((output/'summary.json').read_text())
            self.assertEqual([r['status'] for r in summary],['CRASH','PASS'])
            self.assertEqual(summary[0]['exit'],9)
            self.assertIn('durable synthetic callback',summary[0]['callback_errors'][0])
            self.assertIn('Traceback',summary[0]['callback_errors'][0])

    def test_project_scope_excludes_external_before_import(self):
        runner = Path(__file__).with_name('run_acceptance.py')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'tests').mkdir(); (root/'quaber').mkdir()
            (root/'main.py').write_text(''); (root/'requirements.lock').write_text('')
            (root/'tests/test_windows_login.py').write_text('raise RuntimeError("external module imported")')
            (root/'tests/test_local_fixture.py').write_text('import unittest\nclass Cases(unittest.TestCase):\n def test_ok(self): pass\n')
            output = root/'evidence'
            process = subprocess.run([sys.executable,'-B',str(runner),'--root',str(root),'--output-dir',str(output),
                                      '--suite','inventory','--scope','project'],capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertIn('test_windows_login',json.loads((output/'scope.json').read_text())['excluded'])
    def test_timeout_crash_and_continue_have_parent_json(self):
        runner = Path(__file__).with_name('run_acceptance.py')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'tests').mkdir()
            (root/'quaber').mkdir()
            (root/'main.py').write_text('')
            (root/'requirements.lock').write_text('')
            (root/'tests'/'test_runner_fixture.py').write_text(
                'import os,time,unittest\nclass Cases(unittest.TestCase):\n'
                ' def test_a_timeout(self): time.sleep(4)\n'
                ' def test_b_crash(self): os._exit(7)\n'
                ' def test_c_ok(self): self.assertTrue(True)\n')
            destination = root/'evidence'
            command = [sys.executable,'-B',str(runner),'--root',str(root),'--output-dir',str(destination),
                       '--module','test_runner_fixture','--deadline','1.5']
            result = subprocess.run(command,capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,1,result.stderr)
            summary = json.loads((destination/'summary.json').read_text())
            self.assertEqual([s['status'] for s in summary],['TIMEOUT','CRASH','PASS'])
            self.assertEqual(summary[1]['exit'],7)
            duplicate = subprocess.run(command,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(duplicate.returncode,0)
            self.assertIn('already exists',duplicate.stderr)
