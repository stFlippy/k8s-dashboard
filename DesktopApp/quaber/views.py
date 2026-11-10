"""Matrix tab persistence and compatibility with the web export format."""

from __future__ import annotations

import copy
import uuid


def default_view() -> dict:
    return {"id": "default", "name": "Основная", "filters": {"Running": True, "Pending": True, "Failed": True, "Unknown": True},
            "recentRestartFilter": True, "problemOnly": False, "hideFilteredNamespaces": True,
            "selectedDeployments": None, "selectedNamespaces": None, "selectedClusterIds": None,
            "namespaceOrder": [], "deploymentOrder": []}


class MatrixViews:
    def __init__(self, preferences: dict):
        self.preferences = preferences
        raw = preferences.get("views")
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

    def add(self, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 80 or any(view["name"].casefold() == name.casefold() for view in self.views):
            raise ValueError("Название вкладки пустое, слишком длинное или уже используется.")
        view = copy.deepcopy(self.active)
        view.update(id=str(uuid.uuid4()), name=name)
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
        if not isinstance(incoming, list) or not incoming or not all(isinstance(view, dict) for view in incoming):
            raise ValueError("В файле нет корректных вкладок.")
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
