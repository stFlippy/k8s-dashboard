"""Aligned quota values, with text independent of utilization bars."""
import math
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar, QSizePolicy
from .metrics import memory_bytes


def quota_percent(used, hard):
    amount, limit = memory_bytes(str(used)), memory_bytes(str(hard))
    if amount is None or limit is None or limit <= 0:
        return None
    percent = 100 * amount / limit
    return percent if math.isfinite(percent) else None


class QuotaCard(QFrame):
    def __init__(self, cluster, namespace, quota, parent=None):
        super().__init__(parent)
        self.setObjectName("quotaCard")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        grid = QGridLayout(self)
        grid.setContentsMargins(18, 14, 18, 16)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(12)
        grid.setColumnMinimumWidth(0, 155)
        grid.setColumnMinimumWidth(1, 90)
        grid.setColumnMinimumWidth(2, 90)
        grid.setColumnMinimumWidth(3, 120)
        grid.setColumnMinimumWidth(4, 55)
        grid.setColumnStretch(3, 1)
        title = QLabel(f"quota: {cluster} / {namespace}")
        title.setObjectName("quotaTitle")
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title.setToolTip(f"ResourceQuota: {quota.get('metadata', {}).get('name', '—')}")
        grid.addWidget(title, 0, 0, 1, 5)
        for column, text in enumerate(("Ресурс", "Использовано", "Лимит", "Заполнение", "%")):
            label = QLabel(text)
            label.setObjectName("quotaColumn")
            if column == 4:
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(label, 1, column)
        status = quota.get("status", {})
        used, hard = status.get("used", {}), status.get("hard", {})
        for row, key in enumerate(sorted(set(used) | set(hard)), 2):
            grid.addWidget(QLabel(key), row, 0)
            for column, value in ((1, used.get(key, "0")), (2, hard.get(key, "—"))):
                label = QLabel(str(value))
                label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(label, row, column)
            percent = quota_percent(used.get(key, "0"), hard.get(key))
            progress = QProgressBar()
            progress.setObjectName("quotaProgress")
            progress.setTextVisible(False)
            progress.setFixedHeight(8)
            progress.setRange(0, 1000)
            progress.setValue(min(1000, max(0, round(percent*10))) if percent is not None else 0)
            progress.setProperty("level", "unknown" if percent is None else "critical" if percent >= 90 else "warning" if percent >= 70 else "normal")
            progress.setToolTip("Не удалось вычислить заполнение" if percent is None else f"{percent:.1f}% от лимита")
            grid.addWidget(progress, row, 3)
            label = QLabel("—" if percent is None else f"{percent:.0f}%")
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(label, row, 4)
