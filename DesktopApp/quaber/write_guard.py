"""Local, one-operation dispatch permits; never persisted or sent to Kubernetes."""
import contextvars
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationIntent:
    identity: tuple
    credentials: str
    action: str
    operation_id: str
    parameters: str


def parameter_digest(parameters):
    """Bind confirmed input without retaining YAML or Terminal command text."""
    if isinstance(parameters, dict) and isinstance(parameters.get('metadata'), dict):
        parameters = dict(parameters, metadata={k: v for k, v in parameters['metadata'].items()
                                               if k != 'managedFields'})
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


class WriteGuard:
    ALLOWED = {'deployment': {'yaml', 'resources', 'scale', 'restart'}, 'statefulset': {'scale'},
               'configmap': {'yaml'}, 'pod': {'yaml', 'delete', 'exec'}}

    def __init__(self, allowed, revision, clock=time.monotonic, context=lambda: None):
        self.allowed, self.revision, self.clock = allowed, revision, clock
        self.context = context
        self.generation = 0
        self.lock = threading.RLock()

    def invalidate(self):
        with self.lock: self.generation += 1

    def authorize(self, identity, credentials, action, *, parameters, origin, generation, validator, deadline):
        """Shared worker admission. Never hold the local lock across RBAC HTTP."""
        identity = tuple(identity)
        def check():
            with self.lock:
                if (self.generation != generation or self.clock() >= deadline or not self.allowed() or self.context() != origin
                        or self.revision(identity[0]) != credentials):
                    raise PermissionError('Confirmed operation context expired or changed.')
        check()
        if validator(identity[0]) is not True:
            raise PermissionError('Не удалось подтвердить права администратора.')
        with self.lock:
            check()
            return self.issue(identity, credentials, action, authorized=True,
                              parameters=parameters, deadline=deadline)

    def issue(self, identity, credentials, action, *, authorized, parameters, deadline=None):
        identity = tuple(identity)
        if (authorized is not True or len(identity) != 5 or not identity[4]
                or action not in self.ALLOWED.get(identity[1], ())):
            raise PermissionError('Unsupported or unverified operation intent.')
        with self.lock:
            if not self.allowed():
                raise PermissionError('Changes are locked.')
            origin = self.context()
            intent = OperationIntent(identity, credentials, action, uuid.uuid4().hex,
                                     parameter_digest(parameters))
            return WritePermit(self, intent, self.generation,
                               min(self.clock()+60, deadline if deadline is not None else self.clock()+60))


class WritePermit:
    def __init__(self, guard, intent, generation, deadline):
        self.guard, self.intent, self.generation, self.deadline = guard, intent, generation, deadline
        self.state = 'issued'
        self.origin = guard.context()
        self.owner = None

    def check_parameters(self, parameters):
        self.check()
        if parameter_digest(parameters) != self.intent.parameters:
            raise PermissionError('Operation parameters changed after confirmation.')

    def delegate_dispatch(self):
        """Explicit one-hop ownership for the cluster's serial I/O worker."""
        self.check()
        caller = threading.get_ident()
        @contextmanager
        def dispatched():
            with self.guard.lock:
                if self.owner != caller:
                    raise PermissionError('Operation dispatch ownership changed.')
                self.owner = threading.get_ident()
                try:
                    self.check()
                except BaseException:
                    self.owner = caller
                    raise
            try:
                yield
            finally:
                with self.guard.lock:
                    self.owner = caller
        return dispatched

    def check_rest_parameters(self, method, payload):
        """Also protect callers using request() directly instead of business methods."""
        action, identity = self.intent.action, self.intent.identity
        body = payload if isinstance(payload, dict) else {}
        if action in ('yaml', 'resources'):
            self.check_parameters(body)
        elif action == 'scale':
            expected = {'apiVersion': 'autoscaling/v1', 'kind': 'Scale',
                        'metadata': {'name': identity[3], 'namespace': identity[2],
                                     'uid': identity[4], 'resourceVersion': body.get('metadata', {}).get('resourceVersion')},
                        'spec': {'replicas': body.get('spec', {}).get('replicas')}}
            if body != expected:
                raise PermissionError('Permit does not allow additional scale fields.')
            # The digest is fixed when the intent is approved; it binds both
            # desired replicas and RV, independently of the incoming REST body.
            self.check_parameters({'replicas': body['spec']['replicas'],
                                   'resourceVersion': body['metadata']['resourceVersion']})
        elif action == 'restart':
            stamp = (body.get('spec', {}).get('template', {}).get('metadata', {})
                     .get('annotations', {}).get('kubectl.kubernetes.io/restartedAt'))
            expected = {'metadata': {'uid': identity[4], 'resourceVersion': body.get('metadata', {}).get('resourceVersion')},
                        'spec': {'template': {'metadata': {'annotations': {'kubectl.kubernetes.io/restartedAt': stamp}}}}}
            if body != expected or not isinstance(stamp, str) or not stamp:
                raise PermissionError('Permit only allows the confirmed rollout restart.')
            self.check_parameters(None)
        elif action == 'delete':
            removal = body if method == 'DELETE' else body.get('deleteOptions',{})
            version = removal.get('preconditions',{}).get('resourceVersion')
            if not isinstance(version,str) or not version:
                raise PermissionError('Removal dispatch requires the approved resourceVersion.')
            options = {'preconditions': {'uid': identity[4], 'resourceVersion':version}}
            if method == 'DELETE':
                expected, mode = options, 'delete'
            else:
                expected = {'apiVersion': 'policy/v1', 'kind': 'Eviction',
                            'metadata': {'name': identity[3], 'namespace': identity[2]}, 'deleteOptions': options}
                mode = 'restart'
            if body != expected:
                raise PermissionError('Permit does not allow additional removal options.')
            self.check_parameters({'mode': mode,'resourceVersion':version})
        else:
            raise PermissionError('Permit does not cover REST mutation parameters.')

    def check(self, identity=None, action=None, *, commit=False):
        with self.guard.lock:
            if (self.guard.generation != self.generation or not self.guard.allowed()
                    or self.origin != self.guard.context()
                    or self.owner not in (None, threading.get_ident())
                    or self.guard.clock() >= self.deadline or self.state != 'issued'
                    or self.guard.revision(self.intent.identity[0]) != self.intent.credentials
                    or (identity is not None and tuple(identity) != self.intent.identity)
                    or (action is not None and action != self.intent.action)):
                raise PermissionError('Operation permit expired, locked, changed or already dispatched.')
            self.owner = threading.get_ident()
            if commit:
                self.state = 'commit-dispatched'


_current = contextvars.ContextVar('write_permit', default=None)


def current_permit():
    return _current.get()


@contextmanager
def operation_permit(permit):
    token = _current.set(permit)
    try:
        permit.check()
        yield
    finally:
        _current.reset(token)


def require_permit(permit, identity=None, action=None):
    if not isinstance(permit, WritePermit):
        raise PermissionError('An explicit operation permit is required.')
    permit.check(identity, action)
    return permit
