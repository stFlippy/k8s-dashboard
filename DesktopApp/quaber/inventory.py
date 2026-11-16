"""Read-only Deployment/CronJob snapshots and the CSV format used by export-curl.cmd."""
import csv
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from PyQt6.QtCore import QObject, pyqtSignal
from .kubernetes import Kubernetes

APP_LABEL = "app.kubernetes.io/name"
CSV_FIELDS = ("HOST", "NAMESPACE", "KIND", "NAME", "CONTAINER_IMAGES", "branch", "deployed by", "version")


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
            "version": first("app.kubernetes.io/version")}


def collect_inventory(api, cluster):
    result = {"cluster_id": cluster["id"], "cluster_name": cluster["name"], "server": cluster["server"],
              "captured_at": datetime.now(timezone.utc).isoformat(), "namespace_count": None, "records": [], "errors": []}
    cid = cluster["id"]
    try:
        namespaces = sorted({resource["metadata"]["name"] for resource in api.list_pages(cid, "/api/v1/namespaces")})
    except Exception as exc:
        result["errors"].append(f"Список namespace: {exc}")
        return result
    result["namespace_count"] = len(namespaces)
    result["namespaces"] = namespaces
    result["namespace_errors"] = {}
    for namespace in namespaces:
        for kind in ("Deployment", "CronJob"):
            try:
                resources = api.list_pages(cid, Kubernetes.path(kind.lower(), namespace))
                for resource in resources:
                    if len(result["records"]) >= 100000:
                        result["errors"].append("Превышен предел 100000 ресурсов; снимок неполный.")
                        result["global_errors"] = [result["errors"][-1]]
                        return result
                    result["records"].append(inventory_record(resource, kind, namespace))
            except Exception as exc:
                message = f"{namespace} / {kind}: {exc}"
                result["errors"].append(message)
                result["namespace_errors"].setdefault(namespace, []).append(message)
    return result


class InventoryService(QObject):
    changed = pyqtSignal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.snapshots, self.loading, self.generations = {}, {}, {}

    def clusters(self):
        return [cluster for cluster in self.window.store.clusters() if cluster.get("enabled")]

    @staticmethod
    def signature(cluster):
        return (cluster.get("server"), cluster.get("username"), cluster.get("encryptedKubeconfig"), cluster.get("enabled"))

    def get(self, cid):
        cluster = next((c for c in self.clusters() if c["id"] == cid), None)
        return self.get_for_cluster(cluster) if cluster else None

    def get_for_cluster(self, cluster):
        cached = self.snapshots.get(cluster["id"])
        return cached[2] if cached and cached[0] == self.signature(cluster) else None

    def refresh(self, ids, *, force=False):
        for cluster in self.clusters():
            cid = cluster["id"]
            if cid not in ids or (cid in self.loading and not force):
                continue
            cached = self.snapshots.get(cid)
            if not force and self.get_for_cluster(cluster) is not None and time.monotonic()-cached[1] < 60:
                continue
            self.generations[cid] = self.generations.get(cid, 0)+1
            generation = self.generations[cid]
            self.loading[cid] = generation
            signature, api = self.signature(cluster), self.window.api
            def done(snapshot, cid=cid, generation=generation, signature=signature):
                if self.window._closing or self.loading.get(cid) != generation:
                    return
                self.loading.pop(cid, None)
                current = next((c for c in self.clusters() if c["id"] == cid), None)
                if current and self.signature(current) == signature:
                    previous = self.get_for_cluster(current)
                    if snapshot["namespace_count"] is None and previous:
                        snapshot = dict(snapshot, namespaces=sorted(set(previous.get("namespaces", [])) |
                                        {record["namespace"] for record in previous["records"]}))
                    self.snapshots[cid] = (signature, time.monotonic(), snapshot)
                elif current:
                    self.refresh([cid], force=True)
                self.changed.emit()
            def failed(message, cluster=cluster, done=done):
                done({"cluster_id": cluster["id"], "cluster_name": cluster["name"], "server": cluster["server"],
                      "captured_at": datetime.now(timezone.utc).isoformat(), "namespace_count": None, "records": [], "errors": [message]})
            if self.window.background.submit(lambda cluster=cluster, api=api: collect_inventory(api, cluster), done, failed,
                                             key=("inventory", cid)) is False:
                failed("Не удалось начать чтение снимка.")
        self.changed.emit()


def inventory_row(snapshot, record):
    """The eight export-curl.cmd fields, shared by the Dumps table and CSV."""
    return (snapshot["server"], record["namespace"], record["kind"], record["name"],
            ";".join(record["images"]), record["branch"], record["deployed_by"], record["version"])


def write_inventory(destination, snapshots, *, rows=None, include_log=True):
    """Publish a CSV and its collection log; no kubeconfigs or resource bodies are exported."""
    destination = Path(destination)
    if destination.suffix.lower() != ".csv":
        raise ValueError("Для дампа требуется файл .csv.")
    log_path = destination.with_suffix(".log")
    temporary = []
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=destination.parent, delete=False) as stream:
            csv_temp = Path(stream.name)
            temporary.append(csv_temp)
            writer = csv.writer(stream, quoting=csv.QUOTE_ALL)
            writer.writerow(CSV_FIELDS)
            if rows is None:
                rows = [inventory_row(snapshot, record) for snapshot in snapshots for record in snapshot["records"]]
            for row in rows:
                if len(row) != len(CSV_FIELDS):
                    raise ValueError("В строке дампа должно быть восемь полей.")
                writer.writerow(row)
        if not include_log:
            os.replace(csv_temp, destination)
            return str(destination), None
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
            log_temp = Path(stream.name)
            temporary.append(log_temp)
            for snapshot in snapshots:
                stream.write(f"{snapshot['captured_at']} · {snapshot['cluster_name']} · {snapshot['server']} · ресурсов: {len(snapshot['records'])}\n")
                stream.write("Полный снимок\n" if not snapshot["errors"] else "НЕПОЛНЫЙ СНИМОК\n")
                for message in snapshot["errors"]:
                    stream.write(f"  {message}\n")
        os.replace(csv_temp, destination)
        try:
            os.replace(log_temp, log_path)
        except OSError as exc:
            raise OSError(f"CSV сохранён в {destination}, но журнал записать не удалось: {exc}") from exc
        return str(destination), str(log_path)
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
    incomplete = any(snapshot["errors"] for snapshot in snapshots)
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
            if not records and snapshot["errors"]:
                text, detail = "Нет данных", "\n".join(snapshot["errors"])
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
