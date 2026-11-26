"""One aggregate/scoped strategy shared by read-only screens."""
from collections import defaultdict
from dataclasses import dataclass
from .authorization import AuthorizationRequired
from .monitoring import MonitoringCancelled


def namespace_index(resources):
    groups = defaultdict(list)
    for resource in resources:
        groups[resource.get('metadata', {}).get('namespace', '')].append(resource)
    return dict(groups)


def pod_owner_kinds(cache, cluster_id, namespace, name):
    """Invalidate the actual controller ancestry, rather than every workload."""
    with cache.lock:
        values = [entry[1] for key, entry in cache.entries.items()
                  if isinstance(key, tuple) and key[0] == cluster_id and not entry[2] and isinstance(entry[1], list)]
    resources = [value for group in values for value in group if isinstance(value, dict)
                 and value.get('metadata', {}).get('namespace', namespace) == namespace]
    target = next((value for value in resources
                   if value.get('metadata', {}).get('name') == name
                   and ('status' in value and ('phase' in value['status'] or 'containerStatuses' in value['status'])
                        or value.get('kind') == 'Pod')), None)
    if target is None:
        # Unknown ancestry: retain conservative invalidation of controller lists.
        return {'pod', 'deployment', 'replicaset', 'statefulset', 'daemonset', 'replicationcontroller', 'job', 'cronjob'}
    by_uid = {value.get('metadata', {}).get('uid'): value for value in resources}
    result, seen = {'pod'}, set()
    for _ in range(4):
        owner = next((o for o in target.get('metadata', {}).get('ownerReferences', []) if o.get('controller')), None)
        if not owner or owner.get('uid') in seen:
            break
        seen.add(owner.get('uid'))
        kind = owner.get('kind', '').lower()
        result.add(kind)
        target = by_uid.get(owner.get('uid'))
        if target is None:
            if kind == 'replicaset':
                result.add('deployment')
            elif kind == 'job':
                result.add('cronjob')
            break
    return result


@dataclass(frozen=True)
class CollectionPlan:
    namespaces: tuple
    aggregate: bool
    full_coverage: bool = True


@dataclass
class CollectionResult:
    groups: dict
    captured: dict
    errors: dict


class CollectionReader:
    def __init__(self, api):
        self.api = api

    def plan(self, cluster_id, namespaces=None):
        names = tuple(dict.fromkeys(namespaces)) if namespaces is not None else None
        if names == ():
            return CollectionPlan((), True)
        try:
            known = tuple(self.api.namespaces(cluster_id))
        except (MonitoringCancelled, AuthorizationRequired):
            raise
        except Exception:
            # Explicit namespace access can work without permission to discover.
            if names is None:
                raise
            return CollectionPlan(names, False)
        selected = known if names is None else tuple(ns for ns in known if ns in names)
        return CollectionPlan(selected, True, set(known).issubset(selected))

    def read(self, cluster_id, kind, plan):
        from .kubernetes import GVR, AggregateListForbidden
        groups, captured, errors = {}, {}, {}
        if not plan.namespaces:
            return CollectionResult(groups, captured, errors)
        aggregate = plan.aggregate
        if aggregate and hasattr(self.api, 'all_namespace_resources'):
            try:
                resources = self.api.all_namespace_resources(cluster_id, kind)
                base, plural = GVR[kind]
                stamp = self.api.snapshot_captured_at(cluster_id, f'{base}/{plural}')
                indexed = namespace_index(resources)
                return CollectionResult({ns: indexed.get(ns, []) for ns in plan.namespaces},
                                        {ns: stamp for ns in plan.namespaces}, {})
            except AggregateListForbidden:
                pass
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                from .app_logging import log_problem
                log_problem(f'{cluster_id} · {kind}', exc)
                return CollectionResult({}, {}, {ns: exc for ns in plan.namespaces})
        for namespace in plan.namespaces:
            try:
                groups[namespace] = self.api.list_resources(cluster_id, kind, namespace)
                captured[namespace] = self.api.snapshot_captured_at(cluster_id, self.api.path(kind, namespace))
            except (MonitoringCancelled, AuthorizationRequired):
                raise
            except Exception as exc:
                from .app_logging import log_problem
                log_problem(f'{cluster_id}/{namespace} · {kind}', exc)
                errors[namespace] = exc
        return CollectionResult(groups, captured, errors)
