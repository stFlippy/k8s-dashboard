"""Action-specific SSAR descriptors; no wildcard inference or local role grants."""
from dataclasses import dataclass
from enum import Enum


class AccessOutcome(Enum):
    ALLOWED = 'allowed'
    DENIED = 'denied'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ActionTarget:
    cluster_id: str
    kind: str
    namespace: str
    name: str
    action: str
    eviction: bool = False

    def attributes(self):
        resources = {'deployment': ('apps', 'deployments'), 'statefulset': ('apps', 'statefulsets'),
                     'pod': ('', 'pods'), 'configmap': ('', 'configmaps')}
        if self.kind not in resources or not all((self.cluster_id, self.namespace, self.name)):
            raise ValueError('A concrete supported target is required.')
        group, resource = resources[self.kind]
        allowed = {'deployment': {'yaml','resources','scale','restart'},
                   'statefulset': {'scale'}, 'configmap': {'yaml'}, 'pod': {'yaml','delete','exec'}}
        if self.action not in allowed[self.kind]: raise ValueError('Unsupported action for target kind.')
        verb, subresource = 'update', ''
        if self.action == 'scale': subresource = 'scale'
        elif self.action == 'restart': verb = 'patch'
        elif self.action == 'exec': verb, subresource = 'create', 'exec'
        elif self.action == 'delete':
            verb, subresource = ('create','eviction') if self.eviction else ('delete','')
        return dict(group=group, resource=resource, namespace=self.namespace,
                    name=self.name, verb=verb, **({'subresource': subresource} if subresource else {}))


@dataclass(frozen=True)
class AccessDecision:
    target: ActionTarget
    outcome: AccessOutcome
    reason: str


def review_action(api, target):
    attributes = target.attributes()
    response = api.request(target.cluster_id, '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', 'POST', {
        'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
        'spec': {'resourceAttributes': attributes}})
    status = response.get('status') if isinstance(response, dict) else None
    if (not isinstance(status, dict) or type(status.get('allowed')) is not bool
            or status.get('evaluationError') or status.get('allowed') is True and status.get('denied') is True):
        return AccessDecision(target, AccessOutcome.UNKNOWN, 'Authorization review is incomplete.')
    return AccessDecision(target, AccessOutcome.ALLOWED if status['allowed'] else AccessOutcome.DENIED,
                          'Allowed.' if status['allowed'] else 'Action is not authorized for this target.')
