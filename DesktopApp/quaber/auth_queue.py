"""One login thread; a rejected account is not retried across its clusters."""
import contextvars
import hashlib
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='quaber-login')
_rejected = set()
_lock = threading.Lock()
_connections = {}

class ConnectionLock:
    """Exclusive credential transactions; concurrent admitted resource reads."""
    def __init__(self):
        self.condition=threading.Condition()
        self.owner=None
        self.depth=self.readers=self.waiting=0
    def __enter__(self):
        with self.condition:
            current=threading.get_ident()
            if self.owner==current:
                self.depth+=1
                return self
            self.waiting+=1
            try:
                while self.owner is not None or self.readers:self.condition.wait(.05)
                self.owner=current
                self.depth=1
            finally:self.waiting-=1
        return self
    def __exit__(self,*_):
        with self.condition:
            self.depth-=1
            if not self.depth:self.owner=None
            self.condition.notify_all()
    @contextmanager
    def read(self):
        with self.condition:
            while self.owner is not None or self.waiting:self.condition.wait(.05)
            self.readers+=1
        try:yield
        finally:
            with self.condition:
                self.readers-=1
                self.condition.notify_all()

def connection_lock(store, identifier):
    with _lock:
        return _connections.setdefault((str(store.root),identifier),ConnectionLock())


def account_key(store, cluster):
    return (str(store.root), cluster.get('accountId') or cluster.get('username'))


def clear_rejection(store, cluster):
    key = account_key(store, cluster)
    with _lock:
        _rejected.difference_update(v for v in tuple(_rejected) if v[0] == key)


def serialized_login(store, cluster, password, function):
    context = contextvars.copy_context()
    signature = (account_key(store, cluster), hashlib.sha256(password.encode()).digest())
    def perform():
        from .authorization import AuthorizationRequired
        from .kubectl import _login_message
        with _lock:
            if signature in _rejected:
                raise AuthorizationRequired('Пароль учётной записи отклонён. Обновите его в настройках учётных записей.')
        try:
            return function()
        except Exception as exc:
            message = _login_message(str(exc), password).lower()
            if any(word in message for word in ('unauthorized','invalid credentials','invalid password','incorrect','rejected','неверн','отклонён')):
                with _lock: _rejected.add(signature)
            raise
    return _pool.submit(context.run, perform).result()
