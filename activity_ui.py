"""Favorites, recent selections and operation history; applying a profile never runs a job."""
import tkinter as tk
import threading
import time
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from activity_store import context_key
from operation_progress import duration
from sql_server import format_size
from table_sorting import TableSorter
from log_colors import configure_log, insert_log
from ui_geometry import delayed_wrap
from ui_theme import RoundedSurface


class OperationLogWindow(tk.Toplevel):
    def __init__(self, app, row):
        super().__init__(app.root)
        self.app,self.row,self.offset,self.loading = app,row,0,False
        self.title('Журнал · '+row.get('operation','Операция'))
        self.geometry('1000x650')
        self.configure(bg=app.colors['panel'])
        self.transient(app.root)
        bar=tk.Frame(self,bg=app.colors['panel']);bar.pack(fill=tk.X,padx=12,pady=8)
        self.more=app._button(bar,'Ещё',self.load);self.more.pack(side=tk.RIGHT)
        app._button(bar,'Обновить',self.reload).pack(side=tk.RIGHT,padx=8)
        summary=app._label(bar,row.get('status','')+' · '+row.get('server','')+' / '+row.get('database',''),10,anchor=tk.W,justify=tk.LEFT)
        summary.pack(side=tk.LEFT,fill=tk.X,expand=True);delayed_wrap(summary)
        frame=tk.Frame(self);frame.pack(fill=tk.BOTH,expand=True,padx=12,pady=(0,12))
        frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.output=tk.Text(frame,wrap=tk.NONE,font=('Consolas',10),state=tk.DISABLED)
        configure_log(self.output);self.output.grid(row=0,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(frame,command=self.output.yview);vertical.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(frame,orient=tk.HORIZONTAL,command=self.output.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.output.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.load()

    def reload(self):
        if self.loading:return
        self.offset=0
        self.output.configure(state=tk.NORMAL);self.output.delete('1.0',tk.END);self.output.configure(state=tk.DISABLED)
        self.load()

    def load(self):
        if self.loading:return
        self.loading=True;self.more.configure(state=tk.DISABLED)
        offset=self.offset
        def read():
            try:
                if self.row.get('scheduled_run'):
                    rows=self.app.scheduler.store.journal(self.row['scheduled_run'],offset,limit=200)
                    text=''.join(datetime.fromtimestamp(row['at']).strftime('%Y-%m-%d %H:%M:%S')+'  '+row['text']+'\n' for row in rows)
                    result=(text or ('Журнал пуст или очищен.' if not offset else ''),rows[-1]['id'] if rows else offset,len(rows)==200)
                else:result=self.app.activity.logs.read(self.row['id'],offset)
            except Exception as exc:result=('Не удалось открыть журнал: '+str(exc),offset,False)
            self.app._post(self.loaded,result)
        threading.Thread(target=read,daemon=True).start()

    def loaded(self,result):
        if not self.winfo_exists():return
        text,self.offset,more=result
        self.loading=False
        self.output.configure(state=tk.NORMAL);insert_log(self.output,text);self.output.configure(state=tk.DISABLED)
        self.more.configure(state=tk.NORMAL if more else tk.DISABLED)


def endpoint(profile, role='source'):
    values = profile['values']
    if profile['kind'] == 'sql':
        return values.get('server', '')
    if profile['kind'] == 'cluster':
        return f"{values.get('server', '')}:{values.get('port', '')}"
    prefix = 'target_' if role == 'target' and not values.get('same_server', False) else ''
    return f"{values.get(prefix + 'host', '')}:{values.get(prefix + 'port', '')}"


def history_values(row):
    artifacts = row.get('artifacts', [])
    sizes = [item.get('size') for item in artifacts]
    size = sum(sizes) if sizes and all(isinstance(value, int) for value in sizes) else None
    path = '\n'.join(item['path'] for item in artifacts) or row.get('path', '')
    return (row.get('started', '').replace('T', ' ')[:19], row.get('operation', ''), row.get('server', ''),
            row.get('database', ''), row.get('target', ''), row.get('status', ''), duration(row.get('duration', 0)),
            format_size(size), path, row.get('executor', ''))


class ActivityHub:
    def __init__(self, app):
        self.app, self.store = app, app.activity
        self.pins, self.pending = {}, {}
        self.select_buttons, self.all_buttons, self.refresh_buttons = {}, {}, {}
        self.database_menus = {}
        self.windows = {}

    def profile(self, panel, include_passwords=False):
        if hasattr(panel, 'dbms'):
            target = panel.target()
            if not target:
                return None
            values = dict(
                server=target['server'], port=target['port'], version=target.get('version', ''),
                username=self.app.user_var.get(),
                cluster=(panel.selected_cluster() or {}).get('cluster', ''),
                ib_user=panel.ib_user.get(), db_server=panel.db_server.get(),
                dbms=panel.dbms.get(), db_user=panel.db_user.get())
            if include_passwords:
                values.update(password=self.app.pwd_var.get(), ib_password=panel.ib_password.get(), db_password=panel.db_password.get())
            return dict(kind='cluster', page='cluster_infobase', values=values)
        if hasattr(panel, 'host'):
            names = ('execution', 'host', 'port', 'username', 'sslmode', 'ssh_host', 'ssh_port',
                     'ssh_user', 'ssh_key', 'ssh_use_key', 'client_set', 'data_path', 'ib_user', 'manual_database', 'dt_keep_remote')
            if hasattr(panel, 'target_host'):
                names += ('target_host', 'target_port', 'target_user', 'target_ssl', 'same_server')
            if include_passwords:
                names += ('password', 'ib_password', 'ssh_password')
                if hasattr(panel, 'target_password'):
                    names += ('target_password',)
            return dict(kind='postgres', page=panel.mode,
                        values={name: getattr(panel, name).get() for name in names})
        if hasattr(panel, 'server'):
            names = ('server', 'username', 'windows_auth', 'encrypt', 'trust_certificate')
            if include_passwords:
                names += ('password',)
            return dict(kind='sql', page=panel.mode, values={name: getattr(panel, name).get() for name in names})
        return None

    def install(self, panel):
        self.app.saved_servers.install(panel)
        frame = tk.Frame(panel.form, bg=panel.c['panel'])
        frame.pack(fill=tk.X, before=panel.form.pack_slaves()[0], pady=(0, 6))
        save = self.app._button(frame, '☆ В избранное', lambda: self.save(panel))
        save.pack(side=tk.LEFT)
        recent = self.app._button(frame, 'Недавние базы ▾', lambda: self.recent_menu(panel, recent))
        recent.pack(side=tk.RIGHT)
        panel.inputs.extend((save, recent))
        trees = [(panel.tree, 'source')]
        if hasattr(panel, 'target_tree'):
            trees.append((panel.target_tree, 'target'))
        for tree, role in trees:
            tree.bind('<<TreeviewSelect>>', lambda event, p=panel, side=role: self.remember(p, side), add='+')
            tree.bind('<Double-1>', lambda event, p=panel, side=role: self.double_choose(p, side, event))
            self.database_menus[(panel, role)] = tk.Menu(tree, tearoff=False)
            tree.bind('<Button-3>', lambda event, p=panel, side=role: self.database_context(p, side, event))
            if hasattr(panel, 'source_count'):
                parent = (panel.target_count if role == 'target' else panel.source_count).master
            elif hasattr(panel, 'dbms'):
                parent = tk.Frame(panel.refresh_button.master, bg=panel.c['panel'])
                # The cluster header owns row 2 (search) and row 3 (address).
                # Keep selection actions on their own row so they cannot cover the entry.
                parent.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(6, 0))
            else:
                parent = panel.filter_entry.master
            before = parent.pack_slaves()
            all_button = self.app._button(parent, 'Все', lambda p=panel, side=role: self.unpin(p, side))
            select = self.app._button(parent, 'Выбрать', lambda p=panel, side=role: self.choose_selected(p, side))
            refresh = self.app._button(parent, 'Обновить', lambda p=panel, side=role: self.refresh_databases(p, side))
            for button in (all_button, select, refresh):
                button.configure(font=('Segoe UI', 9), padx=6, pady=3)
                button.pack(side=tk.RIGHT, padx=(4, 0), **({'before': before[0]} if before else {}))
            self.select_buttons[(panel, role)], self.all_buttons[(panel, role)] = select, all_button
            self.refresh_buttons[(panel, role)] = refresh
            panel.inputs.extend((select, all_button, refresh))

    def database_context(self, panel, role, event):
        tree = panel.target_tree if role == 'target' else panel.tree
        row = tree.identify_row(event.y)
        if not row:
            return 'break'
        tree.selection_set(row)
        tree.focus(row)
        tree.focus_set()
        name = tree.set(row, 'name')
        menu = self.database_menus[(panel, role)]
        menu.delete(0, tk.END)
        menu.add_command(label='Удалить…', state=tk.DISABLED if panel.running else tk.NORMAL,
                         command=lambda: self.database_context_action(panel, role, row, name, 'delete'))
        menu.add_command(label='Скопировать название',
                         command=lambda: self.database_context_action(panel, role, row, name, 'copy'))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def database_context_action(self, panel, role, row, name, action):
        tree = panel.target_tree if role == 'target' else panel.tree
        if not tree.exists(row) or tree.set(row, 'name') != name:
            self.app._panel_error(panel, 'Список баз изменился. Откройте меню базы заново.')
            return
        if action == 'copy':
            # Clipboard access occurs only after the user's explicit menu command.
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(name)
            self.app._panel_status(panel, 'Название базы скопировано: ' + name)
        elif not panel.running:
            tree.selection_set(row)
            if hasattr(panel, 'dbms'):
                panel.delete_selected()
            elif role == 'target':
                panel.delete_target_database()
            else:
                panel.delete_selected_database()

    def refresh_databases(self, panel, role):
        if panel.running:
            return
        if hasattr(panel, 'dbms'):
            (panel.load_bases if panel.selected_cluster() else panel.load_clusters)()
        elif role == 'target' and hasattr(panel, 'refresh_target'):
            panel.refresh_target()
        elif role == 'source' and hasattr(panel, 'refresh_source'):
            panel.refresh_source()
        else:
            panel.connect()

    def double_choose(self, panel, role, event):
        if not panel.running:
            tree = panel.target_tree if role == 'target' else panel.tree
            item = tree.identify_row(event.y)
            if item:
                tree.selection_set(item)
                self.choose_selected(panel, role)
        return 'break'

    def choose_selected(self, panel, role):
        tree = panel.target_tree if role == 'target' else panel.tree
        selection = tree.selection()
        if len(selection) != 1:
            self.app._panel_error(panel, 'Сначала выделите одну базу в нужном списке и нажмите «Выбрать».', tree)
            return
        self.choose_database(panel, role, tree.set(selection[0], 'name'))

    def save(self, panel):
        if panel.running:
            return
        profile = self.profile(panel, include_passwords=True)
        if not profile or not endpoint(profile).strip(':'):
            self.app._status('Сначала укажите сервер подключения')
            return
        name = simpledialog.askstring('Избранное подключение', 'Название подключения:',
                                      initialvalue=endpoint(profile), parent=self.app.root)
        if name and name.strip():
            try:
                self.store.save_favorite(name, profile)
            except ValueError as exc:
                self.app._status(str(exc))
                return
            self.app.home.refresh()
            self.app._status('Подключение и пароли сохранены в избранном. Пароли защищены Windows DPAPI.')

    def apply(self, profile, recent=None):
        current = self.app._current_panel()
        panel = current if self.app.mode == profile['page'] and current is not None else self.app._pages.get(profile['page'])
        if panel is not None and panel.running:
            self.app._status('В этой вкладке выполняется операция. Откройте новую вкладку для другого подключения.')
            return False
        try:
            credentials = self.store.credentials(profile)
        except ValueError as exc:
            self.app._status(str(exc))
            return False
        if panel is not current:
            self.app._switch_mode(profile['page'])
            panel = self.app._current_panel()
        values = profile['values']
        if profile['kind'] == 'ssh':
            self.app.saved_servers.apply_ssh(panel, values, credentials)
        elif profile['kind'] == 'cluster':
            target = {key: values.get(key, '') for key in ('server', 'port', 'version')}
            index = next((i for i, item in enumerate(panel.targets)
                          if panel.target_key(item) == panel.target_key(target)), None)
            if index is None:
                panel.targets.append(target)
                panel.server_combo.configure(values=[f"{item['server']}:{item['port']}" for item in panel.targets])
                index = len(panel.targets) - 1
            panel.server_combo.current(index)
            self.app.user_var.set(values.get('username', ''))
            self.app.pwd_var.set(credentials.get('password', ''))
            for key in ('db_server', 'dbms', 'db_user', 'ib_user'):
                if key in values:
                    getattr(panel, key).set(values[key])
            panel.server_changed(values.get('cluster', ''))
            panel.ib_password.set(credentials.get('ib_password', ''))
            panel.db_password.set(credentials.get('db_password', ''))
        else:
            if hasattr(panel, 'same_server'):
                panel.same_server.set(False)
            if hasattr(panel, 'dt_keep_remote'):
                panel.dt_keep_remote.set(False)
            panel.password.set('')
            if hasattr(panel, 'ib_password'):
                panel.ib_password.set('')
            if hasattr(panel, 'target_password'):
                panel.target_password.set('')
            if 'execution' in values:
                panel.execution.set(values['execution'])
            for key, value in values.items():
                if key not in ('execution', 'same_server') and hasattr(panel, key):
                    getattr(panel, key).set(value)
            for key in ('password', 'ib_password', 'target_password', 'ssh_password'):
                if hasattr(panel, key):
                    getattr(panel, key).set(credentials.get(key, ''))
            if hasattr(panel, 'same_server'):
                panel.same_server.set(values.get('same_server', False))
        if recent:
            self.pending[(panel, recent['role'])] = (recent['context'], recent['database'])
            self.restore_pending(panel)
        self.app._status('Параметры SSH заполнены. Нажмите «Проверить SSH» или запустите нужную операцию.' if profile['kind']=='ssh' else
                         'Параметры и сохранённые пароли заполнены. Обновите список баз.' if credentials else
                         'Параметры заполнены. Введите пароль при необходимости и обновите список баз.')
        return True

    def remember(self, panel, role):
        profile = self.profile(panel)
        if not profile:
            return
        tree = panel.target_tree if role == 'target' else panel.tree
        if hasattr(panel, 'dbms'):
            valid = panel.cache_key()
        elif role == 'target' and hasattr(panel, 'target_connected'):
            valid = panel.target_connected == panel.target_key()
        elif hasattr(panel, 'connection_key'):
            valid = panel.connected == panel.connection_key()
        else:
            valid = panel.connected == panel.connection()
        if valid:
            for item in reversed(tree.selection()[:20]):
                name = tree.set(item, 'name')
                self.store.remember(profile, name, role)

    def restore_pending(self, panel):
        for role in ('source', 'target'):
            pending = self.pending.get((panel, role))
            tree = getattr(panel, 'target_tree', None) if role == 'target' else getattr(panel, 'tree', None)
            profile = self.profile(panel)
            if not pending or tree is None or not profile or context_key(profile, role) != pending[0]:
                continue
            for item in tree.get_children():
                if tree.set(item, 'name') == pending[1]:
                    tree.selection_set(item)
                    tree.see(item)
                    del self.pending[(panel, role)]
                    break

    def recent_menu(self, panel, button):
        menu = tk.Menu(button, tearoff=False)
        profile = self.profile(panel)
        for role, label in (('source', 'Источник'), ('target', 'Получатель')):
            if role == 'target' and not hasattr(panel, 'target_tree'):
                continue
            key = context_key(profile, role) if profile else ''
            rows = [item for item in self.store.snapshot('recent') if item['context'] == key and item['role'] == role][:12]
            target = menu
            if hasattr(panel, 'target_tree'):
                target = tk.Menu(menu, tearoff=False)
                menu.add_cascade(label=label, menu=target)
            if not rows:
                target.add_command(label='Нет недавних баз для этого подключения', state=tk.DISABLED)
            for row in rows:
                target.add_command(label=row['database'], command=lambda item=row: self.choose_database(panel, item['role'], item['database']))
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def visible(self, panel, role, database):
        key = (panel, role)
        pin = self.pins.get(key)
        if not pin:
            return True
        profile = self.profile(panel)
        variable = getattr(panel, 'target_filter', None) if role == 'target' else getattr(panel, 'filter_var', None)
        if not profile or context_key(profile, role) != pin[0] or (variable and variable.get() != pin[1]):
            self.pins.pop(key, None)
            return True
        return database == pin[1]

    def choose_database(self, panel, role, database):
        if panel.running:
            return
        tree = panel.target_tree if role == 'target' else panel.tree
        # Only isolate a database that is in the current server's loaded metadata.
        rows = panel.target_databases if role == 'target' and hasattr(panel, 'target_databases') else (
            panel.bases if hasattr(panel, 'dbms') else panel.databases)
        if not any(row.get('name') == database for row in rows):
            self.app._status('Базы нет в текущем списке. Обновите подключение.')
            return
        profile = self.profile(panel)
        self.pins[(panel, role)] = (context_key(profile, role), database)
        variable = getattr(panel, 'target_filter', None) if role == 'target' else getattr(panel, 'filter_var', None)
        if variable is not None:
            variable.set(database)
        else:
            self.render_side(panel, role)
        for item in tree.get_children():
            if tree.set(item, 'name') == database:
                tree.selection_set(item)
                tree.see(item)
        self.app._status('В списке оставлена база ' + database + '. Кнопка «Все» возвращает полный список.')

    def render_side(self, panel, role):
        if hasattr(panel, 'dbms'):
            panel.render_bases(panel.bases)
        else:
            (panel.render_target if role == 'target' else panel.render)()

    def unpin(self, panel, role):
        if panel.running:
            return
        self.pins.pop((panel, role), None)
        variable = getattr(panel, 'target_filter', None) if role == 'target' else getattr(panel, 'filter_var', None)
        if variable is not None:
            variable.set('')
        else:
            self.render_side(panel, role)

    def dialog(self, kind):
        existing = self.windows.get(kind)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.refresh()
            return existing
        window = ActivityDialog(self.app, kind)
        self.windows[kind] = window
        return window


class HomePanel(tk.Frame):
    running = False

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.colors['bg'])
        self.app, self._revision = app, -1
        self._scheduled,self._schedule_reading,self._schedule_at = [],False,0
        self._poll_job=None
        self._schedule_generation=0
        self.bind('<Destroy>',self.destroyed,add='+')
        quick = tk.Frame(self, bg=app.colors['bg'])
        quick.pack(fill=tk.X, padx=22, pady=(0, 10))
        app._label(quick, 'Рабочий обзор', 18, bold=True).pack(side=tk.LEFT, padx=(0, 18))
        for title, mode in (('SQL Server', 'db_backup'), ('PostgreSQL', 'pg_dump'), ('Кластер 1С', 'cluster_infobase')):
            app._button(quick, title, lambda page=mode: app._switch_mode(page)).pack(side=tk.RIGHT, padx=(6, 0))
        self.metrics = {}
        self.metric_strip = tk.Frame(self, bg=app.colors['bg'])
        self.metric_strip.pack(fill=tk.X, padx=22, pady=(3, 16))
        for column, (key, title, symbol, color, background) in enumerate((
                ('favorites', 'Подключения', '☆', app.colors['accent'], app.colors['selected']),
                ('running', 'Активные задачи', '↗', '#149b79', '#e5f6ef'),
                ('today', 'Операции сегодня', '≡', '#326dd5', '#e9f0ff'),
                ('errors', 'Требуют внимания', '!', '#ce465f', '#ffedf1'))):
            self.metric_strip.columnconfigure(column, weight=1, uniform='metrics')
            surface = RoundedSurface(self.metric_strip, app.colors, padding=14, fit_height=True)
            surface.grid(row=0, column=column, sticky='nsew', padx=(0, 10) if column < 3 else 0)
            card = surface.body
            icon = tk.Label(card, text=symbol, font=('Segoe UI', 17, 'bold'),
                            bg=background, fg=color, width=2, padx=3, pady=2)
            icon.pack(side=tk.LEFT, padx=(0, 12))
            info = tk.Frame(card, bg=app.colors['panel'])
            info.pack(side=tk.LEFT, fill=tk.X, expand=True)
            app._label(info, title, 9, 'muted', anchor='w').pack(fill=tk.X)
            value = app._label(info, '0', 22, bold=True, anchor='w')
            value.pack(fill=tk.X)
            self.metrics[key] = value
        split = tk.PanedWindow(self, orient=tk.VERTICAL, bg=app.colors['border'], bd=0, sashwidth=7, opaqueresize=False)
        split.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        top = tk.Frame(split, bg=app.colors['bg'])
        top.columnconfigure((0, 1), weight=1, uniform='home')
        top.rowconfigure(0, weight=1)
        split.add(top, minsize=100, height=230, stretch='always')
        self.tables = {}
        for column, (section, title, columns) in enumerate((
                ('favorites', 'Избранные подключения', [('name', 'Подключение', 180), ('server', 'Сервер', 180)]),
                ('recent', 'Последние выбранные базы', [('name', 'База', 180), ('server', 'Сервер', 160)]))):
            surface = RoundedSurface(top, app.colors)
            surface.grid(row=0, column=column, sticky='nsew', padx=(0, 6) if column == 0 else (6, 0))
            card = surface.body
            app._label(card, title, 11, bold=True).pack(anchor=tk.W, pady=(0, 6))
            tree = self.make_table(card, columns, height=5)
            tree.bind('<Double-1>', lambda event, s=section: self.open_row(s))
            tree.bind('<Return>', lambda event, s=section: self.open_row(s))
            tree.bind('<<TreeviewSelect>>', lambda event: self.update_details())
            self.tables[section] = tree
        surface = RoundedSurface(split, app.colors)
        split.add(surface, minsize=100, height=240, stretch='always')
        bottom = surface.body
        header = tk.Frame(bottom, bg=app.colors['panel'])
        header.pack(fill=tk.X)
        app._label(header, 'Последние операции', 11, bold=True).pack(side=tk.LEFT)
        app._button(header, 'Вся история', lambda: app.hub.dialog('history')).pack(side=tk.RIGHT)
        self.clear_history_button=app._button(header,'Очистить',self.clear_history)
        self.clear_history_button.pack(side=tk.RIGHT,padx=6)
        self.open_log_button=app._button(header,'Открыть журнал',self.open_log)
        self.open_log_button.pack(side=tk.RIGHT)
        self.tables['history'] = self.make_table(bottom, [('date', 'Начало', 135), ('operation', 'Операция', 170),
            ('server', 'Сервер', 140), ('database', 'База', 160), ('result', 'Результат', 100),
            ('duration', 'Время', 65), ('size', 'Размер', 90)], height=5)
        self.tables['history'].bind('<<TreeviewSelect>>', lambda event: self.update_details())
        self.tables['history'].bind('<Double-1>',lambda event:self.open_log())
        self.tables['history'].bind('<Return>',lambda event:self.open_log())
        self.detail = app._label(self, 'Сохраните подключение кнопкой «☆ В избранное» в форме SQL Server, PostgreSQL или 1С.',
                                  9, 'muted', anchor=tk.W)
        self.detail.pack(fill=tk.X, padx=22, pady=(0, 8))
        self.rows = {}

    def make_table(self, parent, columns, height):
        frame = tk.Frame(parent, bg=self.app.colors['panel'])
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[key for key, _, _ in columns], show='headings', height=height, selectmode='browse')
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=55, stretch=True)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.tag_configure('even', background=self.app.colors['panel'])
        tree.tag_configure('odd', background=self.app.colors['row'])
        return tree

    def show_mode(self, mode):
        self.refresh()
        if self._poll_job is None:self._poll_job=self.after(1500,self.poll)

    def poll(self):
        self._poll_job=None
        if self.winfo_ismapped():
            self.refresh()
            self._poll_job=self.after(1500,self.poll)

    def destroyed(self,event):
        if event.widget is self and self._poll_job:
            self.after_cancel(self._poll_job);self._poll_job=None

    def load_scheduled(self):
        scheduler=getattr(self.app,'scheduler',None)
        if self._schedule_reading or not scheduler:return
        has_scheduler=scheduler.store.path.is_file()
        if not has_scheduler and not self.app.activity.logs.directory:return
        if time.monotonic()<self._schedule_at:return
        self._schedule_reading=True;self._schedule_at=time.monotonic()+3
        generation=self._schedule_generation
        def read():
            try:
                self.app.activity.prune_history()
                if has_scheduler:
                    scheduler.ensure_store()
                    scheduler.store.prune_history()
                    runs,_=scheduler.store.history(12)
                else:runs=[]
                rows=[dict(id=run['id'],scheduled_run=run['id'],operation='По расписанию: '+run['name'],
                    started=datetime.fromtimestamp(run['started']).astimezone().isoformat(),status=run['status'],
                    duration=max(0,(run['finished'] or time.time())-run['started']),executor='Планировщик') for run in runs]
                if time.time()-self.app.activity.logs._last_prune>600:self.app.activity.logs.prune()
            except Exception:rows=[]
            self.app._post(self.scheduled_loaded,rows,generation)
        threading.Thread(target=read,daemon=True).start()

    def scheduled_loaded(self,rows,generation=None):
        if not self.winfo_exists():return
        if generation is not None and generation!=self._schedule_generation:return
        self._schedule_reading=False
        if rows!=self._scheduled:
            self._scheduled=rows;self._revision=-1;self.refresh()

    def refresh(self):
        self.load_scheduled()
        self.refresh_metrics()
        if self._revision == self.app.activity.revision:
            return
        self._revision = self.app.activity.revision
        for section, tree in self.tables.items():
            tree.delete(*tree.get_children())
            rows = self.app.activity.snapshot(section, limit=12)
            if section=='history':rows=sorted(rows+self._scheduled,key=lambda row:row.get('started',''),reverse=True)[:12]
            self.rows[section] = rows
            for index, row in enumerate(rows):
                if section == 'history':
                    values = history_values(row)
                    values = (*values[:4], values[5], values[6], values[7])
                else:
                    profile = row if section == 'favorites' else row['profile']
                    values = (row['name'] if section == 'favorites' else row['database'], endpoint(profile, row.get('role', 'source')))
                tree.insert('', tk.END, iid=str(index), values=values, tags=('odd' if index % 2 else 'even',))
            if not rows:
                tree.insert('', tk.END, iid='empty', values=('Пока нет записей',))

    def refresh_metrics(self):
        # The store supplies bounded metadata snapshots, never database queries.
        history = self.app.activity.snapshot('history') + self._scheduled
        today = datetime.now().astimezone().date().isoformat()
        recent = [row for row in history if row.get('started', '')[:10] == today]
        pages = set(getattr(self.app, '_pages', {}).values())
        running = sum(bool(getattr(panel, 'running', False)) for panel in pages)
        scheduler = getattr(self.app, 'scheduler', None)
        running += bool(scheduler and scheduler.active_task)
        values = dict(favorites=len(self.app.activity.snapshot('favorites')), running=running,
                      today=len(recent), errors=sum('ошиб' in row.get('status', '').casefold() for row in recent))
        for key, value in values.items():
            text = str(value)
            if self.metrics[key].cget('text') != text:
                self.metrics[key].configure(text=text)

    def open_row(self, section):
        selection = self.tables[section].selection()
        if selection and selection[0] != 'empty':
            row = self.rows[section][int(selection[0])]
            self.app.hub.apply(row if section == 'favorites' else row['profile'], row if section == 'recent' else None)

    def update_details(self):
        tree = self.tables['history']
        if tree.selection() and tree.selection()[0] != 'empty':
            row = self.rows['history'][int(tree.selection()[0])]
            self.detail.configure(text=history_values(row)[8] or 'Для этой операции путь бэкапа не указан')

    def open_log(self):
        selection=self.tables['history'].selection()
        if selection and selection[0]!='empty':return OperationLogWindow(self.app,self.rows['history'][int(selection[0])])

    def clear_history(self):
        if not messagebox.askyesno('Очистить историю и журналы?',
            'Удалить завершённые записи и их логи? Выполняющиеся задачи, базы и бэкапы сохранятся.',parent=self.app.root,default='no'):return
        self._schedule_generation+=1
        self._schedule_reading=False
        def clear():
            try:
                self.app.activity.clear_history()
                scheduler=self.app.scheduler
                if scheduler.store.path.is_file():scheduler.ensure_store();scheduler.store.prune_history(clear=True)
                self.app._post(self.cleared)
            except Exception as exc:self.app._post(self.app._panel_error,self,str(exc))
        threading.Thread(target=clear,daemon=True).start()

    def cleared(self):
        if not self.winfo_exists():return
        self._scheduled=[];self._revision=-1;self._schedule_at=0;self.refresh()

    def set_compact(self, compact):
        self.detail.pack_forget() if compact else self.detail.pack(fill=tk.X, padx=22, pady=(0, 8))
        if compact:
            self.metric_strip.pack_forget()
        elif not self.metric_strip.winfo_manager():
            self.metric_strip.pack(fill=tk.X, padx=22, pady=(3, 16), after=self.winfo_children()[0])


