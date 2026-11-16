"""Windows port of Addons/kubectl 0.4.0; the Linux script defines the contract."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import getpass
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote
SOURCE = Path(__file__).with_name('kubectl').read_text(encoding='utf-8')
PROGRAM_VERSION = re.search('^PROGRAM_VERSION="([^"]+)"', SOURCE, re.MULTILINE).group(1)
USAGE = SOURCE.split("cat <<'EOF'\n", 1)[1].split('\nEOF', 1)[0] + '\n'

class LoginError(Exception):
    pass

def die(message):
    raise LoginError(message)

def parse_bool(value):
    if value.lower() in ('true', '1', 'yes', 'on'):
        return True
    if value.lower() in ('false', '0', 'no', 'off'):
        return False
    print(f'Ошибка: ожидалось логическое значение true/false, получено: {value}', file=sys.stderr)
    return ''

def windows_saved_path_entries():
    """Read persisted PATH only after the inherited process PATH is exhausted."""
    if os.name != 'nt':
        return
    import winreg
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
        (winreg.HKEY_CURRENT_USER, 'Environment'),
    )
    for hive, key in locations:
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, kind = winreg.QueryValueEx(handle, 'Path')
        except OSError:
            continue
        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
            yield from (entry for entry in value.split(os.pathsep) if entry)


def find_real_kubectl():
    root = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    own_files = [root / 'kubectl.exe', root / 'kubectl', Path(__file__)]
    if getattr(sys, 'frozen', False):
        own_files.append(Path(sys.executable))
    seen = set()
    for entry in chain(os.environ.get('PATH', '').split(os.pathsep), windows_saved_path_entries()):
        directory = os.path.expandvars(entry.strip('"')) if os.name == 'nt' else entry
        candidate = Path(directory or '.') / ('kubectl.exe' if os.name == 'nt' else 'kubectl')
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if any((own.exists() and candidate.samefile(own) for own in own_files)):
            continue
        try:
            if subprocess.run([str(resolved), 'version', '--client'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return resolved
        except OSError:
            continue
    if os.name != 'nt':
        for candidate in (Path('/usr/local/bin/kubectl'), Path('/usr/bin/kubectl')):
            if candidate.is_file() and os.access(candidate, os.X_OK) and (not any((own.exists() and candidate.samefile(own) for own in own_files))):
                return candidate
    return None

def json_get(path, key):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))[key]
        return value if isinstance(value, str) and value else ''
    except (OSError, ValueError, KeyError, TypeError):
        return ''

def json_error_text(path):
    error, description = (json_get(path, 'error'), json_get(path, 'error_description'))
    if error:
        return f'{error}: {description}' if description else error
    return path.read_text(encoding='utf-8', errors='replace').replace('\n', ' ')[:500]

def jwt_claim(token, claim):
    try:
        payload = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))[claim]
        return value if isinstance(value, str) and value else ''
    except (ValueError, KeyError, IndexError, TypeError):
        return ''

def native_value(executable, config, expression):
    result = subprocess.run([str(executable), '--kubeconfig', str(config), 'config', 'view', '--minify', '-o', 'jsonpath=' + expression], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.decode('utf-8', 'replace').rstrip('\n') if result.returncode == 0 else ''

def curl_request(curl, url, output, timeout, ca, insecure, *, config=None, data=None, headers=None):
    args = [curl, '--silent', '--show-error', '--connect-timeout', timeout, '--max-time', timeout]
    if insecure:
        args.append('--insecure')
    elif ca:
        args += ['--cacert', ca]
    if config:
        args += ['--config', str(config)]
    if data:
        args += ['--header', 'Content-Type: application/x-www-form-urlencoded', '--data-binary', '@' + str(data)]
    if headers:
        args += ['--dump-header', str(headers)]
    result = subprocess.run(args + ['--output', str(output), '--write-out', '%{http_code}', url], stdout=subprocess.PIPE)
    return result.stdout.decode('utf-8', 'replace').rstrip('\n') if result.returncode == 0 else None

def check_api_status(status, headers):
    if status in ('200', '403'):
        return
    if status != '401':
        die(f'неожиданный ответ Kubernetes API: HTTP {status}')
    api_date = ''
    for line in headers.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.lower().startswith('date:'):
            api_date = line.split(':', 1)[1].strip()
    now = datetime.now(timezone.utc)
    local_date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')
    try:
        api_epoch = int(parsedate_to_datetime(api_date).timestamp())
    except (ValueError, TypeError, OverflowError):
        api_epoch = None
    if api_epoch is not None:
        skew = abs(api_epoch - int(now.timestamp()))
        if skew > 300:
            die(f'рассинхронизация времени Kubernetes API и backend: {skew} сек. Backend: {local_date}; API: {api_date}. Новый OIDC-токен считается недействительным; синхронизируйте время/NTP control-plane')
    die(f"Kubernetes API отклонил полученный токен (HTTP 401). Backend: {local_date}; API: {api_date or 'не указано'}")

def yaml_quote(value):
    return "'" + value.replace("'", "''") + "'"

def save_config(path, temporary, executable, server, username, token, namespaces, cluster_name, context_name, user_name, api_ca, api_insecure):
    host_port = server.removeprefix('https://')
    cluster_name = cluster_name or host_port
    user_name = user_name or username + '/' + host_port
    selected = ''
    if path.is_file() and path.stat().st_size:
        existing = native_value(executable, path, '{.contexts[0].context.cluster}')
        if existing == cluster_name:
            selected = native_value(executable, path, '{.contexts[0].context.namespace}')
    if selected not in namespaces:
        selected = 'default' if 'default' in namespaces else ''
    selected = selected or (namespaces[0] if namespaces else 'default')
    context_name = context_name or f'{selected}/{host_port}/{username}'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        die(f'не удалось создать каталог: {path.parent}')
    if path.is_symlink():
        die(f'отказ от замены символической ссылки: {path}')
    tls = ''
    if api_insecure:
        tls = '    insecure-skip-tls-verify: true\n'
    elif api_ca:
        tls = '    certificate-authority-data: ' + base64.b64encode(Path(api_ca).read_bytes()).decode() + '\n'
    fragment = temporary / 'new-login.yaml'
    fragment.write_text(f'apiVersion: v1\nkind: Config\nclusters:\n- name: {yaml_quote(cluster_name)}\n  cluster:\n    server: {yaml_quote(server)}\n{tls}users:\n- name: {yaml_quote(user_name)}\n  user:\n    token: {yaml_quote(token)}\ncontexts:\n- name: {yaml_quote(context_name)}\n  context:\n    cluster: {yaml_quote(cluster_name)}\n    user: {yaml_quote(user_name)}\n    namespace: {yaml_quote(selected)}\ncurrent-context: {yaml_quote(context_name)}\n', encoding='utf-8', newline='\n')
    sources = str(fragment)
    if path.is_file() and path.stat().st_size:
        existing = temporary / 'existing-config.yaml'
        try:
            shutil.copyfile(path, existing)
        except OSError:
            die('не удалось прочитать существующий kubeconfig')
        sources += os.pathsep + str(existing)
    output = path.parent / f'.config.kubectl-login.{os.getpid()}.tmp'
    try:
        env = dict(os.environ, KUBECONFIG=sources)
        with output.open('wb') as target:
            result = subprocess.run([str(executable), 'config', 'view', '--raw', '--flatten'], env=env, stdout=target)
        if result.returncode:
            die('не удалось объединить kubeconfig')
        if os.name != 'nt':
            output.chmod(384)
        try:
            os.replace(output, path)
        except OSError:
            die('не удалось сохранить kubeconfig')
    finally:
        output.unlink(missing_ok=True)
    return selected

def run(argv):
    env = os.environ
    server = username = password = ''
    only_token = False
    kubeconfig_path = env.get('KUBECONFIG', '')
    issuer = env.get('QUABER_OIDC_ISSUER_URL', '')
    client_id = env.get('QUABER_OIDC_CLIENT_ID', 'kubernetes')
    client_secret = env.get('QUABER_OIDC_CLIENT_SECRET', '')
    secret_file = env.get('QUABER_OIDC_CLIENT_SECRET_FILE', '')
    scopes = env.get('QUABER_OIDC_SCOPES', 'openid profile email groups')
    api_ca = env.get('QUABER_API_CA_FILE', '')
    oidc_ca = env.get('QUABER_OIDC_CA_FILE', '')
    api_insecure = parse_bool(env.get('QUABER_API_INSECURE_SKIP_TLS_VERIFY', 'true'))
    oidc_insecure = parse_bool(env.get('QUABER_OIDC_INSECURE_SKIP_TLS_VERIFY', 'true'))
    timeout = env.get('QUABER_LOGIN_TIMEOUT', '20')
    cluster_name = env.get('QUABER_CLUSTER_NAME', '')
    context_name = env.get('QUABER_CONTEXT_NAME', '')
    user_name = env.get('QUABER_USER_NAME', '')
    if not argv:
        print(USAGE, end='')
        return 1
    if argv[0] in ('-h', '--help'):
        print(USAGE, end='')
        return 0
    if argv[0] == '--version':
        print(f'kubectl-login simulator {PROGRAM_VERSION}')
        return 0
    if argv[0] not in ('login', '-login'):
        executable = find_real_kubectl()
        if not executable:
            die(f'не найден системный kubectl для команды: {argv[0]}')
        return subprocess.call([str(executable), *argv])
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg in ('-h', '--help'):
            print(USAGE, end='')
            return 0
        if arg == '--version':
            print(f'kubectl-login simulator {PROGRAM_VERSION}')
            return 0
        option = arg.split('=', 1)[0]
        if option in ('-u', '--username', '-p', '--password', '--server') and ('=' not in arg or option.startswith('--')):
            value = arg.split('=', 1)[1] if '=' in arg else args.pop(0) if args else ''
            if not value:
                die(f'для {option} требуется значение')
            if option in ('-u', '--username'):
                username = value
            elif option in ('-p', '--password'):
                password = value
            else:
                server = value
        elif arg == '--onlytoken':
            only_token = True
        elif arg.startswith('--onlytoken='):
            only_token = parse_bool(arg.split('=', 1)[1])
        elif arg.startswith('--'):
            die(f'неизвестный флаг: {arg}')
        else:
            if server:
                die(f'лишний позиционный аргумент: {arg}')
            server = arg
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not curl:
        die('не найдена обязательная команда: curl')
    if not kubeconfig_path:
        home = env.get('HOME')
        if not home and os.name == 'nt':
            home = str(Path.home())
        if not home:
            die('переменная HOME не задана')
        kubeconfig_path = str(Path(home) / '.kube' / 'config')
    path = Path(kubeconfig_path)
    if not server:
        if not path.is_file() or not path.stat().st_size:
            die('не указан URL Kubernetes API и отсутствует активный kubeconfig')
        executable = find_real_kubectl()
        if not executable:
            die('не найден системный kubectl для чтения активного сервера')
        server = native_value(executable, path, '{.clusters[0].cluster.server}')
        if not server:
            die('не удалось определить активный Kubernetes API server из kubeconfig')
    if not re.fullmatch('https://[^/\\s]+/?', server):
        die('URL Kubernetes API должен иметь вид https://host:port')
    server = server.removesuffix('/')
    if not re.fullmatch('[1-9][0-9]*', timeout):
        die('--timeout должен быть целым числом больше нуля')
    if int(timeout) > 600:
        die('--timeout не должен превышать 600 секунд')
    for number, host in ((1, '11'), (2, '12')):
        if server == f'https://192.168.56.{host}:6443':
            issuer = issuer or 'https://192.168.56.10:5556'
            api_ca = api_ca or f'/home/flippy/k8s-lab/kubeconfigs/k8s-lab-{number}-ca.crt'
    if issuer == 'https://192.168.56.10:5556' and (not oidc_ca):
        oidc_ca = '/home/flippy/k8s-lab/dex/tls/ca.crt'
    if not issuer:
        die('для неизвестного кластера укажите --oidc-issuer-url')
    if not re.fullmatch('https://[^/\\s]+(/[^\\s]*)?', issuer):
        die('OIDC issuer должен быть HTTPS URL')
    issuer = issuer.removesuffix('/')
    if not api_insecure and api_ca and (not os.access(api_ca, os.R_OK)):
        die(f'не удаётся прочитать CA Kubernetes API: {api_ca}')
    if not oidc_insecure and oidc_ca and (not os.access(oidc_ca, os.R_OK)):
        die(f'не удаётся прочитать CA Dex: {oidc_ca}')
    if client_secret and secret_file:
        die('нельзя одновременно использовать --oidc-client-secret и --oidc-client-secret-file')
    if secret_file:
        secret_path = Path(secret_file)
        if not secret_path.is_file() or not os.access(secret_file, os.R_OK) or secret_path.is_symlink():
            die(f'не удаётся безопасно прочитать client secret: {secret_file}')
        with secret_path.open(encoding='utf-8', newline='') as source:
            client_secret = source.readline().removesuffix('\n')
    print(f'Server: {server}\n', file=sys.stderr)
    if not username:
        if not sys.stdin.isatty():
            die('укажите --username при неинтерактивном запуске')
        print('Enter Username: ', end='', file=sys.stderr, flush=True)
        username = sys.stdin.readline().removesuffix('\n')
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass('Enter Password: ')
        else:
            password = sys.stdin.readline().removesuffix('\n')
    if not username:
        die('имя пользователя не может быть пустым')
    if not password:
        die('пароль не может быть пустым')
    if ' openid ' not in ' ' + scopes + ' ':
        scopes = 'openid ' + scopes
    with tempfile.TemporaryDirectory(prefix='quaber-login-') as folder:
        temporary = Path(folder)
        discovery, token_file, form = (temporary / name for name in ('discovery.json', 'token.json', 'token-request'))
        oidc_config, response, headers, api_config = (temporary / name for name in ('curl.conf', 'api-response', 'api-headers', 'api-curl.conf'))
        status = curl_request(curl, issuer + '/.well-known/openid-configuration', discovery, timeout, oidc_ca, oidc_insecure)
        if status is None:
            die('не удалось подключиться к Dex')
        if status != '200':
            die(f'Dex discovery вернул HTTP {status}: {json_error_text(discovery)}')
        discovered_issuer = json_get(discovery, 'issuer')
        if not discovered_issuer:
            die('в discovery отсутствует issuer')
        endpoint = json_get(discovery, 'token_endpoint')
        if not endpoint:
            die('в discovery отсутствует token_endpoint')
        if discovered_issuer != issuer:
            die(f'issuer mismatch: ожидался {issuer}, получен {discovered_issuer}')
        if not endpoint.startswith('https://'):
            die('Dex сообщил небезопасный token endpoint')
        basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
        oidc_config.write_text(f'header = "Authorization: Basic {basic}"\n', encoding='utf-8', newline='\n')
        form.write_text('grant_type=password&scope=' + quote(scopes, safe='~') + '&username=' + quote(username, safe='~') + '&password=' + quote(password, safe='~'), encoding='utf-8', newline='\n')
        status = curl_request(curl, endpoint, token_file, timeout, oidc_ca, oidc_insecure, config=oidc_config, data=form)
        if status is None:
            die('ошибка запроса к token endpoint')
        password = ''
        form.write_bytes(b'')
        if status != '200':
            description = json_get(token_file, 'error_description')
            if json_get(token_file, 'error') == 'access_denied' and description:
                print(f'\n{description}', file=sys.stderr)
                return 1
            die(f'Dex отклонил вход (HTTP {status}): {json_error_text(token_file)}')
        token = json_get(token_file, 'id_token')
        if not token:
            die('Dex не вернул id_token')
        if token.count('.') < 2:
            die('Dex вернул некорректный ID-токен')
        if only_token:
            print(file=sys.stderr)
            print(token)
            return 0
        api_config.write_text(f'header = "Authorization: Bearer {token}"\n', encoding='utf-8', newline='\n')
        status = curl_request(curl, server + '/api/v1/namespaces', response, timeout, api_ca, api_insecure, config=api_config, headers=headers)
        if status is None:
            die('не удалось подключиться к Kubernetes API')
        api_config.write_bytes(b'')
        check_api_status(status, headers)
        namespaces = []
        if status == '200':
            try:
                namespaces = sorted((item['metadata']['name'] for item in json.loads(response.read_text(encoding='utf-8'))['items']))
            except (ValueError, KeyError, TypeError):
                pass
        executable = find_real_kubectl()
        if not executable:
            die('не найден системный kubectl, необходимый для безопасного объединения kubeconfig')
        selected = save_config(path, temporary, executable, server, username, token, namespaces, cluster_name, context_name, user_name, api_ca, api_insecure)
        display_user = jwt_claim(token, 'preferred_username') or username
        print(f'\nYou successfully logged into {server} as {display_user}\n')
        if namespaces:
            print("You have access to the following namespaces and can switch between them with 'kubectl config set-context --current --namespace=<namespace>'\n")
            for namespace in namespaces:
                print(('  * ' if namespace == selected else '    ') + namespace)
            print()
        else:
            print('You do not have permission to list namespaces.\n')
        print(f'Using namespace "{selected}".')
        return 0

def main(argv=None):
    previous_umask = os.umask(63)
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except LoginError as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'Ошибка: {exc.strerror}', file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.stderr.reconfigure(encoding='utf-8', newline='\n')
    sys.exit(main())
