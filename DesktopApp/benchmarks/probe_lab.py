"""One bounded API readiness probe; no login attempts or credential output."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import urllib.request
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'benchmarks')]
from lab_connection import open_lab
from quaber.https_transport import HTTPSConnection

with tempfile.TemporaryDirectory() as directory:
    for name,config_path in (('saved',None),('standard',Path.home()/'.kube/config')):
        store,cluster,_=open_lab(ROOT,Path(directory)/name,kubeconfig=config_path)
        config=yaml.safe_load(store.decrypt(cluster['encryptedKubeconfig']))
        token=config['users'][0]['user'].get('token','')
        expiration=None
        if token.count('.')==2:
            try:
                claims=json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'==='))
                if 'exp' in claims:expiration=round(claims['exp']-time.time())
            except (ValueError,TypeError):pass
        print(json.dumps(dict(source=name,token_expires_in_seconds=expiration)),flush=True)
        connection=HTTPSConnection(store,cluster)
        start=time.perf_counter()
        try:
            with connection.opener.open(urllib.request.Request(connection.server+'/version',headers=connection.headers),timeout=4) as response:
                response.read(65536)
                print(json.dumps(dict(source=name,status=response.status,seconds=time.perf_counter()-start)),flush=True)
        except Exception as exc:
            print(json.dumps(dict(source=name,error_type=type(exc).__name__,status=getattr(exc,'code',None),seconds=time.perf_counter()-start)),flush=True)
