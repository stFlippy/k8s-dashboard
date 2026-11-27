"""Account vault settings; clusters bind to a record, never duplicate its secret."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)
from .secret_dialog import SecretDialog
from .resource_list import ResourceList
from .widgets import AppComboBox


class ClusterDialog(SecretDialog):
    def __init__(self, parent, store, app_dir, cluster=None):
        from .connection_presentation import TimeZonePicker
        super().__init__(parent)
        self.store, self.app_dir, self.cluster = store, app_dir, cluster
        self.busy = False
        self.setWindowTitle('Кластер — Quaber')
        self.resize(620, 370)
        box = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit((cluster or {}).get('name', ''))
        self.server = QLineEdit((cluster or {}).get('server', ''))
        self.account = AppComboBox()
        self.account.setObjectName('clusterAccount')
        self.reload_accounts((cluster or {}).get('accountId'))
        account_row = QHBoxLayout()
        account_row.addWidget(self.account, 1)
        manage = QPushButton('Учётные записи…')
        def accounts():
            selected = self.account.currentData()
            show_accounts(self, store)
            self.reload_accounts(selected)
        manage.clicked.connect(accounts)
        account_row.addWidget(manage)
        self.controller_timezone = TimeZonePicker((cluster or {}).get('controllerTimeZone', ''))
        self.kubectl_path = QLineEdit(store.preferences().get('kubectl_path', ''))
        self.kubectl_path.setPlaceholderText('Автоматически из PATH')
        for title, field in (('Имя', self.name), ('API server', self.server), ('Учётная запись', account_row)):
            form.addRow(title, field)
        gap = QLabel()
        gap.setFixedHeight(14)
        form.addRow(gap)
        form.addRow('Часовой пояс', self.controller_timezone)
        self.form_widget = QWidget(self)
        self.form_widget.setLayout(form)
        box.addWidget(self.form_widget)
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.test_button = self.buttons.addButton('Проверить', QDialogButtonBox.ActionRole)
        self.test_button.clicked.connect(lambda: self.connect_cluster(False))
        self.buttons.accepted.connect(lambda: self.connect_cluster(True))
        self.buttons.rejected.connect(self.reject)
        box.addWidget(self.buttons)

    def _clear_secrets(self):
        pass  # Credentials belong only to the account vault.

    def reload_accounts(self, selected=None):
        self.account.clear()
        if (self.cluster or {}).get('authMode') == 'kubeconfig':
            self.account.addItem('Импортированный kubeconfig', None)
        for account in self.store.accounts():
            self.account.addItem(f"{account['name']} · {account['username']}", account['id'])
        if selected:
            self.account.setCurrentIndex(self.account.findData(selected))

    def choose_kubectl(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, 'Выберите kubectl', str(self.app_dir), 'Все файлы (*)')
        if path: self.kubectl_path.setText(path)
        return bool(path)

    def connect_cluster(self, save):
        if self.busy: return
        from .ui import locate_kubectl, login, test_connection
        from .storage import validate_server
        from .auth_queue import serialized_login, clear_rejection
        existing = self.cluster
        aid = self.account.currentData()
        account = next((a for a in self.store.accounts() if a['id'] == aid), None)
        try:
            if account is None and (existing or {}).get('authMode') != 'kubeconfig':
                raise ValueError('Создайте и выберите учётную запись.')
            server = validate_server(self.server.text().strip())
            name = self.name.text().strip()
            if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
                raise ValueError('Укажите имя кластера от 1 до 80 символов.')
            zone = self.store._validate_controller_timezone(self.controller_timezone.text())
            password = self.store.account_password(aid) if aid else ''
            try:
                executable = locate_kubectl(self.app_dir, self.kubectl_path.text())
            except FileNotFoundError:
                if not self.choose_kubectl(): return
                executable = locate_kubectl(self.app_dir, self.kubectl_path.text())
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        preferences = self.store.preferences()
        preferences['kubectl_path'] = str(executable) if self.kubectl_path.text().strip() else ''
        self.store.save_preferences(preferences)
        if hasattr(self.parent(), 'preferences'):
            self.parent().preferences['kubectl_path'] = preferences['kubectl_path']
        username = account['username'] if account else existing['username']
        subject = dict(accountId=aid, username=username, server=server)
        reusable = bool(existing and existing.get('encryptedKubeconfig') and existing['server'] == server
                        and existing.get('accountId') == aid and not self.store.login_required(existing))
        clear_rejection(self.store, subject)
        self.busy = True
        self.buttons.setEnabled(False)
        self.form_widget.setEnabled(False)
        self.status.setText('Проверка подключения…')
        def perform_work():
            config = None
            try:
                if not reusable:
                    if not password: raise ValueError('Введите пароль в настройках учётной записи.')
                    config = serialized_login(self.store, subject, password,
                                              lambda: login(executable, server, username, password))
                if account != next((a for a in self.store.accounts() if a['id'] == aid), None):
                    raise ValueError('Учётная запись изменена во время входа.')
            except (ValueError, RuntimeError, OSError):
                if not save: raise
                return self.store.put_cluster(name, server, username, None, False, (existing or {}).get('id'),
                    expected=existing, controller_timezone=zone, unverified=True, account_id=aid)
            if save:
                return self.store.put_cluster(name, server, username, config,
                    existing.get('enabled', True) if existing else True, (existing or {}).get('id'),
                    expected=existing, controller_timezone=zone, account_id=aid)
            options = {'transport_mode': 'https'} if self.store.preferences().get('api_transport') == 'https' else {}
            return f"Подключение установлено, namespaces: {test_connection(executable, self.store, server, config or self.store.decrypt(existing['encryptedKubeconfig']), **options)}."
        def work():
            from .auth_queue import connection_lock
            with connection_lock(self.store, (existing or {}).get("id", server)):
                return perform_work()
        def finish():
            self.busy = False
            self.buttons.setEnabled(True)
            self.form_widget.setEnabled(True)
        def done(value):
            finish()
            if save: self.accept()
            else: self.status.setText(str(value))
        def failed(message):
            finish()
            self.status.setText(message)
        self.parent().background.submit(work, done, failed, monitor=False)


class AccountDialog(SecretDialog):
    def __init__(self, parent, store, account=None):
        super().__init__(parent)
        self.busy = False
        self.store, self.account = store, account
        self.setWindowTitle('Учётная запись')
        self.resize(480, 260)
        box = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit((account or {}).get('name', ''))
        self.username = QLineEdit((account or {}).get('username', ''))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText('Оставьте пустым, чтобы сохранить текущий пароль' if account else 'Пароль')
        self.remember = QCheckBox('Сохранить пароль на этой машине')
        self.remember.setChecked(bool((account or {}).get('encryptedPassword')))
        for title, field in (('Название', self.name), ('Логин', self.username), ('Пароль', self.password), ('', self.remember)):
            form.addRow(title, field)
        box.addLayout(form)
        note = QLabel('Без сохранения пароль доступен до закрытия приложения. Смена логина отключит связанные подключения до нового входа.')
        note.setWordWrap(True)
        box.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def save(self):
        try:
            self.store.put_account(self.name.text(), self.username.text(), self.password.text() or None,
                                   account_id=(self.account or {}).get('id'), expected=self.account,
                                   remember_password=self.remember.isChecked())
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self._clear_secrets()
        self.accept()


def accounts_page(parent, store, changed=lambda: None):
    from .ui import item, run_dialog, confirm, error
    page = QWidget(parent)
    page.setObjectName('settingsAccounts')
    box = QVBoxLayout(page)
    box.setContentsMargins(0, 0, 0, 0)
    table = ResourceList()
    table.setObjectName('accountsTable')
    table.setColumnCount(4)
    table.setHorizontalHeaderLabels(['Название', 'Логин', 'Пароль сохранён', 'Подключений'])
    box.addWidget(table)
    accounts = []
    def populate():
        nonlocal accounts
        accounts = store.accounts()
        clusters = store.clusters()
        table.setRowCount(len(accounts))
        for row, account in enumerate(accounts):
            for col, value in enumerate((account['name'], account['username'],
                                         'Да' if account.get('encryptedPassword') else 'Нет',
                                         sum(c.get('accountId') == account['id'] for c in clusters))):
                table.setItem(row, col, item(value))
        table.resizeColumnsToContents()
    def selected():
        row = table.currentRow()
        return accounts[row] if 0 <= row < len(accounts) else None
    def edit(account=None):
        editor = AccountDialog(parent, store, account)
        accepted = run_dialog(editor) == QDialog.Accepted
        populate()
        if accepted: changed()
    def remove():
        account = selected()
        if account and confirm(parent, 'Удалить учётную запись', account['name']):
            try: store.delete_account(account['id'])
            except (OSError, ValueError) as exc: error(parent, str(exc))
            populate()
            changed()
    row = QHBoxLayout()
    for label, action in (('Добавить…', lambda: edit()), ('Изменить…', lambda: edit(selected()) if selected() else None), ('Удалить', remove)):
        button = QPushButton(label)
        button.clicked.connect(action)
        row.addWidget(button)
    box.addLayout(row)
    table.cellDoubleClicked.connect(lambda *_: edit(selected()) if selected() else None)
    populate()
    page.reload = populate
    return page


def show_accounts(parent, store):
    from .ui import run_dialog
    dialog = QDialog(parent)
    dialog.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    dialog.setWindowTitle('Учётные записи')
    dialog.resize(760, 400)
    box = QVBoxLayout(dialog)
    box.addWidget(accounts_page(dialog, store))
    run_dialog(dialog)
