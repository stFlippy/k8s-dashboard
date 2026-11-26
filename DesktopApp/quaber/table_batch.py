"""Keep table refreshes atomic and restore review position by resource identity."""
from functools import wraps
from PyQt5.QtCore import Qt


def batch_table(resolve, identity_column=None, key_columns=None):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            table = resolve(*args, **kwargs)
            def identity(row):
                if row < 0:
                    return None
                if identity_column is not None:
                    cell = table.item(row, identity_column)
                    return cell.data(Qt.ItemDataRole.UserRole) if cell else None
                columns = key_columns if key_columns is not None else range(min(4, table.columnCount()))
                return tuple(table.item(row, col).text() for col in columns if table.item(row, col))
            selected = identity(table.currentRow())
            scroll = (table.horizontalScrollBar().value(), table.verticalScrollBar().value())
            # updatesEnabled() also becomes false implicitly while a QTabWidget
            # hides this page. Restoring that value explicitly pins the child
            # disabled forever, even after the tab is shown. Preserve only an
            # explicit disable (also keeps nested batches working).
            forced_disabled = table.testAttribute(Qt.WA_ForceUpdatesDisabled)
            outermost = not table._batch_depth
            blocked = table.blockSignals(True)
            table.begin_batch()
            try:
                return function(*args, **kwargs)
            finally:
                if outermost and table._batch_dirty:
                    table.clearSelection()
                    table.setCurrentItem(None)
                    if selected is not None:
                        for row in range(table.rowCount()):
                            if identity(row) == selected and not table.topLevelItem(row).isHidden():
                                table.selectRow(row)
                                break
                    table.horizontalScrollBar().setValue(scroll[0])
                    table.verticalScrollBar().setValue(scroll[1])
                table.end_batch()
                table.blockSignals(blocked)
                if outermost and table._batch_dirty:
                    table.setUpdatesEnabled(not forced_disabled)
        return wrapped
    return decorate
