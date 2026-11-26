"""Matrix tab persistence and compatibility with the web export format."""

from __future__ import annotations

import copy
import uuid
import re
import math

MAX_VIEWS = 100


def validate_view(value):
    if not isinstance(value, dict):
        raise ValueError("Вкладка должна быть объектом.")
    result = default_view()
    for key in ("id", "name"):
        field = value.get(key, result[key])
        if not isinstance(field, str) or not field.strip() or len(field) > (80 if key == "name" else 128):
            raise ValueError(f"Некорректное поле вкладки {key}.")
        result[key] = field.strip()
    filters = value.get("filters", result["filters"])
    if not isinstance(filters, dict) or any(type(v) is not bool for v in filters.values()):
        raise ValueError("filters должен содержать только логические значения.")
    result["filters"].update({k:v for k,v in filters.items() if k in result["filters"]})
    # Legacy recentRestartFilter is accepted but deliberately no longer controls display.
    if 'recentRestartFilter' in value and type(value['recentRestartFilter']) is not bool:
        raise ValueError('Некорректный флаг recentRestartFilter.')
    for key in ("problemOnly",):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError(f"Некорректный флаг {key}.")
            result[key] = value[key]
    for key in ("selectedDeployments", "selectedNamespaces", "selectedClusterIds", "knownDeployments", "knownNamespaces", "knownClusterIds", "namespaceOrder", "deploymentOrder"):
        field = value.get(key, result[key])
        if field is None and key.startswith(("selected", "known")):
            continue
        if not isinstance(field, list) or len(field) > 5000 or any(not isinstance(v, str) or not v or len(v) > 512 for v in field):
            raise ValueError(f"Некорректный список {key}.")
        result[key] = list(dict.fromkeys(field))
    color = value.get("color", "transparent")
    if not isinstance(color, str) or (color != "transparent" and not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
        raise ValueError("Некорректный цвет вкладки.")
    result["color"] = color
    for key, low, high in (("columnWidths", 36, 1200), ("rowHeights", 30, 2000)):
        sizes = value.get(key, {})
        if not isinstance(sizes, dict) or len(sizes) > 5000 or any(
                not isinstance(k, str) or not k or len(k) > 512 or type(v) is not int or not low <= v <= high
                for k, v in sizes.items()):
            raise ValueError(f"Некорректные размеры {key}.")
        result[key] = dict(sizes)
    return result


def validate_preferences(value):
    result = copy.deepcopy(value)
    result.pop('recent', None)
    from .monitoring import idle_minutes
    result['monitoring_idle_minutes'] = idle_minutes(result.get('monitoring_idle_minutes'))
    threads = result.get('cluster_io_threads', 1)
    result['cluster_io_threads'] = threads if type(threads) is int and 0 <= threads <= 50 else 1
    rate = result.get('cluster_request_rate', 2)
    result['cluster_request_rate'] = rate if type(rate) is int and 1 <= rate <= 100 else 2
    if result.get('api_transport') not in ('proxy', 'https'): result['api_transport'] = 'proxy'
    zones = result.get('controller_timezones',{})
    if not isinstance(zones,dict): zones = {}
    from .schedule import load_zone
    offsets = {}
    for cid, zone in zones.items():
        if isinstance(cid, str) and isinstance(zone, str):
            try:
                load_zone(zone)
                offsets[cid] = zone
            except ValueError:
                pass
    result['controller_timezones'] = offsets
    for label_preference in ('report_label_keys', 'comparison_label_keys'):
        labels = result.get(label_preference, [])
        result[label_preference] = sorted(set(k for k in labels if isinstance(k, str) and 0 < len(k) <= 317))[:512] if isinstance(labels, list) else []
    ranges = {"deployment_width": (100, 500), "header_height": (54, 360), "sidebar_width": (160, 280),
              "log_height": (100, 1000), "refresh_ms": (0, 300000)}
    for key, (low, high) in ranges.items():
        if key in result and (type(result[key]) is not int or not low <= result[key] <= high):
            result.pop(key)
    if result.get("theme") not in (None, "dark", "light"):
        result["theme"] = "dark"
    if not isinstance(result.get("filters", {}), dict) or any(type(v) is not bool for v in result.get("filters", {}).values()):
        result["filters"] = {}
    path = result.get("kubectl_path", "")
    if not isinstance(path, str) or len(path) > 32767 or "\0" in path:
        result["kubectl_path"] = ""
    for key in ("log_visible", "read_only", "show_cluster_names", "sidebar_collapsed"):
        if key in result and type(result[key]) is not bool:
            result.pop(key)
    if "deploy_mode_until" in result:
        deadline = result["deploy_mode_until"]
        if type(deadline) not in (int, float) or not 0 < deadline < 1e12 or not math.isfinite(deadline):
            result.pop("deploy_mode_until")
    if "views" in result:
        try:
            raw = result["views"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_VIEWS:
                raise ValueError("Некорректный набор вкладок.")
            views = [validate_view(v) for v in raw]
            if len({v["id"] for v in views}) != len(views) or len({v["name"].casefold() for v in views}) != len(views):
                raise ValueError("Повторяющиеся вкладки.")
            result["views"] = views
        except ValueError:
            result["views"] = [default_view()]
            result["active_view"] = "default"
            result["preferences_recovered"] = True
    return result


def default_view() -> dict:
    return {"id": "default", "name": "Основная", "filters": {"Running": True, "Pending": True, "Failed": True, "Unknown": True},
            "problemOnly": False,
            "selectedDeployments": None, "selectedNamespaces": None, "selectedClusterIds": None,
            "knownDeployments": None, "knownNamespaces": None, "knownClusterIds": None,
            "namespaceOrder": [], "deploymentOrder": []}


class MatrixViews:
    def reconcile_connections(self, identifiers):
        changed = False
        for view in self.views:
            old = view.get('selectedClusterIds')
            known = view.get('knownClusterIds')
            if old is not None:
                value = [identifier for identifier in old if identifier in identifiers]
                # Extend an explicit former "all" selection after replacement,
                # but never broaden a deliberately narrow connection scope.
                if known and set(known).issubset(old):
                    value += sorted(set(identifiers) - set(known) - set(value))
                if old != value:
                    view['selectedClusterIds'] = value
                    changed = True
            if known != sorted(identifiers):
                view['knownClusterIds'] = sorted(identifiers)
                changed = True
            order = view.get('namespaceOrder', [])
            filtered = [key for key in order if key.split('/', 1)[0] in identifiers]
            if order != filtered:
                view['namespaceOrder'] = filtered
                changed = True
        if changed:
            self.persist()
        return changed

    def __init__(self, preferences: dict):
        self.preferences = preferences
        raw = validate_preferences(preferences).get("views")
        self.views = raw if isinstance(raw, list) and raw else [default_view()]
        self.active_id = preferences.get("active_view", self.views[0]["id"])
        if not any(view["id"] == self.active_id for view in self.views):
            self.active_id = self.views[0]["id"]

    @property
    def active(self) -> dict:
        return next(view for view in self.views if view["id"] == self.active_id)

    def persist(self) -> None:
        self.preferences["views"] = self.views
        self.preferences["active_view"] = self.active_id

    def observe_scopes(self, scopes):
        """Track available values without broadening explicit user selections."""
        changed = False
        for view in self.views:
            for suffix, values in scopes.items():
                known_key, selected_key = "known"+suffix, "selected"+suffix
                known = view.get(known_key)
                updated = sorted(set(values))
                if known != updated:
                    view[known_key] = updated
                    changed = True
        if changed:
            self.persist()
        return changed

    def add(self, name: str) -> dict:
        if len(self.views) >= MAX_VIEWS:
            raise ValueError("Допускается не более 100 вкладок.")
        name = name.strip()
        if not name or len(name) > 80 or any(view["name"].casefold() == name.casefold() for view in self.views):
            raise ValueError("Название вкладки пустое, слишком длинное или уже используется.")
        view = default_view()
        view.update(id=str(uuid.uuid4()), name=name, selectedClusterIds=[],
                    knownClusterIds=list(self.active.get('knownClusterIds') or []))
        self.views.append(view)
        self.active_id = view["id"]
        self.persist()
        return view

    def rename(self, view_id: str, name: str) -> None:
        name = name.strip()
        if not name or len(name) > 80 or any(view["id"] != view_id and view["name"].casefold() == name.casefold() for view in self.views):
            raise ValueError("Название вкладки пустое, слишком длинное или уже используется.")
        next(view for view in self.views if view["id"] == view_id)["name"] = name
        self.persist()

    def remove(self, view_id: str) -> None:
        if len(self.views) <= 1:
            raise ValueError("Должна остаться хотя бы одна вкладка.")
        self.views = [view for view in self.views if view["id"] != view_id]
        self.active_id = self.views[0]["id"]
        self.persist()

    def export(self, clusters: list[dict]) -> dict:
        required_ids = {cluster_id for view in self.views for cluster_id in
                        (view.get("selectedClusterIds") if view.get("selectedClusterIds") is not None else [cluster["id"] for cluster in clusters])}
        return {"format": "quaber-matrix-views", "version": 1,
                "requiredClusters": [{"id": cluster["id"], "name": cluster["name"]} for cluster in clusters if cluster["id"] in required_ids],
                "views": copy.deepcopy(self.views)}

    def import_data(self, payload: dict, clusters: list[dict]) -> int:
        if not isinstance(payload, dict) or payload.get("format") != "quaber-matrix-views" or payload.get("version") != 1:
            raise ValueError("Неверный формат файла вкладок.")
        incoming = payload.get("views")
        if not isinstance(incoming, list) or not incoming or len(incoming)+len(self.views) > MAX_VIEWS:
            raise ValueError("В файле нет корректных вкладок.")
        incoming = [validate_view(view) for view in incoming]
        available = {cluster["id"] for cluster in clusters}
        required = payload.get("requiredClusters")
        if required is None:
            required = [{"id": cluster_id, "name": cluster_id} for view in incoming for cluster_id in
                        [*(view.get("selectedClusterIds") or []), *(key.split("/")[0] for key in view.get("namespaceOrder", []))]]
        if not isinstance(required, list) or any(not isinstance(cluster, dict) for cluster in required):
            raise ValueError("Некорректный список кластеров в файле.")
        missing = [cluster.get("name", cluster.get("id", "?")) for cluster in required if cluster.get("id") not in available]
        if missing:
            raise ValueError("Импорт невозможен. Нет кластеров: " + ", ".join(missing))
        names = [view.get("name", "").strip() for view in incoming]
        existing = {view["name"].casefold() for view in self.views}
        if any(not name or len(name) > 80 for name in names) or len({name.casefold() for name in names}) != len(names) or any(name.casefold() in existing for name in names):
            raise ValueError("В импортируемом файле есть пустые или повторяющиеся имена вкладок.")
        imported = []
        for view, name in zip(incoming, names):
            candidate = copy.deepcopy(default_view())
            candidate.update(view)
            candidate["id"] = str(uuid.uuid4())
            candidate["name"] = name
            candidate["namespaceOrder"] = [key for key in view.get("namespaceOrder", []) if key.split("/")[0] in available]
            candidate["deploymentOrder"] = view.get("deploymentOrder", [])
            imported.append(candidate)
        self.views.extend(imported)
        self.active_id = imported[0]["id"]
        self.persist()
        return len(imported)
