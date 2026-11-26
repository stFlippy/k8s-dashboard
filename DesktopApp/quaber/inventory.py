"""Read-only Deployment/CronJob snapshots and the CSV format used by export-curl.cmd."""
from .csv_io import write_rows
import os
import tempfile
import time
import threading
import logging
import copy
import uuid
from dataclasses import dataclass, field
from contextlib import nullcontext
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from .kubernetes import Kubernetes, AggregateListForbidden
from .polling import polling_allowed

APP_LABEL = "app.kubernetes.io/name"
CSV_FIELDS = ("HOST", "NAMESPACE", "KIND", "NAME", "CONTAINER_IMAGES", "branch", "deployed by", "version", "REPLICAS")
logger = logging.getLogger(__name__)


@dataclass
class InventoryRead:
    cluster_id: str
    generation: int
    revision: str
    subject: tuple
    replans: int
    previous: object = None
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel: threading.Event = field(default_factory=threading.Event)
    terminal: bool = False
    adopted: bool = False
    renewals: tuple = ()


def inventory_record(resource, kind, namespace):
    metadata = resource.get("metadata", {})
    spec = resource.get("spec", {})
    job = spec.get("jobTemplate", {}) if kind == "CronJob" else {}
    template = job.get("spec", {}).get("template", {}) if kind == "CronJob" else spec.get("template", {})
    layers = [metadata.get("labels", {})]
    if kind == "CronJob":
        layers.append(job.get("metadata", {}).get("labels", {}))
    layers.append(template.get("metadata", {}).get("labels", {}))
    labels = {}
    for layer in layers:
        for key, value in layer.items():
            if value not in (None, "") and key not in labels:
                labels[key] = str(value)
    def first(*keys):
        return next((str(layer[key]) for layer in layers for key in keys if layer.get(key) not in (None, "")), "")
    pod_spec = template.get("spec", {})
    return {"namespace": namespace, "kind": kind, "name": metadata.get("name", ""), "labels": labels,
            "images": [container.get("image", "") for key in ("containers", "initContainers") for container in pod_spec.get(key, [])],
            "branch": first("GIT_BRANCH", "vtb.ru/branch"), "deployed_by": first("STARTED_BY", "vtb.ru/started_by"),
            "version": first("app.kubernetes.io/version"),
            "replicas": resource.get("status", {}).get("replicas", 0) if kind == "Deployment" else "—"}


def collect_inventory(api, cluster, progress=None):
    from .request_policy import expected_credentials, credentials_revision
    with expected_credentials({cluster['id']: credentials_revision(cluster)}):
        return _collect_inventory(api, cluster, progress)


def _collect_inventory(api, cluster, progress=None):
    from .monitoring import MonitoringCancelled
    result = {"cluster_id": cluster["id"], "cluster_name": cluster["name"], "server": cluster["server"],
              "captured_at": datetime.now(timezone.utc).isoformat(), "namespace_count": None, "records": [], "errors": []}
    cid = cluster["id"]
    try:
        namespaces = sorted({resource["metadata"]["name"] for resource in api.snapshot_list(cid, "/api/v1/namespaces")})
    except MonitoringCancelled:
        raise
    except Exception as exc:
        result["errors"].append(f"Список namespace: {exc}")
        return result
    result["namespace_count"] = len(namespaces)
    result["namespaces"] = namespaces
    result["namespace_errors"] = {}
    result['failed_types'] = []
    collections = {}
    scoped = {}
    for kind in ('Deployment', 'CronJob'):
        try:
            collections[kind] = api.all_namespace_resources(cid, kind.lower())
        except MonitoringCancelled:
            raise
        except AggregateListForbidden:
            pass
        except Exception as exc:
            collections[kind] = exc
        if progress is not None and kind == 'Deployment':
            fragment = copy.deepcopy(result)
            fragment['partial'] = True
            values = collections.get(kind)
            if kind not in collections:
                values = []
                for namespace in namespaces:
                    try:
                        scoped[(namespace,kind)] = api.snapshot_list(cid,Kubernetes.path(kind.lower(),namespace))
                        values.extend(scoped[(namespace,kind)])
                    except MonitoringCancelled:
                        raise
                    except Exception as exc:
                        scoped[(namespace,kind)] = exc
                        fragment['errors'].append(f'{namespace} / {kind}: {exc}')
            if isinstance(values,Exception):
                fragment['errors'].append(f'{kind}: {values}')
                values = []
            fragment['records'] = [inventory_record(resource,kind,resource.get('metadata',{}).get('namespace',''))
                                   for resource in values
                                   if resource.get('metadata',{}).get('namespace') in namespaces][:100000]
            progress(fragment)
    indexed = {}
    for kind, values in collections.items():
        if isinstance(values, Exception): continue
        indexed[kind] = defaultdict(list)
        for resource in values:
            indexed[kind][resource.get('metadata', {}).get('namespace')].append(resource)
    for namespace in namespaces:
        for kind in ("Deployment", "CronJob"):
            try:
                value = collections.get(kind)
                if isinstance(value, Exception): raise value
                if (namespace,kind) in scoped:
                    value = scoped[(namespace,kind)]
                    if isinstance(value,Exception): raise value
                    resources = value
                else:
                    resources = (indexed[kind].get(namespace, []) if kind in indexed else
                             api.snapshot_list(cid, Kubernetes.path(kind.lower(), namespace)))
                for resource in resources:
                    if len(result["records"]) >= 100000:
                        result["errors"].append("Превышен предел 100000 ресурсов; снимок неполный.")
                        result["global_errors"] = [result["errors"][-1]]
                        return result
                    result["records"].append(inventory_record(resource, kind, namespace))
            except MonitoringCancelled:
                raise
            except Exception as exc:
                result['failed_types'].append((namespace, kind))
                message = f"{namespace} / {kind}: {exc}"
                result["errors"].append(message)
                result["namespace_errors"].setdefault(namespace, []).append(message)
    return result


