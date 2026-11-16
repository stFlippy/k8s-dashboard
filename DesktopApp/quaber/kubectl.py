"""Corporate kubectl login and per-cluster localhost proxy."""

from __future__ import annotations

import os
import hashlib
import queue
import re
import subprocess
import tempfile
import threading
import time
import json
import urllib.request
import uuid
import logging
from dataclasses import dataclass
from pathlib import Path

from .storage import UserStore, sanitize_kubeconfig
from .processes import start_process, stop_process
from .app_logging import register_secret

logger = logging.getLogger("quaber.kubectl")


def locate_kubectl(app_dir: Path, configured_path: str = "") -> Path:
    if configured_path.strip():
        path = Path(os.path.expandvars(configured_path.strip())).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("Указанный kubectl не найден. Выберите исполняемый файл вручную.")
        if os.name == "nt" and path.suffix.lower() != ".exe":
            raise ValueError("На Windows выберите kubectl.exe.")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise ValueError("Указанный kubectl не является исполняемым файлом.")
        return path
    # Only PATH entries are searched; a bundled file must be selected explicitly.
    name = "kubectl.exe" if os.name == "nt" else "kubectl"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(os.path.expandvars(entry.strip('"'))) / name
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return path.resolve()
    raise FileNotFoundError("kubectl не найден в PATH. Укажите путь к kubectl вручную.")


