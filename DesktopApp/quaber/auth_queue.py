"""One login thread; a rejected account is not retried across its clusters."""
import contextvars
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='quaber-login')
_rejected = set()
_lock = threading.Lock()


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
