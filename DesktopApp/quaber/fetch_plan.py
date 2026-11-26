"""Immutable scope selection captured before work leaves the GUI thread."""
from dataclasses import dataclass
import copy
from .request_policy import credentials_revision
from .monitoring import MonitoringCancelled

COLLECTIONS = (('deployments', 'deployment'), ('pods', 'pod'), ('replica_sets', 'replicaset'),
               ('stateful_sets', 'statefulset'), ('daemon_sets', 'daemonset'),
               ('replication_controllers', 'replicationcontroller'), ('jobs', 'job'), ('cron_jobs', 'cronjob'))


@dataclass(frozen=True)
class ClusterPlan:
    identifier: str
    label: str
    credentials: str


@dataclass(frozen=True)
class FetchPlan:
    clusters: tuple
    namespaces: tuple | None
    epoch: int
    purpose: str = 'matrix'

    @classmethod
    def capture(cls, clusters, view, epoch):
        selected = view.get('selectedClusterIds')
        # Namespace filters are a local projection of a complete cluster
        # snapshot. Switching them neither cancels nor multiplies LISTs.
        ns = [] if view.get('selectedNamespaces') == [] else None
        return cls(tuple(ClusterPlan(c['id'], c['name'], credentials_revision(c)) for c in clusters
                         if c.get('enabled') and (selected is None or c['id'] in selected)),
                   tuple(ns) if ns is not None else None, epoch)


@dataclass(frozen=True)
class ScopeProgress:
    plan: FetchPlan
    cluster: ClusterPlan
    namespace: str
    token: object
    scope: dict


def collect_cluster(api, plan, cluster, previous=None, progress=None):
    from .request_policy import expected_credentials
    with expected_credentials({cluster.identifier: cluster.credentials}):
        return _collect_cluster(api, plan, cluster, previous, progress)


def _collect_cluster(api, plan, cluster, previous=None, progress=None):
    """Collect complete lists, preserving stale data independently per type."""
    if credentials_revision(api.cluster(cluster.identifier)) != cluster.credentials:
        raise MonitoringCancelled('Cluster credentials changed after planning.')
    from .collection_reader import CollectionReader
    reader = CollectionReader(api)
    selection = reader.plan(cluster.identifier, plan.namespaces)
    names = selection.namespaces
    old = previous or {}
    scopes = {ns: dict(_stale=False, _last_success=old.get(ns, {}).get('_last_success'), _type_errors={},
                      _type_success=dict(old.get(ns, {}).get('_type_success') or {})) for ns in names}
    for namespace, scope in scopes.items():
        for field, _ in COLLECTIONS:
            scope[field] = old.get(namespace, {}).get(field, [])
    failures = []
    from contextlib import ExitStack
    from concurrent.futures import ThreadPoolExecutor
    from .cluster_workers import configured_threads
    from .monitoring import bind_read
    with ExitStack() as workers:
        count = configured_threads(api)
        executor = workers.enter_context(ThreadPoolExecutor(max_workers=count)) if count > 1 else None
        pending = {kind: executor.submit(bind_read(reader.read), cluster.identifier, kind, selection)
                   for _, kind in COLLECTIONS} if executor else {}
        return _merge_collections(api, plan, cluster, reader, selection, names, old, scopes, failures, progress, pending)


def _merge_collections(api, plan, cluster, reader, selection, names, old, scopes, failures, progress, pending):
    for field, kind in COLLECTIONS:
        collection = pending[kind].result() if pending else reader.read(cluster.identifier, kind, selection)
        for ns in names:
            try:
                if ns in collection.errors:
                    raise collection.errors[ns]
                scopes[ns][field] = collection.groups[ns]
                captured = collection.captured.get(ns)
                if isinstance(captured, str): scopes[ns]['_type_success'][kind] = captured
                else: scopes[ns]['_type_success'].pop(kind, None)
            except MonitoringCancelled:
                raise
            except Exception as exc:
                scopes[ns][field] = old.get(ns, {}).get(field, [])
                scopes[ns]['_type_errors'][kind] = str(exc)
                scopes[ns]['_last_success'] = old.get(ns, {}).get('_last_success')
                if kind in ('deployment', 'pod', 'replicaset'):
                    scopes[ns]['_stale'] = True
                failures.append(f'{cluster.label}/{ns} · {kind}: {exc}')
        if kind == 'replicaset' and progress is not None:
            from .monitoring import _read_token
            for namespace, scope in scopes.items():
                # Emitted snapshots are detached; optional collection never
                # mutates objects already owned by the GUI.
                detached = copy.deepcopy(scope)
                detached['_loading'] = True
                detached['_ownership_pending'] = True
                progress(ScopeProgress(plan, cluster, namespace, _read_token.get(), detached))
    for scope in scopes.values():
        dates = scope['_type_success']
        if not scope['_type_errors'] and len(dates) == len(COLLECTIONS):
            scope['_last_success'] = min(dates.values())
    return {'name': cluster.label, 'namespaces': scopes}, failures
