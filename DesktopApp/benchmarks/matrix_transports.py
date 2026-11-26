"""Reproducible TLS/proxy + real Qt matrix benchmark; synthetic data only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt5.QtCore import QTimer, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtWidgets import QApplication
from https_fixture import TLSEndpoint
from quaber.storage import UserStore
from quaber.kubectl import ProxyManager, locate_kubectl
from quaber.kubernetes import Kubernetes
from quaber.request_policy import RequestPolicy
from quaber.endpoint_budget import EndpointBudget
from quaber.cluster_workers import configure_cluster_threads
from quaber.fetch_plan import FetchPlan, collect_cluster
from quaber.ui import MainWindow
from quaber.shutdown import release_window


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--counts', default=','.join(map(str, range(51))))
    parser.add_argument('--repetitions', default=2, type=int)
    parser.add_argument('--delay-ms', default=50, type=float)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    counts = [int(c) for c in args.counts.split(',')]
    preferences_file = ROOT/'UserData'/'preferences.json'
    preferences = json.loads(preferences_file.read_text(encoding='utf-8')) if preferences_file.exists() else {}
    executable = locate_kubectl(ROOT, preferences.get('kubectl_path', ''))
    metadata = dict(python=sys.version, qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR,
        platform=platform.platform(), qt_platform='offscreen', geometry=[1450,900],
        counts=counts, repetitions=args.repetitions, server_delay_ms=args.delay_ms,
        rate=2, burst=2, namespaces=10, deployments=50, pods=200,
        source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in [*sorted((ROOT/'quaber').glob('*.py')), Path(__file__), ROOT/'tests'/'https_fixture.py']},
        kubectl_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
    (output/'environment.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    app = QApplication.instance() or QApplication([])
    window = None
    with tempfile.TemporaryDirectory(prefix='quaber-matrix-bench-') as directory:
        folder = Path(directory)
        endpoint = TLSEndpoint(folder, delay=args.delay_ms/1000)
        store = UserStore(folder/'UserData')
        proxy = ProxyManager(executable, store)
        api = None
        try:
            cluster = store.put_cluster('Benchmark', endpoint.server_url, 'fixture', endpoint.config())
            store.save_preferences(dict(store.preferences(),refresh_ms=0,kubectl_path=str(executable),idle_minutes=240))
            def no_login(*_): raise RuntimeError('Synthetic benchmark never logs in.')
            api = Kubernetes(store, proxy, authorization=SimpleNamespace(renew=no_login, block=lambda *_: None))
            with patch('quaber.ui.locate_kubectl', return_value=executable), \
                 patch.object(MainWindow, '_prompt_expired_login'), \
                 patch.object(MainWindow, '_load_initial_matrix'), patch.object(MainWindow, 'refresh'):
                window = MainWindow(store, ROOT)
                window.resize(1450,900)
                window.show()
                app.processEvents()
                for timer in window.findChildren(QTimer): timer.stop()
                # Do not benchmark view migration or preference disk writes.
                window.views.active.update(selectedClusterIds=None, selectedNamespaces=None, selectedDeployments=None)
                window._save_preferences = lambda: None
                window._save_view_controls = lambda: None
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix='bench-collector') as collector:
                    cold = time.perf_counter()
                    collector.submit(proxy.url,cluster).result(timeout=20)
                    metadata['cold_proxy_start_seconds'] = time.perf_counter()-cold
                    # Warm TLS contexts/process and Qt widgets once, outside measurements.
                    for mode in ('proxy','https'):
                        api.set_transport_mode(mode)
                        api.request_policy = RequestPolicy(endpoint_budget=EndpointBudget(folder/f'warm-{mode}.sqlite3'))
                        collector.submit(api.namespaces, cluster['id']).result(timeout=10)
                    warm_plan = FetchPlan.capture([cluster],dict(selectedNamespaces=None),0)
                    warm_snapshot, failures = collector.submit(collect_cluster,api,warm_plan,warm_plan.clusters[0]).result(timeout=25)
                    assert not failures
                    window.snapshot = {cluster['id']:warm_snapshot}
                    window.render_matrix()
                    app.processEvents()
                    window.matrix.grab().toImage()
                    for timer in window.findChildren(QTimer): timer.stop()
                    fields = ['mode','threads','repetition','load_seconds','render_ms','total_seconds',
                              'cpu_seconds','requests','bytes','policy_peak','server_peak','pods','cells','columns','rows']
                    with (output/'measurements.csv').open('w',newline='',encoding='utf-8') as handle, \
                         (output/'requests.jsonl').open('w',encoding='utf-8') as requests:
                        writer = csv.DictWriter(handle,fields)
                        writer.writeheader()
                        for repetition in range(args.repetitions):
                            for count in counts:
                                modes = ('proxy','https') if (count+repetition)%2 == 0 else ('https','proxy')
                                for mode in modes:
                                    api.set_transport_mode(mode)
                                    api.cache.invalidate()
                                    api.request_policy = RequestPolicy(endpoint_budget=EndpointBudget(folder/f'{mode}-{count}-{repetition}.sqlite3'))
                                    configure_cluster_threads(api,count)
                                    plan = FetchPlan.capture([cluster],dict(selectedNamespaces=None),0)
                                    window._reset_matrix_state()
                                    app.processEvents()
                                    with endpoint.lock:
                                        assert endpoint.active == 0
                                        endpoint.calls.clear()
                                        endpoint.peak = 0
                                    cpu = time.process_time()
                                    start = time.perf_counter()
                                    snapshot, failures = collector.submit(collect_cluster,api,plan,plan.clusters[0]).result(timeout=25)
                                    loaded = time.perf_counter()
                                    if failures: raise RuntimeError(str(failures))
                                    scopes = snapshot['namespaces']
                                    pods = sum(len(s['pods']) for s in scopes.values())
                                    assert len(scopes) == 10 and pods == 200
                                    window.snapshot = {cluster['id']:snapshot}
                                    window.render_matrix()
                                    app.processEvents()
                                    window.matrix.viewport().repaint()
                                    frame = window.matrix.grab().toImage()
                                    finished = time.perf_counter()
                                    cells = len(window.matrix_data)
                                    assert cells == 50, cells
                                    assert window.matrix.columnCount() == 11
                                    assert window.matrix.rowCount() == 5
                                    assert sum(len(ref[4]) for ref in window.matrix_data.values()) == 200
                                    stats = api.request_policy.diagnostics()
                                    assert stats['attempts'] == 9, stats
                                    with endpoint.lock:
                                        assert len(endpoint.calls) == 9
                                        calls, peak = list(endpoint.calls),endpoint.peak
                                    assert peak <= max(1,count)
                                    record = dict(mode=mode,threads=count,repetition=repetition+1,
                                        load_seconds=loaded-start,render_ms=(finished-loaded)*1000,total_seconds=finished-start,
                                        cpu_seconds=time.process_time()-cpu, requests=stats['attempts'],bytes=stats['bytes'],
                                        policy_peak=stats['peak'],server_peak=peak,pods=pods,cells=cells,
                                        columns=window.matrix.columnCount(),rows=window.matrix.rowCount())
                                    writer.writerow(record)
                                    handle.flush()
                                    requests.write(json.dumps(dict(mode=mode,threads=count,repetition=repetition+1,
                                        calls=[dict(path=c['path'],seconds=c['time']-start) for c in calls]))+'\n')
                                    requests.flush()
                                    if count == counts[0] and repetition == 0:
                                        frame.save(str(output/f'matrix-{mode}.png'))
                                    print(f'{mode} threads={count:02d} rep={repetition+1} load={loaded-start:.3f}s render={(finished-loaded)*1000:.1f}ms peak={peak}',flush=True)
                                    for timer in window.findChildren(QTimer): timer.stop()
                                    app.processEvents()
            (output/'environment.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
            (output/'COMPLETE').write_text('All requested measurements and invariants passed.\n',encoding='utf-8')
        finally:
            if window is not None:
                window.close()
                window._cleanup_thread.join(8)
                release_window(window)
                app.processEvents()
            if api is not None: api.close()
            proxy.close()
            endpoint.close()


if __name__ == '__main__': main()
