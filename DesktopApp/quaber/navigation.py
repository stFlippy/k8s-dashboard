"""Small vector navigation icons rendered at the display's pixel density."""
from functools import lru_cache
from PyQt5.QtCore import QByteArray, QRectF
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtCore import Qt

PATHS = {
    "events": '<path d="M4 3h16v18H4zM8 7h9M8 12h9M8 17h9M6 7h.01M6 12h.01M6 17h.01"/>',
    "workloads": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
    "quota": '<path d="M3 21h18M5 17V9h3v8m4 0V3h3v14m4 0v-6h3v6"/>',
    "config": '<path d="M6 2h9l4 4v16H6zM14 2v6h5M9 12h7M9 16h7"/>',
    "pods": '<path d="M12 2 3 7v10l9 5 9-5V7zM3 7l9 5 9-5M12 12v10M7 4.5l9 5"/>',
    "logs": '<path d="m3 5 4 4-4 4M11 7h10M11 12h10M3 18h18"/>',
    "dumps": '<path d="M5 3h14v18H5zM8 7h8M8 10h8M12 13v5m-3-3 3 3 3-3"/>',
    "versions": '<path d="M4 4v16M20 4v16M4 8h10m-3-3 3 3-3 3M20 16H10m3-3-3 3 3 3"/>',
    "settings": '<path fill="{color}" stroke="none" fill-rule="evenodd" d="m19.4 13 .1-1-.1-1 2-1.6-2-3.5-2.5 1a8 8 0 0 0-1.7-1L14.8 3h-4l-.4 2.9a8 8 0 0 0-1.7 1l-2.5-1-2 3.5 2 1.6-.1 1 .1 1-2 1.6 2 3.5 2.5-1a8 8 0 0 0 1.7 1l.4 2.9h4l.4-2.9a8 8 0 0 0 1.7-1l2.5 1 2-3.5ZM12.8 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/>',
    "help": '<circle fill="{color}" stroke="none" cx="12" cy="12" r="10"/><path stroke="{background}" stroke-width="2.4" d="M9 8.5a3 3 0 0 1 6 0c0 2-3 2.3-3 5M12 17h.01"/>',
}


@lru_cache(maxsize=40)
def navigation_icon(name, color, background="#FFFFFF"):
    paths = PATHS[name].replace("{color}", color).replace("{background}", background)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{paths}</g></svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, 24, 24))
    painter.end()
    return QIcon(pixmap)