class InventoryService(QObject):
    changed = pyqtSignal()
    progress = pyqtSignal(object)

    def prune_connections(self, valid):
        for cid in tuple(self.operations):
            if cid not in valid:
                self.operations[cid].cancel.set()
                self._finish(self.operations[cid], 'CANCELLED')
        for mapping in (self.snapshots, self.loading, self.generations, self.cancellations, self.results):
            for cid in set(mapping) - valid:
                mapping.pop(cid, None)
        self.requested.intersection_update(valid)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.snapshots, self.loading, self.generations = {}, {}, {}
        self.demanded = False
        self.cancellations = {}
        self.operations, self.results = {}, {}
        self.requested = set()
        self._demand_owner = None
        self.progress.connect(self._progress)

    def _progress(self, message):
        cid, operation_id, signature, snapshot = message
        operation = self.operations.get(cid)
        if self.window._closing or operation is None or operation.operation_id != operation_id or operation.cancel.is_set():
            return
        current = next((cluster for cluster in self.clusters() if cluster['id']==cid),None)
        if current is not None and self.signature(current) == signature:
            self.snapshots[cid] = (signature,time.monotonic(),snapshot)
            logger.info('inventory.read operation=%s cluster=%s generation=%s outcome=PROGRESS_ADOPTED rows=%s',
                        operation_id,cid,operation.generation,len(snapshot['records']))
            self.changed.emit()

    def set_demanded(self, active, owner=None):
        if not active and owner is not None and self._demand_owner is not owner:
            return
        self._demand_owner = owner if active else None
        self.demanded = active
        if not active:
            self.cancel_reads('PAUSED')

    def cancel_reads(self, reason='CANCELLED'):
        for operation in list(self.operations.values()):
            operation.cancel.set()
            self._finish(operation, reason)

    def _finish(self, operation, state, reason=''):
        if operation.terminal: return
        operation.terminal = True
        if self.operations.get(operation.cluster_id) is not operation: return
        self.operations.pop(operation.cluster_id,None)
        self.loading.pop(operation.cluster_id,None)
        self.cancellations.pop(operation.cluster_id,None)
        self.results[operation.cluster_id] = (state, reason)
        cached = self.snapshots.get(operation.cluster_id)
        if cached is not None and state in ('CANCELLED','PAUSED','STALE_DELIVERY','REJECTED'):
            snapshot = dict(cached[2],stale=True,delivery_state=state,
                            last_success=cached[2].get('last_success',cached[2]['captured_at']))
            self.snapshots[operation.cluster_id] = (cached[0],cached[1],snapshot)
        logger.info('inventory.read operation=%s cluster=%s generation=%s outcome=%s adopted=%s cached=%s rows=%s',
                    operation.operation_id,operation.cluster_id,operation.generation,state,operation.adopted,
                    cached is not None,len(cached[2]['records']) if cached else 0)
        if not self.window._closing: self.changed.emit()

    def state(self, cid):
        if cid in self.loading and cid not in self.operations:
            self.loading.pop(cid,None)
            self.cancellations.pop(cid,None)
            self.results[cid] = ('REJECTED','Нарушен lifecycle чтения; повторите загрузку.')
            logger.error('inventory.read cluster=%s outcome=LIFECYCLE_DEFECT orphan_loading=true',cid)
        if cid in self.operations: return 'LOADING'
        if cid in self.results: return self.results[cid][0]
        snapshot = self.get(cid)
        return ('PARTIAL' if snapshot.get('errors') else 'READY') if snapshot is not None else 'NOT_REQUESTED'

    def diagnostics(self, ids):
        controller = getattr(self.window,'monitoring',None)
        if controller is None:
            return f'demand={self.demanded} monitoring=STARTING'
        policy = controller.policy
        lines = [f'demand={self.demanded} monitoring={policy.state} reason={policy.hard_reason or ", ".join(sorted(policy.reasons)) or "—"}']
        for cid in sorted(ids):
            operation = self.operations.get(cid)
            lines.append(f'{cid}: state={self.state(cid)} generation={self.generations.get(cid,0)} '
                         f'inflight={operation.operation_id if operation else "—"}')
        return '\n'.join(lines)

    def status_text(self, ids):
        states = [self.state(cid) for cid in ids]
        if not states: return 'Выберите кластеры.'
        if not polling_allowed(self):
            policy = self.window.monitoring.policy
            return 'Загрузка приостановлена: '+(policy.hard_reason or ', '.join(sorted(policy.reasons)) or policy.state)
        loading = states.count('LOADING')
        if loading: return f'Загрузка снимков: {loading} из {len(states)}…'
        if any(s == 'PAUSED' for s in states): return 'Загрузка приостановлена. Возобновите мониторинг.'
        if any(s in ('CANCELLED','STALE_DELIVERY','REJECTED') for s in states):
            return 'Загрузка отменена или отложена. Повторите загрузку.'
        if any(s == 'FAILED' for s in states): return 'Не удалось загрузить часть подключений. Повторите загрузку или проверьте настройки.'
        if all(s == 'NOT_REQUESTED' for s in states): return 'Снимки ещё не запрошены. Повторите загрузку.'
        return ''

    def clusters(self):
        return [cluster for cluster in self.window.store.clusters() if cluster.get("enabled")]

    @staticmethod
    def signature(cluster):
        from .request_policy import credentials_revision
        return credentials_revision(cluster)

    def get(self, cid):
        cluster = next((c for c in self.clusters() if c["id"] == cid), None)
        return self.get_for_cluster(cluster) if cluster else None

    def get_for_cluster(self, cluster):
        cached = self.snapshots.get(cluster["id"])
        return cached[2] if cached and cached[0] == self.signature(cluster) else None

    def refresh(self, ids, *, force=False, _replans=0, _preserve_selection=False):
        ids = set(ids)
        if not _preserve_selection: self.requested = ids
        if not self.demanded or not polling_allowed(self):
            return
        for cluster in self.clusters():
            cid = cluster["id"]
            if cid not in ids or (cid in self.loading and not force):
                continue
            cached = self.snapshots.get(cid)
            if (not force and self.get_for_cluster(cluster) is not None and time.monotonic()-cached[1] < 60
                    and self.results.get(cid,('READY',''))[0] in ('READY','PARTIAL')):
                continue
            self.generations[cid] = self.generations.get(cid, 0)+1
            generation = self.generations[cid]
            old = self.operations.get(cid)
            if old is not None:
                old.cancel.set()
                self._finish(old,'CANCELLED')
            operation = InventoryRead(cid,generation,self.signature(cluster),
                                      (cluster.get('server'),cluster.get('username')), _replans,
                                      previous=self.get_for_cluster(cluster))
            self.operations[cid] = operation
            canceled = self.cancellations[cid] = operation.cancel
            self.loading[cid] = generation
            signature, api = self.signature(cluster), self.window.api
            progress_signal = self.progress
            logger.info('inventory.read operation=%s cluster=%s generation=%s outcome=QUEUED',operation.operation_id,cid,generation)
            def done(snapshot, cid=cid, generation=generation, signature=signature, operation=operation):
                if (self.window._closing or operation.terminal or operation.cancel.is_set()
                        or self.operations.get(cid) is not operation):
                    return
                current = next((c for c in self.clusters() if c["id"] == cid), None)
                if current and self.signature(current) == signature:
                    previous = operation.previous
                    if snapshot["namespace_count"] is None and previous:
                        snapshot = dict(snapshot, namespaces=sorted(set(previous.get("namespaces", [])) |
                                        {record["namespace"] for record in previous["records"]}),
                                        records=previous['records'], stale=True,
                                        last_success=previous.get('last_success', previous['captured_at']))
                    elif previous and snapshot.get('failed_types'):
                        failed_types = {tuple(key) for key in snapshot['failed_types']}
                        snapshot['records'].extend(dict(record, stale=True) for record in previous['records']
                            if (record['namespace'], record['kind']) in failed_types)
                        snapshot['stale'] = True
                        snapshot['last_success'] = previous.get('last_success', previous['captured_at'])
                    self.snapshots[cid] = (signature, time.monotonic(), snapshot)
                    operation.adopted = True
                    self._finish(operation,'FAILED' if snapshot['namespace_count'] is None else
                                 'PARTIAL' if snapshot.get('errors') else 'READY')
                else:
                    self._finish(operation,'STALE_DELIVERY','Подключение изменилось; повторите загрузку.')
            def failed(message, cluster=cluster, done=done):
                done({"cluster_id": cluster["id"], "cluster_name": cluster["name"], "server": cluster["server"],
                      "captured_at": datetime.now(timezone.utc).isoformat(), "namespace_count": None, "records": [], "errors": [message]})
            from .monitoring import cancellable_read
            def collect(cluster=dict(cluster), api=api, operation=operation, signature=signature):
                from .monitoring import _read_token
                from dataclasses import replace
                parent = _read_token.get()
                reset = _read_token.set(replace(parent,allow_renewal=False)) if operation.replans and parent is not None else None
                try:
                    return collect_inventory(api,cluster,progress=lambda snapshot:
                        progress_signal.emit((operation.cluster_id,operation.operation_id,signature,snapshot)))
                finally:
                    token = _read_token.get()
                    if token is not None: operation.renewals = tuple(token.renewals)
                    if reset is not None: _read_token.reset(reset)
                    logger.info('inventory.read operation=%s cluster=%s generation=%s outcome=WORKER_COMPLETED',
                                operation.operation_id,operation.cluster_id,operation.generation)
            read = cancellable_read(collect,
                                    canceled, self.window.monitoring.token())
            def finalized(outcome, operation=operation):
                if operation.terminal: return
                if self.window._closing:
                    self._finish(operation,'CANCELLED')
                    return
                current = next((c for c in self.clusters() if c['id']==operation.cluster_id),None)
                own_renewal = (current is not None and
                    (current.get('server'),current.get('username')) == operation.subject and
                    (operation.cluster_id,operation.revision,self.signature(current)) in operation.renewals)
                self._finish(operation,'STALE_DELIVERY' if outcome=='ACCEPTED' else 'CANCELLED')
                if (own_renewal and operation.replans < 1 and self.demanded and polling_allowed(self)
                        and operation.cluster_id in self.requested and not self.window._closing):
                    monitor = self.window.monitoring
                    if hasattr(monitor, 'accept_renewals'):
                        monitor.accept_renewals(operation.renewals)
                    def replan():
                        if (self.demanded and polling_allowed(self) and not self.window._closing
                                and operation.cluster_id not in self.operations and operation.cluster_id in self.requested):
                            self.refresh({operation.cluster_id},force=True,_replans=operation.replans+1,
                                         _preserve_selection=True)
                    QTimer.singleShot(0,replan)
            def valid(operation=operation):
                return (not operation.terminal and not operation.cancel.is_set()
                        and self.operations.get(operation.cluster_id) is operation)
            from .request_policy import manual_read
            with manual_read() if force else nullcontext():
                admitted = self.window.background.submit(read, done, failed,
                                             key=("inventory", cid), finalized=finalized, plan_valid=valid,
                                             plan_identity=(cid,generation,signature))
            if admitted is False:
                self._finish(operation,'REJECTED','Очередь занята; повторите загрузку.')
        self.changed.emit()


