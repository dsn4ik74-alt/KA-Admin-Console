"""Official configuration setup on this Windows PC and deployment to a cluster."""
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from administration_ui import JobPanel
from cluster_infobase_ui import ClusterInfobasePanel
from configuration_catalog import ConfigurationPortal
from configuration_requirements import requirements_text
from configuration_install import cleanup_configuration, deploy_configuration, designer_for_version, install_configuration
from favorite_secrets import protect, unprotect
from infobase_address import infobase_address
from table_sorting import TableSorter


class ConfigurationInstallPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Установка конфигураций', 'Установить')
        self.enable_cancellation()
        self.portal, self.metadata, self.target_dialog = None, None, None
        self.projects, self.releases, self.distributions, self.templates = [], [], [], []
        self.username, self.password = tk.StringVar(), tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.folder = tk.StringVar(value=str(Path.home() / 'Downloads' / '1C'))
        self.setup_method = tk.StringVar(value='Автоматически')
        self.executable = tk.StringVar()
        self.field('Логин ИТС', self.username)
        self.field('Пароль ИТС', self.password, show='•')
        self.check('Сохранять вход с защитой Windows', self.remember)
        self.button('Войти / обновить каталог', self.load_catalog)
        self.note('Типовые конфигурации фирмы «1С» для России. Шаблоны устанавливаются на этом компьютере Windows; кластер может находиться на другом сервере.')
        self.field('Папка временных дистрибутивов', self.folder)
        self.button('Выбрать папку…', self.choose_folder)
        self.app._label(self.form, 'Способ установки шаблонов', 10, 'muted').pack(anchor=tk.W, pady=(8, 4))
        self.setup_choice = ttk.Combobox(self.form, textvariable=self.setup_method, width=1, state='readonly',
                                       values=('Автоматически', 'Через setup.exe', 'Потоковое извлечение EFD'))
        self.setup_choice.pack(fill=tk.X)
        self.setup_choice.bind('<MouseWheel>', lambda event: 'break')
        self.setup_choice.bind('<Shift-MouseWheel>', lambda event: 'break')
        self.inputs.append(self.setup_choice)
        self.note('В автоматическом режиме крупные EFD от 2 ГБ извлекаются порциями без запуска setup. Для остальных поставок используется setup, а при его сбое — извлечение EFD. Проверяется версия чистой и демонстрационной базы.')
        self.designer_entry = self.field('Конфигуратор · автоматически по версии кластера', self.executable)
        self.designer_entry.configure(state='readonly')
        self.button('Указать нестандартный путь к 1cv8.exe…', self.choose_designer)
        self.note('После выбора кластера консоль читает версию агента 1С и находит локальный полный клиент той же версии. Порт кластера также определяется автоматически.')
        self.deploy_button = self.button('Добавить базу на сервер…', self.open_target)
        self.cleanup_button = self.button('Удалить временные шаблоны', self.cleanup_templates)
        self.note('После успешной загрузки базы удаляются архив, распакованный setup, временная файловая база и установленные здесь шаблоны. При ошибке загрузки шаблоны остаются для повтора. Чужие шаблоны не удаляются.')
        self.note('Один щелчок только выделяет строку. Выберите конфигурацию и версию кнопкой «Выбрать». Скачивание начинается по «Установить» или двойному щелчку дистрибутива.')
        table = self.split_log()
        self.catalog_tabs = ttk.Notebook(table)
        self.catalog_tabs.grid(row=0, column=0, sticky='nsew')
        self.searches, self.trees, self.sorters, self.search_entries = {}, {}, {}, {}
        for key, title, columns, choose in (
                ('projects', 'Конфигурации', (('title', 'Название', 430), ('version', 'Версия', 120), ('date', 'Дата', 110)), self.choose_project),
                ('releases', 'Версии', (('version', 'Версия', 150), ('date', 'Дата выхода', 125),
                                      ('platform_minimum', 'Минимальная платформа', 210)), self.choose_release),
                ('distributions', 'Дистрибутивы', (('title', 'Дистрибутив', 470), ('kind', 'Тип', 160)), self.choose_distribution),
                ('templates', 'Шаблоны', (('title', 'Название', 470), ('version', 'Версия', 130), ('variant', 'База', 150)), self.choose_template)):
            self.make_table(key, title, columns, choose)
        self.trees['distributions'].bind('<Double-1>', self.execute)
        self.trees['releases'].bind('<<TreeviewSelect>>', self.preview_release)
        self.trees['templates'].bind('<<TreeviewSelect>>', self.choose_template)
        self.trees['distributions'].bind('<<TreeviewSelect>>', self.choose_distribution)
        self.catalog_tabs.bind('<<NotebookTabChanged>>', lambda event: self.update_actions())
        self.execute_button.configure(command=self.primary_action)
        info = tk.Frame(self.right, bg=colors['panel'])
        info.grid(row=1, column=0, sticky='ew', pady=(0, 7))
        info.columnconfigure(0, weight=1)
        self.selection_note = app._label(info, 'Войдите в ИТС и выберите конфигурацию', 10, 'muted', anchor=tk.W)
        self.selection_note.grid(row=0, column=0, columnspan=2, sticky='ew')
        self.platform_note = app._label(info, 'Платформа: выберите версию конфигурации', 10, anchor=tk.W)
        self.platform_note.grid(row=1, column=0, sticky='ew', pady=(5, 0))
        self.requirements_button = app._button(info, 'Требования 1С…', self.show_requirement_details)
        self.requirements_button.grid(row=1, column=1, padx=(8, 0))
        self.platform_requirements = {}
        info.bind('<Configure>', lambda event: self.platform_note.configure(wraplength=max(180, event.width - 170)))
        try:
            saved = unprotect(app._settings['its_credentials']) if app._settings.get('its_credentials') else {}
            self.username.set(saved.get('username', ''))
            self.password.set(saved.get('password', ''))
        except ValueError as exc:
            self.note(str(exc))
        self.update_actions()

    def button(self, title, command):
        button = self.app._button(self.form, title, command)
        button.pack(fill=tk.X, pady=(7, 0))
        self.inputs.append(button)
        return button

    def make_table(self, key, title, columns, choose):
        frame = tk.Frame(self.catalog_tabs, bg=self.c['panel'])
        self.catalog_tabs.add(frame, text=title)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        toolbar = tk.Frame(frame, bg=self.c['panel'])
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 7))
        toolbar.columnconfigure(1, weight=1)
        self.app._label(toolbar, 'Поиск', 9, 'muted').grid(row=0, column=0, padx=(0, 7))
        variable = self.searches[key] = tk.StringVar()
        entry = self.app._entry(toolbar, variable)
        entry.grid(row=0, column=1, sticky='ew')
        self.search_entries[key] = entry
        button = self.app._button(toolbar, 'Выбрать', choose)
        button.grid(row=0, column=2, padx=(7, 0))
        self.inputs.extend((entry, button))
        tree = self.trees[key] = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode='browse', height=4)
        for column, label, width in columns:
            tree.heading(column, text=label)
            tree.column(column, width=width, minwidth=90, stretch=True)
        tree.grid(row=1, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        vertical.grid(row=1, column=1, sticky='ns')
        horizontal.grid(row=2, column=0, sticky='ew')
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.sorters[key] = TableSorter(tree, colors=self.c)
        tree.bind('<Double-1>', choose)
        tree.bind('<Return>', choose)
        tree.bind('<Button-1>', lambda event: 'break' if self.running else None)
        tree.bind('<KeyPress>', lambda event: 'break' if self.running else None)
        variable.trace_add('write', lambda *args: self.render(key))

    def render(self, key):
        tree, rows = self.trees[key], getattr(self, key)
        selected = tree.selection()
        tree.delete(*tree.get_children())
        query, values = self.searches[key].get().strip().casefold(), {}
        columns = tree['columns']
        for index, row in enumerate(rows):
            if query and query not in ' '.join(str(row.get(c, '')) for c in columns).casefold():
                continue
            item = str(index)
            tree.insert('', tk.END, iid=item, values=[row.get(c, '') for c in columns])
            values[item] = row
            if item in selected:
                tree.selection_set(item)
        self.sorters[key].apply(values)
        self.update_actions()

    def selected(self, key):
        selection = self.trees[key].selection()
        rows = getattr(self, key)
        return deepcopy(rows[int(selection[0])]) if selection and int(selection[0]) < len(rows) else None

    def show_requirements(self, row=None):
        self.platform_requirements = deepcopy((row or {}).get('platform_requirements', {}))
        value = self.platform_requirements
        self.platform_note.configure(text=('Минимальная платформа: ' + (value.get('minimum') or 'не указана в карточке релиза') +
                                          ' · Рекомендуемая: ' + (value.get('recommended') or 'не указана в карточке релиза'))
                                     if row else 'Платформа: выберите версию конфигурации')
        self.requirements_button.configure(state=tk.NORMAL if value else tk.DISABLED)

    def preview_release(self, event=None):
        if not self.running:
            self.show_requirements(self.selected('releases'))

    def show_requirement_details(self):
        if self.platform_requirements:
            window = tk.Toplevel(self.app.root)
            window.title('Требования к платформе 1С')
            window.geometry('780x360')
            frame = tk.Frame(window, padx=12, pady=12)
            frame.pack(fill=tk.BOTH, expand=True)
            text = tk.Text(frame, wrap=tk.WORD, font=('Segoe UI', 10), padx=8, pady=8)
            scrollbar = ttk.Scrollbar(frame, command=text.yview)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            text.configure(yscrollcommand=scrollbar.set)
            text.pack(fill=tk.BOTH, expand=True)
            text.insert('1.0', requirements_text(self.platform_requirements))
            text.configure(state=tk.DISABLED)

    def update_actions(self):
        if not hasattr(self, 'trees'):
            return
        available = not self.running and self.target_dialog is None
        ready = available and self.metadata and not self.metadata.get('loaded') and self.selected('templates')
        templates_tab = self.on_templates_tab()
        enabled = ready if templates_tab else available and self.portal and self.selected('distributions')
        self.execute_button.configure(text='Добавить базу на сервер' if templates_tab else 'Установить',
                                      state=tk.NORMAL if enabled else tk.DISABLED)
        self.deploy_button.configure(state=tk.NORMAL if ready else tk.DISABLED)
        self.cleanup_button.configure(state=tk.NORMAL if available and self.metadata else tk.DISABLED)

    def on_templates_tab(self):
        selected = self.catalog_tabs.select()
        return bool(selected) and selected == str(self.trees['templates'].master)

    def primary_action(self):
        if self.on_templates_tab():
            self.open_target()
        else:
            self.execute()

    def finish(self, text='', error=''):
        super().finish(text, error)
        self.designer_entry.configure(state='readonly')
        self.setup_choice.configure(state='readonly')
        self.update_actions()

    def load_catalog(self):
        if self.running:
            return
        username, password, remember = self.username.get().strip(), self.password.get(), self.remember.get()
        if not username or not password:
            self.app._panel_error(self, 'Введите логин и пароль ИТС')
            return
        def operation(event):
            portal = ConfigurationPortal(self.cancel_event)
            event(dict(kind='stage', text='Вход в ИТС и получение российских конфигураций'))
            portal.login(username, password)
            return dict(portal=portal, projects=portal.projects(), credentials=protect(dict(username=username, password=password)) if remember else None)
        if self.start():
            self.work(operation, self.catalog_loaded)

    def catalog_loaded(self, result):
        self.portal, self.projects = result['portal'], result['projects']
        self.releases, self.distributions = [], []
        self.show_requirements()
        for key in ('projects', 'releases', 'distributions'):
            self.render(key)
        if result['credentials']:
            self.app._settings['its_credentials'] = result['credentials']
        else:
            self.app._settings.pop('its_credentials', None)
        self.app._save_settings()
        self.catalog_tabs.select(0)
        self.finish('Доступных российских конфигураций: ' + str(len(self.projects)))

    def choose_project(self, event=None):
        row = self.selected('projects')
        if self.running or not row or not self.portal:
            return
        portal = self.portal
        if self.start():
            self.work(lambda emit: portal.configuration_releases(row), lambda rows: self.releases_loaded(row, rows))

    def releases_loaded(self, project, rows):
        self.releases, self.distributions = rows, []
        self.show_requirements()
        self.searches['releases'].set('')
        self.render('releases')
        self.render('distributions')
        self.selection_note.configure(text=project['title'])
        self.catalog_tabs.select(1)
        self.finish('Версий: ' + str(len(rows)) + '. Выберите версию.')

    def choose_release(self, event=None):
        row = self.selected('releases')
        if self.running or not row or not self.portal:
            return
        portal = self.portal
        if self.start():
            self.work(lambda emit: portal.configuration_details(row), self.distributions_loaded)

    def distributions_loaded(self, result):
        row, self.distributions = result['release'], result['distributions']
        for index, release in enumerate(self.releases):
            if release['url'] == row['url']:
                self.releases[index] = row
        self.render('releases')
        self.show_requirements(row)
        self.searches['distributions'].set('')
        self.render('distributions')
        self.catalog_tabs.select(2)
        self.finish('Выберите полный дистрибутив и нажмите «Установить».')

    def choose_distribution(self, event=None):
        row = self.selected('distributions')
        if not self.running and row:
            self.selection_note.configure(text=row['project']['title'] + ' · ' + row['version'] + ' · ' + row['title'])
            self.show_requirements(row)
            self.update_actions()

    def choose_template(self, event=None):
        row = self.selected('templates')
        if not self.running and row:
            self.selection_note.configure(text=row['title'] + ' · ' + row['version'])
            self.show_requirements((self.metadata or {}).get('distribution'))
            self.update_actions()

    def execute(self, event=None):
        row = self.selected('distributions')
        if self.running or self.target_dialog is not None or not self.portal or not row:
            return
        folder = self.folder.get().strip()
        if not folder or not Path(folder).is_absolute():
            self.app._panel_error(self, 'Укажите полный путь папки временных дистрибутивов', self.folder_entry if hasattr(self, 'folder_entry') else None)
            return
        if row['kind'] == 'Обновление':
            self.app._panel_error(self, 'Для пустой или демонстрационной базы выберите полный дистрибутив. Обновление CFU не содержит исходной базы.')
            return
        old, portal = self.metadata, self.portal
        method = {'Автоматически': 'auto', 'Через setup.exe': 'setup', 'Потоковое извлечение EFD': 'stream'}.get(self.setup_method.get(), 'auto')
        def operation(emit):
            if old:
                error = cleanup_configuration(old, emit)
                if error:
                    raise RuntimeError(error)
            return install_configuration(portal, row, folder, emit, self.cancel_event, method=method)
        if self.start():
            self.work(operation, self.installed, history=dict(operation='Установка конфигурации', database=row['project']['title'] + ' ' + row['version'], executor='Windows', path=folder))

    def installed(self, result):
        self.metadata, self.templates = result, result['templates']
        self.show_requirements(result.get('distribution'))
        self.searches['templates'].set('')
        self.render('templates')
        preferred = next((i for i, row in enumerate(self.templates) if not row['demo']), 0)
        self.trees['templates'].selection_set(str(preferred))
        self.catalog_tabs.select(3)
        self.finish('Шаблоны установлены. Выберите вариант и нажмите «Добавить базу на сервер».')

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.app.root, title='Папка временных дистрибутивов')
        if folder:
            self.folder.set(folder)

    def choose_designer(self):
        path = filedialog.askopenfilename(parent=self.app.root, title='Полный клиент / Конфигуратор 1С', filetypes=[('1cv8.exe', '1cv8.exe')])
        if path:
            self.executable.set(path)
            if self.target_dialog is not None:
                self.target_dialog.winfo_children()[0].load_clusters()

    def cleanup_templates(self):
        if self.running or not self.metadata or self.target_dialog is not None:
            return
        package = self.metadata
        if self.start():
            def operation(emit):
                error = cleanup_configuration(package, emit)
                if error:
                    raise RuntimeError(error)
            self.work(operation, self.templates_removed)

    def templates_removed(self, result):
        self.metadata, self.templates = None, []
        self.render('templates')
        self.finish('Временные шаблоны удалены.')

    def open_target(self, refresh=True):
        if self.running or not self.metadata or self.metadata.get('loaded') or not self.selected('templates'):
            return
        if self.target_dialog is not None:
            self.target_dialog.lift()
            return
        window = self.target_dialog = tk.Toplevel(self.app.root)
        window.title('Добавить базу на сервер · ' + self.selected('templates')['variant'])
        window.geometry('1360x850')
        window.minsize(940, 580)
        window.transient(self.app.root)
        panel = ConfigurationTargetPanel(window, self)
        panel.pack(fill=tk.BOTH, expand=True)
        def close():
            if panel.running:
                panel.interrupt()
                panel.append('Дождитесь завершения запроса перед закрытием окна.')
                return
            self.target_dialog = None
            window.destroy()
            self.update_actions()
        window.protocol('WM_DELETE_WINDOW', close)
        panel.close_dialog = close
        panel.show_mode('cluster_infobase')
        self.update_actions()
        if refresh and panel.target():
            panel.load_clusters()

    def deploy(self, template, plan):
        package = self.metadata
        if self.start():
            self.work(lambda emit: deploy_configuration(package, template, plan, emit, self.cancel_event), self.deployed,
                      history=dict(operation='Загрузка конфигурации в кластер', server=plan['target']['server'],
                                   database=plan['parameters']['name'] if plan['new'] else plan['base']['name'],
                                   executor='Windows', path=template['title']))

    def deployed(self, result):
        self.metadata['loaded'] = True
        self.templates = []
        self.render('templates')
        message = 'База загружена: ' + result['name'] + '\n' + result['address']
        warning = result.get('settings_error', '')
        if warning:
            self.append(message)
            self.finish(error='База загружена; требуется проверка:\n' + warning)
            self.state_label.configure(text='Загружено · требуется проверка')
        else:
            self.metadata = None
            self.finish(message + '\nВременные файлы удалены.')


