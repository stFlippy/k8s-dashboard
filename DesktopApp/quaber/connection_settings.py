"""Feature controller extracted from the desktop shell."""
from __future__ import annotations


class ConnectionSettings:
    def __init__(self, view):
        self.view = view

    def show(self):
        _ui_view = self.view
        from .ui import ClusterDialog, PanelBackground, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget, Qt, ResourceList, TextDialog, confirm, error, item, locate_kubectl, run_dialog
        from .settings_shell import SettingsDialog
        dialog = SettingsDialog(_ui_view)
        cluster_area = QWidget()
        cluster_area.setObjectName('settingsClusters')
        cluster_layout = QVBoxLayout(cluster_area)
        cluster_layout.setContentsMargins(0, 0, 0, 0)
        cluster_layout.setSpacing(8)
        general = QWidget()
        general.setObjectName('settingsGeneral')
        general_form = QFormLayout(general)
        general_form.setContentsMargins(0, 0, 0, 0)
        general_form.setVerticalSpacing(16)
        general_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        from .accounts_ui import accounts_page
        def accounts_changed():
            _ui_view.set_read_only(True)
            populate()
            _ui_view.refresh()
        from .monitoring import idle_minutes
        idle = QComboBox()
        idle.setObjectName('monitoringIdleMinutes')
        for minutes in range(10, 121, 5):
            idle.addItem(f'{minutes} мин', minutes)
        value = idle_minutes(_ui_view.preferences.get('monitoring_idle_minutes'))
        idle.setCurrentIndex(idle.findData(min(120, max(10, ((value + 2) // 5) * 5))))
        idle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        idle.setToolTip('При отсутствии действий в Quaber. Для продолжения нажмите Reconnect.')

        def set_idle(value):
            _ui_view.preferences['monitoring_idle_minutes'] = value
            _ui_view.monitoring.set_idle_minutes(value)
            _ui_view._save_preferences()
        idle.currentIndexChanged.connect(lambda _: set_idle(idle.currentData()))
        if idle.currentData() != value:
            set_idle(idle.currentData())
        general_form.addRow('Автопауза при бездействии', idle)
        from PyQt5.QtWidgets import QSpinBox
        from .cluster_workers import configure_cluster_threads
        parallel = QSpinBox()
        parallel.setObjectName('clusterParallelism')
        parallel.setRange(1,50)
        parallel.setValue(_ui_view.preferences.get('cluster_parallelism',2))
        parallel.setFixedWidth(90)
        parallel.setToolTip('На каждый кластер: столько же сетевых исполнителей и запросов в секунду. Стартовый запас — 2 запроса.')
        def set_parallel(value):
            configure_cluster_threads(_ui_view.api,value)
            _ui_view.api.request_policy.set_cluster_rate(value)
            _ui_view.preferences.update(cluster_parallelism=value,cluster_io_threads=value,cluster_request_rate=value)
            _ui_view._save_preferences()
        parallel.valueChanged.connect(set_parallel)
        general_form.addRow('Потоков и запросов/с на кластер',parallel)
        client_row = QHBoxLayout()
        client = QLineEdit(_ui_view.store.preferences().get('kubectl_path', ''))
        client.setPlaceholderText('Автоматически из PATH')
        client_row.addWidget(client, 1)

        def set_client():
            try:
                if client.text().strip():
                    locate_kubectl(_ui_view.app_dir, client.text())
                _ui_view.preferences['kubectl_path'] = client.text().strip()
                _ui_view._save_preferences()
            except (OSError, ValueError) as exc:
                error(dialog, str(exc))

        def browse_client():
            path, _ = QFileDialog.getOpenFileName(dialog, 'Общий kubectl', str(_ui_view.app_dir), 'Все файлы (*)')
            if path:
                client.setText(path)
                set_client()
        choose = QPushButton('Обзор…')
        choose.clicked.connect(browse_client)
        client.editingFinished.connect(set_client)
        client_row.addWidget(choose)
        general_form.addRow('kubectl path', client_row)
        table = ResourceList()
        table.setColumnCount(6)
        table.setProperty('clusterSettings', True)
        table.setHorizontalHeaderLabels(['Имя', 'API server', 'Учётная запись', 'Мониторинг', 'Пароль сохранён', 'Доступ'])
        table.headerItem().setToolTip(3, 'Управляет опросом и отображением подключения в Quaber.')
        table.enable_text_copy({1: 'хост', 2: 'логин'})
        table.setEditTriggers(table.EditTrigger.NoEditTriggers)
        table.headerItem().setToolTip(0, 'Двойной клик по строке — редактировать подключение. Ctrl+C / ПКМ — копировать хост или логин. Цвет обозначает группу логина.')
        cluster_layout.addWidget(table, 1)
        from PyQt5.QtCore import QTimer
        from .connection_presentation import login_color, ConnectionStatePicker
        from .view_tabs import color_icon
        access_background = PanelBackground(_ui_view.background, dialog)

        def request_access(cluster):
            cid, login_signature = (cluster['id'], cluster.get('encryptedKubeconfig'))
            namespaces = list(getattr(_ui_view, 'snapshot', {}).get(cid, {}).get('namespaces', {}))

            def display(profile):
                current = next((c for c in _ui_view.store.clusters() if c['id'] == cid), None)
                if current is None or not current.get('enabled') or current.get('encryptedKubeconfig') != login_signature:
                    return
                profile = profile if isinstance(profile, dict) else {'level': 'Не определён', 'detail': 'Не получен ответ проверки прав.'}
                for row in range(table.rowCount()):
                    if table.item(row, 0).data(Qt.ItemDataRole.UserRole) == cid:
                        table.item(row, 5).setText(profile.get('level', 'Не определён'))
                        table.item(row, 5).setToolTip(profile.get('detail', ''))
                        break
            access_background.submit(lambda: _ui_view.api.access_summary(cid, namespaces), display, lambda message: display({'level': 'Не определён', 'detail': message}))

        def populate():
            blocked = table.blockSignals(True)
            clusters = sorted(_ui_view.store.clusters(), key=lambda c: (c['username'].casefold(), c['name'].casefold()))
            table.setRowCount(len(clusters))
            for row, cluster in enumerate(clusters):
                for col, value in enumerate((cluster['name'], cluster['server'], cluster.get('accountName') or cluster['username'], 'Включён' if cluster['enabled'] else 'Отключён', 'Да' if cluster.get('encryptedPassword') else 'Нет')):
                    table.setItem(row, col, item(value))
                table.item(row, 0).setData(Qt.ItemDataRole.UserRole, cluster['id'])
                entry = table.topLevelItem(row)
                entry.setIcon(2, color_icon(login_color(cluster.get('accountId') or cluster['username']), 'transparent'))
                entry.setToolTip(2, 'Логин: ' + cluster['username'])
                state = ConnectionStatePicker(table)
                state.setObjectName('connectionEnabled')
                state.addItem('Включён', True)
                state.addItem('Отключён', False)
                state.setCurrentIndex(0 if cluster.get('enabled') else 1)
                state.ensurePolished()
                state.setFixedWidth(state.sizeHint().width())
                state.setToolTip('Отключённое подключение не опрашивается. Ресурсы кластера продолжат работать.')
                state.currentIndexChanged.connect(lambda _, cid=cluster['id'], state=state: toggle_connection(cid, state.currentData()))
                table.setItemWidget(entry, 3, state)
                table.setItem(row, 5, item('Проверка…' if cluster.get('enabled') else 'Отключён'))
                if cluster.get('enabled') and cluster_area.isVisible():
                    request_access(cluster)
            table._fitted_schema = None
            table.resizeColumnsToContents()
            if clusters:
                table.setColumnWidth(3, table.itemWidget(table.topLevelItem(0), 3).width() + 24)
            QTimer.singleShot(0, fit_connections)
            table.clearSelection()
            table.setCurrentItem(None)
            table.blockSignals(blocked)
            return clusters
        def fit_connections():
            from PyQt5 import sip
            if sip.isdeleted(dialog) or not cluster_area.isVisible():
                return
            row_height = max(44, table.sizeHintForRow(0)) if table.rowCount() else 44
            desired = table.header().height() + row_height * max(2, table.rowCount()) + 8
            screen = dialog.screen().availableGeometry()
            overhead = max(250, dialog.sizeHint().height() - table.sizeHint().height())
            table.setMinimumHeight(min(desired, max(100, screen.height() - overhead - 80)))
            dialog.resize(min(max(1100, dialog.width()), screen.width() - 40),
                          min(max(640, desired + overhead), screen.height() - 40))

        def toggle_connection(cid, enabled):
            try:
                _ui_view.store.set_cluster_enabled(cid, enabled)
                if not enabled:
                    _ui_view.background.submit(lambda: _ui_view.proxy.close_disabled(cid), monitor=False)
                _ui_view.refresh()
            except (ValueError, OSError) as exc:
                error(dialog, str(exc))
            finally:
                def refresh_open_dialog():
                    from PyQt5 import sip
                    if not _ui_view._closing and not sip.isdeleted(dialog) and dialog.isVisible():
                        populate()
                QTimer.singleShot(0, refresh_open_dialog)

        clusters = populate()

        actions_strip = QWidget()
        buttons = QHBoxLayout(actions_strip)
        buttons.setContentsMargins(0, 0, 0, 0)

        def selected():
            entry = table.currentItem()
            cid = entry.data(0, Qt.ItemDataRole.UserRole) if entry else None
            return next((c for c in _ui_view.store.clusters() if c['id'] == cid), None)

        def edit(cluster=None):
            nonlocal clusters
            if run_dialog(ClusterDialog(_ui_view, _ui_view.store, _ui_view.app_dir, cluster)) == QDialog.DialogCode.Accepted:
                clusters = populate()
                _ui_view.refresh()
        table.cellDoubleClicked.connect(lambda row, column: edit(selected()) if selected() else None)
        add = QPushButton('Добавить кластер…')
        add.setToolTip('Добавить подключение. Новый кластер Kubernetes не создаётся.')
        add.clicked.connect(lambda: edit())
        buttons.addWidget(add)
        import_config = QPushButton('Добавить из kubeconfig…')

        def choose_config():
            path, _ = QFileDialog.getOpenFileName(dialog, 'Импорт kubeconfig', '', 'Все файлы (*)')
            if not path:
                return
            from .kubeconfig_import import read_contexts

            def choose_contexts(entries):
                selection = QDialog(dialog)
                selection.setWindowTitle('Контексты kubeconfig')
                selection.resize(650, 360)
                box = QVBoxLayout(selection)
                listing = QListWidget()
                box.addWidget(listing)
                for entry in entries:
                    choice = QListWidgetItem(f"{entry['name']} · {entry['server']} · {entry['username']}")
                    choice.setCheckState(Qt.CheckState.Checked)
                    listing.addItem(choice)
                actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                actions.button(QDialogButtonBox.StandardButton.Ok).setText('Добавить')
                actions.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
                actions.accepted.connect(selection.accept)
                actions.rejected.connect(selection.reject)
                box.addWidget(actions)
                if run_dialog(selection) != QDialog.DialogCode.Accepted:
                    return
                chosen = [entry for index, entry in enumerate(entries) if listing.item(index).checkState() == Qt.CheckState.Checked]

                def done(_):
                    nonlocal clusters
                    clusters = populate()
                    _ui_view.refresh()
                access_background.submit(lambda: _ui_view.store.import_contexts(chosen), done, lambda message: error(dialog, message))
            access_background.submit(lambda: read_contexts(path), choose_contexts, lambda message: error(dialog, message))
        import_config.clicked.connect(choose_config)
        buttons.addWidget(import_config)
        change = QPushButton('Настроить / войти…')
        change.setToolTip('Изменить выбранное подключение или восстановить вход в кластер.')
        change.clicked.connect(lambda: edit(selected()) if selected() else None)
        buttons.addWidget(change)
        test = QPushButton('Проверить связь')
        test.setToolTip('Проверить доступ к списку namespace выбранного кластера.')

        def test_selected():
            cluster = selected()
            if cluster:
                _ui_view.background.submit(lambda: len(_ui_view.api.namespaces(cluster['id'])), lambda count: QMessageBox.information(dialog, 'Quaber', f'Подключение работает. Namespaces: {count}'), lambda message: error(dialog, message))
        test.clicked.connect(test_selected)
        buttons.addWidget(test)
        remove = QPushButton('Удалить подключение…')
        remove.setToolTip('Удалить локальную запись и сохранённые данные входа. Сам кластер и его ресурсы останутся.')

        def remove_selected():
            nonlocal clusters
            cluster = selected()
            if cluster and confirm(dialog, 'Удаление подключения', f"Удалить подключение {cluster['name']} и данные входа? Кластер останется."):
                _ui_view.background.submit(lambda: _ui_view.proxy.close(cluster['id']))
                _ui_view.store.delete_cluster(cluster['id'])
                clusters = populate()
                _ui_view.refresh()
        remove.clicked.connect(remove_selected)
        buttons.addWidget(remove)

        def update_selection():
            enabled = selected() is not None and bool(table.selectedItems())
            for action in (change, test, remove):
                action.setEnabled(enabled)
        table.itemSelectionChanged.connect(update_selection)
        update_selection()
        journal = QPushButton('Журнал действий')
        journal.setToolTip('История операций изменения: начало, результат и выбранная цель.')

        def show_journal():

            def display(records):
                content = '\n'.join((f"{record.get('time', '')} · {record.get('phase', '')} · {record.get('action', '')} · {record.get('clusterId', '')} · {record.get('namespace', '')}/{record.get('name', '')} · {record.get('operationId', '')}" for record in reversed(records))) or 'Действий пока нет.'
                run_dialog(TextDialog(_ui_view, 'Журнал действий', content))
            _ui_view.background.submit(_ui_view.store.recent_audit, display, lambda message: error(_ui_view, message))
        journal.clicked.connect(show_journal)
        buttons.addWidget(journal)
        buttons.addStretch()
        cluster_layout.insertWidget(0, _ui_view._scroll_controls(actions_strip))
        show_clusters = QCheckBox('Названия кластеров рядом с namespace')
        show_clusters.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        show_clusters.setChecked(_ui_view.preferences.get('show_cluster_names', True))

        def set_cluster_names(checked):
            _ui_view.preferences['show_cluster_names'] = checked
            _ui_view._save_preferences()
            _ui_view.render_matrix()
        show_clusters.toggled.connect(set_cluster_names)
        general_form.addRow('', show_clusters)
        theme = QComboBox()
        theme.setObjectName('settingsTheme')
        theme.addItem('Dark Modern', 'dark')
        theme.addItem('Light Modern', 'light')
        theme.setCurrentIndex(1 if _ui_view.preferences.get('theme') == 'light' else 0)
        theme.currentIndexChanged.connect(lambda index: _ui_view._select_theme(theme.itemData(index)))
        theme.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        general_form.addRow('Цветовая тема', theme)
        dialog.add_section('Общее', general)
        accounts = accounts_page(dialog, _ui_view.store, accounts_changed)
        dialog.add_section('Учётные записи', accounts)
        dialog.add_section('Подключения', cluster_area)
        def section_changed(row):
            if row != 2: access_background.cancel_reads()
            if row == 1: accounts.reload()
            elif row == 2: populate()
        dialog.sections.currentRowChanged.connect(section_changed)
        def reveal_debug():
            if dialog.sections.count() == 4: return
            debug = QWidget()
            form = QFormLayout(debug)
            form.setContentsMargins(0, 0, 0, 0)
            form.setFormAlignment(Qt.AlignTop)
            transport = QComboBox()
            transport.setObjectName('apiTransport')
            transport.addItem('kubectl proxy', 'proxy')
            transport.addItem('HTTPS', 'https')
            transport.setCurrentIndex(transport.findData(_ui_view.preferences.get('api_transport', 'proxy')))
            transport.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            def set_transport():
                mode = transport.currentData()
                _ui_view.api.set_transport_mode(mode)
                _ui_view.preferences['api_transport'] = mode
                _ui_view._save_preferences()
            transport.currentIndexChanged.connect(set_transport)
            form.addRow('Подключение к API', transport)
            dialog.add_section('Инструменты отладки', debug)
        dialog.reveal_debug = reveal_debug
        if getattr(_ui_view, '_debug_tools_unlocked', False): reveal_debug()
        run_dialog(dialog)
