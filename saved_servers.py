"""Editable server suggestions shared by database and Linux SSH forms."""
from clipboard_shortcuts import bind_entry_shortcuts
import tkinter as tk
from tkinter import ttk


class SavedServers:
    def __init__(self, app):
        self.app = app
        self.pickers = {}

    def install(self, panel):
        source = getattr(panel, 'host_entry', None) or getattr(panel, 'server_entry', None)
        if source is not None:
            self.register(panel, source, 'source')
        target = getattr(panel, 'target_host_entry', None)
        if target is not None:
            self.register(panel, target, 'target')
        ssh = getattr(panel, 'ssh_host_entry', None)
        if ssh is not None:
            self.register(panel, ssh, 'ssh')

    def register(self, panel, widget, role):
        if widget in self.pickers:
            return
        self.pickers[widget] = (panel, role)
        widget.configure(postcommand=lambda: self.refresh(widget, filter_prefix=True))
        widget.bind('<KeyRelease>', lambda event: self.refresh(widget, filter_prefix=True)
                    if event.char and event.char.isprintable() and not event.state & 4 else None, add='+')
        widget.bind('<<ComboboxSelected>>', lambda event: self.choose(widget))
        widget.bind('<Destroy>', lambda event: self.pickers.pop(widget, None), add='+')
        bind_entry_shortcuts(widget)
        button = self.app._button(widget.server_actions_row, '⋯', lambda: self.manage(widget))
        button.configure(font=('Segoe UI', 10), padx=7, pady=3)
        button.pack(side=tk.RIGHT, before=widget, padx=(5, 0))
        widget.manage_servers_button = button
        panel.inputs.append(button)
        self.refresh(widget)

    def manage(self, widget):
        panel, role = self.pickers[widget]
        if panel.running:
            return
        kind = self.kind(panel, role)
        window = tk.Toplevel(self.app.root)
        window.title('Сохранённые серверы Linux · SSH' if kind == 'ssh' else 'Сохранённые серверы СУБД')
        window.geometry('760x360')
        window.minsize(460, 240)
        window.configure(bg=panel.c['panel'])
        window.transient(self.app.root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        self.app._label(window, 'Сохранённые подключения · ' + ('Linux / SSH' if kind == 'ssh' else 'PostgreSQL' if kind == 'postgres' else 'Microsoft SQL Server'),
                        12, bold=True).grid(row=0, column=0, columnspan=2, sticky='w', padx=14, pady=12)
        tree = ttk.Treeview(window, columns=('connection',), show='headings', selectmode='extended')
        tree.heading('connection', text='Сервер · пользователь · выполнение')
        tree.column('connection', width=620, minwidth=200)
        tree.grid(row=1, column=0, sticky='nsew', padx=(14, 0))
        scroll = ttk.Scrollbar(window, command=tree.yview)
        scroll.grid(row=1, column=1, sticky='ns', padx=(0, 14))
        tree.configure(yscrollcommand=scroll.set)
        for row in self.app.activity.snapshot('connections'):
            if row['kind'] == kind:
                tree.insert('', tk.END, iid=row['id'], values=(self.caption(row),))
        footer = tk.Frame(window, bg=panel.c['panel'])
        footer.grid(row=2, column=0, columnspan=2, sticky='ew', padx=14, pady=12)
        status = self.app._label(footer, '', 9, 'muted')
        status.pack(side=tk.LEFT)
        def remove():
            count = sum(self.app.activity.remove_connection(key) for key in tree.selection())
            for key in tree.selection():
                tree.delete(key)
            for picker in list(self.pickers):
                if picker.winfo_exists():
                    self.refresh(picker)
            status.configure(text=f'Удалено из списка: {count}')
        delete = self.app._button(footer, 'Убрать из списка', remove)
        delete.configure(fg=panel.c['danger'])
        delete.pack(side=tk.RIGHT)
        self.app._button(footer, 'Закрыть', window.destroy).pack(side=tk.RIGHT, padx=6)
        window.connection_tree, window.remove_button = tree, delete
        return window

    @staticmethod
    def kind(panel, role):
        return 'ssh' if role == 'ssh' else 'postgres' if hasattr(panel, 'host') else 'sql'

    @staticmethod
    def caption(row):
        values = row['values']
        if row['kind'] == 'sql':
            return values['server'] + ' · ' + ('Windows' if values.get('windows_auth') else values.get('username', ''))
        if row['kind'] == 'ssh':
            return values['host'] + ':' + str(values.get('port', '22')) + ' · ' + values.get('user', '') + ' · ' + ('SSH-ключ' if values.get('use_key') else 'Пароль')
        text = values['host'] + ':' + values.get('port', '5432') + ' · ' + values.get('username', '')
        if values.get('execution', '').startswith('SSH'):
            text += ' · SSH ' + values.get('ssh_user', '') + '@' + values.get('ssh_host', '')
        return text

    def refresh(self, widget, filter_prefix=False):
        if widget not in self.pickers:
            return
        panel, role = self.pickers[widget]
        kind = self.kind(panel, role)
        rows = [row for row in self.app.activity.snapshot('connections') if row['kind'] == kind]
        prefix = widget.get().strip().casefold() if filter_prefix else ''
        widget._before_choice = widget.get()
        hosts = {str(row['values'].get('host', row['values'].get('server', ''))).casefold() for row in rows}
        if prefix in hosts or prefix in ('localhost', '127.0.0.1', '.'):
            prefix = ''
        choices = {}
        for row in rows:
            label = self.caption(row)
            if prefix and not label.casefold().startswith(prefix):
                continue
            while label in choices:
                label += ' ·'
            choices[label] = row
        widget._saved_choices = choices
        widget.configure(values=tuple(choices))

    def remember(self, panel, connection):
        if hasattr(panel, 'host'):
            values = {key: connection.get(key, '') for key in
                      ('host', 'port', 'username', 'password', 'sslmode', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_key', 'ssh_use_key', 'ssh_password')}
            values['ssh_use_key'] = bool(connection.get('ssh_use_key', False))
            values['execution'] = 'SSH · Linux' if connection.get('execution') == 'ssh' else 'Локально · Windows'
            if connection.get('execution', 'local') == 'local':
                for key in ('ssh_host', 'ssh_port', 'ssh_user', 'ssh_key', 'ssh_password'):
                    values[key] = ''
            profile = dict(kind='postgres', page='pg_dump', values=values)
        else:
            profile = dict(kind='sql', page='db_backup', values=connection)
        try:
            self.app.activity.save_connection(profile)
        except ValueError as exc:
            panel.append('ПРЕДУПРЕЖДЕНИЕ: подключение открыто, но не сохранено: ' + str(exc))
            return
        if hasattr(panel, 'host') and connection.get('execution') == 'ssh':
            from postgres_jobs import ssh_values
            self.remember_ssh(panel, ssh_values(connection), refresh=False)
        self.refresh_all()

    def refresh_all(self):
        for widget in list(self.pickers):
            self.refresh(widget)

    def remember_ssh(self, panel, connection, refresh=True):
        values = {key: connection[key] for key in ('host', 'port', 'user', 'use_key', 'key', 'password', 'sudo_password') if key in connection}
        values.update(host=str(values.get('host', '')).strip(), user=str(values.get('user', '')).strip(),
                      port=str(values.get('port') or '22'), use_key=values.get('use_key', False), key=str(values.get('key', '')).strip())
        if not values['host'] or not values['user']:
            return
        try:
            self.app.activity.save_connection(dict(kind='ssh', page='linux_disk', values=values))
        except ValueError as exc:
            panel.append('ПРЕДУПРЕЖДЕНИЕ: SSH-подключение открыто, но не сохранено: ' + str(exc))
            return
        if refresh:
            self.refresh_all()

    @staticmethod
    def apply_ssh(panel, values, secrets):
        for key, field in (('host', 'ssh_host'), ('port', 'ssh_port'), ('user', 'ssh_user'), ('use_key', 'ssh_use_key'), ('key', 'ssh_key')):
            getattr(panel, field).set(values.get(key, False if key == 'use_key' else '22' if key == 'port' else ''))
        panel.ssh_password.set(secrets.get('password', ''))
        if hasattr(panel, 'sudo_password'):
            panel.sudo_password.set(secrets.get('sudo_password', ''))

    def choose(self, widget):
        panel, role = self.pickers[widget]
        row = getattr(widget, '_saved_choices', {}).get(widget.get())
        if row is None or panel.running:
            return
        try:
            secrets = self.app.activity.credentials(row)
        except ValueError as exc:
            variable = panel.ssh_host if role == 'ssh' else panel.server if row['kind'] == 'sql' else panel.target_host if role == 'target' else panel.host
            variable.set(getattr(widget, '_before_choice', ''))
            self.app._panel_error(panel, str(exc))
            return
        values = row['values']
        if role == 'ssh':
            self.apply_ssh(panel, values, secrets)
        elif row['kind'] == 'sql':
            for key in ('server', 'username', 'windows_auth', 'encrypt', 'trust_certificate'):
                getattr(panel, key).set(values[key])
            panel.password.set(secrets.get('password', ''))
        elif role == 'target':
            for key, target in (('host', 'target_host'), ('port', 'target_port'), ('username', 'target_user'), ('sslmode', 'target_ssl')):
                getattr(panel, target).set(values[key])
            panel.target_password.set(secrets.get('password', ''))
        else:
            panel.execution.set(values['execution'])
            for key in ('host', 'port', 'username', 'sslmode', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_key', 'ssh_use_key'):
                getattr(panel, key).set(values.get(key, ''))
            panel.password.set(secrets.get('password', ''))
            panel.ssh_password.set(secrets.get('ssh_password', ''))
        self.app._clear_panel_error(panel)
        self.app._panel_status(panel, 'Сервер Linux выбран. Нажмите «Проверить SSH» или запустите нужную операцию.' if role == 'ssh' else
                               'Сохранённое подключение выбрано. Нажмите «Подключиться / список БД».')
