"""Print connection metadata and read-only lab inventory, never credentials."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from quaber.storage import UserStore
from quaber.kubernetes import Kubernetes
from quaber.kubectl import ProxyManager,locate_kubectl
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--read',action='store_true')
    parser.add_argument('--renew',action='store_true')
    parser.add_argument('--standard',action='store_true')
    args=parser.parse_args()
    raw=json.loads((ROOT/'UserData/clusters.json').read_text(encoding='utf-8'))
    clusters=raw if isinstance(raw,list) else raw.get('clusters',[])
    print(json.dumps([dict(name=c['name'],id=c['id'],server=c['server'],enabled=c.get('enabled'),
        has_config=bool(c.get('encryptedKubeconfig'))) for c in clusters]))
    if not args.read:return
    store=UserStore(ROOT/'UserData')
    cluster=next(c for c in store.clusters() if c['name']=='lab1')
    temporary=None
    if args.standard:
        from quaber.kubeconfig_import import read_contexts
        contexts=read_contexts(Path.home()/'.kube/config')
        print(json.dumps(dict(standard_contexts=[dict(name=c['name'],server=c['server']) for c in contexts])))
        selected=next((c for c in contexts if c['server']==cluster['server']),None)
        if selected is None:return
        temporary=tempfile.TemporaryDirectory(prefix='quaber-lab-inspect-')
        store=UserStore(Path(temporary.name)/'UserData')
        cluster=store.put_cluster('lab1',selected['server'],selected['username'],selected['kubeconfig'])
    executable=locate_kubectl(ROOT,store.preferences().get('kubectl_path',''))
    proxy=ProxyManager(executable,store)
    def no_login(*_):raise RuntimeError('Explicit lab inspection does not renew credentials.')
    if args.renew:
        available=bool(store.saved_password(cluster))
        print(json.dumps(dict(saved_password_available=available)))
        if not available:return
    api=Kubernetes(store,proxy,**({} if args.renew else dict(authorization=SimpleNamespace(renew=no_login,block=lambda *_:None))))
    api.set_transport_mode('https')
    try:
        def read():
            namespaces=api.namespaces(cluster['id'])
            nodes=api.request(cluster['id'],'/api/v1/nodes')
            pods=api.request(cluster['id'],'/api/v1/pods?limit=250')
            print(json.dumps(dict(namespaces=namespaces,nodes=[dict(name=n['metadata']['name'],
                capacity=n.get('status',{}).get('capacity'),allocatable=n.get('status',{}).get('allocatable')) for n in nodes.get('items',[])],
                pods=len(pods.get('items',[])),continuation=bool(pods.get('metadata',{}).get('continue')))))
        with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(read).result(timeout=30)
    finally:
        api.close();proxy.close()
        if temporary:temporary.cleanup()


if __name__=='__main__':main()