def _login_message(output: str, password: str) -> str:
    safe = output.replace(password, "[REDACTED]") if password else output
    safe = re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", safe, flags=re.I)
    safe = re.sub(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", safe)
    safe = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", safe)
    safe = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", safe).strip()
    if re.search(r"invalid username or password|incorrect password|wrong password|неверн.*парол", safe, re.I):
        return "Неверный логин или пароль кластера."
    if re.search(r"x509|certificate|tls handshake|сертификат", safe, re.I):
        return "Ошибка TLS-сертификата. Проверьте доверенные CA и адрес API."
    if re.search(r"connection refused|no route to host|network is unreachable|timed out|i/o timeout", safe, re.I):
        return "Не удалось подключиться к API-серверу. Проверьте адрес, сеть и порт."
    if re.search(r"unknown flag|unknown command|flag provided but not defined", safe, re.I):
        return "Этот kubectl не поддерживает login URL --username USER --password PASS."
    return safe[-1000:] if safe else "kubectl login завершился без диагностического вывода."


def run_limited(args: list[str], timeout: float, limit: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Read both pipes concurrently and stop a client that exceeds the output budget."""
    process = start_process(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    chunks: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=32)
    stop = threading.Event()
    def read_pipe(name: str, pipe) -> None:
        try:
            while True:
                chunk = pipe.read1(65536)
                while not stop.is_set():
                    try:
                        chunks.put((name, chunk), timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    break
        finally:
            pipe.close()
    threads = [threading.Thread(target=read_pipe, args=(name, pipe), daemon=True)
               for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))]
    for thread in threads:
        thread.start()
    output = {"stdout": [], "stderr": []}
    total = 0
    complete = 0
    deadline = time.monotonic() + timeout
    try:
        while complete < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Внешняя команда превысила {timeout:g} секунд; результат неизвестен.")
            try:
                name, chunk = chunks.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if not chunk:
                complete += 1
                continue
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"Вывод внешней команды превысил {limit // 1024} КиБ; результат неизвестен.")
            output[name].append(chunk)
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"Внешняя команда превысила {timeout:g} секунд; результат неизвестен.") from None
        return subprocess.CompletedProcess(args, code, b"".join(output["stdout"]), b"".join(output["stderr"]))
    finally:
        stop.set()
        stop_process(process)
        for thread in threads:
            thread.join(timeout=1)


def login(executable: Path, server: str, username: str, password: str) -> str:
    register_secret(password)
    if not password or len(password) > 1024:
        raise ValueError("Введите пароль кластера (не более 1024 символов).")
    logger.info("Вход в кластер %s", server)
    with tempfile.TemporaryDirectory(prefix="quaber-login-") as home:
        config = Path(home) / ".kube" / "config"
        config.parent.mkdir(mode=0o700)
        env = {key: value for key, value in os.environ.items() if not key.startswith("QUABER_")}
        env.update(HOME=home, KUBECONFIG=str(config))
        # Existing corporate client requires this exact CLI, including the password argument.
        result = run_limited([str(executable), "login", server, "--username", username, "--password", password],
                             timeout=45, limit=1_000_000, env=env)
        if result.returncode:
            logger.warning("Вход в кластер не выполнен: код %s", result.returncode)
            raise RuntimeError(_login_message((result.stderr + b"\n" + result.stdout).decode("utf-8", "replace"), password))
        if not config.exists():
            raise RuntimeError("kubectl login не создал kubeconfig.")
        sanitized = sanitize_kubeconfig(config.read_text(encoding="utf-8"), server)
        logger.info("Вход в кластер завершён")
        return sanitized


@dataclass
class Proxy:
    process: subprocess.Popen
    temporary: tempfile.TemporaryDirectory
    base_url: str
    last_used: float
    credential_hash: int
    idle_timer: threading.Timer | None = None
    readers: tuple[threading.Thread, ...] = ()


class ProxyManager:
    def __init__(self, executable: Path | None, store: UserStore):
        self.executable, self.store = executable, store
        self._proxies: dict[str, Proxy] = {}
        self._lock = threading.RLock()

    def close(self, cluster_id: str | None = None) -> None:
        with self._lock:
            ids = [cluster_id] if cluster_id else list(self._proxies)
            for item in ids:
                proxy = self._proxies.pop(item, None)
                if proxy:
                    if proxy.idle_timer:
                        proxy.idle_timer.cancel()
                    self._stop(proxy.process, proxy.readers)
                    proxy.temporary.cleanup()

    @staticmethod
    def _stop(process, readers):
        stop_process(process)
        for reader in readers:
            reader.join(timeout=1)
        # Never wait on a buffered pipe lock held by a blocked reader.
        if not any(reader.is_alive() for reader in readers):
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    def executable_for(self, cluster: dict) -> Path:
        if self.store.preferences().get("kubectl_path"):
            return locate_kubectl(self.store.root.parent, self.store.preferences()["kubectl_path"])
        # Resolve PATH at use time, including after a manual client selection.
        return locate_kubectl(self.store.root.parent) if self.executable is None else self.executable

    def _touch(self, cluster_id: str, proxy: Proxy) -> None:
        proxy.last_used = time.monotonic()
        if proxy.idle_timer:
            proxy.idle_timer.cancel()
        proxy.idle_timer = threading.Timer(900, lambda: self.close(cluster_id))
        proxy.idle_timer.daemon = True
        proxy.idle_timer.start()

    def url(self, cluster: dict) -> str:
        executable = self.executable_for(cluster)
        if not cluster["enabled"]:
            raise ValueError(f"Кластер {cluster['name']} отключён.")
        credential = self.store.decrypt(cluster["encryptedKubeconfig"])
        fingerprint = int.from_bytes(hashlib.sha256((str(executable) + "\0" + credential).encode()).digest(), "big")
        with self._lock:
            proxy = self._proxies.get(cluster["id"])
            if proxy and (proxy.process.poll() is not None or proxy.credential_hash != fingerprint or time.monotonic() - proxy.last_used > 900):
                self.close(cluster["id"])
                proxy = None
            if proxy:
                self._touch(cluster["id"], proxy)
                return proxy.base_url
            temporary = tempfile.TemporaryDirectory(prefix="quaber-proxy-")
            config = Path(temporary.name) / "config"
            config.write_text(credential, encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            prefix = "/quaber-" + os.urandom(18).hex()
            args = [str(executable), "proxy", "--kubeconfig", str(config), "--address=127.0.0.1", "--port=0",
                    f"--api-prefix={prefix}", "--reject-paths=^$", r"--accept-hosts=^127\.0\.0\.1$"]
            try:
                process = start_process(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1)
            except BaseException:
                temporary.cleanup()
                raise
            output: queue.Queue[str] = queue.Queue(maxsize=100)
            def put_output(line: str) -> None:
                try:
                    output.put_nowait(line)
                except queue.Full:
                    pass
            def read_stdout() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    put_output(line)
            # Drain stderr too: a verbose client must not block on its pipe.
            def read_stderr() -> None:
                assert process.stderr is not None
                for line in process.stderr:
                    put_output("stderr: " + line[-500:])
            readers = (threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True))
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + 15
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("kubectl proxy завершился до запуска. Проверьте срок действия kubeconfig.")
                    try:
                        line = output.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                    except queue.Empty:
                        continue
                    match = re.search(r"Starting to serve on 127\.0\.0\.1:(\d+)", line)
                    if match:
                        proxy = Proxy(process, temporary, f"http://127.0.0.1:{match.group(1)}{prefix}", time.monotonic(), fingerprint, readers=readers)
                        self._proxies[cluster["id"]] = proxy
                        self._touch(cluster["id"], proxy)
                        return proxy.base_url
                raise TimeoutError("kubectl proxy не запустился за 15 секунд.")
            except Exception:
                self._stop(process, readers)
                temporary.cleanup()
                raise

    def execute(self, cluster: dict, namespace: str, pod: str, container: str, command: str) -> tuple[str, str]:
        executable = self.executable_for(cluster)
        command = command.strip()
        if not command or len(command) > 8192:
            raise ValueError("Команда должна содержать от 1 до 8192 символов.")
        with tempfile.TemporaryDirectory(prefix="quaber-exec-") as home:
            config = Path(home) / "config"
            config.write_text(self.store.decrypt(cluster["encryptedKubeconfig"]), encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            result = run_limited([str(executable), "--kubeconfig", str(config), "exec", "-n", namespace,
                                  pod, "-c", container, "--", "/bin/sh", "-lc", command], timeout=30, limit=2 * 1024 * 1024)
            stdout, stderr = result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")
            if result.returncode:
                raise RuntimeError(stderr[:2000] or f"Exec завершился с кодом {result.returncode}.")
            try:
                self.store.audit("exec-pod", cluster["id"], namespace, pod)
            except OSError as exc:
                raise RuntimeError("Команда выполнена, но запись аудита не удалась. Проверьте результат перед повтором.") from exc
            return stdout, stderr


def test_connection(executable: Path, store: UserStore, server: str, kubeconfig: str) -> int:
    """Exercise the same proxy path without saving a candidate cluster."""
    proxy = ProxyManager(executable, store)
    temporary = {"id": str(uuid.uuid4()), "name": "candidate", "enabled": True,
                 "server": server, "encryptedKubeconfig": store.encrypt(kubeconfig)}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(proxy.url(temporary) + "/api/v1/namespaces", timeout=15) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("Ответ проверки подключения превышает 4 МиБ.")
            result = json.loads(body)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError("Kubernetes вернул некорректный список namespaces.")
        return len(result["items"])
    finally:
        proxy.close()
