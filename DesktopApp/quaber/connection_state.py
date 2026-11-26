"""Reconcile saved connections with runtime snapshots and persistent selections."""
class ConnectionState:
    def __init__(self, window):
        self.window = window

    def reconcile(self):
        view = self.window
        records = view.store.clusters()
        valid = {record['id'] for record in records}
        enabled = {record['id'] for record in records if record.get('enabled')}
        changed = view.views.reconcile_connections(valid)
        removed = set(view.snapshot) - enabled
        if removed:
            view._matrix_generation += 1
            canceled = getattr(view, '_matrix_cancel', None)
            if canceled is not None:
                canceled.set()
            view.loading_matrix = False
            view.matrix_paint_timer.stop()
            view.snapshot = {cid: value for cid, value in view.snapshot.items() if cid in enabled}
            view._scope_index_cache = {key: value for key, value in view._scope_index_cache.items() if key[0] in enabled}
            view._matrix_structure = None
            view._quota_generation = getattr(view, '_quota_generation', 0) + 1
            view.api.cache.invalidate(lambda key: isinstance(key, tuple) and key[0] not in enabled)
        if hasattr(view, 'inventory'):
            view.inventory.prune_connections(enabled)
        if changed:
            view._save_preferences()
        if changed or removed:
            view.render_matrix()
            if hasattr(view, 'events_page'):
                view.events_page.reconcile()
        return records
