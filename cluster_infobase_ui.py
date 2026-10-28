"""The MMC infobase creation fields with a server/cluster picked from the console."""
import tkinter as tk
from tkinter import messagebox, ttk

from add_cluster_infobase import ClusterClient, DBMS, LOCALES, SECURITY, validate_parameters
from administration_ui import JobPanel
from table_sorting import TableSorter
from ui_geometry import delayed_wrap
from infobase_address import infobase_address


class ClusterInfobasePanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Добавить базу в кластер 1С', 'Добавить базу')
        self.enable_cancellation()
        self.targets, self.clusters, self.bases = [], [], []
        self.cluster_cache, self.base_cache = {}, {}
        self._jobs_pending = ''
        self.server = tk.StringVar()
        self.cluster = tk.StringVar()
        self.ib_user, self.ib_password = tk.StringVar(), tk.StringVar()
        self.name, self.description = tk.StringVar(), tk.StringVar()
        self.security = tk.StringVar(value='выключено')
        self.db_server = tk.StringVar()
        self.dbms = tk.StringVar(value='Microsoft SQL Server')
        self.db_name, self.db_user, self.db_password = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.licenses = tk.StringVar(value='Да')
        self.locale = tk.StringVar(value='русский (Россия)')
        self.date_offset = tk.StringVar(value='2000')
        self.dbms.trace_add('write', lambda *args: self.date_offset.set(
            '2000' if self.dbms.get() == 'Microsoft SQL Server' else '0'))
        self.create_db, self.deny_jobs = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.server_combo = self.combo('Сервер 1С · из списка серверов консоли', self.server, ())
        self.server_combo.bind('<<ComboboxSelected>>', lambda event: self.server_changed())
        self.cluster_combo = self.combo('Кластер 1С', self.cluster, ())
        self.cluster_combo.bind('<<ComboboxSelected>>', lambda event: self.cluster_changed())
        self.auth_note = app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT, wraplength=280)
        self.auth_note.pack(fill=tk.X, pady=(8, 0))
        delayed_wrap(self.auth_note)
        self.note('Кластер выбирается в списке выше. Логин и пароль администратора кластера берутся из раздела «ИБ». Без администратора оставьте их пустыми.')
        app._label(self.form, 'Параметры информационной базы', 12, bold=True).pack(anchor=tk.W, pady=(12, 0))
        self.name_entry = self.field('Имя', self.name)
        self.field('Описание', self.description)
        self.combo('Защищённое соединение', self.security, tuple(SECURITY))
        self.field('Сервер баз данных', self.db_server)
        self.combo('Тип СУБД', self.dbms, tuple(DBMS))
        self.field('База данных', self.db_name)
        self.field('Пользователь сервера БД', self.db_user)
        self.field('Пароль пользователя БД', self.db_password, show='•')
        self.combo('Разрешить выдачу лицензий сервером 1С:Предприятия', self.licenses, ('Да', 'Нет'))
        self.combo('Язык (страна)', self.locale, tuple(LOCALES), editable=True)
        self.combo('Смещение дат', self.date_offset, ('0', '2000'))
        self.check('Создать базу данных в случае отсутствия', self.create_db)
        self.check('Установить блокировку регламентных заданий', self.deny_jobs)
        self.ib_auth_button = app._button(self.form, 'Авторизация ИБ…', self.toggle_ib_auth)
        self.ib_auth_button.pack(fill=tk.X, pady=(8, 0))
        self.inputs.append(self.ib_auth_button)
        self.ib_auth_form = tk.Frame(self.form, bg=colors['panel'])
        saved = self.form
        self.form = self.ib_auth_form
        self.field('Пользователь ИБ · если требуется для настройки', self.ib_user)
        self.field('Пароль пользователя ИБ', self.ib_password, show='•')
        self.note('Администратор информационной базы 1С. Это не пользователь СУБД и не администратор кластера. Используется для настройки заданий и удаления регистрации ИБ.')
        self.form = saved
        self.note('Сервер СУБД должен быть доступен с сервера 1С. Пароль БД используется для подключения 1С к СУБД.')
        header = tk.Frame(self.right, bg=colors['panel'])
        header.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        header.columnconfigure(0, weight=1)
        app._label(header, 'Информационные базы', 12, bold=True).grid(row=0, column=0, sticky='w')
        self.refresh_button = app._button(header, 'Обновить список', self.load_clusters)
        self.refresh_button.grid(row=0, column=1, sticky='e')
        self.inputs.append(self.refresh_button)
        actions = tk.Frame(header, bg=colors['panel'])
        actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.delete_button = app._button(actions, 'Удалить из кластера…', self.delete_selected)
        self.delete_button.configure(fg=colors['danger'])
        self.delete_button.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.jobs_button = app._button(actions, 'Применить флажок заданий', self.apply_jobs)
        self.jobs_button.grid(row=0, column=1, sticky='ew')
        self.inputs.extend((self.delete_button, self.jobs_button))
        table = self.split_log()
        self.tree = ttk.Treeview(table, columns=('name', 'descr', 'jobs'), show='headings', selectmode='browse', height=3)
        for key, title, width in (('name', 'Информационная база', 200), ('descr', 'Описание', 190), ('jobs', 'Блокировка заданий', 170)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=80, stretch=True)
        self.sorter = TableSorter(self.tree, colors=colors)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.context_note = app._label(self.right, 'Выберите сервер и обновите кластеры', 10, 'muted', anchor=tk.W,
                                      wraplength=500, justify=tk.LEFT)
        self.context_note.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        delayed_wrap(self.context_note)
        address_bar = tk.Frame(header, bg=colors['panel'])
        address_bar.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        address_bar.columnconfigure(0, weight=1)
        self.address = tk.StringVar()
        self.address_entry = app._entry(address_bar, self.address)
        self.address_entry.configure(state='readonly')
        self.address_entry.grid(row=0, column=0, sticky='ew')
        self.address_copy_button = app._button(address_bar, 'Копировать адрес', self.copy_address)
        self.address_copy_button.configure(font=('Segoe UI', 9), padx=7, pady=3)
        self.address_copy_button.grid(row=0, column=1, padx=(6, 0))
        self.tree.bind('<<TreeviewSelect>>', lambda event: self.update_context())
        search_bar = tk.Frame(header, bg=colors['panel'])
        search_bar.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        search_bar.columnconfigure(1, weight=1)
        app._label(search_bar, 'Поиск баз', 10, 'muted').grid(row=0, column=0, padx=(0, 8))
        self.base_search = tk.StringVar()
        self.base_search_entry = app._entry(search_bar, self.base_search)
        self.base_search_entry.grid(row=0, column=1, sticky='ew')
        self.base_search_entry.bind('<Escape>', lambda event: self.base_search.set(''))
        self.base_count = app._label(search_bar, '0 баз', 9, 'muted')
        self.base_count.grid(row=0, column=2, padx=(8, 0))
        self.base_search.trace_add('write', lambda *args: self.render_bases(self.bases))
        self.update_base_actions()

    def toggle_ib_auth(self):
        if self.running:
            return
        if self.ib_auth_form.winfo_manager():
            self.ib_auth_form.pack_forget()
            self.ib_auth_button.configure(text='Авторизация ИБ…')
        else:
            self.ib_auth_form.pack(fill=tk.X, after=self.ib_auth_button)
            self.ib_auth_button.configure(text='Скрыть авторизацию ИБ')

    def combo(self, title, variable, values, editable=False):
        self.app._label(self.form, title, 10, 'muted', wraplength=280, anchor=tk.W, justify=tk.LEFT).pack(fill=tk.X, pady=(8, 4))
        widget = ttk.Combobox(self.form, textvariable=variable, values=values, state='normal' if editable else 'readonly',
                              font=('Segoe UI', 10))
        widget.editable = editable
        widget.pack(fill=tk.X)
        self.inputs.append(widget)
        return widget

    def finish(self, text='', error=''):
        if self._jobs_pending:
            for row in self.bases:
                if row['infobase'] == self._jobs_pending:
                    row['scheduled_jobs_deny'] = None
            self.render_bases(self.bases, self._jobs_pending)
            self._jobs_pending = ''
        super().finish(text, error)
        for widget in self.inputs:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state='normal' if widget.editable else 'readonly')
        self.update_base_actions()

    def update_base_actions(self):
        state = tk.NORMAL if not self.running and self.selected_base() else tk.DISABLED
        for button in (self.delete_button, self.jobs_button):
            button.configure(state=state)

    def set_compact(self, compact):
        self.context_note.grid_remove() if compact or self.target() and self.selected_cluster() else self.context_note.grid()

    @staticmethod
    def target_key(item):
        return item.get('server', '').casefold(), str(item.get('port', ''))

    def target(self):
        index = self.server_combo.current()
        return dict(self.targets[index]) if 0 <= index < len(self.targets) else None

    def show_mode(self, mode):
        previous = self.target()
        previous_cluster = self.selected_cluster()
        selected = None
        if self.app.mode in ('servers', 'bases') and self.app.tree.selection():
            index = int(self.app.tree.selection()[0])
            if index < len(self.app._visible_items):
                selected = self.app._visible_items[index]
        by_key = {}
        for row in self.app.all_items:
            key = self.target_key(row)
            if key not in by_key or not by_key[key].get('version'):
                by_key[key] = dict(row)
        self.targets = list(by_key.values())
        self.server_combo.configure(values=[str(row['server']) + ':' + str(row['port']) +
                                           (' · ' + row['version'] if row.get('version') else '') for row in self.targets])
        wanted = selected or previous
        index = next((index for index, row in enumerate(self.targets)
                      if wanted and self.target_key(row) == self.target_key(wanted)), 0)
        if self.targets:
            self.server_combo.current(index)
        else:
            self.server.set('')
        preferred = selected.get('cluster_id', '') if selected else ''
        if not preferred and previous and self.target() and previous_cluster and self.target_key(previous) == self.target_key(self.target()):
            preferred = previous_cluster['cluster']
        self.server_changed(preferred)

    def server_changed(self, preferred=''):
        target = self.target()
        known = {}
        if target:
            for row in self.app._all_bases:
                if self.target_key(row) == self.target_key(target) and row.get('cluster_id'):
                    known[row['cluster_id']] = dict(cluster=row['cluster_id'], name=row.get('cluster', '').split(' (')[0],
                                                   host=row.get('cluster_host', ''), port=row.get('cluster_port', ''),
                                                   platform_version=row.get('cluster_version', ''))
        rows = self.cluster_cache.get(self.target_key(target), list(known.values())) if target else []
        self.render_clusters(rows, preferred)
        self.render_bases(self.cached_bases())
        user = self.app.user_var.get()
        self.auth_note.configure(text='Администратор кластера: ' + (user or 'без имени пользователя'))

    def selected_cluster(self):
        index = self.cluster_combo.current()
        return self.clusters[index] if 0 <= index < len(self.clusters) else None

    def selected_base(self):
        selection = self.tree.selection()
        return self.bases[int(selection[0])] if selection else None

    def cache_key(self):
        target, cluster = self.target(), self.selected_cluster()
        return (*self.target_key(target), cluster['cluster']) if target and cluster else None

    def cached_bases(self):
        key = self.cache_key()
        if not key:
            return []
        if key in self.base_cache:
            return self.base_cache[key]
        return [dict(infobase=row['infobase_id'], name=row['base'], descr=row.get('descr', ''))
                for row in self.app._all_bases if row.get('infobase_id') and
                (row['server'].casefold(), str(row['port']), row['cluster_id']) == key]

    def render_clusters(self, rows, preferred=''):
        self.clusters = rows
        self.cluster_combo.configure(values=[row.get('name', '') + ' · ' + row['cluster'] for row in rows])
        index = next((index for index, row in enumerate(rows) if row['cluster'].casefold() == preferred.casefold()), -1)
        if index < 0 and len(rows) == 1:
            index = 0
        self.cluster_combo.current(index) if index >= 0 else self.cluster.set('')
        self.update_context()

    def render_bases(self, rows, preferred=''):
        old = self.selected_base()
        preferred = preferred or (old['infobase'] if old else '')
        self.bases = list(rows)
        self.tree.delete(*self.tree.get_children())
        values = {}
        query = self.base_search.get().strip().casefold()
        for index, row in enumerate(self.bases):
            if query and query not in (row.get('name', '') + ' ' + row.get('descr', '')).casefold():
                continue
            if hasattr(self.app, 'hub') and not self.app.hub.visible(self, 'source', row.get('name', '')):
                continue
            denied = row.get('scheduled_jobs_deny')
            jobs = 'Включена' if denied is True else 'Выключена' if denied is False else 'Не проверена'
            self.tree.insert('', tk.END, iid=str(index), values=(row.get('name', ''), row.get('descr', ''), jobs))
            values[str(index)] = dict(name=row.get('name', ''), descr=row.get('descr', ''), jobs=jobs)
            if row['infobase'] == preferred:
                self.tree.selection_set(str(index))
        self.sorter.apply(values)
        self.base_count.configure(text=f'{len(values)} из {len(self.bases)}')
        if self.cache_key():
            self.base_cache[self.cache_key()] = self.bases
        self.update_context()
        if hasattr(self.app, 'hub'):
            self.app.hub.restore_pending(self)

    def cluster_changed(self):
        self.render_bases(self.cached_bases())
        if self.selected_cluster() and not self.running:
            self.load_bases()

    def load_bases(self):
        if self.running or not self.selected_cluster():
            return
        try:
            client = self.client(lambda event: None)
        except (ValueError, OSError) as exc:
            self.app._panel_error(self, str(exc))
            return
        cluster = self.selected_cluster()['cluster']
        def operation(event):
            client.event = event
            return client.bases(cluster)
        if self.start():
            self.work(operation, self.bases_loaded)

    def bases_loaded(self, rows):
        self.render_bases(rows)
        self.sync_cache(rows)
        self.finish('Загружено ИБ выбранного кластера: ' + str(len(rows)))

    def sync_cache(self, rows):
        key = self.cache_key()
        if not key:
            return
        self.app._all_bases = [row for row in self.app._all_bases
            if (row['server'].casefold(), str(row['port']), row['cluster_id']) != key]
        target, cluster = self.target(), self.selected_cluster()
        self.app._all_bases.extend(dict(server=target['server'], port=target['port'], base=row.get('name', ''),
            descr=row.get('descr', ''), cluster=cluster.get('name', '') + ' (' + cluster['cluster'][:8] + '...)',
            cluster_id=cluster['cluster'], cluster_host=cluster.get('host', ''), cluster_port=cluster.get('port', ''),
            cluster_version=cluster.get('platform_version', ''), infobase_id=row['infobase']) for row in rows)
        self.update_context()

    def update_context(self):
        row = self.selected_cluster()
        target = self.target()
        base = self.selected_base()
        if hasattr(self, 'address'):
            self.address.set(infobase_address(row or {}, base.get('name', '') if base else '',
                                             target.get('server', '') if target else ''))
            self.address_copy_button.configure(state=tk.NORMAL if self.address.get() else tk.DISABLED)
        self.context_note.configure(text='' if target and row else 'Выберите сервер, нажмите «Обновить список» и выберите кластер слева')
        if target and row:
            self.context_note.grid_remove()
        self.update_base_actions()

    def copy_address(self):
        if self.address.get():
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(self.address.get())
            self.app._panel_status(self, 'Адрес информационной базы скопирован')

    def client(self, event):
        target = self.target()
        if not target:
            raise ValueError('Сначала загрузите серверы в разделе «1С → Серверы» и выберите сервер')
        versions = dict(self.app.rac_map)
        ras_port = self.app._settings.get('ras_ports', {}).get(str(target['server']) + ':' + str(target['port']), 1545)
        return ClusterClient(target, versions, self.app.user_var.get(), self.app.pwd_var.get(),
                             self.app.local_ras_var.get(), ras_port, event, self.cancel_event,
                             ib_user=self.ib_user.get(), ib_password=self.ib_password.get())

    def load_clusters(self):
        if self.running:
            return
        try:
            client = self.client(lambda event: None)
        except (ValueError, OSError) as exc:
            self.app._panel_error(self, str(exc))
            return
        selected = self.selected_cluster()
        preferred = selected['cluster'] if selected else ''
        def operation(event):
            client.event = event
            return client.overview(preferred)
        if self.start():
            self.work(operation, lambda result: self.loaded(result, preferred))

    def loaded(self, result, preferred):
        self.cluster_cache[self.target_key(self.target())] = result['clusters']
        self.render_clusters(result['clusters'], result['cluster'])
        self.render_bases(result['bases'])
        self.sync_cache(result['bases'])
        self.finish('Найдено кластеров: ' + str(len(result['clusters'])) + '. ИБ выбранного кластера: ' + str(len(result['bases'])))

    def parameters(self):
        return validate_parameters({'name': self.name.get(), 'descr': self.description.get(),
            'security-level': SECURITY.get(self.security.get(), ''), 'db-server': self.db_server.get(),
            'dbms': DBMS.get(self.dbms.get(), ''), 'db-name': self.db_name.get(), 'db-user': self.db_user.get(),
            'db-pwd': self.db_password.get(), 'license-distribution': 'allow' if self.licenses.get() == 'Да' else 'deny',
            'locale': LOCALES.get(self.locale.get(), self.locale.get()), 'date-offset': self.date_offset.get(),
            'create-database': self.create_db.get(), 'scheduled-jobs-deny': 'on' if self.deny_jobs.get() else 'off'})

    def execute(self):
        if self.running:
            return
        cluster = self.selected_cluster()
        if not cluster:
            self.app._panel_error(self, 'Выберите кластер слева; при необходимости обновите список кластеров')
            return
        try:
            parameters = self.parameters()
            client = self.client(lambda event: None)
        except (ValueError, OSError) as exc:
            self.app._panel_error(self, str(exc))
            return
        target = self.target()
        def operation(event):
            client.event = event
            result = client.create(cluster['cluster'], parameters)
            try:
                return dict(result, bases=client.bases(cluster['cluster']), refresh_error='')
            except Exception as exc:
                return dict(result, bases=None, refresh_error=str(exc))
        if self.start():
            self.work(operation, lambda result: self.created(target, cluster, result),
                      history=self.history_context('Добавление ИБ в кластер', parameters['name']))

    def created(self, target, cluster, result):
        rows = result.get('bases')
        if rows is None:
            rows = list(self.bases)
            if result['infobase_id'] and not any(row['infobase'] == result['infobase_id'] for row in rows):
                rows.append(dict(infobase=result['infobase_id'], name=result['name'], descr=self.description.get()))
            if result.get('refresh_error'):
                self.append('ИБ добавлена, но список не обновлён: ' + result['refresh_error'])
        for row in rows:
            if row['infobase'] == result['infobase_id']:
                row['scheduled_jobs_deny'] = result.get('scheduled_jobs_deny')
        self.render_bases(rows, result['infobase_id'])
        self.sync_cache(rows)
        message = ('ИБ добавлена: ' if result['infobase_id'] else 'Команда добавления завершена: ') + result['name'] + \
                  (' · UUID: ' + result['infobase_id'] if result['infobase_id'] else ' · регистрация требует проверки')
        if result.get('settings_error'):
            self.append(message)
            self.finish(error=result['settings_error'] + '\nРегистрация ИБ сохранена. Для применения и проверки '
                        'выберите эту ИБ и нажмите «Применить флажок заданий».')
            self.state_label.configure(text='ИБ создана · блокировка не проверена')
            self.app._panel_status(self, 'ИБ создана; состояние блокировки заданий требует проверки — подробности в журнале')
        else:
            self.finish(message)

    def delete_selected(self):
        if self.running:
            return
        base, cluster, target = self.selected_base(), self.selected_cluster(), self.target()
        if not base or not cluster or not target:
            self.app._panel_error(self, 'Выберите ИБ в списке выбранного кластера')
            return
        detail = (f"Сервер 1С: {target['server']}:{target['port']}\nКластер: {cluster.get('name', '')}\n"
                  f"UUID кластера: {cluster['cluster']}\nИБ: {base.get('name', '')}\nUUID ИБ: {base['infobase']}\n\n"
                  'Удалить регистрацию ИБ из этого кластера?\nБаза данных в СУБД останется.')
        if not messagebox.askyesno('Удалить ИБ из кластера?', detail, parent=self.app.root, icon='warning', default='no'):
            return
        try:
            client = self.client(lambda event: None)
        except (ValueError, OSError) as exc:
            self.app._panel_error(self, str(exc))
            return
        def operation(event):
            client.event = event
            client.drop(cluster['cluster'], base['infobase'], base.get('name', ''))
            try:
                return client.bases(cluster['cluster']), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.deleted(base, result),
                      history=self.history_context('Удаление ИБ из кластера', base.get('name', '')))

    def deleted(self, base, result):
        rows, error = result
        if rows is None:
            rows = [row for row in self.bases if row['infobase'] != base['infobase']]
            self.append('Регистрация удалена, но список не обновлён: ' + error)
        self.render_bases(rows)
        self.sync_cache(rows)
        if self.app.mode == 'bases':
            self.app._render()
        self.finish('Регистрация ИБ удалена: ' + base.get('name', '') + '. База данных в СУБД сохранена.')

    def apply_jobs(self):
        if self.running:
            return
        base, cluster, target = self.selected_base(), self.selected_cluster(), self.target()
        if not base or not cluster or not target:
            self.app._panel_error(self, 'Выберите ИБ в списке выбранного кластера')
            return
        denied = self.deny_jobs.get()
        detail = (f"Сервер 1С: {target['server']}:{target['port']}\nКластер: {cluster.get('name', '')}\n"
                  f"ИБ: {base.get('name', '')}\nUUID ИБ: {base['infobase']}\n\n" +
                  ('Включить блокировку регламентных заданий?' if denied else 'Выключить блокировку регламентных заданий?'))
        if not messagebox.askyesno('Применить флажок заданий?', detail, parent=self.app.root, default='no'):
            return
        try:
            client = self.client(lambda event: None)
        except (ValueError, OSError) as exc:
            self.app._panel_error(self, str(exc))
            return
        def operation(event):
            client.event = event
            return client.apply_scheduled_jobs(cluster['cluster'], base['infobase'], base.get('name', ''), denied)
        if self.start():
            self._jobs_pending = base['infobase']
            self.work(operation, lambda result: self.jobs_applied(base['infobase'], result),
                      history=self.history_context('Блокировка регламентных заданий', base.get('name', '')))

    def jobs_applied(self, infobase, denied):
        self._jobs_pending = ''
        for row in self.bases:
            if row['infobase'] == infobase:
                row['scheduled_jobs_deny'] = denied
        self.render_bases(self.bases, infobase)
        self.finish('Блокировка регламентных заданий ' + ('включена' if denied else 'выключена') + '. Значение проверено через RAC.')