def inventory_row(snapshot, record):
    """Dump fields shared by the table and exports, including observed replicas."""
    return (snapshot["server"], record["namespace"], record["kind"], record["name"],
            ";".join(record["images"]), record["branch"], record["deployed_by"], record["version"], record.get("replicas", "—"))


def write_inventory(destination, rows, *, fields=CSV_FIELDS):
    """Atomically publish the visible CSV rows; never export credentials."""
    destination = Path(destination)
    if destination.suffix.lower() != ".csv":
        raise ValueError("Для дампа требуется файл .csv.")
    temporary = []
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=destination.parent, delete=False) as stream:
            csv_temp = Path(stream.name)
            temporary.append(csv_temp)
            write_rows(stream,fields,rows)
        os.replace(csv_temp, destination)
        return str(destination), None
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)


def namespace_snapshots(snapshots, selected=None):
    """One comparison column per cluster/namespace, including discovered empty scopes."""
    scopes = []
    for snapshot in snapshots:
        namespaces = set(snapshot.get("namespaces", [])) | {record["namespace"] for record in snapshot["records"]}
        for namespace in sorted(namespaces):
            identity = (snapshot["cluster_id"], namespace)
            if selected is not None and identity not in selected:
                continue
            errors = (snapshot.get("global_errors", [])+snapshot.get("namespace_errors", {}).get(namespace, [])
                      if "namespace_errors" in snapshot else snapshot["errors"])
            scopes.append(dict(snapshot, scope_id=identity, namespace=namespace,
                               scope_name=f"{snapshot['cluster_name']} / {namespace}", errors=errors,
                               stale=bool(snapshot.get('delivery_state')) or bool(errors) and snapshot.get('stale',False),
                               records=[record for record in snapshot["records"] if record["namespace"] == namespace]))
    return scopes