class ActivityDialog(tk.Toplevel):
    def __init__(self, app, kind):
        super().__init__(app.root)
        self.app, self.kind = app, kind
        self.title('Избранные подключения' if kind == 'favorites' else 'История операций')
        self.geometry('1080x600')
        self.minsize(640, 340)
        self.configure(bg=app.colors['bg'])
        self.transient(app.root)
        bar = tk.Frame(self, bg=app.colors['bg'])
        bar.pack(fill=tk.X, padx=14, pady=10)
        self.filter = tk.StringVar()
        app._entry(bar, self.filter).pack(side=tk.LEFT, fill=tk.X, expand=True)
        app._button(bar, 'Обновить', self.refresh).pack(side=tk.LEFT, padx=6)
        app._button(bar, 'Копировать', self.copy).pack(side=tk.LEFT)
        if kind == 'favorites':
            app._button(bar, 'Открыть', self.open_selected, primary=True).pack(side=tk.LEFT, padx=6)
            app._button(bar, 'Переименовать', self.rename).pack(side=tk.LEFT)
            app._button(bar, 'Убрать', self.remove).pack(side=tk.LEFT, padx=6)
            columns = [('name', 'Подключение', 180), ('kind', 'Тип', 100), ('server', 'Сервер', 180), ('user', 'Пользователь', 160)]
        else:
            app._button(bar, 'Очистить историю', self.clear).pack(side=tk.LEFT, padx=6)
            app._button(bar,'Открыть журнал',self.open_log).pack(side=tk.LEFT)
            columns = [('date', 'Начало', 150), ('operation', 'Операция', 180), ('server', 'Сервер', 150),
                       ('database', 'База', 180), ('target', 'Получатель', 160), ('result', 'Результат', 110),
                       ('duration', 'Время', 70), ('size', 'Размер', 90), ('path', 'Путь бэкапа', 350), ('executor', 'Выполнение', 150)]
        detail_frame = tk.Frame(self, bg=app.colors['panel'])
        detail_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=10)
        detail_frame.columnconfigure(0, weight=1)
        self.detail = tk.Text(detail_frame, height=5, font=('Consolas', 10), relief='flat', wrap=tk.NONE,
                              bg=app.colors['panel'], fg=app.colors['fg'], state=tk.DISABLED)
        self.detail.grid(row=0, column=0, sticky='nsew')
        detail_vertical = ttk.Scrollbar(detail_frame, command=self.detail.yview)
        detail_vertical.grid(row=0, column=1, sticky='ns')
        detail_horizontal = ttk.Scrollbar(detail_frame, orient=tk.HORIZONTAL, command=self.detail.xview)
        detail_horizontal.grid(row=1, column=0, sticky='ew')
        self.detail.configure(yscrollcommand=detail_vertical.set, xscrollcommand=detail_horizontal.set)
        self.tree = HomePanel.make_table(self, self, columns, 12)
        self.sorter = TableSorter(self.tree, numeric=('duration', 'size'), colors=app.colors)
        self.tree.bind('<<TreeviewSelect>>', lambda event: self.show_details())
        if kind == 'favorites':
            self.tree.bind('<Double-1>', lambda event: self.open_selected())
        else:self.tree.bind('<Double-1>',lambda event:self.open_log())
        self.filter.trace_add('write', lambda *args: self.refresh())
        self.refresh()

    def refresh(self):
        self.rows = {}
        self.tree.delete(*self.tree.get_children())
        query = self.filter.get().casefold().strip()
        values = {}
        for index, row in enumerate(self.app.activity.snapshot(self.kind)):
            if self.kind == 'favorites':
                value = (row['name'], row['kind'], endpoint(row), row['values'].get('username', row['values'].get('ssh_user', row['values'].get('user',''))))
            else:
                value = history_values(row)
            if query and query not in ' '.join(map(str, value)).casefold():
                continue
            key = str(index)
            self.rows[key] = row
            self.tree.insert('', tk.END, iid=key, values=value)
            values[key] = dict(zip(self.tree['columns'], value))
            if self.kind == 'history':
                sizes = [item.get('size') for item in row.get('artifacts', [])]
                values[key].update(duration=row.get('duration', 0),
                    size=sum(sizes) if sizes and all(isinstance(size, int) for size in sizes) else None)
        self.sorter.apply(values)
        self.show_details()

    def selected(self):
        selection = self.tree.selection()
        return self.rows.get(selection[0]) if selection else None

    def show_details(self):
        row = self.selected()
        if row and self.kind == 'favorites':
            protected = 'Пароли сохранены с защитой Windows DPAPI.' if row.get('credentials') else 'Пароли не сохранены.'
            text = row['name'] + '\n' + endpoint(row) + '\n' + protected + ' Открытие только заполняет форму подключения.'
        elif row:
            text = '\n'.join(f'{key}: {value}' for key, value in zip(
                ('Начало', 'Операция', 'Сервер', 'База', 'Получатель', 'Результат', 'Длительность', 'Размер', 'Путь', 'Выполнение'), history_values(row)))
        else:
            text = 'Выберите строку. «—» в размере означает, что размер не удалось определить.'
        self.detail.configure(state=tk.NORMAL)
        self.detail.delete('1.0', tk.END)
        self.detail.insert('1.0', text)
        self.detail.configure(state=tk.DISABLED)

    def copy(self):
        if self.selected():
            text = self.detail.get('1.0', 'end-1c')
            self.clipboard_clear()
            self.clipboard_append(text)

    def open_selected(self):
        row = self.selected()
        if row and self.app.hub.apply(row):
            self.destroy()

    def open_log(self):
        row=self.selected()
        if row:return OperationLogWindow(self.app,row)

    def rename(self):
        row = self.selected()
        if row:
            name = simpledialog.askstring('Избранное', 'Новое название:', initialvalue=row['name'], parent=self)
            if name and name.strip():
                self.app.activity.save_favorite(name, row, row['id'])
                self.refresh()
                self.app.home.refresh()

    def remove(self):
        row = self.selected()
        if row:
            self.app.activity.remove_favorite(row['id'])
            self.refresh()
            self.app.home.refresh()

    def clear(self):
        if messagebox.askyesno('Очистить историю?', 'Удалить завершённые записи? Базы и файлы бэкапов не изменяются.', parent=self, default='no'):
            self.app.activity.clear_history()
            self.refresh()
            self.app.home.refresh()
