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
    if not raw or len(raw.encode()) > 1_000_000:
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
        if os.name == "nt":
            from .windows_secrets import PREFIX, transform
            if self.key.startswith(PREFIX):
                self.key = transform(self.key[len(PREFIX):], decrypt=True)
            elif len(self.key) == 32:
                # Atomic migration preserves the AES key and all ciphertexts.
                self._atomic_bytes(self.key_file, PREFIX + transform(self.key))
        if len(self.key) != 32:
            raise ValueError("Повреждён ключ UserData/portable.key.")
        self.clusters_file = root / "clusters.json"
        self.preferences_file = root / "preferences.json"

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
        return data["clusters"]

    def save_clusters(self, clusters: list[dict]) -> None:
        with self._lock:
            self._write_json(self.clusters_file, {"version": 1, "clusters": clusters})

    def preferences(self) -> dict:
        value = self._read_json(self.preferences_file, {})
        if not isinstance(value, dict):
            raise ValueError("Повреждён UserData/preferences.json.")
        from .views import validate_preferences
        return validate_preferences(value)

    def save_preferences(self, value: dict) -> None:
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
        if cluster.get("sharedPassword"):
            group = [c for c in self.clusters() if c.get("sharedPassword") and c["username"] == cluster["username"] and c.get("encryptedPassword")]
            cluster = max(group, key=lambda c: c.get("updatedAt", "")) if group else cluster
        encrypted = cluster.get("encryptedPassword")
        return self.decrypt(encrypted) if encrypted else ""

    def put_cluster(self, name: str, server: str, username: str, kubeconfig: str | None, enabled: bool = True,
                    cluster_id: str | None = None, *, password: str | None = None,
                    remember_password: bool | None = None, kubectl_path: str | None = None, expected=None, shared_password=None) -> dict:
        with self._lock:
            if expected is not None:
                current = next((c for c in self.clusters() if c["id"] == cluster_id), None)
                if current != expected:
                    raise ValueError("Кластер изменился во время входа. Откройте форму заново.")
            return self._put_cluster(name, server, username, kubeconfig, enabled, cluster_id,
                                     password, remember_password, kubectl_path, shared_password)

    def _put_cluster(self, name, server, username, kubeconfig, enabled, cluster_id, password, remember_password, kubectl_path, shared_password):
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
        if existing and not kubeconfig and (server != existing["server"] or username != existing["username"]):
            raise ValueError("Для смены API server или пользователя требуется новый вход.")
        if not kubeconfig and not existing:
            raise ValueError("Для нового кластера требуется успешный kubectl login.")
        record = {"id": cluster_id or str(uuid.uuid4()), "name": name, "server": server, "username": username,
                  "enabled": bool(enabled), "encryptedKubeconfig": self.encrypt(sanitize_kubeconfig(kubeconfig, server)) if kubeconfig else existing["encryptedKubeconfig"],
                  "createdAt": existing["createdAt"] if existing else now(), "updatedAt": now()}
        same_identity = existing and server == existing["server"] and username == existing["username"]
        record["sharedPassword"] = bool(shared_password if shared_password is not None else (existing or {}).get("sharedPassword", False))
        record["authMode"] = "login" if password else (existing or {}).get("authMode", "login")
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
        if record["sharedPassword"] and password and remember_password:
            for peer in records:
                if peer.get("sharedPassword") and peer["username"] == username:
                    peer["encryptedPassword"] = self.encrypt(password)
                    peer["updatedAt"] = record["updatedAt"]
        self.save_clusters(records)
        return record

    def set_shared_password(self, identifier, enabled):
        with self._lock:
            records = self.clusters()
            cluster = next(c for c in records if c["id"] == identifier)
            peers = [c for c in records if c["id"] != identifier and c.get("sharedPassword") and c["username"] == cluster["username"] and c.get("encryptedPassword")]
            if enabled and peers:
                cluster["encryptedPassword"] = max(peers, key=lambda c: c.get("updatedAt", ""))["encryptedPassword"]
            cluster["sharedPassword"] = bool(enabled)
            cluster["updatedAt"] = now()
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
        if len({c["server"] for c in selected}) != 1 and not all(c.get("sharedPassword") for c in selected):
            raise ValueError("Общий вход между разными endpoints запрещён; войдите в каждый отдельно.")
        prepared = {cluster["id"]: self.encrypt(sanitize_kubeconfig(updates[cluster["id"]], cluster["server"])) for cluster in selected}
        timestamp = now()
        for cluster in selected:
            cluster["encryptedKubeconfig"] = prepared[cluster["id"]]
            if remember_password is False:
                cluster.pop("encryptedPassword", None)
            elif password is not None and (remember_password is True or cluster.get("encryptedPassword")):
                if not password or len(password) > 1024:
                    raise ValueError("Введите пароль кластера (не более 1024 символов).")
                cluster["encryptedPassword"] = self.encrypt(password)
            cluster["updatedAt"] = timestamp
        if password is not None and any(c.get("sharedPassword") for c in selected):
            for peer in clusters:
                if peer.get("sharedPassword") and peer["username"] in usernames:
                    if remember_password is False:
                        peer.pop("encryptedPassword", None)
                    else:
                        peer["encryptedPassword"] = self.encrypt(password)
                    peer["updatedAt"] = timestamp
        self.save_clusters(clusters)

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
        correlation = str(uuid.uuid4())
        cluster = next((c for c in self.clusters() if c["id"] == cluster_id), {})
        metadata = dict(operationId=correlation, username=cluster.get("username", ""), **details)
        # A failed intent write prevents the operation from starting.
        self.audit(action, cluster_id, namespace, name, phase="intent", **metadata)
        try:
            yield correlation
        except Exception as exc:
            try:
                self.audit(action, cluster_id, namespace, name, phase="unknown", errorType=type(exc).__name__, **metadata)
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
