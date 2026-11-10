"""Portable local state. A copy of UserData also copies access to its credentials."""

from __future__ import annotations

import base64
from collections import deque
import json
import os
import secrets
import tempfile
import uuid
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
    if not isinstance(token, str) or not token or len(token) > 100_000:
        raise ValueError("kubectl login должен вернуть bearer token.")
    if any(key in cluster for key in ("certificate-authority", "proxy-url")) or any(key in user for key in ("exec", "auth-provider", "client-certificate", "client-key", "client-certificate-data", "client-key-data", "username", "password", "as")):
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
        "users": [{"name": "quaber-user", "user": {"token": token}}],
        "contexts": [{"name": "quaber-context", "context": {"cluster": "quaber-cluster", "user": "quaber-user"}}],
    }
    return yaml.safe_dump(safe, allow_unicode=True, sort_keys=False)


class UserStore:
    def __init__(self, root: Path):
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
            self._atomic_bytes(self.key_file, secrets.token_bytes(32))
        self.key = self.key_file.read_bytes()
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
            os.replace(temp, path)
            if os.name != "nt":
                path.chmod(0o600)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _write_json(self, path: Path, value: object) -> None:
        self._atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    @staticmethod
    def _read_json(path: Path, fallback: object) -> object:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback

    def clusters(self) -> list[dict]:
        data = self._read_json(self.clusters_file, {"version": 1, "clusters": []})
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("clusters"), list):
            raise ValueError("Повреждён UserData/clusters.json.")
        return data["clusters"]

    def save_clusters(self, clusters: list[dict]) -> None:
        self._write_json(self.clusters_file, {"version": 1, "clusters": clusters})

    def preferences(self) -> dict:
        value = self._read_json(self.preferences_file, {})
        if not isinstance(value, dict):
            raise ValueError("Повреждён UserData/preferences.json.")
        return value

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
            token = config["users"][0]["user"]["token"]
            encoded = token.split(".")[1]
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            expiry = payload.get("exp")
            return not isinstance(expiry, (int, float)) or expiry <= datetime.now(timezone.utc).timestamp() + 30
        except Exception:
            return True

    def put_cluster(self, name: str, server: str, username: str, kubeconfig: str | None, enabled: bool = True, cluster_id: str | None = None) -> dict:
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
        self.save_clusters([record if item["id"] == cluster_id else item for item in clusters] if existing else [*clusters, record])
        return record

    def delete_cluster(self, cluster_id: str) -> None:
        clusters = self.clusters()
        filtered = [item for item in clusters if item["id"] != cluster_id]
        if len(filtered) == len(clusters):
            raise ValueError("Кластер не найден.")
        self.save_clusters(filtered)

    def update_logins(self, updates: dict[str, str]) -> None:
        if not updates or len(updates) > 100:
            raise ValueError("Выберите от 1 до 100 кластеров.")
        clusters = self.clusters()
        selected = [cluster for cluster in clusters if cluster["id"] in updates]
        if len(selected) != len(updates):
            raise ValueError("Один из кластеров не найден.")
        usernames = {cluster["username"].strip().casefold() for cluster in selected}
        if len(usernames) != 1:
            raise ValueError("Для общего входа кластеры должны иметь одного пользователя.")
        prepared = {cluster["id"]: self.encrypt(sanitize_kubeconfig(updates[cluster["id"]], cluster["server"])) for cluster in selected}
        timestamp = now()
        for cluster in selected:
            cluster["encryptedKubeconfig"] = prepared[cluster["id"]]
            cluster["updatedAt"] = timestamp
        self.save_clusters(clusters)

    def audit(self, action: str, cluster_id: str, namespace: str = "", name: str = "") -> None:
        # No request bodies, tokens, passwords or command strings enter the audit file.
        record = {"time": now(), "action": action, "clusterId": cluster_id, "namespace": namespace, "name": name}
        with (self.root / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def recent_audit(self, limit: int = 200) -> list[dict]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as stream:
            lines = deque(stream, maxlen=min(1000, max(1, limit)))
        return [json.loads(line) for line in lines if line.strip()]
