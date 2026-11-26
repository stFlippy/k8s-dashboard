"""Local encrypted state; Windows keys are bound to the current user by DPAPI."""

from __future__ import annotations

import base64
from collections import deque
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_server(value: str) -> str:
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("API server содержит недопустимые символы.")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("API server должен быть HTTPS-адресом без пути и учётных данных.")
    try:
        _ = url.port
    except ValueError as exc:
        raise ValueError("Некорректный порт API server.") from exc
    return f"https://{url.netloc}"


def sanitize_kubeconfig(raw: str, server: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw.encode()) > 1_000_000:
        raise ValueError("kubectl login вернул пустой или слишком большой kubeconfig.")
    config = yaml.safe_load(raw)
    if not isinstance(config, dict):
        raise ValueError("kubectl login вернул некорректный kubeconfig.")
    context_name = config.get("current-context")
    contexts = [item for item in config.get("contexts", []) if isinstance(item, dict) and item.get("name") == context_name]
    if len(contexts) != 1:
        raise ValueError("В kubeconfig отсутствует текущий context.")
    selected = contexts[0].get("context") or {}
    clusters = [item.get("cluster", {}) for item in config.get("clusters", []) if item.get("name") == selected.get("cluster")]
    users = [item.get("user", {}) for item in config.get("users", []) if item.get("name") == selected.get("user")]
    if len(clusters) != 1 or len(users) != 1 or validate_server(str(clusters[0].get("server", ""))) != validate_server(server):
        raise ValueError("API server в kubeconfig не совпадает с выбранным кластером.")
    cluster, user = clusters[0], users[0]
    token = user.get("token")
    certificate = user.get("client-certificate-data")
    private_key = user.get("client-key-data")
    if token is not None and (not isinstance(token, str) or not token or len(token) > 100_000):
        raise ValueError("Некорректный bearer token.")
    if not token and not (certificate and private_key):
        raise ValueError("Kubeconfig должен содержать bearer token или клиентский сертификат с ключом.")
    for field in (certificate, private_key):
        if field is not None:
            if not isinstance(field, str) or len(field) > 100_000:
                raise ValueError("Некорректный клиентский сертификат или ключ.")
            try:
                base64.b64decode(field, validate=True)
            except Exception as exc:
                raise ValueError("Некорректный клиентский сертификат или ключ.") from exc
    if any(key in cluster for key in ("certificate-authority", "proxy-url")) or any(key in user for key in ("exec", "auth-provider", "client-certificate", "client-key", "username", "password", "as")):
        raise ValueError("kubectl login вернул неподдерживаемые поля kubeconfig.")
    ca = cluster.get("certificate-authority-data")
    if ca is not None:
        if not isinstance(ca, str) or len(ca) > 100_000:
            raise ValueError("Некорректный сертификат CA в kubeconfig.")
        try:
            base64.b64decode(ca, validate=True)
        except Exception as exc:
            raise ValueError("Некорректный сертификат CA в kubeconfig.") from exc
    safe = {
        "apiVersion": "v1", "kind": "Config", "current-context": "quaber-context",
        "clusters": [{"name": "quaber-cluster", "cluster": {"server": validate_server(server), **({"certificate-authority-data": ca} if ca else {}), **({"insecure-skip-tls-verify": True} if cluster.get("insecure-skip-tls-verify") is True else {})}}],
        "users": [{"name": "quaber-user", "user": {"token": token} if token else {"client-certificate-data": certificate, "client-key-data": private_key}}],
        "contexts": [{"name": "quaber-context", "context": {"cluster": "quaber-cluster", "user": "quaber-user"}}],
    }
    return yaml.safe_dump(safe, allow_unicode=True, sort_keys=False)


class KeyUnavailableError(OSError):
    """Only a DPAPI unprotect failure of an existing marked portable key."""

    def __init__(self, cause):
        super().__init__('Windows DPAPI: local key is unavailable for this Windows account.')
        self.winerror = getattr(cause, 'winerror', None)
        self.reason = 'unprotect'


class UnsupportedKeyProtection(KeyUnavailableError):
    """A recognized protected key cannot be opened on this platform."""
    def __init__(self):
        OSError.__init__(self, 'Windows DPAPI UserData cannot be opened on this platform. Add clusters again after local recovery.')
        self.winerror = None
        self.reason = 'unsupported_protection'


