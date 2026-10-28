"""PostgreSQL/ibcmd forms with local and SSH execution and draggable logs."""
import os
from pathlib import Path
import tkinter as tk
from log_colors import insert_log
from tkinter import filedialog, messagebox, ttk

from administration_ui import JobPanel
from postgres_jobs import run_job, read_only_job, ssh_values
from ssh_connection import probe as probe_ssh, UnknownHostKey
from ssh_ui import add_auth_fields
from postgres_clients import bundled_tools, server_major
from sql_server import format_size
from database_dialog import ask_create_database
from table_sorting import TableSorter
from ui_geometry import delayed_wrap


PG_MODES = {'pg_dump': 'Бэкап PostgreSQL · pg_dump', 'pg_restore': 'Восстановление PostgreSQL · pg_restore / psql',
            'ibcmd_dump': 'Выгрузка 1С в DT · ibcmd', 'ibcmd_restore': 'Восстановление 1С из DT · ibcmd'}


def local_tools():
    result = dict(psql='psql', pg_dump='pg_dump', pg_restore='pg_restore', ibcmd='ibcmd')
    for root in (os.environ.get('ProgramFiles', ''), os.environ.get('ProgramFiles(x86)', '')):
        if not root:
            continue
        for product, tool in (('PostgreSQL', 'psql'), ('1cv8', 'ibcmd')):
            found = list((Path(root) / product).glob('*/bin/' + tool + '.exe'))
            if found:
                found.sort(key=lambda path: tuple(int(part) for part in path.parent.parent.name.split('.') if part.isdigit()), reverse=True)
                directory = found[0].parent
                for name in (('psql', 'pg_dump', 'pg_restore') if tool == 'psql' else ('ibcmd',)):
                    result[name] = str(directory / (name + '.exe'))
    bundled = bundled_tools()
    if bundled:
        result.update(bundled[max(bundled)])
    return result


class PostgresPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, PG_MODES['pg_dump'], 'Создать бэкап')
        self.enable_cancellation()
        self.mode = 'pg_dump'
        self.databases, self.connected = [], None
        self.paths, self.logs = {}, {}
        self.execution = tk.StringVar(value='Локально · Windows')
        self.ssh_host = tk.StringVar()
        self.ssh_user = tk.StringVar()
        self.ssh_port = tk.StringVar(value='22')
        self.ssh_key = tk.StringVar()
        self.ssh_password = tk.StringVar()
        self.ssh_use_key = tk.BooleanVar(value=False)
        self.host = tk.StringVar(value='localhost')
        self.port = tk.StringVar(value='5432')
        self.username = tk.StringVar(value='postgres')
        self.password = tk.StringVar()
        self.sslmode = tk.StringVar(value='prefer')
        self.path = tk.StringVar()
        self.filter_var = tk.StringVar()
        self.format = tk.StringVar(value='Архив .dump · pg_restore')
        self.retention = tk.StringVar(value='0')
        self.dump_jobs = tk.StringVar(value='1')
        self.restore_jobs = tk.StringVar(value='1')
        self.compression = tk.StringVar(value='6')
        self.data_path = tk.StringVar()
        self.ib_user = tk.StringVar()
        self.ib_password = tk.StringVar()
        self.manual_database = tk.StringVar()
        self.dt_keep_remote = tk.BooleanVar(value=False)
        self._dt_previous_remote = False
        self._dt_destinations = {'local': '', 'remote': ''}
        self.exclusive = tk.BooleanVar(value=False)
        self.bundled = bundled_tools()
        self.client_set = tk.StringVar(value='Автоматически')
        self._applying_client = False
        self.tool_values = {key: tk.StringVar(value=value) for key, value in local_tools().items()}
        self._tool_sets = {'local': {key: value.get() for key, value in self.tool_values.items()},
                           'ssh': dict(psql='psql', pg_dump='pg_dump', pg_restore='pg_restore',
                                       ibcmd='/opt/1cv8/x86_64/8.3.27.1859/ibcmd')}
        self._execution = 'local'
        self.execution_combo = self.combo('Где выполнять команды', self.execution, ('Локально · Windows', 'SSH · Linux'))
        self.client_form = tk.Frame(self.form, bg=colors['panel'])
        self.client_form.pack(fill=tk.X)
        saved = self.form
        self.form = self.client_form
        self.client_combo = self.combo('Утилиты PostgreSQL', self.client_set, ('Автоматически',
            *('Встроенные PostgreSQL ' + str(major) for major in sorted(self.bundled)), 'Свои пути'))
        self.form = saved
        self.ssh_form = tk.Frame(self.form, bg=colors['panel'])
        saved = self.form
        self.form = self.ssh_form
        self.ssh_host_entry = self.server_field('Компьютер выполнения · адрес SSH', self.ssh_host)
        self.field('Пользователь SSH', self.ssh_user)
        self.field('Порт SSH', self.ssh_port)
        add_auth_fields(self)
        self.ssh_probe_button = self.browse_button('Проверить SSH', self.check_ssh)
        self.note('На Linux-ВМ нужны SSH-сервер, Python 3 и выбранные утилиты. При первом подключении подтвердите отпечаток сервера.')
        self.form = saved
        self.host_entry = self.server_field('Сервер PostgreSQL · адрес с компьютера выполнения', self.host)
        self.pg_label = self.form.pack_slaves()[-2]
        self.field('Порт PostgreSQL', self.port)
        self.field('Логин PostgreSQL', self.username)
        self.field('Пароль PostgreSQL', self.password, show='•')
        self.combo('SSL PostgreSQL', self.sslmode, ('disable', 'prefer', 'require', 'verify-ca', 'verify-full'))
        self.connect_button = self.browse_button('Подключиться / список БД', self.connect)
        self.create_db_button = self.browse_button('+ Создать базу', self.create_new_database)
        self.delete_db_button = self.browse_button('Удалить выбранную базу…', self.delete_selected_database)
        self.delete_db_button.configure(fg=colors['danger'])
        self.path_label = app._label(self.form, 'Папка бэкапов на компьютере выполнения', 10, 'muted')
        self.path_label.pack(anchor=tk.W, pady=(12, 4))
        self.path_row = tk.Frame(self.form, bg=colors['panel'])
        self.path_row.pack(fill=tk.X)
        self.path_entry = app._entry(self.path_row, self.path, width=1)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.inputs.append(self.path_entry)
        saved, self.form = self.form, self.path_row
        self.dt_destination_check = self.check('Оставить DT\nна ПК выполнения', self.dt_keep_remote)
        self.dt_destination_check.configure(font=('Segoe UI', 9), justify=tk.LEFT)
        self.dt_destination_check.pack_forget()
        self.form = saved
        self.picker_button = self.browse_button('Выбрать в проводнике…', self.choose_path)
        self.directory_picker = self.browse_button('Выбрать каталог .pgdir…', self.choose_directory_archive)
        self.directory_picker.pack_forget()
        self.path_note = app._label(self.form, '', 9, 'muted', justify=tk.LEFT, anchor=tk.W, wraplength=280)
        self.path_note.pack(fill=tk.X, pady=(8, 4))
        delayed_wrap(self.path_note)
        self.backup_options = tk.Frame(self.form, bg=colors['panel'])
        self.performance_options = tk.Frame(self.form, bg=colors['panel'])
        self.restore_options = tk.Frame(self.form, bg=colors['panel'])
        self.ib_options = tk.Frame(self.form, bg=colors['panel'])
        self.form = self.backup_options
        self.format_widget = self.combo('Формат pg_dump', self.format,
                                       ('Архив .dump · pg_restore', 'SQL gzip .sql.gz · psql', 'Каталог .pgdir · параллельный бэкап'))
        self.field('Удалять старше N суток · 0 = хранить всё', self.retention)
        self.note('Отдельная папка для каждой базы; в имени дата и время. Очистка только бэкапов этой консоли после успешного бэкапа той же базы. Каталог .pgdir храните и переносите целиком.')
        self.form = self.performance_options
        self.dump_jobs_widget = self.combo('Потоки бэкапа · больше 1 только для каталога', self.dump_jobs, ('1', '2', '4', '8', '16'))
        self.combo('Сжатие · 0 без сжатия, 1 быстрее, 9 компактнее', self.compression, ('0', '1', '3', '6', '9'))
        self.note('Начните с 4 потоков и сжатия 1. Нагрузка на сервер и диск возрастёт; ускорение зависит от базы. SQL gzip требует сжатия от 1.')
        self.form = self.restore_options
        self.restore_jobs_widget = self.combo('Потоки восстановления · .dump / каталог', self.restore_jobs, ('1', '2', '4', '8', '16'))
        self.note('1 поток — загрузка в одной транзакции. Несколько потоков — без общей транзакции: при ошибке возможны частичные данные. SQL / SQL.gz и совместимый поток SQL работают в 1 поток.')
        self.form = self.ib_options
        self.ibcmd_probe_button = self.browse_button('Проверить ibcmd на компьютере выполнения', self.check_ibcmd)
        self.ibcmd_executor_note = app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT, wraplength=280)
        self.ibcmd_executor_note.pack(fill=tk.X, pady=(8, 4))
        delayed_wrap(self.ibcmd_executor_note)
        self.field('Имя базы PostgreSQL · можно ввести без получения списка', self.manual_database)
        self.field('Рабочая папка ibcmd на компьютере выполнения · --data', self.data_path)
        self.note('Отдельная папка служебных данных ibcmd с правами записи, например D:\\1C\\ibcmd_data\\YKT или /home/usr1cv8/ibcmd_data/YKT. Указывать рабочий каталог кластера или файлы PostgreSQL не требуется. Для разных баз используйте разные папки.')
        self.field('Пользователь 1С · пустое для базы без аутентификации', self.ib_user)
        self.field('Пароль пользователя 1С', self.ib_password, show='•')
        self.check('Доступ 1С к базе остановлен, другие серверы её не используют', self.exclusive)
        self.note('ibcmd требует установленную платформу подходящей версии и исключительный доступ к базе. Для выгрузки DT достаточно ввести имя базы; получение списка необязательно. Для восстановления DT нужен также psql для проверки целевой базы. Для резервного копирования PostgreSQL используйте pg_dump.')
        self.form = saved
        self.tools_form = tk.Frame(self.form, bg=colors['panel'])
        self.tools_form.pack(fill=tk.X)
        self.form = self.tools_form
        self.tool_rows = {}
        for name, variable in self.tool_values.items():
            group = tk.Frame(self.form, bg=colors['panel'])
            group.pack(fill=tk.X)
            self.form = group
            self.field('Утилита ' + name + ' · путь или имя в PATH', variable)
            self.tool_rows[name] = group
            self.form = self.tools_form
        self.form = saved
        self.note('Успешные подключения сохраняются автоматически. Пароли защищены Windows DPAPI. Выберите сохранённый сервер стрелкой в поле адреса.')
        filter_row = tk.Frame(self.right, bg=colors['panel'])
        filter_row.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        app._label(filter_row, 'Базы PostgreSQL', 12, bold=True).pack(side=tk.LEFT, padx=(0, 10))
        self.filter_entry = app._entry(filter_row, self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.selection_note = app._label(self.right, 'Для бэкапа можно выбрать несколько баз · Ctrl / Shift',
                                          9, 'muted', anchor=tk.W)
        self.selection_note.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        delayed_wrap(self.selection_note)
        table = self.split_log()
        self.tree = ttk.Treeview(table, columns=('name', 'size', 'owner'), show='headings', selectmode='extended', height=4)
        for key, title, width in (('name', 'База данных', 220), ('size', 'Размер', 120), ('owner', 'Владелец', 140)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=90, stretch=key == 'name', anchor=tk.E if key == 'size' else tk.W)
        self.tree.tag_configure('odd', background=colors['row'])
        self.sorter = TableSorter(self.tree, numeric=('size',), colors=colors)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.filter_var.trace_add('write', self.render)
        self.execution.trace_add('write', self.execution_changed)
        self.dt_keep_remote.trace_add('write', self.dt_destination_changed)
        self.client_set.trace_add('write', self.client_changed)
        for name in ('psql', 'pg_dump', 'pg_restore'):
            self.tool_values[name].trace_add('write', self.tool_edited)
        for variable in (self.host, self.port, self.username, self.password, self.sslmode,
                         self.ssh_host, self.ssh_port, self.ssh_user, self.ssh_key, self.ssh_use_key, self.ssh_password, self.tool_values['psql']):
            variable.trace_add('write', self.connection_changed)
        self.show_mode(self.mode)
        self.update_execution_note()

    def combo(self, title, variable, values):
        self.app._label(self.form, title, 10, 'muted').pack(anchor=tk.W, pady=(8, 4))
        widget = ttk.Combobox(self.form, textvariable=variable, values=values, state='readonly', font=('Segoe UI', 10))
        widget.caption = self.form.pack_slaves()[-1]
        widget.pack(fill=tk.X)
        self.inputs.append(widget)
        return widget

    def finish(self, text='', error=''):
        super().finish(text, error)
        for widget in self.inputs:
            if isinstance(widget, ttk.Combobox) and widget is not self.host_entry and widget is not getattr(self, 'target_host_entry', None):
                widget.configure(state='readonly')

    def browse_button(self, title, command):
        button = self.app._button(self.form, title, command)
        button.pack(fill=tk.X, pady=(8, 0))
        self.inputs.append(button)
        return button

    def connection(self):
        connection = dict(execution=self._execution, host=self.host.get().strip(), port=self.port.get().strip(),
                    username=self.username.get(), password=self.password.get(), sslmode=self.sslmode.get(),
                    ssh_host=self.ssh_host.get().strip(), ssh_user=self.ssh_user.get().strip(),
                    ssh_port=self.ssh_port.get().strip(), ssh_key=self.ssh_key.get().strip(),
                    ssh_password=self.ssh_password.get(), ssh_use_key=self.ssh_use_key.get(),
                    ssh_host_keys={**self.app._settings.get('its_ssh_hosts', {}), **self.app._settings.get('ssh_hosts', {})},
                    data_path=self.data_path.get().strip(), ib_user=self.ib_user.get(), ib_password=self.ib_password.get(),
                    dt_keep_remote=self.dt_keep_remote.get(),
                    format='sql.gz' if self.format.get().startswith('SQL') else 'directory' if self.format.get().startswith('Каталог') else 'dump',
                    retention_days=self.retention.get(), dump_jobs=self.dump_jobs.get(), restore_jobs=self.restore_jobs.get(),
                    compression=self.compression.get(),
                    **{key: value.get().strip() for key, value in self.tool_values.items()})
        if self._execution == 'local' and self.client_set.get() == 'Автоматически':
            # Reader compatibility follows the archive, not the target server.
            # Keep psql/pg_dump and the connected database selection unchanged.
            connection['restore_candidates'] = [tools['pg_restore'] for major, tools in sorted(self.bundled.items())]
        return connection

    def connection_key(self):
        connection = self.connection()
        return tuple(connection[key] for key in ('execution', 'host', 'port', 'username', 'password', 'sslmode',
                                                 'ssh_host', 'ssh_user', 'ssh_port', 'ssh_key', 'ssh_use_key', 'ssh_password', 'psql'))

    def connection_changed(self, *args):
        self.app.database_sizes.cancel(self)
        self.connected = None
        self.databases = []
        self.render()
        self.update_ibcmd_executor_note()

    def execution_changed(self, *args):
        if self.execution.get() not in ('Локально · Windows', 'SSH · Linux'):
            self.execution.set('Локально · Windows')
        self._tool_sets[self._execution] = {key: value.get() for key, value in self.tool_values.items()}
        self._execution = 'ssh' if self.execution.get() == 'SSH · Linux' else 'local'
        self._applying_client = True
        try:
            for key, value in self._tool_sets[self._execution].items():
                self.tool_values[key].set(value)
        finally:
            self._applying_client = False
        self.path.set('')
        self.paths.clear()
        self._dt_destinations = {'local': '', 'remote': ''}
        self.data_path.set('')
        self.exclusive.set(False)
        if self._execution != 'local':
            self.client_form.pack_forget()
            self.ssh_form.pack(fill=tk.X, before=self.pg_label)
        else:
            self.ssh_form.pack_forget()
            self.client_form.pack(fill=tk.X, before=self.pg_label)
        self.update_execution_note()
        self.connection_changed()

    def update_execution_note(self):
        remote = self._execution != 'local'
        download = remote and self.mode == 'ibcmd_dump' and not self.dt_keep_remote.get()
        if self.mode == 'ibcmd_dump':
            self.dt_destination_check.pack(side=tk.RIGHT, before=self.path_entry, padx=(8, 0))
            system = 'Linux' if remote else 'Windows'
            self.path_label.configure(text='Папка DT на компьютере выполнения · ' + system if self.dt_keep_remote.get() else 'Локальная папка DT · на ВМ с консолью')
        else:
            self.dt_destination_check.pack_forget()
        self.picker_button.configure(text='Путь на Linux вводится вручную' if remote and not download else 'Выбрать в проводнике…')
        self.path_note.configure(text=('DT остаётся на компьютере выполнения Windows. В локальном режиме это та же ВМ, где открыта консоль; '
                                       'отдельное копирование не требуется.' if not remote and self.mode == 'ibcmd_dump' and self.dt_keep_remote.get() else
                                       'Выберите локальную папку на ВМ с консолью. DT будет выгружен на Linux и скачан сюда; '
                                       'после проверки размера и SHA256 временный DT на Linux удалится. --data и путь ibcmd относятся к Linux.' if download else
                                       'Все пути к бэкапам, --data и утилитам относятся к Linux-ВМ. localhost — эта ВМ. '
                                       'Файлы не скачиваются и не загружаются автоматически.' if remote else
                                       'Все пути относятся к компьютеру консоли Windows. PostgreSQL может быть удалённым; '
                                       'файл бэкапа создаётся или читается здесь. ' +
                                       ('Встроены утилиты PostgreSQL ' + ', '.join(map(str, sorted(self.bundled))) + '.'
                                        if self.bundled else 'Укажите установленные клиентские утилиты.')))
        self.update_ibcmd_executor_note()

    def dt_destination_changed(self, *args):
        current = self.dt_keep_remote.get()
        if self.mode == 'ibcmd_dump' and self._execution == 'ssh':
            self._dt_destinations['remote' if self._dt_previous_remote else 'local'] = self.path.get()
            self.path.set(self._dt_destinations['remote' if current else 'local'])
        self._dt_previous_remote = current
        self.update_execution_note()

    def update_ibcmd_executor_note(self, *args):
        if self._execution != 'local':
            computer = self.ssh_host.get().strip() or 'укажите адрес SSH'
            text = ('ibcmd выполняется на Linux-ВМ: ' + computer + '. Путь к ibcmd и --data задаются на Linux. ' +
                    ('Готовый DT остаётся на Linux в указанной папке; скачивания в консоль не будет.' if self.mode == 'ibcmd_dump' and self.dt_keep_remote.get() else
                     'Готовый DT скачивается в выбранную локальную папку на ВМ с консолью.' if self.mode == 'ibcmd_dump' else
                     'Для восстановления укажите файл DT на Linux.'))
        else:
            text = ('ibcmd выполняется на компьютере консоли. Для другой Linux-ВМ выберите «SSH · Linux» '
                    'в поле «Где запускать ibcmd».')
        self.ibcmd_executor_note.configure(text=text)

    def apply_client(self, major):
        self._applying_client = True
        try:
            for name, path in self.bundled[major].items():
                self.tool_values[name].set(path)
        finally:
            self._applying_client = False

    def tool_edited(self, *args):
        if self._execution == 'local' and not self._applying_client:
            if self.client_set.get() != 'Свои пути':
                self.client_set.set('Свои пути')

    def client_changed(self, *args):
        if self._execution != 'local' or not self.bundled:
            return
        choice = self.client_set.get()
        if choice == 'Автоматически':
            self.apply_client(max(self.bundled))
        elif choice.startswith('Встроенные PostgreSQL '):
            self.apply_client(int(choice.rsplit(' ', 1)[1]))

    def show_mode(self, mode):
        if mode != self.mode:
            self.paths[self.mode] = self.path.get()
            self.logs[self.mode] = self.output.get('1.0', 'end-1c')
            self.path.set(self.paths.get(mode, ''))
            self.output.configure(state=tk.NORMAL)
            self.output.delete('1.0', tk.END)
            insert_log(self.output, self.logs.get(mode, ''))
            self.output.configure(state=tk.DISABLED)
        self.mode = mode
        backup = mode.endswith('dump')
        ibcmd = mode.startswith('ibcmd')
        self.execution_combo.caption.configure(text='Где запускать ibcmd' if ibcmd else 'Где выполнять команды')
        multiple = mode == 'pg_dump'
        self.title.configure(text=PG_MODES[mode])
        self.execute_button.configure(text='Выгрузить DT' if ibcmd and backup else ('Создать бэкап' if backup else 'Восстановить'))
        self.path_label.configure(text='Локальная папка DT · на ВМ с консолью' if mode == 'ibcmd_dump' else
                                  'Папка бэкапов на компьютере выполнения' if backup else 'Файл .dt' if ibcmd else 'Файл .dump / .backup / .tar / .sql / .sql.gz')
        if mode == 'pg_restore':
            self.path_label.configure(text='Файл бэкапа или каталог .pgdir с toc.dat')
            self.directory_picker.pack(fill=tk.X, after=self.picker_button, pady=(8, 0))
        else:
            self.directory_picker.pack_forget()
        self.tree.configure(selectmode='extended' if multiple else 'browse')
        if not multiple and len(self.tree.selection()) > 1:
            self.tree.selection_set(self.tree.selection()[0])
        self.selection_note.configure(text=('Для бэкапа можно выбрать несколько баз · Ctrl / Shift' if multiple else
                                            'Для выгрузки DT выберите одну информационную базу' if backup else
                                            'Выберите одну целевую базу · текущие данные будут заменены'))
        for group, show in ((self.backup_options, backup), (self.performance_options, mode == 'pg_dump'),
                            (self.restore_options, mode == 'pg_restore'), (self.ib_options, ibcmd)):
            if show:
                group.pack(fill=tk.X, before=self.tools_form)
            else:
                group.pack_forget()
        if ibcmd:
            self.format_widget.caption.pack_forget()
            self.format_widget.pack_forget()
        else:
            label = self.format_widget.caption
            following = [widget for widget in self.backup_options.pack_slaves() if widget not in (label, self.format_widget)]
            label.pack(anchor=tk.W, pady=(8, 4), **({'before': following[0]} if following else {}))
            self.format_widget.pack(fill=tk.X, after=label)
        for name, group in self.tool_rows.items():
            show = name == 'psql' or (name == 'ibcmd' if ibcmd else name in ('pg_dump', 'pg_restore'))
            group.pack(fill=tk.X) if show else group.pack_forget()
        self.update_execution_note()

    def set_compact(self, compact):
        self.selection_note.grid_remove() if compact else self.selection_note.grid()

    def selected(self):
        return [self.tree.item(key, 'values')[0] for key in self.tree.selection()]

    def render(self, *args):
        selected = set(self.selected())
        query_text = self.filter_var.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        values = {}
        for row in self.databases:
            if hasattr(self.app, 'hub') and not self.app.hub.visible(self, 'source', row['name']):
                continue
            if query_text in row['name'].casefold():
                key = self.tree.insert('', tk.END, values=(row['name'], format_size(row.get('size_bytes')), row.get('owner', '')),
                                       tags=('odd',) if len(values) % 2 else ())
                if row['name'] in selected:
                    self.tree.selection_add(key)
                values[key] = dict(name=row['name'], size=row.get('size_bytes'), owner=row.get('owner', ''))
        self.sorter.apply(values)
        if hasattr(self.app, 'hub'):
            self.app.hub.restore_pending(self)

    def create_new_database(self):
        if self.running:
            return
        connection, key = self.connection(), self.connection_key()
        settings = ask_create_database(self.app.root, 'PostgreSQL', connection['host'] + ':' + connection['port'] +
                                       (' · SSH ' + connection['ssh_host'] if self._execution != 'local' else ''),
                                       connection['username'], self.c)
        if not settings:
            return
        database = settings['name']
        def operation(event):
            run_job(dict(mode='create', connection=connection, database=database, owner=settings['owner']), event)
            try:
                return run_job(dict(mode='list', connection=connection, fast=True), event), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.database_created(key, database, result),
                      history=self.history_context('Создание БД · PostgreSQL', database))

    def database_created(self, key, database, result):
        refreshed, error = result
        if refreshed is not None:
            self.loaded(key, refreshed)
            self.filter_var.set('')
            for row in self.tree.get_children():
                if self.tree.set(row, 'name') == database:
                    self.tree.selection_set(row)
                    self.tree.see(row)
        else:
            self.append('База создана, но список не обновлён: ' + error + '\nНажмите «Подключиться / список БД».')
        self.finish('База создана: ' + database)

    def delete_selected_database(self):
        if self.running:
            return
        databases = self.selected()
        if len(databases) != 1 or self.connected != self.connection_key():
            self.app._panel_error(self, 'Подключитесь к PostgreSQL и выберите одну базу для удаления')
            return
        database = databases[0]
        if database in ('postgres', 'template0', 'template1'):
            self.app._panel_error(self, 'Удаление служебной базы PostgreSQL запрещено')
            return
        connection, key = self.connection(), self.connection_key()
        detail = (f"Выполнение: {self.execution.get()}\nPostgreSQL: {connection['host']}:{connection['port']}\n"
                  + (f"SSH: {connection['ssh_user']}@{connection['ssh_host']}\n" if self._execution != 'local' else '') +
                  f'База: {database}\n\nБаза и её данные будут удалены. Текущие подключения будут завершены.\n'
                  'Резервная копия автоматически не создаётся.\n\nУдалить эту базу?')
        if not messagebox.askyesno('Удалить базу PostgreSQL?', detail, parent=self.app.root, icon='warning', default='no'):
            return
        def operation(event):
            run_job(dict(mode='delete', connection=connection, database=database), event)
            try:
                return run_job(dict(mode='list', connection=connection, fast=True), event), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.database_deleted(key, database, result),
                      history=self.history_context('Удаление БД · PostgreSQL', database))

    def database_deleted(self, key, database, result):
        refreshed, error = result
        if refreshed is not None:
            self.loaded(key, refreshed)
        else:
            self.databases = [row for row in self.databases if row['name'] != database]
            self.render()
            self.append('База удалена, но список не обновлён: ' + error + '\nНажмите «Подключиться / список БД».')
        self.finish('База удалена: ' + database)

    def connect(self):
        connection = self.connection()
        key = self.connection_key()
        if self.start():
            self.work(lambda event: read_only_job({'mode': 'list', 'connection': connection, 'fast': True}, event, runner=run_job),
                      lambda result: self.connection_loaded(key, connection, result))

    def connection_loaded(self, key, connection, rows):
        if self.confirm_ssh_host(rows, self.connect):
            return
        self.loaded(key, rows)
        self.app.saved_servers.remember(self, connection)

    def check_ibcmd(self):
        if self.running:
            return
        connection = self.connection()
        if self._execution != 'local' and (not connection['ssh_host'] or not connection['ssh_user']):
            self.app._panel_error(self, 'Укажите адрес компьютера выполнения ibcmd и пользователя SSH')
            return
        if self.start():
            self.work(lambda event: read_only_job(dict(mode='ibcmd_probe', connection=connection), event, runner=run_job), self.ibcmd_checked)

    def check_ssh(self):
        if self.running:
            return
        if self._execution != 'ssh':
            self.app._panel_error(self, 'Выберите «SSH · Linux», чтобы проверить SSH-подключение')
            return
        values = ssh_values(self.connection())
        def operation(event):
            event(dict(kind='stage', text='Проверка SSH: ' + values['host']))
            try:
                return probe_ssh(values)
            except UnknownHostKey as exc:
                return dict(ssh_host_key=exc.key)
        if self.start():
            self.work(operation, lambda result: self.ssh_checked(result, values))

    def ssh_checked(self, result, connection):
        if self.confirm_ssh_host(result, self.check_ssh):
            return
        self.append(result.get('description', ''))
        self.app.saved_servers.remember_ssh(self, connection)
        self.finish('SSH-подключение проверено: ' + connection['host'])

    def ibcmd_checked(self, result):
        if self.confirm_ssh_host(result, self.check_ibcmd):
            return
        if self._execution == 'ssh':
            self.app.saved_servers.remember_ssh(self, ssh_values(self.connection()))
        self.append('Компьютер выполнения: ' + result['computer'] + ' · ' + result['system'] +
                    '\nibcmd: ' + result['ibcmd'] + '\n' + result['version'])
        self.finish('ibcmd доступен на компьютере ' + result['computer'] + '. Можно настроить выгрузку DT.')

    def confirm_ssh_host(self, result, retry):
        if not isinstance(result, dict) or 'ssh_host_key' not in result:
            return False
        if self.defer_until_visible(lambda: self.confirm_ssh_host(result, retry)):
            return True
        self.finish('Требуется подтверждение ключа SSH')
        key = result['ssh_host_key']
        if messagebox.askyesno('Первое подключение SSH', 'Адрес: ' + key['host'] + '\nКлюч: ' + key['algorithm'] +
                              '\nОтпечаток: ' + key['fingerprint'] + '\n\nСверьте отпечаток с ВМ. Доверять этому ключу?',
                              parent=self.app.root, default='no'):
            self.app._settings.setdefault('ssh_hosts', {})[key['host']] = key
            self.app._save_settings()
            retry()
        return True

    def loaded(self, key, result):
        if self._execution == 'local' and self.client_set.get() == 'Автоматически':
            major = server_major(result)
            if major in self.bundled:
                self.apply_client(major)
                key = self.connection_key()
                self.append(f'PostgreSQL {major}: выбран встроенный комплект утилит')
            elif major and self.bundled:
                self.append(f'PostgreSQL {major}: встроенного комплекта этой версии нет. При необходимости укажите свои пути.')
        self.connected, self.databases = key, result
        self.render()
        self.finish(f'Подключено: {self.host.get()}:{self.port.get()}. Доступных БД: {len(result)}. Выберите базу.')
        self.app.database_sizes.refresh(self)

    def choose_key(self):
        if not self.running:
            path = filedialog.askopenfilename(parent=self.app.root, title='Закрытый ключ OpenSSH', filetypes=[('Все файлы', '*.*')])
            if path:
                self.ssh_key.set(path)

    def choose_path(self):
        if self.running:
            return
        if self._execution != 'local' and (self.mode != 'ibcmd_dump' or self.dt_keep_remote.get()):
            self.app._panel_error(self, 'Введите полный путь на Linux, например /BACKUP/do. Проводник консоли выбирает только локальные файлы.')
            self.path_entry.focus_set()
            return
        if self.mode.endswith('dump'):
            path = filedialog.askdirectory(parent=self.app.root, title='Локальная папка DT' if self.mode == 'ibcmd_dump' else 'Папка бэкапов PostgreSQL')
        else:
            path = filedialog.askopenfilename(parent=self.app.root, title='Файл для восстановления',
                filetypes=[('Выгрузка 1С', '*.dt')] if self.mode.startswith('ibcmd') else
                [('Бэкапы PostgreSQL', '*.dump *.backup *.tar *.sql *.sql.gz'), ('Все файлы', '*.*')])
        if path:
            self.path.set(path)

    def choose_directory_archive(self):
        if self.running or self.mode != 'pg_restore':
            return
        if self._execution != 'local':
            self.app._panel_error(self, 'Введите полный путь каталога pg_dump на компьютере выполнения, содержащего toc.dat.')
            self.path_entry.focus_set()
            return
        path = filedialog.askdirectory(parent=self.app.root, title='Каталог архива pg_dump · содержит toc.dat')
        if path:
            self.path.set(path)

    def execute(self):
        if self.running:
            return
        manual = self.manual_database.get().strip() if self.mode.startswith('ibcmd') else ''
        databases = [manual] if manual else self.selected()
        if not manual and self.connected != self.connection_key():
            self.app._panel_error(self, 'Подключение к PostgreSQL не установлено или его параметры изменены. Нажмите «Подключиться / список БД».')
            return
        if not databases:
            self.app._panel_error(self, 'Выберите базу данных в списке справа.', self.tree)
            return
        if not self.path.get().strip():
            self.app._panel_error(self, 'Укажите локальную папку DT на ВМ с консолью' if self.mode == 'ibcmd_dump' and
                                  not (self._execution == 'ssh' and self.dt_keep_remote.get()) else
                                  'Укажите папку или файл на компьютере выполнения', self.path_entry)
            return
        if self.mode.startswith('ibcmd') and not self.exclusive.get():
            self.app._panel_error(self, 'Для ibcmd остановите доступ 1С к базе и отметьте это в форме')
            return
        connection = self.connection()
        path = self.path.get().strip()
        if self.mode == 'pg_dump' and int(connection['dump_jobs']) > 1 and connection['format'] != 'directory':
            self.app._panel_error(self, 'Для нескольких потоков бэкапа выберите «Каталог .pgdir»; для .dump / SQL оставьте 1 поток')
            return
        if self.mode == 'pg_dump' and connection['format'] == 'sql.gz' and connection['compression'] == '0':
            self.app._panel_error(self, 'SQL gzip требует сжатие от 1 до 9')
            return
        restore = self.mode.endswith('restore')
        if restore:
            if len(databases) != 1:
                self.app._panel_error(self, 'Выберите одну целевую базу')
                return
            detail = (f"Выполнение: {self.execution.get()}\nPostgreSQL: {connection['host']}:{connection['port']}\n"
                      f"Целевая база: {databases[0]}\nФайл: {path}\n\nТекущие данные будут заменены, подключения закрыты.\n")
            detail += ('pg_restore / psql удалит и создаст целевую базу заново. При ошибке старые данные не возвращаются.\n'
                       if self.mode == 'pg_restore' else 'ibcmd загрузит DT с принудительным завершением сеансов.\n')
            if self.mode == 'pg_restore' and int(connection['restore_jobs']) > 1:
                detail += (f"Потоков: {connection['restore_jobs']}. Общая транзакция отключена; при ошибке новая база "
                           'может остаться частично заполненной. Для SQL / SQL.gz и совместимого потока SQL выберите 1 поток.\n')
            detail += 'Отдельная копия старой базы автоматически не создаётся. Продолжить?'
            if not messagebox.askyesno('Восстановить PostgreSQL-базу?', detail, parent=self.app.root, icon='warning', default='no'):
                return
        payload = dict(mode=self.mode, connection=connection, path=path, databases=databases)
        output_message = ('DT сохранены на компьютере выполнения · ' + ('Linux' if self._execution == 'ssh' else 'Windows') + ':\n' if self.mode == 'ibcmd_dump' and self.dt_keep_remote.get() else
                          'DT сохранены на ВМ с консолью:\n' if self.mode == 'ibcmd_dump' else 'Созданы файлы:\n')
        def completed(result):
            if connection['execution']=='ssh':self.app.saved_servers.remember_ssh(self,ssh_values(connection))
            self.finish('Восстановление завершено: '+result if restore else output_message+'\n'.join(result))
        if self.start():
            self.append(f"{PG_MODES[self.mode]}\nВыполнение: {self.execution.get()}\nСервер: {connection['host']}:{connection['port']}\nБазы: {', '.join(databases)}\nПуть: {path}")
            self.progress_state.complete_task = True
            self.work(lambda event: run_job(payload, event),
                      completed,
                      history=self.history_context(PG_MODES[self.mode], ', '.join(databases), path=path))
