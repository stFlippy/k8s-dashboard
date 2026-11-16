"""Qt mapping of VS Code Dark Modern / Light Modern color tokens.

Sources (Microsoft, MIT):
https://github.com/microsoft/vscode/tree/main/extensions/theme-defaults/themes
https://github.com/microsoft/vscode/blob/main/src/vs/workbench/browser/media/style.css
"""
import sys
from pathlib import Path
from PyQt6.QtCore import QTranslator
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette


class ButtonTranslations(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source, disambiguation=None, n=-1):
        if context in ("QPlatformTheme", "QDialogButtonBox", "QMessageBox"):
            return {"Yes": "Да", "Cancel": "Отмена", "No": "Нет", "OK": "ОК", "Save": "Сохранить",
                    "Discard": "Отбросить", "Close": "Закрыть", "Apply": "Применить"}.get(source.replace("&", ""))
        return None  # Qt requires a null translation to continue looking up unknown text.


def install_button_translations(app):
    if not hasattr(app, "quaber_button_translations"):
        app.quaber_button_translations = ButtonTranslations(app)
        app.installTranslator(app.quaber_button_translations)

DARK = dict(name="Dark Modern", editor="#151B24", sidebar="#10161F", panel="#10161F",
            foreground="#D8E1ED", muted="#97A6BA", border="#2A3545", input="#202B3A",
            input_border="#3C4B60", accent="#478FE8", button_hover="#026EC1",
            active_text="#FFFFFF", inactive_text="#97A6BA", selection="#264F78",
            line_number="#6E7681", error="#F85149", success="#2EA043")
LIGHT = dict(name="Light Modern", editor="#FFFFFF", sidebar="#F3F6FA", panel="#F3F6FA",
             foreground="#3B3B3B", muted="#616161", border="#DFE5EE", input="#FFFFFF",
             input_border="#CECECE", accent="#005FB8", button_hover="#0258A8",
             active_text="#3B3B3B", inactive_text="#616161", selection="#ADD6FF",
             line_number="#6E7681", error="#F85149", success="#369432")
_bundled_fonts_loaded = False


def theme(light=False):
    return LIGHT if light else DARK


def write_background(light=False):
    return "#760F29" if light else "#B51F35"


