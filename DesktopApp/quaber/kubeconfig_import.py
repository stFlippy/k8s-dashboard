"""Import static token/certificate contexts without executing credential plugins."""
import base64
import copy
from pathlib import Path
import yaml
from .storage import sanitize_kubeconfig, validate_server


def read_contexts(path):
    path = Path(path).resolve()
    if path.stat().st_size > 1_000_000:
        raise ValueError("Kubeconfig превышает 1 МБ.")
    source = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(source, dict) or source.get("kind") != "Config":
        raise ValueError("Ожидался kubeconfig kind: Config.")
    clusters = {item['name']: item['cluster'] for item in source.get('clusters', [])}
    users = {item['name']: item['user'] for item in source.get('users', [])}
    result = []
    for context in source.get('contexts', []):
        values = context['context']
        cluster = copy.deepcopy(clusters[values['cluster']])
        user = copy.deepcopy(users[values['user']])
        if user.get('exec') or user.get('auth-provider'):
            raise ValueError(f"Контекст {context['name']}: exec/auth-provider требует внешнего входа. Импортируйте kubeconfig со статическим token или сертификатом.")
        for body, field in ((cluster, 'certificate-authority'), (user, 'client-certificate'), (user, 'client-key')):
            if field in body:
                file = Path(body.pop(field)).expanduser()
                if not file.is_absolute():
                    file = path.parent/file
                if file.stat().st_size > 100_000:
                    raise ValueError(f"Файл {field} слишком большой.")
                body[field+'-data'] = base64.b64encode(file.read_bytes()).decode('ascii')
        if 'tokenFile' in user:
            token_file = Path(user.pop('tokenFile')).expanduser()
            if not token_file.is_absolute():
                token_file = path.parent/token_file
            if token_file.stat().st_size > 100_000:
                raise ValueError("Файл tokenFile слишком большой.")
            user['token'] = token_file.read_text(encoding='utf-8').strip()
        server = validate_server(cluster['server'])
        config = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'imported',
                  'contexts': [{'name': 'imported', 'context': {'cluster': 'cluster', 'user': 'user'}}],
                  'clusters': [{'name': 'cluster', 'cluster': cluster}], 'users': [{'name': 'user', 'user': user}]}
        result.append({'name': context['name'], 'server': server, 'username': values['user'],
                       'kubeconfig': sanitize_kubeconfig(yaml.safe_dump(config), server)})
    if not result:
        raise ValueError("В kubeconfig нет контекстов.")
    return result