def compare_versions(snapshots, label_keys):
    services, skipped = defaultdict(lambda: defaultdict(list)), []
    for snapshot in snapshots:
        for record in snapshot["records"]:
            name = record["labels"].get(APP_LABEL)
            if name:
                services[name][snapshot.get("scope_id", snapshot["cluster_id"])].append(record)
            else:
                skipped.append(f"{snapshot['cluster_name']} / {record['namespace']} / {record['kind']}/{record['name']}")
    incomplete = any(snapshot["errors"] or snapshot.get('stale') or snapshot.get('partial') for snapshot in snapshots)
    rows = []
    for service, clusters in sorted(services.items()):
        cells, vectors = {}, []
        absent = False
        for snapshot in snapshots:
            records = clusters.get(snapshot.get("scope_id", snapshot["cluster_id"]), [])
            values = {tuple(record["labels"].get(key, "") for key in label_keys) for record in records}
            vectors.append(values)
            absent |= not records
            text = "\n".join(f"{key}: {', '.join(sorted({record['labels'].get(key) or '—' for record in records}))}" for key in label_keys) if records else "Отсутствует"
            detail = "\n".join(f"{record['namespace']} · {record['kind']}/{record['name']} · " +
                                ", ".join(f"{key}={record['labels'].get(key, '—')}" for key in label_keys) for record in records)
            if snapshot.get('stale') or not records and snapshot["errors"]:
                text, detail = "Нет данных", "\n".join(snapshot["errors"] or ['Снимок не обновлён.'])+'\n'+detail
            cells[snapshot.get("scope_id", snapshot["cluster_id"])] = (text, detail)
        if not label_keys:
            status = "Выберите метки"
        elif incomplete:
            status = "Неполные данные"
        elif absent:
            status = "Отсутствует"
        elif len(snapshots) < 2:
            status = "Один namespace"
        elif any(len(values) != 1 for values in vectors) or len(set.union(*vectors)) != 1:
            status = "Различается"
        else:
            status = "Совпадает"
        label_statuses = {}
        for index, key in enumerate(label_keys):
            values = [{vector[index] for vector in group} for group in vectors]
            label_statuses[key] = ("Неполные данные" if incomplete else "Отсутствует" if absent else
                                   "Один namespace" if len(snapshots) < 2 else
                                   "Совпадает" if all(len(group) == 1 for group in values) and len(set.union(*values)) == 1 else "Различается")
        rows.append({"service": service, "status": status, "cells": cells, "label_statuses": label_statuses})
    return rows, skipped