class UserStore:
    def __init__(self, root: Path):
        self._lock = threading.RLock()
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".write-check-", delete=True):
                pass
        except OSError as exc:
            raise OSError(f"Нет прав записи в UserData: {root}") from exc
        self.key_file = root / "portable.key"
        if not self.key_file.exists():
            if (root / "clusters.json").exists():
                raise ValueError("В UserData отсутствует portable.key. Сохранённые подключения нельзя расшифровать.")
            key = secrets.token_bytes(32)
            if os.name == "nt":
                from .windows_secrets import PREFIX, transform
                key = PREFIX + transform(key)
            self._atomic_bytes(self.key_file, key)
        self.key = self.key_file.read_bytes()
        from .windows_secrets import PREFIX
        if os.name != 'nt' and self.key.startswith(PREFIX):
            raise UnsupportedKeyProtection()
        if os.name == "nt":
            from .windows_secrets import PREFIX, transform
            if self.key.startswith(PREFIX):
                blob = self.key[len(PREFIX):]
                if not blob:
                    raise ValueError('Повреждён ключ UserData/portable.key.')
                try:
                    self.key = transform(blob, decrypt=True)
                except OSError as exc:
                    raise KeyUnavailableError(exc) from exc
            elif len(self.key) == 32:
                # Atomic migration preserves the AES key and all ciphertexts.
                self._atomic_bytes(self.key_file, PREFIX + transform(self.key))
        if len(self.key) != 32:
            raise ValueError("Повреждён ключ UserData/portable.key.")
        self.clusters_file = root / "clusters.json"
        self.preferences_file = root / "preferences.json"
        self._account_passwords = {}

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(7):
                try:
                    os.replace(temp, path)
                    break
                except PermissionError as exc:
                    if attempt == 6:
                        raise PermissionError(f"Не удалось сохранить {path.name}: файл занят или нет прав на замену. Закройте другие экземпляры приложения и повторите вход.") from exc
                    time.sleep(.025 * 2**attempt)
            if os.name != "nt":
                path.chmod(0o600)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _write_json(self, path: Path, value: object) -> None:
        self._atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    @staticmethod
    def _read_json(path: Path, fallback: object) -> object:
        if not path.exists():
            return fallback
        with path.open("rb") as stream:
            body = stream.read(4_000_001)
        if len(body) > 4_000_000:
            raise ValueError(f"Файл {path.name} превышает допустимые 4 МБ.")
        return json.loads(body.decode("utf-8"))

    def clusters(self) -> list[dict]:
        with self._lock:
            data = self._read_json(self.clusters_file, {"version": 1, "clusters": []})
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("clusters"), list):
                raise ValueError("Повреждён UserData/clusters.json.")
            changed = False
            for cluster in data['clusters']:
                if isinstance(cluster, dict) and cluster.get('controllerTimeZone'):
                    try:
                        self._validate_controller_timezone(cluster['controllerTimeZone'])
                    except ValueError:
                        cluster['controllerTimeZone'] = ''
                        changed = True
            if changed:
                self._write_json(self.clusters_file, data)
            if self._migrate_accounts(data):
                self._write_json(self.clusters_file, data)
            accounts = {a['id']: a for a in data.get('accounts', [])}
            records = []
            for record in data['clusters']:
                value = dict(record)
                if value.get('accountId'):
                    account = accounts.get(value['accountId'])
                    if account is None:
                        raise ValueError('Учётная запись подключения не найдена.')
                    value.update(username=account['username'], accountName=account['name'],
                                 accountRevision=account['updatedAt'])
                    if account.get('encryptedPassword'):
                        value['encryptedPassword'] = account['encryptedPassword']
                records.append(value)
            return records

    def _migrate_accounts(self, data):
        """Migrate legacy passwords and references atomically in one document."""
        accounts = data.setdefault('accounts', [])
        if not any(c.get('authMode') != 'kubeconfig' and not c.get('accountId') for c in data['clusters']):
            return False
        known = {(a['username'], self.decrypt(a['encryptedPassword']) if a.get('encryptedPassword') else ''): a
                 for a in accounts}
        shared = {}
        for c in sorted(data['clusters'], key=lambda c: c.get('updatedAt', '')):
            if c.get('sharedPassword') and c.get('encryptedPassword'):
                shared[c.get('username')] = c['encryptedPassword']
        changed = False
        for record in data['clusters']:
            if record.get('authMode') == 'kubeconfig' or record.get('accountId'):
                continue
            username = record.get('username', '').strip()
            cipher = record.get('encryptedPassword', '')
            if record.get('sharedPassword'):
                cipher = shared.get(username, cipher)
            try:
                plaintext = self.decrypt(cipher) if cipher else ''
            except Exception:
                cipher, plaintext = '', ''
                record['credential_error'] = 'Сохранённый пароль повреждён. Обновите учётную запись.'
            key = (username, plaintext)
            account = known.get(key)
            if account is None:
                label = username or 'Учётная запись'
                if any(a['name'] == label for a in accounts): label += f' ({len(accounts)+1})'
                account = dict(id=str(uuid.uuid4()), name=label, username=username, updatedAt=now())
                if cipher: account['encryptedPassword'] = cipher
                accounts.append(account)
                known[key] = account
            record['accountId'] = account['id']
            for field in ('username', 'encryptedPassword', 'sharedPassword'):
                record.pop(field, None)
            changed = True
        return changed

    def accounts(self):
        with self._lock:
            self.clusters()
            return self._read_json(self.clusters_file, {}).get('accounts', [])

    def account_password(self, account_id):
        with self._lock:
            account = next((a for a in self.accounts() if a['id'] == account_id), None)
            if account is None: raise ValueError('Учётная запись не найдена.')
            return self._account_passwords.get(account_id) or (self.decrypt(account['encryptedPassword']) if account.get('encryptedPassword') else '')

    def put_account(self, name, username, password=None, *, account_id=None, remember_password=None, expected=None):
        name, username = name.strip(), username.strip()
        if not name or len(name) > 80 or not username or len(username) > 256 or any(ord(c) < 32 for c in name+username):
            raise ValueError('Укажите имя учётной записи и логин без управляющих символов.')
        if password is not None and (not password or len(password) > 1024):
            raise ValueError('Укажите пароль от 1 до 1024 символов.')
        with self._lock:
            self.clusters()
            data = self._read_json(self.clusters_file, {'version':1,'clusters':[],'accounts':[]})
            existing = next((a for a in data['accounts'] if a['id'] == account_id), None)
            if account_id and existing is None: raise ValueError('Учётная запись не найдена.')
            if expected is not None and existing != expected: raise ValueError('Учётная запись изменена. Откройте её заново.')
            if any(a['name'].casefold() == name.casefold() and a['id'] != account_id for a in data['accounts']):
                raise ValueError('Имя учётной записи уже используется.')
            account = dict(existing or {}, id=account_id or str(uuid.uuid4()), name=name, username=username, updatedAt=now())
            if password is not None:
                self._account_passwords[account['id']] = password
                from .app_logging import register_secret
                register_secret(password)
                if remember_password is True or (remember_password is None and (existing or {}).get('encryptedPassword')):
                    account['encryptedPassword'] = self.encrypt(password)
                else: account.pop('encryptedPassword', None)
            elif remember_password is True and not account.get('encryptedPassword'):
                session_password = self._account_passwords.get(account['id'])
                if session_password:
                    account['encryptedPassword'] = self.encrypt(session_password)
            elif remember_password is False:
                if account.get('encryptedPassword'):
                    self._account_passwords[account['id']] = self.decrypt(account['encryptedPassword'])
                account.pop('encryptedPassword', None)
            if existing and existing['username'] != username:
                for c in data['clusters']:
                    if c.get('accountId') == account['id']:
                        c.update(encryptedKubeconfig='', enabled=False, updatedAt=now())
            data['accounts'] = [a for a in data['accounts'] if a['id'] != account['id']] + [account]
            self._write_json(self.clusters_file, data)
            return account

    def delete_account(self, identifier):
        with self._lock:
            self.clusters()
            data = self._read_json(self.clusters_file, {})
            if any(c.get('accountId') == identifier for c in data['clusters']):
                raise ValueError('Учётная запись используется подключением. Сначала выберите для него другую запись.')
            if not any(a['id'] == identifier for a in data['accounts']): raise ValueError('Учётная запись не найдена.')
            data['accounts'] = [a for a in data['accounts'] if a['id'] != identifier]
            self._account_passwords.pop(identifier, None)
            self._write_json(self.clusters_file, data)

    def save_clusters(self, clusters: list[dict]) -> None:
        with self._lock:
            previous = self._read_json(self.clusters_file, {})
            data = dict(version=1, clusters=[dict(c) for c in clusters], accounts=previous.get('accounts', []))
            self._migrate_accounts(data)
            for record in data['clusters']:
                if record.get('accountId'):
                    for field in ('username','encryptedPassword','sharedPassword','accountName','accountRevision'):
                        record.pop(field, None)
            self._write_json(self.clusters_file, data)

    def preferences(self) -> dict:
        with self._lock:
            value = self._read_json(self.preferences_file, {})
        if not isinstance(value, dict):
            raise ValueError("Повреждён UserData/preferences.json.")
        from .views import validate_preferences
        return validate_preferences(value)

    def save_preferences(self, value: dict) -> None:
        with self._lock:
            self._write_json(self.preferences_file, value)

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(12)
        return "v1:" + base64.b64encode(nonce + AESGCM(self.key).encrypt(nonce, plaintext.encode(), None)).decode()

    def decrypt(self, ciphertext: str) -> str:
        version, encoded = ciphertext.split(":", 1)
        if version != "v1":
            raise ValueError("Неподдерживаемый формат kubeconfig.")
        data = base64.b64decode(encoded, validate=True)
        return AESGCM(self.key).decrypt(data[:12], data[12:], None).decode()

    def login_required(self, cluster: dict) -> bool:
        try:
            config = yaml.safe_load(self.decrypt(cluster["encryptedKubeconfig"]))
            if config["users"][0]["user"].get("client-certificate-data"):
                return False
            token = config["users"][0]["user"]["token"]
            encoded = token.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            expiry = payload.get("exp")
            if expiry is None and cluster.get("authMode") == "kubeconfig":
                return False
            return not isinstance(expiry, (int, float)) or expiry <= datetime.now(timezone.utc).timestamp() + 30
        except Exception:
            return cluster.get("authMode") != "kubeconfig"

    def saved_password(self, cluster: dict) -> str:
        if cluster.get('accountId'):
            return self.account_password(cluster['accountId'])
        encrypted = cluster.get("encryptedPassword")
        return self.decrypt(encrypted) if encrypted else ""

    def put_cluster(self, name: str, server: str, username: str, kubeconfig: str | None, enabled: bool = True,
                    cluster_id: str | None = None, *, password: str | None = None,
                    remember_password: bool | None = None, kubectl_path: str | None = None, expected=None, shared_password=None,
                    controller_timezone: str | None = None, unverified: bool = False, account_id=None) -> dict:
        with self._lock:
            if expected is not None:
                current = next((c for c in self.clusters() if c["id"] == cluster_id), None)
                if current != expected:
                    raise ValueError("Кластер изменился во время входа. Откройте форму заново.")
            return self._put_cluster(name, server, username, kubeconfig, enabled, cluster_id,
                                     password, remember_password, kubectl_path, shared_password, controller_timezone, unverified, account_id)

    def _put_cluster(self, name, server, username, kubeconfig, enabled, cluster_id, password, remember_password, kubectl_path, shared_password, controller_timezone=None, unverified=False, account_id=None):
        name, username = name.strip(), username.strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Имя кластера должно содержать от 1 до 80 символов без управляющих знаков.")
        if not username or len(username) > 256:
            raise ValueError("Укажите пользователя Kubernetes.")
        server = validate_server(server)
        clusters = self.clusters()
        existing = next((item for item in clusters if item["id"] == cluster_id), None)
        if cluster_id and not existing:
            raise ValueError("Кластер не найден.")
        if any(item["name"].casefold() == name.casefold() and item["id"] != cluster_id for item in clusters):
            raise ValueError("Кластер с таким именем уже существует.")
        if unverified and (enabled or kubeconfig):
            raise ValueError('Непроверенное подключение можно сохранить только отключённым, без токена.')
        if existing and not kubeconfig and not unverified and (server != existing["server"] or username != existing["username"]):
            raise ValueError("Для смены API server или пользователя требуется новый вход.")
        if not kubeconfig and not existing and not unverified:
            raise ValueError("Для нового кластера требуется успешный kubectl login.")
        if enabled and not kubeconfig and not (existing or {}).get('encryptedKubeconfig'):
            raise ValueError('Сначала выполните успешный вход в настройках подключения.')
        record = {"id": cluster_id or str(uuid.uuid4()), "name": name, "server": server, "username": username,
                  "enabled": bool(enabled), "encryptedKubeconfig": '' if unverified else self.encrypt(sanitize_kubeconfig(kubeconfig, server)) if kubeconfig else existing["encryptedKubeconfig"],
                  "createdAt": existing["createdAt"] if existing else now(), "updatedAt": now()}
        binding = account_id or ((existing or {}).get('accountId') if (existing or {}).get('username') == username else None)
        if binding:
            account = next((a for a in self.accounts() if a['id'] == binding), None)
            if account is None or account['username'] != username: raise ValueError('Учётная запись подключения изменена.')
            record['accountId'] = binding
        same_identity = existing and server == existing["server"] and username == existing["username"]
        zone = controller_timezone if controller_timezone is not None else (existing or {}).get('controllerTimeZone', '')
        record['controllerTimeZone'] = self._validate_controller_timezone(zone)
        record["authMode"] = "login" if password else (existing or {}).get("authMode", "login")
        if binding: record['authMode'] = 'login'
        if unverified:
            record['authMode'] = 'login'
            record['credential_error'] = 'Вход не подтверждён. Подключение сохранено отключённым. Исправьте данные и выполните вход.'
            # An unverified password must never replace a shared group's saved password.
            password, remember_password = None, False
        if remember_password is True:
            if password:
                if len(password) > 1024:
                    raise ValueError("Пароль должен содержать не более 1024 символов.")
                record["encryptedPassword"] = self.encrypt(password)
            elif same_identity and existing.get("encryptedPassword"):
                record["encryptedPassword"] = existing["encryptedPassword"]
            else:
                raise ValueError("Введите пароль, который нужно сохранить.")
        elif remember_password is None and same_identity and existing.get("encryptedPassword"):
            record["encryptedPassword"] = existing["encryptedPassword"]
        client = kubectl_path if kubectl_path is not None else (existing or {}).get("kubectlPath", "")
        if client:
            if not isinstance(client, str) or len(client) > 32767 or "\0" in client:
                raise ValueError("Некорректный путь к kubectl.")
            record["kubectlPath"] = client.strip()
        records = [record if item["id"] == cluster_id else item for item in clusters] if existing else [*clusters, record]
        self.save_clusters(records)
        if binding and not unverified and (password is not None or remember_password is False):
            self.put_account(account['name'], username, password, account_id=binding, remember_password=remember_password)
        return next(c for c in self.clusters() if c['id'] == record['id'])

    @staticmethod
    def _validate_controller_timezone(value):
        if not isinstance(value, str) or len(value) > 128:
            raise ValueError('Укажите UTC-смещение, например UTC+04:00.')
        value = value.strip()
        if value:
            from .schedule import load_zone
            try:
                load_zone(value)
            except ValueError:
                raise ValueError('Допустимы только UTC и смещения вида UTC+04:00.') from None
        return value


    def set_cluster_enabled(self, identifier, enabled):
        with self._lock:
            records = self.clusters()
            cluster = next((c for c in records if c['id'] == identifier), None)
            if cluster is None:
                raise ValueError('Кластер не найден.')
            if enabled and not cluster.get('encryptedKubeconfig'):
                raise ValueError('Сначала выполните успешный вход: Настройки → подключение → Настроить / войти.')
            cluster['enabled'] = bool(enabled)
            self.save_clusters(records)


    def import_contexts(self, entries):
        if not entries or len(entries) > 100:
            raise ValueError("Выберите от 1 до 100 контекстов.")
        with self._lock:
            records = self.clusters()
            names = {record["name"].casefold() for record in records}
            imported = []
            for entry in entries:
                name = base = entry["name"][:70] or "Кластер"
                index = 2
                while name.casefold() in names:
                    name = f"{base} ({index})"
                    index += 1
                names.add(name.casefold())
                server = validate_server(entry["server"])
                record = {"id": str(uuid.uuid4()), "name": name, "server": server, "username": entry["username"],
                          "enabled": True, "authMode": "kubeconfig", "sharedPassword": False,
                          "encryptedKubeconfig": self.encrypt(sanitize_kubeconfig(entry["kubeconfig"], server)),
                          "createdAt": now(), "updatedAt": now()}
                imported.append(record)
            self.save_clusters(records+imported)
            return imported

    def delete_cluster(self, cluster_id: str) -> None:
        with self._lock:
            self._delete_cluster(cluster_id)

    def _delete_cluster(self, cluster_id: str) -> None:
        clusters = self.clusters()
        filtered = [item for item in clusters if item["id"] != cluster_id]
        if len(filtered) == len(clusters):
            raise ValueError("Кластер не найден.")
        self.save_clusters(filtered)

    def update_logins(self, updates: dict[str, str], password: str | None = None, *, expected=None, remember_password=None) -> None:
        with self._lock:
            self._update_logins(updates, password, expected, remember_password)

    def _update_logins(self, updates: dict[str, str], password: str | None = None, expected=None, remember_password=None) -> None:
        if not updates or len(updates) > 100:
            raise ValueError("Выберите от 1 до 100 кластеров.")
        clusters = self.clusters()
        selected = [cluster for cluster in clusters if cluster["id"] in updates]
        if len(selected) != len(updates):
            raise ValueError("Один из кластеров не найден.")
        if expected is not None:
            for cluster in selected:
                if cluster != expected.get(cluster["id"]):
                    raise ValueError("Учётная запись изменилась во время входа. Старый результат отклонён.")
                if not cluster.get("enabled"):
                    raise ValueError("Кластер отключён во время входа.")
        usernames = {cluster["username"] for cluster in selected}
        if len(usernames) != 1:
            raise ValueError("Для общего входа кластеры должны иметь одного пользователя.")
        account_ids = {c.get('accountId') for c in selected}
        if len(account_ids) != 1:
            raise ValueError('Для общего входа выберите одну учётную запись.')
        prepared = {cluster["id"]: self.encrypt(sanitize_kubeconfig(updates[cluster["id"]], cluster["server"])) for cluster in selected}
        timestamp = now()
        for cluster in selected:
            cluster["encryptedKubeconfig"] = prepared[cluster["id"]]
            cluster.pop('credential_error', None)
            cluster["updatedAt"] = timestamp
        self.save_clusters(clusters)
        if password is not None and next(iter(account_ids), None):
            account = next(a for a in self.accounts() if a['id'] in account_ids)
            self.put_account(account['name'], account['username'], password, account_id=account['id'],
                             remember_password=remember_password)

    def audit(self, action: str, cluster_id: str, namespace: str = "", name: str = "", **details) -> None:
        # No request bodies, tokens, passwords or command strings enter the audit file.
        record = {"time": now(), "action": action, "clusterId": cluster_id, "namespace": namespace, "name": name, **details}
        with self._lock:
            path = self.root / "audit.jsonl"
            if path.exists() and path.stat().st_size > 2_000_000:
                for index in range(3, 0, -1):
                    source = path if index == 1 else self.root / f"audit.jsonl.{index-1}"
                    if source.exists():
                        os.replace(source, self.root / f"audit.jsonl.{index}")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    @contextmanager
    def operation(self, action, cluster_id, namespace, name, **details):
        from .write_guard import current_permit
        permit = current_permit()
        correlation = permit.intent.operation_id if permit is not None else str(uuid.uuid4())
        cluster = next((c for c in self.clusters() if c["id"] == cluster_id), {})
        metadata = dict(operationId=correlation, username=cluster.get("username", ""), **details)
        # A failed intent write prevents the operation from starting.
        self.audit(action, cluster_id, namespace, name, phase="intent", **metadata)
        try:
            yield correlation
        except Exception as exc:
            try:
                phase = 'unknown'
                if permit is not None:
                    phase = {'issued': 'canceled-before-dispatch', 'rejected': 'rejected',
                             'accepted': 'accepted'}.get(permit.state, 'unknown')
                self.audit(action, cluster_id, namespace, name, phase=phase, errorType=type(exc).__name__, **metadata)
            except OSError:
                pass
            raise
        else:
            try:
                self.audit(action, cluster_id, namespace, name, phase="success", **metadata)
            except OSError as exc:
                raise RuntimeError("Операция выполнена, но результат аудита не записан. Проверьте состояние перед повтором.") from exc

    def recent_audit(self, limit: int = 200) -> list[dict]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        with self._lock, path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size-1_000_000))
            if stream.tell():
                stream.readline()
            lines = deque(stream, maxlen=min(1000, max(1, limit)))
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
        return records