def load_fonts():
    global _bundled_fonts_loaded
    if not QFontDatabase.families() and sys.platform == "win32":
        for name in ("segoeui.ttf", "seguisym.ttf", "seguiemj.ttf", "consola.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    if not _bundled_fonts_loaded:
        path = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Roboto.ttf"
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
        _bundled_fonts_loaded = True


def ui_family():
    load_fonts()
    families = QFontDatabase.families()
    if sys.platform == "win32":
        return "Segoe WPC" if "Segoe WPC" in families else "Segoe UI"
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def code_font():
    load_fonts()
    font = QFont("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono")
    font.setPixelSize(14)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def apply_palette(widget, light=False):
    colors = theme(light)
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "editor"), (QPalette.ColorRole.Base, "editor"),
                      (QPalette.ColorRole.WindowText, "foreground"), (QPalette.ColorRole.Text, "foreground"),
                      (QPalette.ColorRole.Button, "sidebar"), (QPalette.ColorRole.ButtonText, "foreground"),
                      (QPalette.ColorRole.Mid, "border"), (QPalette.ColorRole.Dark, "input_border"),
                      (QPalette.ColorRole.AlternateBase, "panel"), (QPalette.ColorRole.Highlight, "selection"),
                      (QPalette.ColorRole.HighlightedText, "active_text"), (QPalette.ColorRole.ToolTipBase, "panel"),
                      (QPalette.ColorRole.ToolTipText, "foreground")):
        palette.setColor(role, QColor(colors[key]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(colors["muted"]))
    widget.setPalette(palette)
    font = widget.font()
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    widget.setFont(font)


def stylesheet(light=False):
    from .widgets import COLORS
    c = theme(light)
    write = write_background(light)
    write_hover = "#921A39" if light else "#C62A41"
    asset_dir = (Path(__file__).resolve().parent.parent / "assets").as_posix()
    monospace = code_font().family()
    brand_family = "Roboto" if "Roboto" in QFontDatabase.families() else ui_family()
    return f"""
    QWidget {{ background:{c['editor']}; color:{c['foreground']}; font-family:'{ui_family()}'; font-size:13px; }}
    QLabel {{ background:transparent; }}
    QMainWindow, QDialog {{ background:{c['editor']}; }}
    QToolBar {{ background:{c['sidebar']}; border:0; border-bottom:1px solid {c['border']}; padding:6px; spacing:8px; }}
    QWidget#sidebar {{ background:{c['sidebar']}; border-right:1px solid {c['border']}; }}
    QWidget#sidebarFooter {{ background:{c['sidebar']}; border:0; }}
    QPushButton#adminMode, QPushButton#helpButton {{ background:transparent; border:0; border-left:2px solid transparent; border-radius:0; text-align:left; padding:0 16px; }}
    QPushButton#adminMode:hover {{ background:{c['input']}; }}
    QPushButton#helpButton:hover {{ background:{c['input']}; }}
    QPushButton#sidebarToggle {{ background:transparent; border:0; padding:0; }}
    QPushButton#sidebarToggle:hover {{ background:{c['input']}; }}
    QWidget#sidebar[collapsed="true"] QPushButton#sideNav, QWidget#sidebar[collapsed="true"] QPushButton#helpButton,
    QWidget#sidebar[collapsed="true"] QPushButton#adminMode {{ text-align:center; padding:0 15px; }}
    QWidget#sidebar QLabel {{ background:transparent; }}
    QWidget#brandArea {{ background:transparent; }}
    QLabel#brand {{ background:transparent; padding:0; }}
    QLabel#brandName {{ background:transparent; font-family:'{brand_family}'; font-size:27px; font-weight:700; }}
    QLabel#sidebarTitle {{ color:{c['muted']}; font-size:11px; padding:8px 12px; }}
    QPushButton {{ background:{c['sidebar']}; border:1px solid {c['input_border']}; border-radius:5px; padding:4px 10px; }}
    QPushButton:hover {{ background:{c['input']}; }}
    QPushButton:disabled {{ color:{'#898F98' if light else '#8490A1'}; background:{'#E4E7EC' if light else '#282E38'}; border-color:{'#D4D8DF' if light else '#39414E'}; }}
    QPushButton#sideNav {{ border:0; border-left:2px solid transparent; border-radius:0; text-align:left; padding:0 16px; background:transparent; }}
    QPushButton#sideNav:hover {{ background:{c['input']}; }}
    QPushButton#sideNav:checked {{ border-left:2px solid {c['accent']}; background:{c['editor']}; color:{c['active_text']}; }}
    QWidget#sidebar[writeEnabled="true"], QWidget#sidebar[writeEnabled="true"] QWidget#sidebarFooter {{ background:{write}; }}
    QWidget#sidebar[writeEnabled="true"] QLabel, QWidget#sidebar[writeEnabled="true"] QCheckBox {{ color:#FFFFFF; background:transparent; }}
    QWidget#sidebar[writeEnabled="true"] QPushButton {{ color:#FFFFFF; background:{write}; border-color:#E38896; }}
    QWidget#sidebar[writeEnabled="true"] QPushButton#sideNav {{ border:0; border-left:2px solid transparent; background:transparent; }}
    QWidget#sidebar[writeEnabled="true"] QPushButton#sideNav:hover {{ background:{write_hover}; }}
    QWidget#sidebar[writeEnabled="true"] QPushButton#adminMode:hover, QWidget#sidebar[writeEnabled="true"] QPushButton#helpButton:hover {{ background:{write_hover}; }}
    QWidget#sidebar[writeEnabled="true"] QPushButton#sideNav:checked {{ background:#891629; color:#FFFFFF; border-left-color:#FFFFFF; }}
    QLineEdit,QTextEdit,QPlainTextEdit,QComboBox,QSpinBox {{ background:{c['input']}; border:1px solid {c['input_border']}; border-radius:5px; padding:4px 8px; selection-background-color:{c['selection']}; }}
    QLineEdit:focus,QPlainTextEdit:focus,QComboBox:focus {{ border-color:{c['accent']}; }}
    QLineEdit#copyField {{ color:{c['foreground']}; padding:0 2px; border-radius:2px; selection-background-color:{c['accent'] if light else c['selection']}; selection-color:#FFFFFF; }}
    QPlainTextEdit#codeEditorText,QPlainTextEdit#codeLineNumbers,QPlainTextEdit#podLogs,QPlainTextEdit#podConsole,QPlainTextEdit#applicationLog {{ font-family:'{monospace}'; font-size:14px; }}
    QPlainTextEdit#podLogs {{ background:{c['panel']}; border-color:{c['border']}; padding:8px; }}
    QPlainTextEdit#codeEditorText {{ background:{c['editor']}; }}
    QPlainTextEdit#codeLineNumbers {{ background:{c['editor']}; color:{c['line_number']}; border:0; }}
    QPlainTextEdit#applicationLog {{ background:{c['panel']}; border:0; padding:8px; }}
    QTableView,QTableWidget {{ background:{c['editor']}; border:0; gridline-color:{c['border']}; }}
    QHeaderView::section {{ background:{c['sidebar']}; color:{c['foreground']}; border:1px solid {c['border']}; padding:6px; font-weight:700; }}
    QTableView::item:selected {{ background:{c['selection']}; color:{c['foreground']}; }}
    QTableView#frozenDeployment {{ background:{c['sidebar']}; border-right:1px solid {c['input_border']}; font-weight:600; }}
    QTreeWidget#resourceList {{ background:{c['editor']}; alternate-background-color:{c['sidebar']}; border:0; outline:0; }}
    QTreeWidget#resourceList::item {{ padding:9px 12px; border:0; border-bottom:1px solid {c['border']}; min-height:22px; }}
    QTreeWidget#resourceList::item:hover {{ background:{'#D9E8FA' if light else '#263C56'}; }}
    QTreeWidget#resourceList::item:selected, QTreeWidget#resourceList::item:selected:hover,
    QTreeWidget#resourceList::item:selected:!active {{ background:{c['accent'] if light else c['selection']}; color:#FFFFFF; border-bottom-color:{c['accent']}; }}
    QTreeWidget#resourceList QHeaderView::section {{ text-align:left; padding:10px 12px; border:0; border-bottom:1px solid {c['border']}; color:{c['muted']}; font-weight:400; }}
    QFrame#quotaCard {{ background:{c['sidebar']}; border:1px solid {c['border']}; border-radius:5px; }}
    QFrame#quotaCard QLabel {{ background:transparent; border:0; }}
    QLabel#sectionTitle {{ font-size:17px; font-weight:700; color:{c['foreground']}; padding:3px 0 6px; }}
    QFrame#quotaCard QLabel#quotaTitle {{ font-size:16px; font-weight:700; color:{c['foreground']}; }}
    QLabel#quotaContext,QLabel#quotaColumn {{ color:{c['muted']}; }}
    QProgressBar#quotaProgress {{ background:{c['input_border']}; border:0; border-radius:4px; padding:0; }}
    QProgressBar#quotaProgress::chunk {{ background:{c['accent']}; border-radius:4px; }}
    QProgressBar#quotaProgress[level="warning"]::chunk {{ background:{'#9A6700' if light else '#D7BA7D'}; }}
    QProgressBar#quotaProgress[level="critical"]::chunk {{ background:{'#C72E3B' if light else '#F85149'}; }}
    QTabWidget::pane {{ border:0; }}
    QTabBar {{ background:{c['sidebar']}; }}
    QTabBar::tab {{ background:{c['sidebar']}; color:{c['inactive_text']}; border:0; border-right:1px solid {c['border']}; border-top:2px solid transparent; padding:7px 14px; }}
    QTabBar::tab:selected {{ background:{c['editor']}; color:{c['active_text']}; border-top:2px solid {c['accent']}; }}
    QTabBar#matrixViewTabs::tab {{ padding:4px 12px; border-radius:0; }}
    QWidget#viewStrip {{ background:{c['sidebar']}; border-bottom:1px solid {c['border']}; }}
    QPushButton#viewAction, QPushButton#addView {{ background:transparent; border:0; color:{c['muted']}; padding:2px 8px; }}
    QPushButton#addView {{ font-size:19px; }}
    QWidget#statusStrip,QWidget#filterStrip {{ background:{c['editor']}; border-bottom:1px solid {c['border']}; }}
    QCheckBox {{ background:transparent; spacing:5px; padding:4px; }}
    QCheckBox::indicator {{ width:14px; height:14px; border:1px solid {c['input_border']}; border-radius:3px; background:{c['input']}; }}
    QCheckBox::indicator:checked {{ image:url('{asset_dir}/check.svg'); background:{c['accent']}; border-color:{c['accent']}; }}
    QWidget#statusStrip QCheckBox::indicator:hover {{ border-color:{c['accent']}; }}
    QWidget#statusStrip QCheckBox:hover {{ color:{c['active_text']}; background:{c['input']}; }}
    QComboBox:hover, QLineEdit:hover, QSpinBox:hover {{ border-color:{c['muted']}; }}
    QComboBox::down-arrow {{ image:url('{asset_dir}/down.svg'); width:14px; height:14px; }}
    QWidget#statusStrip QCheckBox {{ border:1px solid {c['border']}; border-radius:4px; padding:3px 7px 3px 14px; font-size:12px; }}
    QCheckBox#statusFilter[status="Running"] {{ border-left:1px solid {c['border']}; }}
    QCheckBox#statusFilter[status="Pending"] {{ border-left:1px solid {c['border']}; }}
    QCheckBox#statusFilter[status="Failed"] {{ border-left:1px solid {c['border']}; }}
    QCheckBox#statusFilter[status="Unknown"] {{ border-left:1px solid {c['border']}; }}
    QCheckBox#recentFilter {{ border-left:1px solid {c['border']}; }}
    QLabel#lastUpdated {{ color:{c['muted']}; font-size:12px; }}
    QLabel#totalCounter {{ font-weight:600; padding:6px; }}
    QWidget#resourceHeader,QWidget#resourceContext {{ background:{c['sidebar']}; border-bottom:1px solid {c['border']}; }}
    QWidget#resourceContext QLabel,QWidget#resourceHeader QLabel {{ background:transparent; }}
    QWidget#resourceContext QWidget#resourceContextField {{ background:transparent; }}
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceHeader,
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceContext,
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceNavigation,
    QWidget#resourcePanel[writeEnabled="true"] QLabel#resourceRefresh,
    QWidget#resourcePanel[writeEnabled="true"] QLabel#deploymentNotice {{ background:{write}; color:#FFFFFF; }}
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceHeader QLabel,
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceContext QLabel,
    QWidget#resourcePanel[writeEnabled="true"] QWidget#resourceNavigation QLabel {{ color:#FFFFFF; }}
    QTabBar[writeEnabled="true"], QTabBar[writeEnabled="true"]::tab {{ background:{write}; color:#FFFFFF; }}
    QTabWidget#resourceTabs[writeEnabled="true"] {{ background:{write}; }}
    QTabBar[writeEnabled="true"]::tab:selected {{ background:{write_hover}; color:#FFFFFF; border-top-color:#FFFFFF; }}
    QWidget#resourcePanel {{ border-right:1px solid {c['border']}; }}
    QLabel#resourceTitle {{ font-size:24px; font-weight:700; }}
    QTextBrowser#resourceDetails {{ background:transparent; border:0; padding:0; }}
    QLabel#replicaDraftNotice {{ color:{c['accent']}; font-weight:600; }}
    QFrame#editorFind {{ background:{c['panel']}; border:1px solid {c['input_border']}; border-radius:4px; }}
    QFrame#editorFind QLabel {{ border:0; }}
    QWidget#yamlPage QPushButton, QWidget#podLogView QPushButton {{ padding:2px 8px; min-height:16px; }}
    QWidget#podLogView QLineEdit, QWidget#podLogView QComboBox {{ padding:2px 6px; min-height:18px; }}
    QWidget#podLogView QCheckBox {{ padding:1px 2px; spacing:4px; }}
    QWidget#podLogView QPlainTextEdit#podLogs {{ padding:5px; }}
    QLabel#yamlDraftState {{ color:{c['muted']}; }}
    QLabel#contextValue {{ font-weight:600; padding-top:4px; }}
    QDockWidget {{ border-top:1px solid {c['border']}; }}
    QWidget#logTitlebar {{ background:{c['panel']}; border-top:1px solid {c['border']}; border-bottom:1px solid {c['border']}; }}
    QWidget#logTitlebar QPushButton {{ background:transparent; border:0; padding:3px 8px; }}
    QWidget#logTitlebar QPushButton:checked {{ color:{c['accent']}; }}
    QStatusBar {{ background:{c['sidebar']}; border-top:1px solid {c['border']}; }}
    QStatusBar QLabel, QStatusBar QToolButton {{ background:transparent; border:0; }}
    QSplitter::handle {{ background:{c['border']}; }}
    QSplitter::handle:hover {{ background:{c['accent']}; }}
    QMenu {{ border-radius:5px; background:{c['editor']}; border:1px solid {c['input_border']}; padding:5px; }}
    QMenu::item {{ padding:6px 20px; border-radius:3px; }}
    QMenu::item:selected {{ background:{c['accent']}; color:white; }}
    QMenu::indicator {{ width:14px; height:14px; border:1px solid {c['input_border']}; border-radius:3px; background:{c['input']}; }}
    QMenu::indicator:checked {{ image:url('{asset_dir}/check.svg'); background:{c['accent']}; border-color:{c['accent']}; }}
    QMenu#multiSelectMenu::item {{ padding-left:28px; }}
    QMenu#multiSelectMenu::indicator {{ margin-left:8px; }}
    QLabel#emptyState {{ color:{c['muted']}; }}
    QPushButton:focus, QToolButton:focus {{ border-color:{c['accent']}; }}
    QCheckBox:focus {{ outline:1px solid {c['accent']}; }}
    QPushButton#viewAction:hover, QPushButton#addView:hover {{ background:{c['input']}; color:{c['foreground']}; }}
    QPushButton[primary="true"], QDialogButtonBox QPushButton:default {{ background:{c['accent']}; color:white; border-color:{c['accent']}; }}
    QPushButton[primary="true"]:hover, QDialogButtonBox QPushButton:default:hover {{ background:{c['button_hover']}; }}
    QPushButton[primary="true"]:disabled, QDialogButtonBox QPushButton:default:disabled {{ color:{'#898F98' if light else '#8490A1'}; background:{'#E4E7EC' if light else '#282E38'}; border-color:{'#D4D8DF' if light else '#39414E'}; }}
    QToolButton {{ background:transparent; border:1px solid transparent; border-radius:4px; padding:5px 8px; }}
    QToolButton:hover {{ background:{c['input']}; border-color:{c['input_border']}; }}
    QToolTip {{ background:{c['panel']}; color:{c['foreground']}; border:1px solid {c['input_border']}; padding:6px; }}
    QMenu::separator {{ height:1px; background:{c['border']}; margin:5px 8px; }}
    QMenu::item:disabled {{ color:{c['muted']}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width:0; height:0; border:0; }}
    QSpinBox[replicaCount="true"]::up-button, QSpinBox[replicaCount="true"]::down-button {{ width:16px; height:12px; }}
    QComboBox {{ combobox-popup:0; }}
    QComboBox {{ padding-right:24px; }}
    QComboBox::drop-down {{ subcontrol-origin:padding; subcontrol-position:top right; width:22px; border:0; }}
    QComboBox QAbstractItemView {{ background:{c['input']}; color:{c['foreground']}; border:1px solid {c['input_border']}; padding:0; outline:0; selection-background-color:{c['selection']}; selection-color:{c['foreground']}; }}
    QComboBox QAbstractItemView::item {{ min-height:28px; padding:0 10px; border:0; margin:0; }}
    QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{ background:{c['selection']}; color:{c['foreground']}; }}
    QComboBox QAbstractItemView::item:disabled {{ color:{c['muted']}; background:transparent; }}
    QListWidget {{ border:1px solid {c['border']}; outline:0; }}
    QListWidget::item {{ padding:6px 10px; border:1px solid transparent; }}
    QListWidget::item:hover {{ background:{c['input']}; }}
    QListWidget::item:selected {{ background:{c['selection']}; color:{c['foreground']}; }}
    QTabBar::tab:hover {{ color:{c['active_text']}; background:{c['input']}; }}
    QWidget#refreshStrip {{ background:{c['sidebar']}; border-top:1px solid {c['border']}; }}
    QWidget#refreshStrip QLabel, QWidget#refreshStrip QPushButton, QWidget#refreshStrip QComboBox {{ font-size:11px; }}
    QWidget#refreshStrip QPushButton, QWidget#refreshStrip QComboBox {{ padding:3px 7px; border-radius:4px; }}
    QTabBar#matrixViewTabs::tab {{ padding:4px 10px; min-height:20px; border-top-left-radius:4px; border-top-right-radius:4px; }}
    QLabel#pageTitle {{ font-size:18px; font-weight:600; }}
    QScrollBar:vertical {{ width:10px; background:transparent; }}
    QScrollBar:horizontal {{ height:10px; background:transparent; }}
    QScrollBar::handle {{ border-radius:4px; background:{c['input_border']}; min-height:24px; min-width:24px; }}
    QScrollBar::handle:hover {{ background:{c['muted']}; }}
    QScrollBar::add-line,QScrollBar::sub-line {{ width:0; height:0; }}
    """
