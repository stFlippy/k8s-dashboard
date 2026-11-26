"""Sustained lab API reads and real matrix renders across thread/QPS settings."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(ROOT/'benchmarks')]
os.environ['QT_QPA_PLATFORM']='offscreen'
from PyQt5.QtCore import QTimer,QT_VERSION_STR,PYQT_VERSION_STR
from PyQt5.QtWidgets import QApplication
from quaber.storage import UserStore
from quaber.kubectl import ProxyManager,locate_kubectl
from quaber.request_policy import RequestPolicy
from quaber.endpoint_budget import EndpointBudget
from quaber.cluster_workers import configure_cluster_threads
from quaber.fetch_plan import FetchPlan,collect_cluster
from quaber.ui import MainWindow
from quaber.shutdown import release_window
from http_timing import MeasuredKubernetes
from lab_connection import open_lab
from lab_dataset import LabDataset


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--kubeconfig',type=Path)
    parser.add_argument('--synthetic',action='store_true',help='Instrumentation pilot only, never labelled as a real cluster result.')
    parser.add_argument('--rates',default='2,4,6,8,10')
    parser.add_argument('--threads',default=','.join(map(str,range(1,26,2))))
    parser.add_argument('--repetitions',type=int,default=2)
    parser.add_argument('--warm-seconds',type=float,default=5)
    args=parser.parse_args()
    out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    rates=[int(v) for v in args.rates.split(',')]
    counts=[int(v) for v in args.threads.split(',')]
    app=QApplication.instance() or QApplication([])
    endpoint=dataset=api=proxy=window=None
    metadata=dict(kind='synthetic-instrumentation-pilot' if args.synthetic else 'real-lab',cluster='lab1',
        python=sys.version,qt=QT_VERSION_STR,pyqt=PYQT_VERSION_STR,qt_platform='offscreen',geometry=[1450,900],
        rates=rates,threads=counts,repetitions=args.repetitions,burst=2,started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [*sorted((ROOT/'quaber').glob('*.py')),*sorted((ROOT/'benchmarks').glob('*.py'))]})
    try:
        with tempfile.TemporaryDirectory(prefix='quaber-lab-load-') as directory:
            folder=Path(directory)
            try:
                if args.synthetic:
                    from https_fixture import TLSEndpoint
                    endpoint=TLSEndpoint(folder,delay=.05)
                    original=UserStore(ROOT/'UserData')
                    executable=locate_kubectl(ROOT,original.preferences().get('kubectl_path',''))
                    store=UserStore(folder/'UserData')
                    cluster=store.put_cluster('Benchmark',endpoint.server_url,'fixture',endpoint.config())
                    selected=[f'ns-{i:02d}' for i in range(1,11)]
                    metadata['dataset']=dict(pods=200,running=200)
                    store.save_preferences(dict(store.preferences(),kubectl_path=str(executable),refresh_ms=0))
                else:
                    store,cluster,executable=open_lab(ROOT,folder,kubeconfig=args.kubeconfig)
                    # Check read access and allocation capacity before any lab write.
                    dataset=LabDataset(store,cluster,out/'cleanup-state.json')
                    nodes=dataset.request('GET','/api/v1/nodes')['items']
                    metadata['nodes']=[dict(name=n['metadata']['name'],allocatable=n.get('status',{}).get('allocatable')) for n in nodes]
                    metadata['dataset']=dataset.create()
                    selected=list(dataset.namespaces)
                proxy=ProxyManager(executable,store)
                def no_login(*_):raise RuntimeError('Load tests never retry login passwords.')
                api=MeasuredKubernetes(store,proxy,authorization=SimpleNamespace(renew=no_login,block=lambda *_:None),
                    policy=RequestPolicy(endpoint_budget=EndpointBudget(folder/'shared-budget.sqlite3')))
                with patch('quaber.ui.locate_kubectl',return_value=executable),patch.object(MainWindow,'refresh'), \
                     patch.object(MainWindow,'_load_initial_matrix'),patch.object(MainWindow,'_prompt_expired_login'):
                    window=MainWindow(store,ROOT)
                    window.resize(1450,900)
                    window.show()
                    app.processEvents()
                    window._save_preferences=lambda:None
                    window._save_view_controls=lambda:None
                    window.views.active.update(selectedClusterIds=None,selectedNamespaces=selected,selectedDeployments=None)
                    for timer in window.findChildren(QTimer):timer.stop()
                    metadata['selection_count']=len(selected)
                    (out/'environment.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
                    fields=['mode','rate','threads','repetition','load_seconds','render_ms','total_seconds','requests',
                            'bytes','policy_peak','pods','cells','actual_rps','api_mean_ms','queue_mean_ms']
                    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='lab-collector') as collector, \
                         (out/'measurements.csv').open('w',newline='',encoding='utf-8') as csv_file, \
                         (out/'http.jsonl').open('w',encoding='utf-8') as traces:
                        writer=csv.DictWriter(csv_file,fields)
                        writer.writeheader()
                        start=time.perf_counter()
                        collector.submit(proxy.url,cluster).result(timeout=20)
                        metadata['cold_proxy_seconds']=time.perf_counter()-start
                        # Exclude first font, model and TLS-context initialization from timings.
                        api.set_transport_mode('https')
                        plan=FetchPlan.capture([cluster],dict(selectedNamespaces=None),0)
                        initial,failures=collector.submit(collect_cluster,api,plan,plan.clusters[0]).result(timeout=60)
                        if failures:raise RuntimeError('Initial lab collection failed: '+str(failures))
                        window.snapshot={cluster['id']:initial}
                        window.render_matrix();app.processEvents();window.matrix.grab().toImage()
                        for timer in window.findChildren(QTimer):timer.stop()
                        for rate in rates:
                            api.request_policy.set_cluster_rate(rate)
                            configure_cluster_threads(api,25)
                            api.set_transport_mode('https')
                            warm_until=time.monotonic()+args.warm_seconds
                            while time.monotonic()<warm_until:
                                api.cache.invalidate()
                                _,failures=collector.submit(collect_cluster,api,plan,plan.clusters[0]).result(timeout=60)
                                if failures:raise RuntimeError('Warmup failed: '+str(failures))
                            api.take_records()
                            for repetition in range(args.repetitions):
                                order=counts if repetition%2==0 else list(reversed(counts))
                                for count in order:
                                    modes=('proxy','https') if (count+repetition)%4==1 else ('https','proxy')
                                    for mode in modes:
                                        api.set_transport_mode(mode)
                                        configure_cluster_threads(api,count)
                                        api.cache.invalidate()
                                        window._reset_matrix_state();app.processEvents()
                                        api.take_records()
                                        with api.request_policy.condition:
                                            api.request_policy.stats['peak']=api.request_policy.inflight
                                        before=api.request_policy.diagnostics()
                                        started=time.perf_counter()
                                        snapshot,failures=collector.submit(collect_cluster,api,plan,plan.clusters[0]).result(timeout=90)
                                        loaded=time.perf_counter()
                                        if failures:raise RuntimeError('Matrix collection failed: '+str(failures))
                                        scope={ns:snapshot['namespaces'][ns] for ns in selected}
                                        assert sum(len(s['pods']) for s in scope.values())==200
                                        window.snapshot={cluster['id']:snapshot}
                                        window.render_matrix();app.processEvents();window.matrix.viewport().repaint()
                                        frame=window.matrix.grab().toImage()
                                        ended=time.perf_counter()
                                        assert len(window.matrix_data)==50 and sum(len(v[4]) for v in window.matrix_data.values())==200
                                        records=api.take_records()
                                        assert records and all(r['status']==200 for r in records),records
                                        after=api.request_policy.diagnostics()
                                        calls=after['attempts']-before['attempts']
                                        assert calls==len(records)
                                        record=dict(mode=mode,rate=rate,threads=count,repetition=repetition+1,
                                            load_seconds=loaded-started,render_ms=(ended-loaded)*1000,total_seconds=ended-started,
                                            requests=calls,bytes=after['bytes']-before['bytes'],policy_peak=after['peak'],pods=200,cells=50,
                                            actual_rps=calls/(loaded-started),api_mean_ms=sum(r['response_ms'] for r in records)/len(records),
                                            queue_mean_ms=sum(r['queue_ms'] for r in records)/len(records))
                                        writer.writerow(record);csv_file.flush()
                                        for physical in records:
                                            physical['start']-=started
                                            traces.write(json.dumps(dict(mode=mode,rate=rate,threads=count,repetition=repetition+1,**physical))+'\n')
                                        traces.flush()
                                        if rate==rates[0] and count==counts[0] and repetition==0:frame.save(str(out/f'matrix-{mode}.png'))
                                        for timer in window.findChildren(QTimer):timer.stop()
                                        print(f'{mode} rate={rate} threads={count} rep={repetition+1}: load={loaded-started:.3f}s render={(ended-loaded)*1000:.1f}ms HTTP={record["api_mean_ms"]:.1f}ms',flush=True)
                    metadata['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
                    (out/'environment.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
            finally:
                try:
                    if window is not None:
                        window.close();window._cleanup_thread.join(10);release_window(window);app.processEvents()
                    if api is not None:api.close()
                    if proxy is not None:proxy.close()
                finally:
                    try:
                        if dataset is not None and dataset.started:dataset.cleanup()
                    finally:
                        if endpoint is not None:endpoint.close()
        (out/'COMPLETE').write_text('All requested measurements, invariants and cleanup passed.\n',encoding='utf-8')
    except BaseException as exc:
        (out/'FAILED.json').write_text(json.dumps(dict(type=type(exc).__name__,message=str(exc)[:1000])),encoding='utf-8')
        raise


if __name__=='__main__':main()
