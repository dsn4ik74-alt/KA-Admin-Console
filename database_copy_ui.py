"""Explicit source/target workflows, retaining the backup used for restore."""
import tkinter as tk
from tkinter import messagebox, ttk

from administration_ui import SqlPanel
from sql_server import backup_and_restore, create_database, delete_database, list_databases
from database_dialog import ask_create_database
from postgres_ui import PostgresPanel
from postgres_clients import server_major
from postgres_jobs import run_job, read_only_job
from sql_server import format_size
from table_sorting import TableSorter
from ui_geometry import delayed_wrap


def database_columns(panel, container, source_filter, target_filter, columns, numeric=()):
    """Actual Treeview parents must be the panes, not covered siblings."""
    for child in container.winfo_children():
        child.destroy()
    container.columnconfigure(0, weight=1)
    container.rowconfigure(0, weight=1)
    split = tk.PanedWindow(container, orient=tk.HORIZONTAL, bg=panel.c['border'],
                           bd=0, sashwidth=9, sashpad=2, showhandle=True,
                           handlesize=12, sashcursor='sb_h_double_arrow', opaqueresize=False)
    split.grid(row=0, column=0, sticky='nsew')
    tables, sorters, areas, counts = [], [], [], []
    for index, (title, variable) in enumerate((('Источник', source_filter), ('Получатель', target_filter))):
        area = tk.Frame(split, bg=panel.c['panel'], padx=5)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(2, weight=1)
        split.add(area, minsize=140, width=340, stretch='always')
        header = tk.Frame(area, bg=panel.c['panel'])
        header.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(2, 4))
        panel.app._label(header, title, 11, bold=True).pack(side=tk.LEFT)
        count = panel.app._label(header, '0 баз', 9, 'muted')
        count.pack(side=tk.RIGHT)
        search = panel.app._entry(area, variable, width=1)
        search.configure(bd=4, font=('Segoe UI', 10))
        search.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 6))
        tree = ttk.Treeview(area, columns=tuple(key for key, title, width in columns), show='headings',
                            selectmode='browse', height=5, style='Copy.Treeview')
        for key, caption, width in columns:
            tree.heading(key, text=caption)
            tree.column(key, width=width, minwidth=65 if key != 'name' else 140,
                        stretch=key == 'name', anchor=tk.E if key in numeric else tk.W)
        tree.grid(row=2, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(area, command=tree.yview)
        vertical.grid(row=2, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(area, orient=tk.HORIZONTAL, command=tree.xview)
        horizontal.grid(row=3, column=0, sticky='ew')
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tables.append(tree)
        sorters.append(TableSorter(tree, numeric=numeric, colors=panel.c))
        areas.append(area)
        counts.append(count)
        if index == 0:
            panel.filter_entry = search
    panel.database_split = split
    panel.source_area, panel.target_area = areas
    panel.source_count, panel.target_count = counts
    # Give the lists and log useful initial heights; both dividers remain draggable.
    panel.log_split.paneconfigure(panel.log_split.panes()[0], minsize=100, height=330)
    return (*tables, *sorters)


def compact_copy_title(panel, compact):
    if not hasattr(panel, '_copy_title_pack'):
        panel._copy_title_pack = panel.title.pack_info()
    if compact:
        panel.title.pack_forget()
    else:
        panel.title.pack(**panel._copy_title_pack, before=panel.right.master)


def install_new_copy_tab(panel):
    # Remains usable while this particular panel's operation locks its inputs.
    panel.new_task_button = panel.app._button(panel.actions, '+ Новая вкладка',
        lambda:panel.app.bookmarks._duplicate_tab(panel._page_key))
    panel.new_task_button.pack(side=tk.RIGHT, padx=(0,8), before=panel.cancel_button)


def confirm_database_delete(panel, vendor, server, database, role, execution=''):
    detail = (f'{vendor}: {server}\nСписок: {role}\nБаза: {database}\n' +
              (f'Выполнение: {execution}\n' if execution else '') +
              '\nБаза и её данные будут удалены. Подключения будут завершены. '
              'Резервная копия автоматически не создаётся.\n\nУдалить эту базу?')
    return messagebox.askyesno('Удалить базу ' + vendor + '?', detail, parent=panel.app.root,
                               icon='warning', default='no')


class SqlCopyPanel(SqlPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors)
        self.destination = tk.StringVar()
        self.delete_db_button.configure(text='Удалить базу источника…')
        self.delete_target_button = app._button(self.form, 'Удалить базу получателя…', self.delete_target_database)
        self.delete_target_button.configure(fg=colors['danger'])
        self.delete_target_button.pack(fill=tk.X, after=self.delete_db_button, pady=(4, 4))
        self.inputs.append(self.delete_target_button)
        self.target_filter = tk.StringVar()
        self.filter_entry.master.grid_remove()
        container = self.tree.master
        self.tree, self.target_tree, self.sorter, self.target_sorter = database_columns(
            self, container, self.filter_var, self.target_filter,
            (('name', 'База данных', 220), ('data', 'Данные', 90), ('log', 'Лог', 90),
             ('total', 'Всего', 90), ('recovery', 'Восстановление', 120)), ('data', 'log', 'total'))
        self.tree.bind('<<TreeviewSelect>>', self.update_summary)
        self.target_tree.bind('<<TreeviewSelect>>', self.target_selected)
        self.destination.trace_add('write', self.select_target)
        self.target_filter.trace_add('write', self.render_target)
        self.selected_label.grid_remove()
        self.summary.grid_remove()
        self.table_note.grid_remove()
        self.show_mode('db_copy')
        install_new_copy_tab(self)

    def show_mode(self, mode):
        self.mode = 'db_copy'
        self.title.configure(text='Бэкап + восстановление · Microsoft SQL Server')
        self.execute_button.configure(text='Бэкап + восстановление')
        self.path_label.configure(text='Папка нового FULL-бэкапа на SQL Server')
        self.table_note.configure(text='Выберите источник в левой таблице, получателя — в правой.\n'
                                 'Обе базы должны существовать на одном SQL Server; созданный .bak сохраняется.')
        self.options.pack(fill=tk.X)
        self.verify_check.pack(fill=tk.X, pady=(8,0))

    def render(self, *args):
        super().render(*args)
        if hasattr(self, 'target_tree'):
            names = [row['name'] for row in self.databases]
            if self.destination.get() not in names:
                self.destination.set('')
            # A source filter must not rebuild the independent target table.
            if not args:
                self.render_target()
            self.source_count.configure(text=f'{len(self.tree.get_children())} / {len(names)} баз')

    def _resize_summary(self):
        self.summary.grid_remove()
        self.table_note.grid_remove()

    def set_compact(self, compact):
        super().set_compact(compact)
        compact_copy_title(self, compact)

    def render_target(self, *args):
        if not hasattr(self, 'target_tree'):
            return
        query_text = self.target_filter.get().strip().casefold()
        self.target_tree.delete(*self.target_tree.get_children())
        values = {}
        for item in self.databases:
            if hasattr(self.app, 'hub') and not self.app.hub.visible(self, 'target', item['name']):
                continue
            if query_text not in item['name'].casefold():
                continue
            row = self.target_tree.insert('', tk.END, values=(item['name'], format_size(item.get('data_bytes')),
                format_size(item.get('log_bytes')), format_size(item.get('total_bytes')), item['recovery_model_desc']))
            values[row] = dict(name=item['name'], data=item.get('data_bytes'), log=item.get('log_bytes'),
                               total=item.get('total_bytes'), recovery=item['recovery_model_desc'])
        self.target_sorter.apply(values)
        self.select_target()
        self.target_count.configure(text=f'{len(values)} / {len(self.databases)} баз')
        if hasattr(self.app, 'hub'):
            self.app.hub.restore_pending(self)

    def select_target(self, *args):
        for row in self.target_tree.get_children():
            if self.target_tree.set(row, 'name') == self.destination.get():
                self.target_tree.selection_set(row)
                return
        self.target_tree.selection_remove(*self.target_tree.selection())

    def target_selected(self, event=None):
        selection = self.target_tree.selection()
        if selection:
            name = self.target_tree.set(selection[0], 'name')
            if name != self.destination.get():
                self.destination.set(name)

    def create_new_database(self):
        self._database_action('create', 'target')

    def delete_selected_database(self):
        self._database_action('delete', 'source')

    def delete_target_database(self):
        self._database_action('delete', 'target')

    def _database_action(self, action, side):
        if self.running:
            return
        connection = self.connection()
        if action == 'create':
            settings = ask_create_database(self.app.root, 'SQL Server', connection['server'],
                connection['username'] if not connection['windows_auth'] else '', self.c)
            if not settings:
                return
            database = settings['name']
        else:
            tree = self.tree if side == 'source' else self.target_tree
            selection = tree.selection()
            if self.connected != connection or len(selection) != 1:
                self.app._panel_error(self, 'Подключитесь к SQL Server и выберите базу в нужном списке для удаления')
                return
            database = tree.set(selection[0], 'name')
            if database.casefold() in ('master', 'model', 'msdb', 'tempdb'):
                self.app._panel_error(self, 'Удаление системной базы SQL Server запрещено')
                return
            if not confirm_database_delete(self, 'SQL Server', connection['server'], database,
                                           'Источник' if side == 'source' else 'Получатель'):
                return
        def operation(event):
            (create_database if action == 'create' else delete_database)(connection, database, event)
            try:
                return list_databases(connection, event), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.database_action_done(connection, action, database, result),
                      history=self.history_context(('Создание' if action == 'create' else 'Удаление') + ' БД · SQL Server', database))

    def database_action_done(self, connection, action, database, result):
        refreshed, error = result
        if refreshed is not None:
            self.databases, metadata = refreshed
            self.machine, self.connected = metadata.get('machine', ''), connection
        else:
            self.connected = None
            if action == 'delete':
                self.databases = [row for row in self.databases if row['name'] != database]
            self.append('База ' + ('создана' if action == 'create' else 'удалена') +
                        ', но список не обновлён: ' + error + '\nОбновите подключение. Повторять действие не нужно.')
        if action == 'create' and refreshed is not None:
            self.target_filter.set('')
            self.destination.set(database)
        self.render()
        if action == 'create' and self.target_tree.selection():
            self.target_tree.see(self.target_tree.selection()[0])
        self.finish(('База создана: ' if action == 'create' else 'База удалена: ') + database)

    def execute(self):
        if self.running:
            return
        selection = self.target_tree.selection()
        target = self.target_tree.set(selection[0], 'name') if selection else ''
        source, connection = self.selected(), self.connection()
        if self.connected != connection:
            self.app._panel_error(self, 'Подключение к SQL Server не установлено или его параметры изменены. Подключитесь и обновите список баз.')
            return
        if not source:
            self.app._panel_error(self, 'Источник: выберите базу, для которой нужно создать бэкап.', self.tree)
            return
        if not target:
            self.app._panel_error(self, 'Получатель: выберите базу для восстановления бэкапа.', self.target_tree)
            return
        if source.casefold() == target.casefold():
            self.app._panel_error(self, 'Источник и целевая база должны различаться')
            return
        folder = self.server_path.get().strip()
        if not folder:
            self.app._panel_error(self, 'Укажите папку нового бэкапа, доступную службе SQL Server', self.path_entry)
            return
        detail = (f"SQL Server: {connection['server']}\nИсточник: {source}\nЦелевая база: {target}\n"
                  f'Папка бэкапа на сервере: {folder}\n\nСначала создаётся новый FULL-бэкап источника. '
                  'Затем он автоматически восстанавливается в целевую базу.\n\n'
                  'Данные целевой базы будут заменены, её подключения завершены. Отдельной копии прежней '
                  'целевой базы не будет. Созданный бэкап источника сохранится.\n'
                  'После восстановления: SIMPLE → сжатие журнала получателя до 1 ГБ → FULL.\n'
                  + ('Проверка VERIFYONLY перед восстановлением включена.' if self.verify_restore.get() else
                     'Проверка VERIFYONLY перед восстановлением отключена.') + '\nПродолжить?')
        if not messagebox.askyesno('Бэкап + восстановление MSSQL', detail, parent=self.app.root, icon='warning', default='no'):
            return
        compression, copy_only, verify = self.compression.get(), self.copy_only.get(), self.verify_restore.get()
        if self.start():
            self.progress_state.complete_task = True
            self.append(f"Сервер: {connection['server']}\nИсточник: {source} → получатель: {target}")
            self.work(lambda event: backup_and_restore(connection, source, target, folder, event,
                                                       compression=compression, copy_only=copy_only, verify=verify),
                      lambda result: self.finish(f"База {target} восстановлена из {source}.\nБэкап сохранён: {result['backup_path']}"),
                      history=self.history_context('Бэкап + восстановление · SQL Server', source, target))


class PostgresCopyPanel(PostgresPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors)
        self.same_server = tk.BooleanVar(value=False)
        self.target_host = tk.StringVar(value='localhost')
        self.target_port = tk.StringVar(value='5432')
        self.target_user = tk.StringVar(value='postgres')
        self.target_password = tk.StringVar()
        self.target_ssl = tk.StringVar(value='prefer')
        self.target_filter = tk.StringVar()
        self.target_connected, self.target_databases = None, []
        group = tk.Frame(self.form, bg=colors['panel'])
        group.pack(fill=tk.X, before=self.path_label)
        saved, self.form = self.form, group
        self.target_caption = app._label(group, 'Получатель · отдельное подключение PostgreSQL', 9, 'muted',
                                         anchor=tk.W, justify=tk.LEFT, wraplength=280)
        self.target_caption.pack(fill=tk.X, pady=(10, 6))
        delayed_wrap(self.target_caption)
        self.same_server_check = self.check('Тот же сервер, что и у источника', self.same_server)
        self.target_connection_form = tk.Frame(group, bg=colors['panel'])
        self.target_connection_form.pack(fill=tk.X)
        self.form = self.target_connection_form
        self.target_host_entry = self.server_field('Сервер PostgreSQL · получатель', self.target_host)
        self.field('Порт получателя', self.target_port)
        self.field('Логин получателя', self.target_user)
        self.field('Пароль получателя', self.target_password, show='•')
        self.combo('SSL получателя', self.target_ssl, ('disable', 'prefer', 'require', 'verify-ca', 'verify-full'))
        self.target_connect_button = self.browse_button('Подключить получателя / список БД', self.connect_target)
        self.form = group
        self.target_refresh_button = self.browse_button('Обновить список получателя', self.refresh_target)
        self.target_create_button = self.browse_button('+ Создать базу получателя', self.create_target_database)
        self.target_delete_button = self.browse_button('Удалить базу получателя…', self.delete_target_database)
        self.target_delete_button.configure(fg=colors['danger'])
        self.note('Обе утилиты запускаются на одном компьютере: Windows либо выбранном Linux по SSH. '
                  'Этот компьютер должен видеть оба сервера PostgreSQL. Бэкап остаётся в указанной папке.')
        self.form = saved
        self.create_db_button.configure(text='+ Создать базу источника')
        self.delete_db_button.configure(text='Удалить базу источника…')
        self.connect_button.configure(text='Подключить источник / список БД')
        self.source_refresh_button = app._button(self.form, 'Обновить список источника', self.refresh_source)
        self.source_refresh_button.pack(fill=tk.X, after=self.connect_button, pady=(4, 4))
        self.inputs.append(self.source_refresh_button)
        self.pg_label.configure(text='Сервер PostgreSQL · источник')
        # Rebuild in the actual pane parents: geometry reparenting alone leaves
        # the old source widget behind the new opaque PanedWindow on Windows.
        self.filter_entry.master.grid_remove()
        self.selection_note.grid_remove()
        source_table = self.tree.master
        self.tree, self.target_tree, self.sorter, self.target_sorter = database_columns(
            self, source_table, self.filter_var, self.target_filter,
            (('name', 'База данных', 220), ('size', 'Размер', 100), ('owner', 'Владелец', 110)), ('size',))
        for variable in (self.target_host, self.target_port, self.target_user, self.target_password, self.target_ssl):
            variable.trace_add('write', self.target_changed)
        self.target_filter.trace_add('write', self.render_target)
        self._source_input_key, self._target_input_key = self.connection_key(), self.target_key()
        self.same_server.trace_add('write', self.same_server_changed)
        self.show_mode('pg_copy')
        install_new_copy_tab(self)

    def show_mode(self, mode):
        if not hasattr(self, 'target_host'):
            return super().show_mode(mode)
        # The initial pg_dump form is retained in this separate panel, including
        # its path, selections and log when navigation leaves and returns.
        self.mode = 'pg_copy'
        self.title.configure(text='Бэкап + восстановление · PostgreSQL')
        self.execute_button.configure(text='Бэкап + восстановление')
        self.tree.configure(selectmode='browse')
        self.selection_note.configure(text='Источник — слева, получатель — справа. Бэкап сохраняется после операции.')
        self.restore_options.pack(fill=tk.X, before=self.tools_form)
        self.format_widget.configure(values=('Архив .dump · pg_restore', 'Каталог .pgdir · параллельный бэкап'))

    def set_compact(self, compact):
        self.selection_note.grid_remove()
        compact_copy_title(self, compact)

    def render(self, *args):
        super().render(*args)
        if hasattr(self, 'source_count'):
            self.source_count.configure(text=f'{len(self.tree.get_children())} / {len(self.databases)} баз')

    def target_connection(self):
        if self.same_server.get():
            return dict(host=self.host.get().strip(), port=self.port.get().strip(),
                        username=self.username.get(), password=self.password.get(), sslmode=self.sslmode.get())
        return dict(host=self.target_host.get().strip(), port=self.target_port.get().strip(),
                    username=self.target_user.get(), password=self.target_password.get(), sslmode=self.target_ssl.get())

    def same_server_changed(self, *args):
        if self.same_server.get():
            self.target_connection_form.pack_forget()
            self.sync_target_list()
        else:
            self.target_connection_form.pack(fill=tk.X, before=self.target_refresh_button)
            self.target_changed()
        # A filter from another endpoint must not hide the newly shared list.
        self.target_filter.set('')
        self.update_target_caption()
        self.source_refresh_button.configure(text='Обновить оба списка' if self.same_server.get() else 'Обновить список источника')
        self.target_refresh_button.configure(text='Обновить оба списка' if self.same_server.get() else 'Обновить список получателя')

    def update_target_caption(self):
        settings = self.target_connection()
        self.target_caption.configure(text=(
            'Получатель · сервер источника ' + settings['host'] + ':' + settings['port'] +
            '\nПараметры подключения общие. Выберите разные базы в двух списках.' if self.same_server.get() else
            'Получатель · отдельное подключение PostgreSQL'))

    def sync_target_list(self):
        valid = self.connected == self.connection_key()
        self.target_connected = self.target_key() if valid else None
        self.target_databases = [dict(row) for row in self.databases] if valid else []
        self._target_input_key = self.target_key()
        self.render_target()

    def loaded(self, key, rows):
        super().loaded(key, rows)
        if self.same_server.get():
            self.sync_target_list()
        self.app.database_sizes.refresh(self)

    def target_key(self):
        settings = self.target_connection()
        # Source credentials and automatic client selection do not change the
        # target database server or invalidate an already loaded target list.
        config = self.connection()
        executor = tuple(config[key] for key in ('execution', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_key', 'ssh_use_key', 'ssh_password'))
        return executor, tuple(settings.items())

    def connection_key(self):
        # A client version switch leaves server/database metadata valid. Each
        # operation independently validates tools before altering a database.
        return super().connection_key()[:-1]

    def target_changed(self, *args):
        self.app.database_sizes.cancel(self, 'target')
        if self.same_server.get():
            self.sync_target_list()
            return
        self.target_connected, self.target_databases = None, []
        self._target_input_key = self.target_key()
        self.render_target()

    def connection_changed(self, *args):
        if not hasattr(self, 'target_tree'):
            return super().connection_changed(*args)
        source_key, target_key = self.connection_key(), self.target_key()
        if source_key != self._source_input_key:
            self.app.database_sizes.cancel(self, 'source')
        if source_key != self._source_input_key:
            self.connected, self.databases = None, []
            self.render()
        if target_key != self._target_input_key:
            self.target_changed()
        self._source_input_key, self._target_input_key = source_key, target_key
        self.update_target_caption()

    def render_target(self, *args):
        query_text = self.target_filter.get().strip().casefold()
        selection = self.target_tree.selection()
        selected = self.target_tree.set(selection[0], 'name') if selection else ''
        self.target_tree.delete(*self.target_tree.get_children())
        values = {}
        for item in self.target_databases:
            if hasattr(self.app, 'hub') and not self.app.hub.visible(self, 'target', item['name']):
                continue
            if query_text not in item['name'].casefold():
                continue
            row = self.target_tree.insert('', tk.END, values=(item['name'], format_size(item.get('size_bytes')), item.get('owner', '')))
            values[row] = dict(name=item['name'], size=item.get('size_bytes'), owner=item.get('owner', ''))
            if item['name'] == selected:
                self.target_tree.selection_set(row)
        self.target_sorter.apply(values)
        self.target_count.configure(text=f'{len(values)} / {len(self.target_databases)} баз')
        if hasattr(self.app, 'hub'):
            self.app.hub.restore_pending(self)

    def connect_target(self):
        if self.running:
            return
        if self.same_server.get():
            self.connect()
            return
        connection = dict(self.connection(), **self.target_connection())
        key = self.target_key()
        if self.start():
            self.work(lambda event: read_only_job(dict(mode='list', connection=connection, fast=True), event, runner=run_job),
                      lambda result: self.target_connection_loaded(key, connection, result))

    def target_connection_loaded(self, key, connection, rows):
        if self.confirm_ssh_host(rows, self.connect_target):
            return
        self.target_loaded(key, rows)
        self.app.saved_servers.remember(self, connection)

    def refresh_source(self):
        self.connect()

    def refresh_target(self):
        self.connect_target()

    def target_loaded(self, key, rows):
        if self.same_server.get():
            self.loaded(self.connection_key(), rows)
            return
        self.target_connected, self.target_databases = key, rows
        self.render_target()
        self.finish(f'Получатель подключён: {self.target_host.get()}:{self.target_port.get()}. Баз: {len(rows)}. Выберите целевую базу.')
        self.app.database_sizes.refresh(self)

    def create_new_database(self):
        self._database_action('source', 'create')

    def delete_selected_database(self):
        self._database_action('source', 'delete')

    def create_target_database(self):
        self._database_action('target', 'create')

    def delete_target_database(self):
        self._database_action('target', 'delete')

    def _database_action(self, side, action):
        if self.running:
            return
        source_connection = self.connection()
        target_connection = dict(source_connection, **self.target_connection())
        connection = source_connection if side == 'source' else target_connection
        role = 'Источник' if side == 'source' else 'Получатель'
        endpoint = connection['host'] + ':' + connection['port']
        execution = self.execution.get() + (f" · {connection['ssh_user']}@{connection['ssh_host']}" if self._execution != 'local' else '')
        if action == 'create':
            settings = ask_create_database(self.app.root, 'PostgreSQL', endpoint + ' · ' + role + ' · ' + execution,
                                           connection['username'], self.c)
            if not settings:
                return
            database = settings['name']
        else:
            tree = self.tree if side == 'source' else self.target_tree
            selection = tree.selection()
            connected = self.connected == self.connection_key() if side == 'source' else self.target_connected == self.target_key()
            if not connected or len(selection) != 1:
                self.app._panel_error(self, 'Подключите ' + role.lower() + ' и выберите базу в его списке для удаления')
                return
            database = tree.set(selection[0], 'name')
            if database in ('postgres', 'template0', 'template1'):
                self.app._panel_error(self, 'Удаление служебной базы PostgreSQL запрещено')
                return
            if not confirm_database_delete(self, 'PostgreSQL', endpoint, database, role, execution):
                return
        # Refresh each already connected side using its own credentials, even
        # when two different host names point to the same PostgreSQL server.
        refresh = {side: connection}
        shared = self.same_server.get()
        if not shared and side != 'source' and self.connected == self.connection_key():
            refresh['source'] = source_connection
        if not shared and side != 'target' and self.target_connected == self.target_key():
            refresh['target'] = target_connection
        def operation(event):
            payload = dict(mode=action, connection=connection, database=database)
            if action == 'create':
                payload['owner'] = settings['owner']
            run_job(payload, event)
            results = {}
            for key, config in refresh.items():
                try:
                    results[key] = (run_job(dict(mode='list', connection=config, fast=True), event), '')
                except Exception as exc:
                    results[key] = (None, str(exc))
            if shared:
                # One endpoint, one read. Both views receive the same result.
                results['source'] = results['target'] = results[side]
            return results
        if self.start():
            self.work(operation, lambda result: self.database_action_done(side, action, database, result),
                      history=self.history_context(('Создание' if action == 'create' else 'Удаление') + ' БД · PostgreSQL',
                        database, server=(connection['host'] + ':' + connection['port'])))

    def database_action_done(self, side, action, database, results):
        source_rows = results.get('source', (None, ''))[0]
        if source_rows is not None and self._execution == 'local' and self.client_set.get() == 'Автоматически':
            major = server_major(source_rows)
            if major in self.bundled and any(self.tool_values[name].get() != path for name, path in self.bundled[major].items()):
                self.apply_client(major)
        for key, (rows, error) in results.items():
            if rows is not None:
                if key == 'source':
                    self.databases, self.connected = rows, self.connection_key()
                else:
                    self.target_databases, self.target_connected = [dict(row) for row in rows], self.target_key()
            else:
                if key == 'source':
                    self.connected = None
                else:
                    self.target_connected = None
                # An unsuccessful read cannot prove an alias points to the
                # mutated server. Remove the known deleted row only on its side.
                if action == 'delete' and (key == side or self.same_server.get()):
                    if key == 'source':
                        self.databases = [item for item in self.databases if item['name'] != database]
                    else:
                        self.target_databases = [item for item in self.target_databases if item['name'] != database]
                self.append(('Источник' if key == 'source' else 'Получатель') +
                            ': список не обновлён: ' + error + '\nДействие уже выполнено; обновите подключение.')
        if action == 'create' and results.get(side, (None, ''))[0] is not None:
            (self.filter_var if side == 'source' else self.target_filter).set('')
        self.render()
        self.render_target()
        if action == 'create':
            tree = self.tree if side == 'source' else self.target_tree
            for row in tree.get_children():
                if tree.set(row, 'name') == database:
                    tree.selection_set(row)
                    tree.see(row)
                    break
        self.finish(('База создана: ' if action == 'create' else 'База удалена: ') + database)
        self.app.database_sizes.refresh(self)

    def choose_path(self):
        # A combined task always creates a new backup; never ask for an existing file.
        if self.running:
            return
        if self._execution != 'local':
            self.app._panel_error(self, 'Введите папку нового бэкапа на компьютере выполнения: /BACKUP для Linux или D:\\Backup для Windows')
            self.path_entry.focus_set()
            return
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=self.app.root, title='Папка нового бэкапа PostgreSQL')
        if path:
            self.path.set(path)

    def execute(self):
        if self.running:
            return
        sources, selection = self.selected(), self.target_tree.selection()
        if self.connected != self.connection_key():
            self.app._panel_error(self, 'Источник: подключение не установлено или его параметры изменены. Нажмите «Подключить источник / список БД».')
            return
        if self.target_connected != self.target_key():
            self.app._panel_error(self, 'Получатель: подключение не установлено или его параметры изменены. Нажмите «Подключить получателя / список БД».')
            return
        if len(sources) != 1:
            self.app._panel_error(self, 'Источник: выберите ровно одну базу для бэкапа.', self.tree)
            return
        if not selection:
            self.app._panel_error(self, 'Получатель: выберите базу для восстановления бэкапа.', self.target_tree)
            return
        source, destination = sources[0], self.target_tree.set(selection[0], 'name')
        settings = self.connection()
        target = self.target_connection()
        if (source == destination and settings['host'].casefold() == target['host'].casefold() and
                settings['port'] == target['port']):
            self.app._panel_error(self, 'Источник и получатель должны быть разными базами')
            return
        path = self.path.get().strip()
        if not path:
            self.app._panel_error(self, 'Укажите папку нового бэкапа на компьютере выполнения', self.path_entry)
            return
        if int(settings['dump_jobs']) > 1 and settings['format'] != 'directory':
            self.app._panel_error(self, 'Для нескольких потоков бэкапа выберите «Каталог .pgdir»')
            return
        settings['target_connection'] = target
        detail = (f"Выполнение: {self.execution.get()}\nИсточник: {settings['host']}:{settings['port']} / {source}\n"
                  f"Получатель: {target['host']}:{target['port']} / {destination}\nПапка нового бэкапа: {path}\n\n"
                  'Сначала создаётся бэкап источника, затем автоматически восстанавливается в выбранную целевую базу.\n'
                  'Целевая база будет удалена и создана заново, её прежние данные не возвращаются при ошибке. '
                  'Бэкап источника сохранится; отдельной копии прежней целевой базы не будет.\n')
        if int(settings['restore_jobs']) > 1:
            detail += f"Потоков восстановления: {settings['restore_jobs']}; без общей транзакции, возможны частичные данные при ошибке.\n"
        detail += 'Продолжить?'
        if not messagebox.askyesno('Бэкап + восстановление PostgreSQL', detail, parent=self.app.root, icon='warning', default='no'):
            return
        payload = dict(mode='copy', connection=settings, source_database=source, target_database=destination, path=path)
        if self.start():
            self.progress_state.complete_task = True
            self.append(f"Источник: {settings['host']} / {source}\nПолучатель: {target['host']} / {destination}")
            self.work(lambda event: run_job(payload, event),
                      lambda result: self.finish(f"База {destination} восстановлена из {source}.\nБэкап сохранён: {result['backup_path']}"),
                      history=self.history_context('Бэкап + восстановление · PostgreSQL', source,
                        target['host'] + ':' + target['port'] + ' / ' + destination))
