"""Use an existing lab connection without exposing or exporting credentials."""
from pathlib import Path
from quaber.storage import UserStore
from quaber.kubeconfig_import import read_contexts
from quaber.kubectl import locate_kubectl


def open_lab(root, work, *, name='lab1', kubeconfig=None):
    original=UserStore(root/'UserData')
    cluster=next(c for c in original.clusters() if c['name']==name)
    # Explicitly bound to the known lab; production entries cannot be selected.
    if name not in ('lab1','lab2') or cluster['server'] not in ('https://192.168.56.11:6443','https://192.168.56.12:6443'):
        raise ValueError('Only the configured laboratory endpoints are supported.')
    executable=locate_kubectl(root,original.preferences().get('kubectl_path',''))
    if kubeconfig:
        context=next(c for c in read_contexts(kubeconfig) if c['server']==cluster['server'])
        config,username=context['kubeconfig'],context['username']
    else:
        config,username=original.decrypt(cluster['encryptedKubeconfig']),cluster['username']
    store=UserStore(Path(work)/'UserData')
    candidate=store.put_cluster(name,cluster['server'],username,config)
    store.save_preferences(dict(store.preferences(),kubectl_path=str(executable),refresh_ms=0))
    return store,candidate,executable
