"""ITS catalog, background downloads and configurable local 1C installation."""
import os
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from administration_ui import JobPanel
from console_core import find_all_rac
from favorite_secrets import protect, unprotect
from its_portal import Portal
from platform_install import Msi, extract_distribution, install_msi, install_properties, installable_features, seven_zip
from service_management import installed_agents
from platform_service import SERVICE_ROLES, default_srvinfo, display_template, service_options, service_image_path
from table_sorting import TableSorter
from ui_geometry import delayed_wrap
from install_workspace import create_workspace, cleanup_workspace
from ssh_ui import add_auth_fields
from ras_tools import managed_tools, update_tools
import linux_platform
from linux_platform_runner import LINUX_DATA_DEFAULT, linux_service_options, environment_lines, ras_service_options

FEATURES = {'FileVersion': '1С:Предприятие · толстый клиент и конфигуратор',
            'MCWFileVersion': 'Тонкий клиент', 'MCVersion': 'Тонкий клиент · файловый вариант',
            'CSServer': 'Сервер 1С:Предприятия', 'WebServices': 'Модуль расширения веб-сервера',
            'CSClient': 'Администрирование сервера 1С', 'DepotServer': 'Сервер хранилища конфигураций',
            'AdminFunc': 'Дополнительные функции администрирования', 'Convertor': 'Конвертор ИБ 7.7'}
LANGUAGES = {'Русский': 'RU', 'English': 'EN', 'Українська': 'UK', 'Қазақша': 'KK', 'Deutsch': 'DE',
             'Polski': 'PL', 'Türkçe': 'TR', 'Română': 'RO', 'Български': 'BG', '简体中文': 'ZH'}


def windows_distribution_features(distribution):
    # An archive name does not prove which MSI features it contains.
    return None


class PlatformInstallPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Установка 1С из ИТС', 'Установить')
        self.enable_cancellation()
        self.portal = None
        self.metadata = None
        self.prepared_key = None
        self._ssh_verified = None
        self.releases, self.distributions = [], []
        self.setting_inputs, self.combos = [], []
        self.feature_values, self.feature_widgets = {}, {}
        self.feature_titles = {}
        self._updating_features = False
        self.username, self.password = tk.StringVar(), tk.StringVar()
        self.remember = tk.BooleanVar(value=True)
        self.branch = tk.StringVar(value='8.5')
        self.system = tk.StringVar(value='Windows · этот компьютер')
        self.version_search, self.distribution_search = tk.StringVar(), tk.StringVar()
        self.delete_after = tk.BooleanVar(value=True)
        self.ssh_host, self.ssh_user, self.ssh_password = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.ssh_port, self.sudo_password = tk.StringVar(value='22'), tk.StringVar()
        self.ssh_use_key, self.ssh_key = tk.BooleanVar(value=False), tk.StringVar()
        self.folder = tk.StringVar(value=str(Path.home() / 'Downloads' / '1C'))
        self.directory = tk.StringVar()
        self.language = tk.StringVar(value='Русский')
        self.service, self.new_user = tk.BooleanVar(), tk.BooleanVar()
        self.service_user = tk.StringVar(value='USR1CV8')
        self.service_password, self.service_confirm = tk.StringVar(), tk.StringVar()
        self.service_role = tk.StringVar(value=SERVICE_ROLES[0])
        self.service_port = tk.StringVar(value='1540')
        self.srvinfo = tk.StringVar(value=default_srvinfo())
        self.service_debug = tk.BooleanVar()
        self.ras_service, self.ras_port = tk.BooleanVar(), tk.StringVar(value='1545')
        self.ras_preview = tk.StringVar()
        self._ras_port_template = '1545'
        self._ras_available = False
        self._ras_verified = False
        self.service_display_name, self.service_preview = tk.StringVar(), tk.StringVar()
        self._display_template = ''
        self._editing_install_settings = False
        self.linux_service_data = tk.StringVar(value=LINUX_DATA_DEFAULT)
        self.linux_service_name = tk.StringVar(value='srv1cv8_1541.service')
        self.linux_service_preview = tk.StringVar()
        self._linux_name_template = 'srv1cv8_1541.service'
        self._linux_data_template = LINUX_DATA_DEFAULT
        self.hasp, self.hardware = tk.BooleanVar(), tk.BooleanVar()
        self.credential_error = ''
        if app._settings.get('its_credentials'):
            try:
                values = unprotect(app._settings['its_credentials'])
                self.username.set(values.get('username', ''))
                self.password.set(values.get('password', ''))
            except ValueError as exc:
                self.credential_error = str(exc)
        self.field('Логин ИТС', self.username)
        self.field('Пароль ИТС', self.password, show='•')
        self.check('Сохранять вход с защитой Windows', self.remember)
        self.catalog_button = self.button('Войти / обновить версии', self.load_catalog)
        self.ras_update_button = self.button('Обновить RAS консоли', self.update_ras)
        self.ras_tools_note = app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT)
        self.ras_tools_note.pack(fill=tk.X, pady=(5, 0))
        self.update_ras_note()
        self.combo('Ветка платформы', self.branch, ('8.3', '8.5'))
        self.system_combo = self.combo('Где установить', self.system, ('Windows · этот компьютер', 'Linux · Ubuntu / Debian по SSH'))
        self.system_combo.bind('<<ComboboxSelected>>', self.system_changed)
        self.ssh_frame = tk.Frame(self.form, bg=colors['panel'])
        saved, self.form = self.form, self.ssh_frame
        self.ssh_host_entry = self.server_field('Адрес Linux-ВМ', self.ssh_host)
        self.field('Порт SSH', self.ssh_port)
        self.field('Пользователь SSH', self.ssh_user)
        add_auth_fields(self)
        self.field('Пароль sudo · пусто = пароль SSH при входе по паролю', self.sudo_password, show='•')
        self.button('Проверить SSH', self.check_ssh)
        self.note('На ВМ нужны root или sudo и доступ к репозиториям apt. При первом входе подтвердите отпечаток ключа SSH. После успешной проверки сервер и пароли сохраняются с защитой Windows.')
        self.form = saved
        self.field('Папка для дистрибутивов на этом компьютере', self.folder)
        self.button('Выбрать папку…', self.choose_folder)
        self.check('Удалять файлы после успешной установки', self.delete_after)
        self.note('Один щелчок выделяет строку. «Выбрать» или двойной щелчок загружает дистрибутив. «Установить» выполняет загрузку и установку; параметры доступны во время загрузки.')
        if self.credential_error:
            self.note(self.credential_error)
        first = len(self.inputs)
        app._label(self.form, 'Параметры установки', 12, bold=True).pack(anchor=tk.W, pady=(12, 3))
        self.capabilities_note = app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT)
        self.capabilities_note.pack(fill=tk.X, pady=(3, 7))
        delayed_wrap(self.capabilities_note)
        self.feature_frame = tk.Frame(self.form, bg=colors['panel'])
        self.feature_frame.pack(fill=tk.X)
        for key, title in FEATURES.items():
            self.add_feature(key, title, key in ('FileVersion', 'MCWFileVersion', 'CSClient'))
        for key, title in linux_platform.FEATURES.items():
            self.add_feature(key, title, key in ('server', 'server_admin'))
        self.language_combo = self.combo('Основной язык интерфейса', self.language, tuple(LANGUAGES))
        self.directory_entry = self.field('Каталог установки · пусто = стандартный', self.directory)
        self.service_widget = self.check('Установить сервер как службу Windows', self.service)
        self.service_widget.configure(command=self.service_choice)
        self.windows_frame = tk.Frame(self.form, bg=colors['panel'])
        self.windows_frame.pack(fill=tk.X)
        saved, self.form = self.form, self.windows_frame
        self.new_user_widget = self.check('Создать пользователя службы USR1CV8', self.new_user)
        self.service_user_entry = self.field('Пользователь службы', self.service_user)
        self.service_password_entry = self.field('Пароль пользователя службы', self.service_password, show='•')
        self.service_confirm_entry = self.field('Подтверждение пароля службы', self.service_confirm, show='•')
        first_service = len(self.inputs)
        self.combo('Назначение службы · шаблон имени', self.service_role, SERVICE_ROLES)
        self.field('Порт агента службы', self.service_port)
        self.field('Папка данных сервера · srvinfo', self.srvinfo)
        self.button('Выбрать папку srvinfo…', self.choose_srvinfo)
        self.check('Включена отладка · добавить -debug', self.service_debug)
        self.field('Отображаемое имя службы · можно изменить', self.service_display_name)
        self.button('Подставить имя из шаблона', self.reset_service_display)
        self.service_controls = self.inputs[first_service:]
        self.service_preview_label = self.app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT)
        self.service_preview_label.configure(textvariable=self.service_preview)
        self.service_preview_label.pack(fill=tk.X, pady=(7, 0))
        delayed_wrap(self.service_preview_label)
        self.note('После установки служба будет настроена через sc config и останется остановленной. Отображаемое имя меняется без переименования системного имени службы. Назначение задаёт шаблон имени.')
        for variable in (self.service_role, self.service_port, self.srvinfo, self.service_debug, self.directory):
            variable.trace_add('write', self.update_service_preview)
        self.new_user.trace_add('write', self.update_service_fields)
        self.service.trace_add('write', self.update_service_fields)
        self.hasp_widget = self.check('Установить драйвер аппаратной лицензии HASP', self.hasp)
        self.hardware_widget = self.check('Использовать аппаратные лицензии', self.hardware)
        self.form = saved
        self.linux_service_frame = tk.Frame(self.form, bg=colors['panel'])
        saved, self.form = self.form, self.linux_service_frame
        first_linux_service = len(self.inputs)
        self.field('Порт агента службы', self.service_port)
        self.field('Имя файла службы · /etc/systemd/system/', self.linux_service_name)
        self.button('Подставить имя файла по порту', self.reset_linux_service_name)
        self.field('Папка данных сервера на Linux', self.linux_service_data)
        self.button('Подставить путь данных по порту', self.reset_linux_service_data)
        self.check('Включена отладка · SRV1CV8_DEBUG=-debug', self.service_debug)
        self.linux_service_controls = self.inputs[first_linux_service:]
        preview = self.app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT)
        preview.configure(textvariable=self.linux_service_preview)
        preview.pack(fill=tk.X, pady=(7, 0))
        delayed_wrap(preview)
        self.note('Штатный unit копируется в /etc/systemd/system/ с выбранным именем. После настройки включаются автозапуск и запуск службы. Существующий unit с таким именем не заменяется.')
        self.form = saved
        self.service_port.trace_add('write', self.update_linux_service_preview)
        self.linux_service_data.trace_add('write', self.update_linux_service_preview)
        self.linux_service_name.trace_add('write', self.update_linux_service_preview)
        self.service_debug.trace_add('write', self.update_linux_service_preview)
        ras_row = tk.Frame(self.form, bg=colors['panel'])
        ras_row.pack(fill=tk.X, pady=(10, 0))
        saved, self.form = self.form, ras_row
        self.ras_widget = self.check('Создать RAS · порт', self.ras_service)
        self.ras_widget.pack_configure(side=tk.LEFT, fill=tk.NONE)
        self.ras_widget.configure(command=self.update_ras_fields)
        self.ras_port_entry = self.app._entry(ras_row, self.ras_port)
        self.ras_port_entry.configure(width=8)
        self.ras_port_entry.pack(side=tk.LEFT, padx=(10, 0), pady=(7, 0))
        self.inputs.append(self.ras_port_entry)
        self.form = saved
        self.ras_agent_entry = self.field('Порт агента 1С, к которому подключается RAS', self.service_port)
        ras_preview = self.app._label(self.form, '', 9, 'muted', anchor=tk.W, justify=tk.LEFT)
        ras_preview.configure(textvariable=self.ras_preview)
        ras_preview.pack(fill=tk.X, pady=(5, 0))
        delayed_wrap(ras_preview)
        self.note('RAS создаётся с автозапуском и запускается после установки. Существующая служба с таким именем не заменяется. Если служба сервера 1С остановлена, запустите её отдельно для доступа к кластеру.')
        self.ras_port.trace_add('write', self.update_ras_preview)
        self.service_port.trace_add('write', self.update_ras_preview)
        self.setting_inputs.extend(self.inputs[first:])
        self.note('Установка начинается только кнопкой «Установить» после завершения загрузки. Автоматическая перезагрузка отключена.')
        self.button('О встроенном 7-Zip · лицензия', self.show_archive_license)
        table = self.split_log()
        self.catalog_tabs = ttk.Notebook(table, style='Platform.TNotebook')
        self.catalog_tabs.grid(row=0, column=0, sticky='nsew')
        self.release_tree = self.make_table('Версии', (('version', 'Версия', 170), ('date', 'Дата выхода', 150)), self.version_search, self.release_selected)
        self.distribution_tree = self.make_table('Дистрибутивы', (('title', 'Дистрибутив', 650), ('architecture', 'Разрядность', 110)), self.distribution_search, self.prepare_distribution)
        self.release_sorter = TableSorter(self.release_tree, colors=colors)
        self.release_tree.bind('<Double-1>', self.release_selected)
        self.release_tree.bind('<Return>', self.release_selected)
        self.distribution_tree.bind('<<TreeviewSelect>>', self.distribution_selected)
        self.distribution_tree.bind('<Double-1>', self.prepare_distribution)
        self.distribution_tree.bind('<Return>', self.prepare_distribution)
        self.version_search.trace_add('write', lambda *args: self.render_releases())
        self.distribution_search.trace_add('write', lambda *args: self.render_distributions())
        for tree in (self.release_tree, self.distribution_tree):
            tree.bind('<Button-1>', self.catalog_input)
            tree.bind('<KeyPress>', self.catalog_input)
        self.selection_note = app._label(self.right, 'Войдите в ИТС для получения списка релизов', 10, 'muted', anchor=tk.W)
        self.selection_note.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        self.execute_button.configure(state=tk.DISABLED)
        self.update_service_fields()
        self.system_changed()

    def show_mode(self, mode):
        style, colors = ttk.Style(self), self.c
        style.configure('Platform.TNotebook', background=colors['panel'], borderwidth=0, tabmargins=(0, 3, 0, 0))
        style.configure('Platform.TNotebook.Tab', padding=(12, 7), font=('Segoe UI', 10))
        style.map('Platform.TNotebook.Tab', background=[('selected', colors['selected']), ('!selected', colors['panel'])],
                  foreground=[('selected', colors['fg']), ('!selected', colors['muted'])])

    def set_compact(self, compact):
        self.selection_note.grid_remove() if compact else self.selection_note.grid()

    def catalog_input(self, event):
        return 'break' if self.running else None

    def make_table(self, title, columns, search, choose):
        frame = tk.Frame(self.catalog_tabs, bg=self.c['panel'])
        self.catalog_tabs.add(frame, text=title)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        toolbar = tk.Frame(frame, bg=self.c['panel'])
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 7))
        toolbar.columnconfigure(1, weight=1)
        self.app._label(toolbar, 'Поиск', 9, 'muted').grid(row=0, column=0, padx=(0, 7))
        search_entry = self.app._entry(toolbar, search)
        search_entry.grid(row=0, column=1, sticky='ew')
        choose_button = self.app._button(toolbar, 'Выбрать', choose)
        choose_button.grid(row=0, column=2, padx=(7, 0))
        self.inputs.extend((search_entry, choose_button))
        tree = ttk.Treeview(frame, columns=tuple(key for key, _, _ in columns), show='headings', selectmode='browse', height=4)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=100, stretch=key != 'architecture')
        tree.grid(row=1, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(frame, command=tree.yview)
        vertical.grid(row=1, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        horizontal.grid(row=2, column=0, sticky='ew')
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def add_feature(self, key, title, selected=False):
        variable = tk.BooleanVar(value=selected)
        widget = tk.Checkbutton(self.feature_frame, text=title, variable=variable, bg=self.c['panel'],
                                fg=self.c['fg'], activebackground=self.c['panel'], anchor=tk.W,
                                font=('Segoe UI', 10), wraplength=290, justify=tk.LEFT)
        widget.pack(fill=tk.X)
        delayed_wrap(widget)
        self.feature_values[key], self.feature_widgets[key], self.feature_titles[key] = variable, widget, title
        widget.configure(command=self.update_features)
        self.inputs.append(widget)
        return widget

    def check(self, title, variable):
        widget = super().check(title, variable)
        delayed_wrap(widget)
        return widget

    def button(self, title, command):
        widget = self.app._button(self.form, title, command)
        widget.pack(fill=tk.X, pady=(7, 0))
        self.inputs.append(widget)
        return widget

    def combo(self, title, variable, values):
        caption = self.app._label(self.form, title, 10, 'muted')
        caption.pack(anchor=tk.W, pady=(8, 4))
        widget = ttk.Combobox(self.form, textvariable=variable, values=values, state='readonly', font=('Segoe UI', 10))
        widget.caption_label = caption
        widget.pack(fill=tk.X)
        widget.bind('<MouseWheel>', lambda event: 'break')
        widget.bind('<Shift-MouseWheel>', lambda event: 'break')
        self.inputs.append(widget)
        self.combos.append(widget)
        return widget

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.app.root, title='Папка дистрибутивов 1С')
        if folder:
            self.folder.set(folder)

    def show_archive_license(self):
        folder = Path(seven_zip()).parent
        content = '\n\n'.join((folder / name).read_text(encoding='utf-8') for name in ('NOTICE.txt', 'License.txt'))
        window = tk.Toplevel(self.app.root)
        window.title('7-Zip · лицензия и исходный код')
        window.geometry('760x560')
        window.transient(self.app.root)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        text = tk.Text(window, wrap='word', bg=self.c['panel'], fg=self.c['fg'], font=('Segoe UI', 10), padx=14, pady=12)
        text.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        text.configure(yscrollcommand=scroll.set)
        text.insert('1.0', content)
        text.configure(state=tk.DISABLED)
        self.app._button(window, 'Закрыть', window.destroy).grid(row=1, column=0, columnspan=2, sticky='e', padx=12, pady=10)

    def finish(self, text='', error=''):
        self._editing_install_settings = False
        super().finish(text, error)
        for widget in self.combos:
            widget.configure(state='readonly')
        self.update_actions()
        self.update_service_fields()
        self.update_features()

    def events(self, events):
        for event in events:
            if event.get('cancellable') is False:
                self.cancel_button.configure(state=tk.DISABLED, text='Ожидание установщика…')
            elif event.get('cancellable') is True:
                self.cancel_button.configure(state=tk.NORMAL, text='Прервать запрос')
        super().events(events)

    def update_service_fields(self, *args):
        if self.new_user.get():
            self.service_user.set('USR1CV8')
        enabled = self.service.get() and str(self.service_widget.cget('state')) != 'disabled' and (not self.running or self._editing_install_settings)
        state = tk.NORMAL if enabled else tk.DISABLED
        self.new_user_widget.configure(state=state)
        self.service_user_entry.configure(state='readonly' if enabled and self.new_user.get() else state)
        for widget in (self.service_password_entry, self.service_confirm_entry):
            widget.configure(state=state)
        for widget in self.service_controls:
            widget.configure(state='readonly' if enabled and isinstance(widget, ttk.Combobox) else state)
        for widget in self.linux_service_controls:
            widget.configure(state=state)
        self.update_service_preview()
        self.update_linux_service_preview()

    def service_choice(self):
        self.update_service_fields()

    def update_ras_fields(self):
        available = self._ras_available and (not self.running or self._editing_install_settings)
        self.ras_widget.configure(state=tk.NORMAL if available else tk.DISABLED,
                                  text='Создать RAS · порт' + (' · нет в дистрибутиве' if self._ras_verified and not self._ras_available else ''))
        state = tk.NORMAL if available and self.ras_service.get() else tk.DISABLED
        self.ras_port_entry.configure(state=state)
        self.ras_agent_entry.configure(state=state)
        self.update_ras_preview()

    def update_ras_preview(self, *args):
        if not hasattr(self, 'ras_port_entry'):
            return
        try:
            defaults = ras_service_options(dict(service_port=self.service_port.get()))
            automatic = str(defaults['port'])
            if self.ras_port.get() == self._ras_port_template:
                self._ras_port_template = automatic
                if self.ras_port.get() != automatic:
                    self.ras_port.set(automatic)
            else:
                self._ras_port_template = automatic
            version = self.selected_version()
            options = ras_service_options(dict(service_port=self.service_port.get(), ras_port=self.ras_port.get()),
                                          version if version != 'версия' else '8.3.0.0', windows=not self.is_linux())
            description = '/etc/systemd/system/' + options['name'] if self.is_linux() else (
                'Имя службы: ' + options['name'] + f"\nОтображаемое имя: 1C:RAS | {options['port']} | {version}")
            self.ras_preview.set(description + '\nАгент 1С: ' + options['endpoint'])
        except ValueError as exc:
            self.ras_preview.set(str(exc))

    def reset_linux_service_name(self):
        try:
            options = linux_service_options(dict(service_port=self.service_port.get()))
        except ValueError as exc:
            self.app._panel_error(self, str(exc))
            return
        self._linux_name_template = options['name']
        self.linux_service_name.set(options['name'])

    def update_linux_service_preview(self, *args):
        try:
            defaults = linux_service_options(dict(service_port=self.service_port.get()))
            automatic = defaults['name']
            if self.linux_service_data.get() == self._linux_data_template:
                self._linux_data_template = defaults['data']
                if self.linux_service_data.get() != defaults['data']:
                    self.linux_service_data.set(defaults['data'])
            else:
                self._linux_data_template = defaults['data']
            if self.linux_service_name.get() == self._linux_name_template:
                self._linux_name_template = automatic
                if self.linux_service_name.get() != automatic:
                    self.linux_service_name.set(automatic)
            else:
                self._linux_name_template = automatic
            options = linux_service_options(dict(service_port=self.service_port.get(), service_debug=self.service_debug.get(),
                         linux_service_data=self.linux_service_data.get(), linux_service_name=self.linux_service_name.get()))
            self.linux_service_preview.set('/etc/systemd/system/' + options['name'] + '\n' + '\n'.join(environment_lines(options)))
        except ValueError as exc:
            self.linux_service_preview.set(str(exc))

    def reset_linux_service_data(self):
        try:
            value = linux_service_options(dict(service_port=self.service_port.get()))['data']
        except ValueError as exc:
            self.app._panel_error(self, str(exc))
            return
        self._linux_data_template = value
        self.linux_service_data.set(value)

    def selected_version(self):
        row = self.selected_distribution() if hasattr(self, 'distribution_tree') else None
        if row:
            return row['version']
        if self.metadata:
            return self.metadata['properties']['ProductVersion']
        selection = self.release_tree.selection() if hasattr(self, 'release_tree') else ()
        return self.releases[int(selection[0])]['version'] if selection else 'версия'

    def reset_service_display(self):
        self.service_display_name.set(display_template(self.service_role.get(), self.service_port.get(), self.selected_version()))
        self._display_template = self.service_display_name.get()

    def update_service_preview(self, *args):
        version = self.selected_version()
        template = display_template(self.service_role.get(), self.service_port.get(), version)
        if not self.service_display_name.get() or self.service_display_name.get() == self._display_template:
            self.service_display_name.set(template)
        self._display_template = template
        try:
            metadata = dict(properties=dict(ProductVersion=version))
            options = service_options(metadata, dict(service_role=self.service_role.get(), service_port=self.service_port.get(),
                         srvinfo=self.srvinfo.get(), service_debug=self.service_debug.get(), service_display_name=self.service_display_name.get()))
            directory = Path(self.directory.get().strip()) if self.directory.get().strip() else Path(default_srvinfo()).parent / version
            self.service_preview.set(service_image_path(str(directory / 'bin' / 'ragent.exe'), options))
        except ValueError as exc:
            self.service_preview.set(str(exc))

    def choose_srvinfo(self):
        folder = filedialog.askdirectory(parent=self.app.root, title='Папка данных сервера 1С · srvinfo', initialdir=self.srvinfo.get())
        if folder:
            self.srvinfo.set(folder)

    def update_features(self):
        if self._updating_features:
            return
        self._updating_features = True
        try:
            allowed = set(linux_platform.FEATURES) if self.is_linux() else set(self.feature_widgets) - set(linux_platform.FEATURES)
            row = self.selected_distribution() if hasattr(self, 'distribution_tree') else None
            current = self.metadata and (not row or not self.prepared_key or self.prepared_key == self.distribution_key(row))
            metadata = self.metadata if current else None
            verified = bool(metadata and metadata.get('capabilities_verified', True))
            available = ({item['id'] for item in metadata['features']} if self.is_linux() else installable_features(metadata)) if verified else allowed
            bundled = metadata.get('bundled_features', {}) if verified else {}
            busy = self.running and not self._editing_install_settings
            for key, widget in self.feature_widgets.items():
                if key not in allowed:
                    widget.pack_forget()
                    continue
                widget.pack(fill=tk.X)
                reason = ' · нет в дистрибутиве' if verified and key not in available else ''
                if verified and not self.is_linux() and key not in available and any(item['id'] == key for item in metadata['features']):
                    reason = ' · отключён в MSI (Level=0)'
                enabled = key in available and not busy
                if verified and key not in available and self.feature_values[key].get():
                    self.feature_values[key].set(False)
                if key in bundled:
                    parent = bundled[key]['with_feature']
                    selected = bool(self.feature_values[parent].get()) if parent in self.feature_values else any(self.feature_values[k].get() for k in available if k not in bundled)
                    self.feature_values[key].set(selected)
                    # A DEB package cannot be split into separate binaries.
                    # Show one selectable package with its included components,
                    # instead of an automatically checked, disabled duplicate.
                    widget.pack_forget()
                    widget.configure(state=tk.DISABLED if busy else tk.NORMAL)
                    continue
                included = [self.feature_titles[child] for child, details in bundled.items()
                            if details['with_feature'] == key and child in self.feature_titles]
                if included:
                    reason += ' · включает: ' + ', '.join(included)
                widget.configure(state=tk.NORMAL if enabled else tk.DISABLED, text=self.feature_titles[key] + reason)
            if not verified:
                note = 'Выберите нужные компоненты. ' + ('Для проверки RUN нажмите «Проверить состав на ВМ».' if metadata and self.is_linux() else '«Выбрать» загрузит и проверит дистрибутив.')
                note += ' После проверки отсутствующие компоненты будут отключены.'
            else:
                note = 'Состав подтверждён: ' + metadata.get('capabilities_source', 'метаданные выбранного дистрибутива')
                if self.is_linux() and metadata.get('format') == 'deb':
                    note += '\nDEB: каталог фиксирован пакетами; языковые ресурсы устанавливаются из комплекта.'
                    if any(not details['with_feature'] for details in bundled.values()):
                        note += ' Общие компоненты входят в выбранные пакеты.'
                elif self.is_linux() and not metadata.get('directory_option'):
                    note += '\nRUN не поддерживает изменение каталога установки.'
            self.capabilities_note.configure(text=note)
            server = 'server' if self.is_linux() else 'CSServer'
            service_available = server in available
            if verified and server not in available and self.service.get():
                self.service.set(False)
            self.service_widget.configure(state=tk.NORMAL if service_available and not busy else tk.DISABLED)
            ras_features = set(metadata.get('ras_features', [])) if verified else (
                {'server', 'server_admin'} if self.is_linux() else {'CSServer', 'CSClient'})
            self._ras_verified = verified
            self._ras_available = bool(ras_features & available or 'common' in ras_features and available)
            if verified and not self._ras_available:
                self.ras_service.set(False)
            self.update_parameter_fields(metadata, verified, busy)
            self.update_service_fields()
            self.update_ras_fields()
        finally:
            self._updating_features = False

    def update_parameter_fields(self, metadata, verified, busy):
        metadata = metadata or {}
        directory = not verified or (bool(metadata.get('directory_option')) if self.is_linux() else
                                     'directories' not in metadata or 'INSTALLDIR' in metadata['directories'])
        self.directory_entry.configure(state=tk.NORMAL if directory and not busy else tk.DISABLED)
        if verified and not directory and self.directory.get():
            self.directory.set('')
        self.language_combo.caption_label.configure(text='Языковые ресурсы' if self.is_linux() else 'Основной язык интерфейса')
        if verified and self.is_linux() and metadata.get('format') == 'deb':
            self.language_combo.configure(values=('Языковые ресурсы из DEB-комплекта',), state=tk.DISABLED)
            self.language.set('Языковые ресурсы из DEB-комплекта')
        else:
            if not verified:
                languages = list(LANGUAGES)
            elif self.is_linux():
                languages = [title for title, code in LANGUAGES.items() if code in metadata.get('languages', [])]
            else:
                feature_ids = installable_features(metadata)
                languages = [title for title, code in LANGUAGES.items() if code in feature_ids] if 'Languages' in feature_ids else ['English']
            self.language_combo.configure(values=languages, state='readonly' if languages and not busy else tk.DISABLED)
            if verified and languages and self.language.get() not in languages:
                self.language.set(languages[0])
        hasp = not self.is_linux() and (not verified or bool(metadata.get('hasp_driver')))
        self.hasp_widget.configure(state=tk.NORMAL if hasp and not busy else tk.DISABLED,
                                  text='Установить драйвер аппаратной лицензии HASP' + (' · нет установщика в комплекте' if verified and not hasp else ''))
        if not hasp:
            self.hasp.set(False)
        hardware = not self.is_linux() and (not verified or 'USEHWLICENSES' in metadata.get('properties', {}))
        self.hardware_widget.configure(state=tk.NORMAL if hardware and not busy else tk.DISABLED,
                                      text='Использовать аппаратные лицензии' + (' · параметр отсутствует в MSI' if verified and not hardware else ''))
        if not hardware:
            self.hardware.set(False)

    def is_linux(self):
        return self.system.get().startswith('Linux')

    def system_changed(self, event=None):
        if self.running:
            return
        self.metadata, self.prepared_key, self._ssh_verified = None, None, None
        if self.is_linux():
            self.ssh_frame.pack(fill=tk.X, after=self.system_combo)
            self.windows_frame.pack_forget()
            self.linux_service_frame.pack(fill=tk.X, after=self.service_widget)
        else:
            self.ssh_frame.pack_forget()
            self.linux_service_frame.pack_forget()
            self.windows_frame.pack(fill=tk.X, after=self.service_widget)
        self.service_widget.configure(text='Развернуть и запустить службу сервера 1С' if self.is_linux() else 'Установить сервер как службу Windows')
        self.update_features()
        self.directory.set('')
        self.render_distributions()
        self.update_actions()
        self.update_service_preview()

    def connection(self):
        return linux_platform.ssh_settings(dict(host=self.ssh_host.get(), port=self.ssh_port.get(), user=self.ssh_user.get(),
                    password=self.ssh_password.get(), use_key=self.ssh_use_key.get(), key=self.ssh_key.get(),
                    sudo_password=self.sudo_password.get(), host_keys={**self.app._settings.get('its_ssh_hosts', {}), **self.app._settings.get('ssh_hosts', {})}))

    def check_ssh(self, install_after=False):
        if self.running:
            return
        try:
            values = self.connection()
        except ValueError as exc:
            self.app._panel_error(self, str(exc))
            return
        def operation(event):
            event(dict(kind='stage', text='Проверка SSH: ' + values['host']))
            try:
                return linux_platform.probe(values)
            except linux_platform.UnknownHostKey as exc:
                return dict(host_key=exc.key)
        if self.start():
            self.allow_setting_edits()
            self.work(operation, lambda result: self.ssh_checked(result, values, install_after))

    def ssh_checked(self, result, values, install_after):
        if result.get('host_key') and self.defer_until_visible(lambda: self.ssh_checked(result, values, install_after)):
            return
        self.finish('Проверка SSH завершена')
        if result.get('host_key'):
            key = result['host_key']
            if messagebox.askyesno('Первое подключение SSH', 'Адрес: ' + key['host'] + '\nКлюч: ' + key['algorithm'] +
                                  '\nОтпечаток: ' + key['fingerprint'] + '\n\nСверьте отпечаток с ВМ. Доверять этому ключу?', parent=self.app.root, default='no'):
                self.app._settings.setdefault('ssh_hosts', {})[key['host']] = key
                self.app._save_settings()
                self.check_ssh(install_after)
            return
        self._ssh_verified = values
        self.app.saved_servers.remember_ssh(self, values)
        self.append(result.get('description', ''))
        if install_after:
            self.execute()

    def selected_distribution(self):
        selection = self.distribution_tree.selection()
        return self.distributions[int(selection[0])] if selection else None

    @staticmethod
    def distribution_key(row):
        return row.get('url', row.get('title', '')), row['version'], row.get('os', 'windows')

    def update_actions(self):
        row = self.selected_distribution()
        self.execute_button.configure(state=tk.NORMAL if not self.running and (self.metadata or row) else tk.DISABLED)
        self.execute_button.configure(text='Проверить состав на ВМ' if self.needs_inspection() else 'Установить')

    def needs_inspection(self):
        return bool(self.metadata and self.metadata.get('kind') == 'linux' and self.metadata.get('format') == 'run'
                    and self.metadata.get('capabilities_verified') is False)

    def inspect_linux(self):
        if self.running:
            return
        try:
            connection = self.connection()
        except ValueError as exc:
            self.app._panel_error(self, str(exc))
            return
        if self._ssh_verified != connection:
            self.check_ssh(install_after=True)
            return
        metadata = self.metadata
        if self.start():
            self.allow_setting_edits()
            self.work(lambda emit: linux_platform.inspect_distribution(metadata, connection, emit, self.cancel_event), self.inspection_ready)

    def inspection_ready(self, result):
        capabilities = dict(result['capabilities'])
        features = capabilities.pop('features')
        self.metadata.update(capabilities, remote_prepared=result['remote_prepared'])
        self.metadata['features'] = [dict(id=key, title=linux_platform.FEATURES[key], parent='', display='1', level='200') for key in features]
        self.package_ready(self.metadata)
        self.append('Состав и параметры RUN подтверждены. Выберите компоненты и нажмите «Установить». Переданный дистрибутив будет использован повторно.')

    def render_releases(self):
        old = self.release_tree.selection()
        self.release_tree.delete(*self.release_tree.get_children())
        search = self.version_search.get().strip().casefold()
        for index, row in enumerate(self.releases):
            if search in (row['version'] + ' ' + row['date']).casefold():
                self.release_tree.insert('', 'end', iid=str(index), values=(row['version'], row['date']))
        self.release_sorter.apply({str(index): dict(version='.'.join(part.zfill(10) for part in row['version'].split('.')),
                                  date=datetime.strptime(row['date'], '%d.%m.%Y').strftime('%Y%m%d') if row['date'] else '')
                                  for index, row in enumerate(self.releases)})
        if old and self.release_tree.exists(old[0]):
            self.release_tree.selection_set(old)

    def render_distributions(self):
        old = self.distribution_tree.selection()
        self.distribution_tree.delete(*self.distribution_tree.get_children())
        search = self.distribution_search.get().strip().casefold()
        for index, row in enumerate(self.distributions):
            if (row.get('os', 'windows') == 'linux') != self.is_linux() or search not in row['title'].casefold():
                continue
            # RPM packages do not apply to the selected Ubuntu/Debian mode.
            if self.is_linux() and '.rpm' in row.get('filename', '').casefold():
                continue
            self.distribution_tree.insert('', 'end', iid=str(index), values=(row['title'], row['architecture']))
        if old and self.distribution_tree.exists(old[0]):
            self.distribution_tree.selection_set(old)
        self.update_actions()

    def load_catalog(self):
        if self.running:
            return
        username, password, branch = self.username.get(), self.password.get(), self.branch.get()
        remember = self.remember.get()
        portal = Portal(self.cancel_event)
        def operation(event):
            event(dict(kind='stage', text='Вход в ИТС и получение релизов ' + branch))
            portal.login(username, password)
            return dict(portal=portal, releases=portal.releases(branch),
                        credentials=protect(dict(username=username, password=password)) if remember else None)
        if self.start():
            self.allow_setting_edits()
            self.work(operation, self.catalog_loaded)

    def update_ras_note(self):
        tools = managed_tools()
        self.ras_tools_note.configure(text='RAS консоли: ' + (tools[0][2] if tools else 'комплект отсутствует'))

    def update_ras(self):
        if self.running:
            return
        username, password, remember = self.username.get().strip(), self.password.get(), self.remember.get()
        if not username or not password:
            self.app._panel_error(self, 'Введите логин и пароль ИТС для проверки и обновления RAS консоли')
            return
        def operation(event):
            credentials = protect(dict(username=username, password=password)) if remember else None
            portal = Portal(self.cancel_event)
            event(dict(kind='stage', text='Вход в ИТС для обновления RAS консоли'))
            portal.login(username, password)
            return dict(update_tools(portal, event, self.cancel_event), credentials=credentials)
        if self.start():
            self.allow_setting_edits()
            self.work(operation, self.ras_updated)

    def ras_updated(self, result):
        if result['credentials']:
            self.app._settings['its_credentials'] = result['credentials']
        else:
            self.app._settings.pop('its_credentials', None)
        self.app._save_settings()
        self.update_ras_note()
        self.finish(('RAS обновлён до ' if result['updated'] else 'Уже установлен последний RAS: ') + result['version'])

    def catalog_loaded(self, result):
        self.portal, self.releases, self.metadata = result['portal'], result['releases'], None
        self.release_tree.delete(*self.release_tree.get_children())
        self.distribution_tree.delete(*self.distribution_tree.get_children())
        self.distributions = []
        self.render_releases()
        self.catalog_tabs.select(0)
        if result['credentials']:
            self.app._settings['its_credentials'] = result['credentials']
        else:
            self.app._settings.pop('its_credentials', None)
        self.app._save_settings()
        self.finish('Доступных релизов: ' + str(len(self.releases)) + '. Выберите версию.')

    def release_selected(self, event=None):
        if self.running or not self.release_tree.selection() or not self.portal:
            return
        row = self.releases[int(self.release_tree.selection()[0])]
        self.metadata = None
        if self.start():
            self.allow_setting_edits()
            self.work(lambda emit: self.portal.distributions(row), lambda result: self.distributions_loaded(row, result))

    def distributions_loaded(self, release, rows):
        self.distributions = rows
        self.render_distributions()
        self.catalog_tabs.select(1)
        self.selection_note.configure(text='Версия ' + release['version'] + ' · ' + release['date'])
        self.finish('Выделите дистрибутив и нажмите «Выбрать» или «Установить»')

    def distribution_selected(self, event=None):
        # Selecting a row updates availability, never the user's choices.
        self.update_features()
        self.update_service_preview()
        self.update_actions()

    def prepare_distribution(self, event=None, install_after=False):
        if self.running or not self.distribution_tree.selection() or not self.portal:
            return
        row = dict(self.distributions[int(self.distribution_tree.selection()[0])])
        self.distribution_selected()
        key = self.distribution_key(row)
        if self.metadata and self.prepared_key == key:
            if install_after:
                self.execute()
            else:
                self.app._panel_status(self, 'Дистрибутив уже готов. Выберите параметры установки.')
            return
        folder = self.folder.get().strip()
        if not folder or not Path(folder).is_absolute():
            self.app._panel_error(self, 'Выберите полный путь папки для дистрибутивов')
            return
        self.metadata = None
        is_linux = self.is_linux()
        def operation(emit):
            emit(dict(kind='stage', text='Скачивание 1С ' + row['version']))
            workspace = create_workspace(folder, row['version'])
            path = self.portal.download(row, workspace['root'], emit)
            if is_linux:
                metadata = linux_platform.linux_metadata(path, row, emit, self.cancel_event)
            else:
                msi = extract_distribution(path, emit, self.cancel_event)
                metadata = Msi().metadata(msi, row['version'])
            metadata.update(workspace=workspace, distribution_key=key)
            return metadata
        if self.start():
            self.allow_setting_edits()
            self.work(operation, lambda metadata: self.package_ready(metadata, install_after), history=dict(operation='Загрузка платформы 1С', database=row['version'],
                      server='releases.1c.ru', executor=os.environ.get('COMPUTERNAME', 'Windows')))

    def allow_setting_edits(self):
        """Catalog/download/inspection jobs never own the installation draft."""
        self._editing_install_settings = True
        for widget in self.setting_inputs:
            widget.configure(state='readonly' if isinstance(widget, ttk.Combobox) else tk.NORMAL)
        self.update_features()

    def package_ready(self, metadata, install_after=False):
        self.metadata = metadata
        self.prepared_key = metadata.get('distribution_key')
        available = {row['id']: row for row in metadata['features']}
        for key, row in available.items():
            if key not in self.feature_values and int(row.get('display') or 0) > 0 and row.get('parent', '') in ('', 'Advanced') and key not in ('Languages', 'Advanced'):
                widget = self.add_feature(key, row.get('title') or key, False)
                self.setting_inputs.append(widget)
        self.update_features()
        self.append('Дистрибутив готов: ' + metadata['path'])
        self.finish('Выберите компоненты и нажмите «Установить»')
        if install_after:
            self.execute()

    def settings(self):
        available = {row['id'] for row in self.metadata['features']} if self.metadata else set()
        bundled = self.metadata.get('bundled_features', {}) if self.metadata else {}
        return dict(features=[key for key, variable in self.feature_values.items() if variable.get() and key in available and key not in bundled],
                    language=LANGUAGES.get(self.language.get(), 'RU'), directory=self.directory.get(),
                    service=self.service.get(), new_user=self.new_user.get(), service_user=self.service_user.get(),
                    service_password=self.service_password.get(), service_password_confirm=self.service_confirm.get(),
                    service_role=self.service_role.get(), service_port=self.service_port.get(), srvinfo=self.srvinfo.get(),
                    service_debug=self.service_debug.get(), service_display_name=self.service_display_name.get(),
                    ras_service=self.ras_service.get(), ras_port=self.ras_port.get(),
                    linux_service_data=self.linux_service_data.get(), linux_service_name=self.linux_service_name.get(),
                    hasp=self.hasp.get(), hardware_licenses=self.hardware.get())

    def execute(self):
        if self.running:
            return
        row = self.selected_distribution()
        if not self.metadata or row and self.prepared_key != self.distribution_key(row):
            self.prepare_distribution(install_after=True)
            return
        if self.needs_inspection():
            self.inspect_linux()
            return
        metadata, settings = self.metadata, self.settings()
        try:
            is_linux = metadata.get('kind') == 'linux'
            if is_linux:
                linux_platform.validate_settings(metadata, settings)
                connection = self.connection()
                # Never send Windows service passwords to Linux.
                settings = linux_platform.installation_settings(settings)
                if self._ssh_verified != connection:
                    self.check_ssh(install_after=True)
                    return
            else:
                install_properties(metadata, settings)
        except ValueError as exc:
            self.app._panel_error(self, str(exc))
            return
        cleanup = self.delete_after.get()
        def operation(emit):
            if is_linux:
                result = linux_platform.install(metadata, settings, connection, emit, self.cancel_event, cleanup=cleanup)
            else:
                result = install_msi(metadata, settings, emit, self.cancel_event)
                try:
                    result.update(versions=find_all_rac(), agents=installed_agents())
                except Exception as exc:
                    emit(dict(kind='message', text='ПРЕДУПРЕЖДЕНИЕ: платформа установлена, обнаружение версий: ' + str(exc)))
            result['cleaned'] = False
            if cleanup and metadata.get('workspace'):
                try:
                    cleanup_workspace(metadata['workspace'])
                    result['cleaned'] = True
                    emit(dict(kind='message', text='Скачанный архив и каталог распаковки удалены'))
                except (OSError, ValueError) as exc:
                    emit(dict(kind='message', text='ПРЕДУПРЕЖДЕНИЕ: платформа установлена; очистка файлов: ' + str(exc)))
            return result
        if self.start():
            self._editing_install_settings = False
            self.progress_state.complete_task = True
            self.work(operation, self.installed, history=dict(operation='Установка платформы 1С',
                      database=metadata['properties'].get('ProductVersion', ''),
                      server=connection['host'] if is_linux else os.environ.get('COMPUTERNAME', 'localhost'), executor='Linux · SSH' if is_linux else 'Windows'))

    def installed(self, result):
        if 'versions' in result:
            self.app.rac_map = result['versions']
            self.app.services.agents = result['agents']
            self.app.services.version_entry.configure(values=sorted(result['agents'], reverse=True))
        if result.get('cleaned'):
            self.metadata, self.prepared_key = None, None
        self.finish('Установка завершена' + (' · требуется перезагрузка Windows' if result['reboot'] else ''))
