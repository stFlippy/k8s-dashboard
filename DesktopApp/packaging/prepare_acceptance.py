"""Prepare isolated ZIP extractions and read-only lab checks. Credentials via stdin."""
import json
import argparse
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quaber.storage import UserStore
from quaber.kubectl import login, ProxyManager
from quaber.kubernetes import Kubernetes

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline_zip', type=Path)
    parser.add_argument('candidate_zip', type=Path)
    parser.add_argument('work', type=Path, help='New, isolated extraction directory')
    args = parser.parse_args()
    credentials = json.loads(sys.stdin.read())
    work = args.work.resolve()
    work.mkdir(exist_ok=False)
    roots = []
    for archive, dirname in [(args.baseline_zip, 'baseline'), (args.candidate_zip, 'conservative')]:
        dest = work / dirname
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        roots.append(dest / 'Quaber')
    client = ROOT / 'Addons/kubectl.exe'
    results = []
    for server in credentials['servers']:
        try:
            config = login(client, server, credentials['username'], credentials['password'])
            stores = [UserStore(root / 'UserData') for root in roots]
            records = [s.put_cluster(f'Lab {len(results)+1}', server, credentials['username'], config,
                                    kubectl_path=str(client)) for s in stores]
            manager = ProxyManager(client, stores[0])
            api = Kubernetes(stores[0], manager)
            try:
                identifier = records[0]['id']
                namespaces = api.namespaces(identifier)
                pods = api.request(identifier, '/api/v1/pods')['items']
                deployments = api.request(identifier, '/apis/apps/v1/deployments')['items']
                configmaps = api.request(identifier, '/api/v1/configmaps')['items']
                events = api.request(identifier, '/api/v1/events')['items']
                checks = {'server': server, 'namespaces': len(namespaces), 'pods': len(pods),
                          'deployments': len(deployments), 'configmaps': len(configmaps), 'events': len(events)}
                try:
                    checks['metrics'] = len(api.request(identifier, '/apis/metrics.k8s.io/v1beta1/pods')['items'])
                except Exception as exc:
                    checks['metrics_error_type'] = type(exc).__name__
                running = next((p for p in pods if p.get('status', {}).get('phase') == 'Running'), None)
                if running:
                    ns, name = running['metadata']['namespace'], running['metadata']['name']
                    checks['log_characters'] = len(api.logs(identifier, ns, name, running['spec']['containers'][0]['name'], tail=10))
                results.append(checks)
            finally:
                manager.close()
        except Exception as exc:
            # Do not persist API bodies, credentials or tokens, even on errors.
            results.append({'server': server, 'error_type': type(exc).__name__})
    (ROOT / 'reports/build-size-2026-11-18/lab-read-check.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    main()
