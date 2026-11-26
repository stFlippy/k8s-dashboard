"""Create and remove only this run's uniquely labelled lab namespaces."""
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid
from quaber.https_transport import HTTPSConnection


class LabDataset:
    def __init__(self,store,cluster,state):
        self.connection=HTTPSConnection(store,cluster)
        self.state=Path(state)
        self.run=uuid.uuid4().hex[:10]
        self.namespaces=[]
        self.owned=[]
        self.started=False
    def request(self,method,path,body=None):
        raw=json.dumps(body).encode() if body is not None else None
        headers=dict(self.connection.headers)
        if raw is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(self.connection.server+path,headers=headers,data=raw,method=method)
        with self.connection.opener.open(request,timeout=20) as response:
            return json.loads(response.read(4*1024*1024))
    def save(self):
        self.state.write_text(json.dumps(dict(run=self.run,namespaces=self.owned),indent=2),encoding='utf-8')
    def create(self):
        self.started=True
        self.save()  # Persist the selector before a POST with an unknown outcome.
        for number in range(1,11):
            ns=f'quaber-load-{self.run}-{number:02d}'
            result=self.request('POST','/api/v1/namespaces',dict(apiVersion='v1',kind='Namespace',
                metadata=dict(name=ns,labels={'quaber-benchmark':self.run})))
            self.namespaces.append(ns)
            self.owned.append(dict(name=ns,uid=result['metadata']['uid']))
            self.save()
            for app in range(1,6):
                name=f'app-{app:02d}'
                labels={'app':name,'quaber-benchmark':self.run}
                body=dict(apiVersion='apps/v1',kind='Deployment',metadata=dict(name=name,namespace=ns,labels=labels),
                    spec=dict(replicas=4,selector=dict(matchLabels=labels),template=dict(metadata=dict(labels=labels),
                        spec=dict(terminationGracePeriodSeconds=1,containers=[dict(name='main',image='registry.k8s.io/pause:3.10',
                            resources=dict(requests=dict(cpu='5m',memory='8Mi'),limits=dict(cpu='20m',memory='16Mi')))]))))
                self.request('POST',f'/apis/apps/v1/namespaces/{ns}/deployments',body)
            print(f'Created dataset namespace {number}/10',flush=True)
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            pods=self.request('GET','/api/v1/pods?labelSelector=quaber-benchmark%3D'+self.run)['items']
            if len(pods)==200:
                return dict(pods=200,running=sum(p.get('status',{}).get('phase')=='Running' for p in pods),
                            phases={phase:sum(p.get('status',{}).get('phase')==phase for p in pods)
                                    for phase in sorted({p.get('status',{}).get('phase','Unknown') for p in pods})})
            time.sleep(2)
        raise TimeoutError('The lab did not create the requested 200 Pod objects within 180 seconds.')
    def cleanup(self):
        failures=[]
        for record in self.owned:
            if not record['name'].startswith('quaber-load-'+self.run+'-'):
                raise ValueError('Foreign namespace in cleanup record.')
        # Recover a namespace whose POST succeeded but whose response was lost.
        found=self.request('GET','/api/v1/namespaces?labelSelector=quaber-benchmark%3D'+self.run)['items']
        known={record['name'] for record in self.owned}
        for resource in found:
            meta=resource['metadata']
            if not meta['name'].startswith('quaber-load-'+self.run+'-') or meta.get('labels',{}).get('quaber-benchmark')!=self.run:
                raise ValueError('Unexpected namespace returned by cleanup selector.')
            if meta['name'] not in known:
                self.owned.append(dict(name=meta['name'],uid=meta['uid']))
        self.save()
        for record in reversed(self.owned):
            ns=record['name']
            if not ns.startswith('quaber-load-'+self.run+'-'):raise ValueError('Foreign namespace in cleanup record.')
            try:
                self.request('DELETE','/api/v1/namespaces/'+ns,dict(apiVersion='v1',kind='DeleteOptions',
                    preconditions=dict(uid=record['uid']),propagationPolicy='Foreground'))
            except urllib.error.HTTPError as exc:
                if exc.code!=404:failures.append(dict(name=ns,status=exc.code))
            except Exception as exc:failures.append(dict(name=ns,error=type(exc).__name__))
        if failures:raise RuntimeError('Dataset cleanup incomplete: '+json.dumps(failures))
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            remaining=self.request('GET','/api/v1/namespaces?labelSelector=quaber-benchmark%3D'+self.run)['items']
            if not remaining:
                self.state.write_text(json.dumps(dict(run=self.run,cleanup='verified absent')),encoding='utf-8')
                return
            time.sleep(2)
        raise TimeoutError('Dataset namespaces are still terminating; cleanup state was preserved.')

    @classmethod
    def resume(cls,store,cluster,state):
        import re
        saved=json.loads(Path(state).read_text(encoding='utf-8'))
        if not re.fullmatch('[0-9a-f]{10}',saved.get('run','')):raise ValueError('Invalid benchmark run identifier.')
        value=cls(store,cluster,state)
        value.run=saved['run']
        value.owned=saved.get('namespaces',[])
        value.started=True
        return value