class ConfigurationTargetPanel(ClusterInfobasePanel):
    """Reuse the same cluster fields and refresh implementation as the main form."""
    def __init__(self, parent, owner):
        self.owner = owner
        self._continue_after_refresh = False
        super().__init__(parent, owner.app, owner.c)
        self.cancel_event = owner.cancel_event
        self.title.configure(text='Куда загрузить выбранную конфигурацию')
        self.execute_button.configure(text='Загрузить базу')
        self.delete_button.grid_remove()
        self.jobs_button.grid_remove()
        self.ib_auth_button.pack_forget()
        self.ib_auth_form.pack(fill=tk.X)
        self.new = tk.BooleanVar(value=False)
        first = list(self.form.winfo_children())[0]
        self.mode_check = self.app._button(self.form, 'Режим: существующая база · изменить', self.toggle_new)
        self.mode_check.pack(fill=tk.X, before=first, pady=(0, 7))
        self.new_fields = list(self.inputs)[2:]
        # Keep refresh and IB authentication usable in either mode.
        self.creation_fields = [w for w in self.new_fields if w not in (self.refresh_button, self.delete_button, self.jobs_button, self.ib_auth_button)
                                and not str(w).startswith(str(self.ib_auth_form) + '.')]
        children = list(self.form.winfo_children())
        start = next(i for i, w in enumerate(children) if isinstance(w, tk.Label) and w.cget('text') == 'Параметры информационной базы')
        self.creation_widgets = [(w, w.pack_info()) for w in children[start:children.index(self.ib_auth_button)] if w.winfo_manager() == 'pack']
        self.deny_jobs.set(True)
        self.runtime_note = self.app._label(self.form, 'Версия платформы и порт кластера определяются после подключения.',
                                           9, 'muted', anchor=tk.W, justify=tk.LEFT, wraplength=420)
        self.runtime_note.pack(fill=tk.X, before=self.auth_note, pady=(6, 0))
        value = (owner.metadata or {}).get('distribution', {}).get('platform_requirements', {})
        self.requirement_note = self.app._label(self.form,
            'Требования конфигурации: минимум ' + (value.get('minimum') or 'не указан в карточке релиза') +
            '; рекомендованная платформа ' + (value.get('recommended') or 'не указана в карточке релиза') +
            '. Полные условия — «Требования 1С…» на вкладке установки.',
            9, 'muted', anchor=tk.W, justify=tk.LEFT, wraplength=420)
        self.requirement_note.pack(fill=tk.X, before=self.auth_note, pady=(6, 0))
        self.mode_changed()

    def server_changed(self, preferred=''):
        super().server_changed(preferred)
        if self.target() and hasattr(self, 'runtime_note') and not self.running:
            self.load_clusters()

    def show_mode(self, mode):
        # Refresh is started by open_target after callbacks/close handler exist.
        self._initializing_target = True
        try:
            super().show_mode(mode)
        finally:
            self._initializing_target = False

    def load_clusters(self):
        if self.running or self.owner.running or getattr(self, '_initializing_target', False):
            return
        try:
            client = self.client(lambda item: None)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Подключение не запущено', str(exc), parent=self.winfo_toplevel())
            return
        selected = self.selected_cluster()
        preferred = selected['cluster'] if selected else ''
        hint, versions = self.owner.executable.get(), dict(self.app.rac_map)
        def operation(event):
            client.event = event
            result = client.overview(preferred, include_version=True)
            cluster = next((row for row in result['clusters'] if row['cluster'] == result['cluster']), None)
            if cluster:
                try:
                    result['designer'] = designer_for_version(cluster['platform_version'], hint, versions)
                except ValueError as exc:
                    result['designer_error'] = str(exc)
            return result
        if self.start():
            self.work(operation, lambda result: self.loaded(result, preferred))

    def loaded(self, result, preferred):
        super().loaded(result, preferred)
        self.owner.executable.set(result.get('designer', ''))
        cluster = self.selected_cluster()
        if cluster:
            version = cluster.get('platform_version', '')
            index = self.server_combo.current()
            if 0 <= index < len(self.targets) and version:
                self.targets[index]['version'] = version
                self.server_combo.configure(values=[str(row['server']) + ':' + str(row['port']) +
                                                    (' · ' + row['version'] if row.get('version') else '') for row in self.targets])
                self.server_combo.current(index)
            address = str(cluster.get('host') or self.target()['server']) + ':' + str(cluster.get('port', ''))
            self.runtime_note.configure(text='Кластер: ' + address + '\nПлатформа сервера: ' + version +
                                        ('\n1cv8.exe найден автоматически.' if result.get('designer') else '\n' + result.get('designer_error', 'Выберите подходящий полный клиент 1С.')),
                                        fg=self.c['muted'] if result.get('designer') else self.c['danger'])
            if result.get('designer'):
                self.append('Автоматически выбран 1cv8.exe версии ' + version + ': ' + result['designer'])
                self.owner.append('Автоматически выбран 1cv8.exe версии ' + version + ': ' + result['designer'])
            elif result.get('designer_error'):
                self.append('ОШИБКА: ' + result['designer_error'])
        resume, self._continue_after_refresh = self._continue_after_refresh, False
        if resume and not result.get('designer_error'):
            self.execute()

    def cluster_changed(self):
        # The chosen cluster's address/version and client are refreshed together.
        self.render_bases(self.cached_bases())
        if self.selected_cluster() and not self.running:
            self.load_clusters()

    def toggle_new(self):
        if not self.running:
            self.new.set(not self.new.get())
            self.mode_changed()

    def mode_changed(self):
        self.mode_check.configure(text='Режим: ' + ('новая база' if self.new.get() else 'существующая база') + ' · изменить')
        for widget, options in self.creation_widgets:
            if self.new.get():
                widget.pack(**dict(options, before=self.ib_auth_form))
            else:
                widget.pack_forget()
        for widget in self.creation_fields:
            widget.configure(state=('readonly' if isinstance(widget, ttk.Combobox) and not widget.editable else 'normal') if self.new.get() and not self.running else 'disabled')

    def start(self):
        if not self.owner.start():
            return False
        if super().start():
            self.mode_check.configure(state=tk.DISABLED)
            return True
        self.owner.finish()
        return False

    def finish(self, text='', error=''):
        if error:
            self._continue_after_refresh = False
        super().finish(text, error)
        self.owner.finish(text, error)
        if hasattr(self, 'mode_check'):
            self.mode_check.configure(state=tk.NORMAL)
            self.mode_changed()

    def events(self, events):
        super().events(events)
        self.owner.events(events)

    def execute(self):
        if self.running:
            return
        try:
            cluster, target, base = self.selected_cluster(), self.target(), self.selected_base()
            if not target or not cluster:
                raise ValueError('Выберите сервер и обновите список кластеров.')
            parameters = self.parameters() if self.new.get() else None
            if not self.new.get() and not base:
                raise ValueError('Выберите существующую базу в списке справа либо переключите режим на новую базу.')
            name = parameters['name'] if parameters else base['name']
            address = infobase_address(cluster, name, target['server'])
            if not address or not cluster.get('platform_version'):
                self._continue_after_refresh = True
                self.load_clusters()
                return
            executable = Path(self.owner.executable.get())
            if not executable.is_file() or executable.name.casefold() != '1cv8.exe':
                raise ValueError('На компьютере консоли не найден полный клиент 1С версии ' + cluster['platform_version'] +
                                 '. Установите эту версию или укажите нестандартный путь в основной вкладке.')
            template = self.owner.selected('templates')
            if not template:
                raise ValueError('Выберите пустой или демонстрационный шаблон в основной вкладке.')
            self.client(lambda event: None)  # Validate the selected agent/RAS settings.
        except (ValueError, OSError) as exc:
            messagebox.showerror('Загрузка не запущена', str(exc), parent=self.winfo_toplevel())
            return
        detail = (template['title'] + '\nВерсия: ' + template['version'] + '\nВариант: ' + template['variant'] + '\n\n' + address +
                  '\nКластер: ' + cluster['cluster'] + '\n' +
                  ('Новая регистрация. СУБД: ' + parameters['db-server'] + ' / ' + parameters['db-name'] if parameters else 'UUID ИБ: ' + base['infobase']) +
                  '\n\nТекущие данные целевой базы будут заменены выбранным шаблоном.\nПродолжить загрузку?')
        if not messagebox.askyesno('Подтвердите загрузку базы', detail, parent=self.winfo_toplevel(), icon='warning', default='no'):
            return
        ras_port = self.app._settings.get('ras_ports', {}).get(str(target['server']) + ':' + str(target['port']), 1545)
        plan = deepcopy(dict(target=target, cluster=cluster, base=base, new=self.new.get(), parameters=parameters,
                    executable=str(executable), folder=self.owner.folder.get(), versions=self.app.rac_map,
                    cluster_user=self.app.user_var.get(), cluster_password=self.app.pwd_var.get(),
                    local=self.app.local_ras_var.get(), ras_port=ras_port, ib_user=self.ib_user.get(), ib_password=self.ib_password.get()))
        self.close_dialog()
        self.owner.deploy(template, plan)
