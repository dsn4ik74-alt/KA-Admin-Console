"""1C Console: server lists, database search and the standard MMC console."""
import base64
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from console_core import (collect_bases, discover_local_lst, find_all_rac,
                          path_key, platform_roots, read_lst)
from local_ras import LocalRasManager
from accounts_ui import AccountsPanel
from bookmark_navigation import BookmarkBar
from services_ui import ServicesPanel, SERVICE_MODES
from administration_ui import SqlPanel, ArchivePanel, SQL_MODES
from postgres_ui import PostgresPanel, PG_MODES
from database_copy_ui import SqlCopyPanel, PostgresCopyPanel
from clipboard_shortcuts import bind_entry_shortcuts, install_edit_shortcuts
from table_sorting import text_key
from cluster_infobase_ui import ClusterInfobasePanel
from activity_store import ActivityStore
from activity_ui import ActivityHub, HomePanel
from branding import tk_icon
from ui_geometry import delayed_wrap
from log_colors import configure_log, insert_log
from platform_install_ui import PlatformInstallPanel
from configuration_install_ui import ConfigurationInstallPanel
from windirstat_ui import WinDirStatPanel
from postgres_install_ui import PostgresInstallPanel
from linux_disk_ui import LinuxDiskPanel
from running_tasks import RunningTasks
from bookmark_navigation import PAGES
from form_notice import FormNotice
from saved_servers import SavedServers
from database_sizes import DatabaseSizes
from scheduler_ui import SchedulerPanel, install_schedule_button
from infobase_launcher import prepare_client, start_client, choose_open_mode
from infobase_address import infobase_address
from ui_theme import COLORS, WorkspaceSidebar, apply_styles
from web_publication_ui import WebPublicationPanel
from file_transfer_ui import FileTransferPanel
from postgres_runner import OperationCancelled

APP_NAME = "Консоль администратора КА"
APP_VERSION = "20.33.27"
ACTIVITY_FILE = Path.home() / '.1c_console_activity.json'
SETTINGS_FILE = Path.home() / ".1c_mmc_settings.json"
CACHE_FILE = Path.home() / ".1c_mmc_cache.dat"
_CACHE_KEY = b"1CMMC2024!secret"
C = COLORS
COLUMNS = ("server", "port", "version", "descr", "base", "cluster", "address")
TABLES = {
    "servers": [("server", "Сервер", 240), ("port", "Порт", 90),
                ("version", "Версия платформы", 160), ("descr", "Описание", 430)],
    "bases": [("base", "База", 340), ("address", "Адрес базы · сервер кластера", 660)],
}


def encrypt(text):
    return base64.b64encode(bytes(b ^ _CACHE_KEY[i % len(_CACHE_KEY)]
                                 for i, b in enumerate(text.encode("utf-8")))).decode()


def decrypt(text):
    try:
        return bytes(b ^ _CACHE_KEY[i % len(_CACHE_KEY)]
                     for i, b in enumerate(base64.b64decode(text))).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""


def load_cache():
    try:
        lines = CACHE_FILE.read_text(encoding="utf-8").splitlines()
        return decrypt(lines[0]) if lines else "", decrypt(lines[1]) if len(lines) > 1 else ""
    except (OSError, UnicodeError):
        return "", ""


def save_cache(user, password):
    CACHE_FILE.write_text(encrypt(user) + "\n" + encrypt(password) + "\n", encoding="utf-8")


def load_settings():
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def write_lst_entry(path, server, port, version, description):
    def escape(value):
        return str(value).replace('"', '""').replace("\r", " ").replace("\n", " ")
    port = int(port)
    if not 0 < port <= 65535:
        raise ValueError("Некорректный порт сервера")
    description = escape(f"{version} | {port} | {description}")
    Path(path).write_text(f'{{1,\n{{"tcp","{escape(server)}",{port},"{description}"}}\n}}\n', encoding="utf-8")


def open_mmc():
    for root in platform_roots():
        for name in ("1CV8 Servers (x86-64).msc", "1CV8 Servers.msc"):
            path = root / "common" / name
            if path.is_file():
                subprocess.Popen(["mmc.exe", str(path)])
                return True
    return False


def ensure_administrator():
    """Request elevation before constructing the UI or starting discovery."""
    if os.name != "nt" or ctypes.windll.shell32.IsUserAnAdmin():
        return True
    marker = "--console-elevated"
    if marker in sys.argv:
        ctypes.windll.user32.MessageBoxW(None, "Не удалось получить права администратора.", APP_NAME, 0x10)
        return False
    arguments = list(sys.argv[1:])
    if not getattr(sys, "frozen", False):
        arguments.insert(0, str(Path(__file__).resolve()))
    arguments.append(marker)
    execute = ctypes.windll.shell32.ShellExecuteW
    execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    execute.restype = ctypes.c_void_p
    result = execute(None, "runas", sys.executable, subprocess.list2cmdline(arguments), os.getcwd(), 1)
    if not result or result <= 32:
        ctypes.windll.user32.MessageBoxW(
            None, "Запуск с правами администратора отменён или недоступен. Консоль не запущена.", APP_NAME, 0x30)
    return False  # Only the elevated process may create the application.


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.report_callback_exception = self._report_callback_exception
        install_edit_shortcuts(self.root)
        self.root.title(f"{APP_NAME} · {APP_VERSION}")
        self.root.geometry("1480x880")
        self.root.minsize(960, 540)
        self.root.configure(bg=C["bg"])
        self.colors = C
        self._app_icon = tk_icon(self.root)
        self.root.iconphoto(True, self._app_icon)
        self.root.option_add('*Text.selectForeground', C['fg'])
        self.root.option_add('*Entry.selectForeground', C['fg'])
        self._settings = load_settings()
        self._lst_path = self._settings.get("lst_path", "")
        self.all_items, self._all_bases, self._visible_items = [], [], []
        self.rac_map, self._backups = {}, {}
        self._opening_bases, self._opened_clients = {}, []
        self.mode = "servers"
        self.search_running = self._discovering = self._closed = False
        self._sort_col = self._filter_job = self._rendered_mode = None
        self._sort_reverse = False
        self._errors = []
        self._events = queue.Queue()
        self._stop = threading.Event()
        self.activity = ActivityStore(ACTIVITY_FILE)
        self.saved_servers = SavedServers(self)
        self.database_sizes = DatabaseSizes(self)
        self.hub = ActivityHub(self)
        self._base_pin = None
        self._last_activity_error = ''
        self.root.bind('<Destroy>', self._activity_destroyed, add='+')
        self._ras_manager = None
        self._build_ui()
        self._apply_theme()
        self._render()
        self._compact_mode = None
        self.root.bind("<Configure>", self._resize)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-f>", self._focus_search)
        self.root.bind("<F5>", lambda event: self._on_update_clicked())
        self._event_job = self.root.after(50, self._drain_events)
        self._switch_mode('home')
        self._discover(startup=True)

    def _activity_destroyed(self, event):
        if event.widget == self.root:
            self.activity.close(wait=True)

    def _report_callback_exception(self, kind, error, trace):
        if getattr(self, '_closed', False) or getattr(self, '_reporting_error', False):
            return
        self._reporting_error = True
        try:
            detail = ''.join(traceback.format_exception(kind, error, trace))
            secrets = []
            for owner in (self, *getattr(self, '_pages', {}).values()):
                for name in ('pwd_var', 'password', 'target_password', 'ib_password', 'db_password', 'service_password', 'service_confirm', 'ssh_password', 'sudo_password'):
                    variable = getattr(owner, name, None)
                    if isinstance(variable, tk.Variable):
                        try:
                            value = variable.get()
                        except tk.TclError:
                            continue
                        if isinstance(value, str) and value:
                            secrets.append(value)
            for value in sorted(set(secrets), key=len, reverse=True):
                detail = detail.replace(value, '[скрыто]')
            detail = 'Ошибка интерфейса:\n' + detail[-12000:]
            errors = list(getattr(self, '_errors', []))
            if not errors or errors[-1] != detail:
                errors.append(detail)
            self._set_errors(errors[-20:])
            self._status('Ошибка интерфейса. Нажмите «Подробности». Введённые данные остаются в окне.')
            panel = self._current_panel()
            if panel is not None:
                message = str(error)
                for value in sorted(set(secrets), key=len, reverse=True):
                    message = message.replace(value, '[скрыто]')
                self._panel_error(panel, message, title='Проверьте параметры' if issubclass(kind, ValueError) else 'Ошибка интерфейса')
        except (tk.TclError, AttributeError):
            # Closing a native dialog can deliver a final callback after destroy.
            pass
        finally:
            self._reporting_error = False

    def _label(self, parent, text, size=10, color="fg", bold=False, **kwargs):
        return tk.Label(parent, text=text, font=("Segoe UI", size, "bold" if bold else "normal"),
                        bg=parent.cget("bg"), fg=C[color], **kwargs)

    def _button(self, parent, text, command, primary=False):
        bg, hover = (C["accent"], C["hover"]) if primary else (C["button"], C["button_hover"])
        button = tk.Button(parent, text=text, command=command, font=("Segoe UI", 10, "bold" if primary else "normal"),
                           bg=bg, fg=C["accent_fg"] if primary else C["fg"], activebackground=hover,
                           activeforeground=C["accent_fg"] if primary else C["fg"],
                           disabledforeground='#d5dffa' if primary else C["muted"],
                           relief="flat", bd=0, cursor="hand2", padx=12, pady=7,
                           highlightthickness=1, highlightbackground=bg, highlightcolor=C['accent'])
        button._base_bg = bg
        button._hover_bg = hover
        button.bind("<Enter>", lambda event: button.configure(bg=button._hover_bg) if button.cget("state") != "disabled" else None)
        button.bind("<Leave>", lambda event: button.configure(bg=button._base_bg))
        return button

    def _entry(self, parent, variable, width=None, show=None):
        widget = tk.Entry(parent, textvariable=variable, width=width, show=show, font=("Segoe UI", 10),
                        bg=C["panel"], fg=C["fg"], insertbackground=C["accent"], selectbackground=C["text_selected"],
                        selectforeground=C['fg'], disabledbackground=C['button'], disabledforeground=C['muted'],
                        relief="flat", bd=5, highlightthickness=1,
                        highlightbackground=C["border"], highlightcolor=C["accent"])
        bind_entry_shortcuts(widget)
        return widget

    def _build_ui(self):
        self.sidebar = WorkspaceSidebar(self.root, self, C, APP_VERSION)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        # Pack the footer first: Tk reserves it before shrinking the content.
        footer = tk.Frame(self.root, bg=C["panel"], padx=20, pady=10)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        source = tk.Frame(self.root, bg=C["panel"], padx=16, pady=12)
        source.pack(fill=tk.X, padx=22, pady=(0, 12))
        self._label(source, "ИСТОЧНИК", 9, "accent", True).pack(side=tk.LEFT, padx=(0, 16))
        self.lst_lbl = self._label(source, "Поиск локальных списков…", 10, "muted", anchor=tk.W)
        self.lst_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        delayed_wrap(self.lst_lbl, minimum=200, inset=8)

        navigation = tk.Frame(self.root, bg=C["bg"])
        navigation.pack(fill=tk.X, padx=18, pady=(14, 10), before=source)
        self.home_badge = self.sidebar.brand
        self.home_button = self.sidebar.home
        self.btn_pick = self._button(source, 'Выбрать файл', self._pick_lst)
        self.btn_pick.pack(side=tk.RIGHT, padx=(8, 0))
        self.btn_local = self._button(source, 'Локальные списки', self._discover)
        self.btn_local.pack(side=tk.RIGHT)
        self.history_button = self._button(navigation, 'История', lambda: self.hub.dialog('history'))
        self.history_button.pack(side=tk.RIGHT)
        self.favorites_button = self._button(navigation, '☆ Избранное', lambda: self.hub.dialog('favorites'))
        self.favorites_button.pack(side=tk.RIGHT, padx=6)
        self.btn_update = self._button(navigation, "Обновить", self._on_update_clicked, primary=True)
        self.btn_update.pack(side=tk.RIGHT)
        self.bookmarks = BookmarkBar(self.sidebar.folder_host, C, self._settings.get("navigation_layout"),
                                     self._switch_mode, self._save_navigation, vertical=True)
        self.bookmarks.activate_tab = self._switch_mode
        self.bookmarks.can_close_tab = self._can_close_tab
        self.running_tasks = RunningTasks(navigation, C, self._switch_mode)
        self._settings["navigation_layout"] = self.bookmarks.layout.snapshot()
        self.bookmarks.pack(fill=tk.BOTH, expand=True)
        self._navigation_hint = self._label(self.root, self.bookmarks.breadcrumb(self.mode),
                                            9, "muted", anchor=tk.W)
        self._navigation_hint.pack(fill=tk.X, padx=22, pady=(0, 6), before=source)
        self._navigation_hint_pack = self._navigation_hint.pack_info()

        auth = tk.Frame(self.root, bg=C["panel"], padx=16, pady=12)
        auth.pack(fill=tk.X, padx=22, pady=(0, 12))
        user, password = load_cache()
        self.user_var = tk.StringVar(value=user)
        self.pwd_var = tk.StringVar(value=password)
        self._label(auth, "Доступ к кластеру", 10, bold=True).pack(side=tk.LEFT, padx=(0, 24))
        self._label(auth, "Логин", 10, "muted").pack(side=tk.LEFT, padx=(0, 8))
        self._entry(auth, self.user_var, width=18).pack(side=tk.LEFT, padx=(0, 18))
        self._label(auth, "Пароль", 10, "muted").pack(side=tk.LEFT, padx=(0, 8))
        self._entry(auth, self.pwd_var, width=18, show="•").pack(side=tk.LEFT)
        self._label(auth, "Без администратора — оставьте поля пустыми", 9, "muted").pack(side=tk.RIGHT)

        ras_options = tk.Frame(self.root, bg=C["bg"])
        ras_options.pack(fill=tk.X, padx=22, pady=(0, 12))
        self.local_ras_var = tk.BooleanVar(value=self._settings.get("local_ras", True))
        self.local_ras_check = tk.Checkbutton(
            ras_options, text="Запускать временный RAS на этом компьютере для операций с кластером",
            variable=self.local_ras_var, command=self._save_ras_mode, font=("Segoe UI", 10),
            bg=C["bg"], fg=C["fg"], selectcolor=C["panel"], activebackground=C["bg"],
            activeforeground=C["accent"], bd=0, highlightthickness=0,
        )
        self.local_ras_check.pack(side=tk.LEFT)

        search = tk.Frame(self.root, bg=C["bg"])
        search.pack(fill=tk.X, padx=22, pady=(0, 12))
        self._label(search, "Фильтр", 10, "muted").pack(side=tk.LEFT, padx=(0, 12))
        self.search_var = tk.StringVar()
        self.search_entry = self._entry(search, self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.search_entry.bind("<Return>", lambda event: self._do_rac_search() if self.mode == "bases" else self._render())
        self.search_entry.bind("<Escape>", lambda event: self.search_var.set(""))
        self._button(search, "Сбросить", lambda: self.search_var.set("")).pack(side=tk.LEFT, padx=(8, 0))
        self.search_var.trace_add("write", self._on_search)

        table_header = tk.Frame(self.root, bg=C["bg"])
        table_header.pack(fill=tk.X, padx=22, pady=(0, 10))
        self.lbl_count = self._label(table_header, "", 11, bold=True)
        self.lbl_count.pack(side=tk.LEFT)
        self.choose_base_button = self._button(table_header, 'Выбрать', self._choose_base)
        self.all_bases_button = self._button(table_header, 'Все', self._show_all_bases)
        self.open_base_button = self._button(table_header, 'Открыть', self._open_base, primary=True)
        self.cancel_open_button = self._button(table_header, 'Прервать открытие', self._cancel_base_opening)
        self.cancel_open_button.configure(fg=C['danger'])
        self._table_hint = self._label(table_header, "Двойной щелчок — открыть MMC    ·    Ctrl+C — копировать", 9, "muted")
        self._table_hint.pack(side=tk.RIGHT)
        table = tk.Frame(self.root, bg=C["border"], padx=1, pady=1)
        table.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="extended")
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", self._on_dbl)
        self.tree.bind("<Control-c>", self._copy_selection)
        self.tree.bind("<Control-C>", self._copy_selection)
        self.tree.bind('<<TreeviewSelect>>', self._remember_base, add='+')
        self.tree.bind('<<TreeviewSelect>>', lambda event: self._update_open_actions(), add='+')
        self.tree.bind('<Return>', self._open_base)
        self.base_context_menu = tk.Menu(self.tree, tearoff=False)
        self.tree.bind('<Button-3>', self._base_context)
        self.empty_lbl = self._label(table, "", 12, "muted", justify=tk.CENTER)

        self.btn_errors = self._button(footer, "Подробности", self._show_errors)
        self.progress = ttk.Progressbar(footer, length=140, mode="determinate")
        self.progress.pack(side=tk.RIGHT, padx=(12, 0))
        self.progress_pct = self._label(footer, "", 9, "accent", True, width=5)
        self.progress_pct.pack(side=tk.RIGHT)
        self.status_lbl = self._label(footer, "Готово", 9, "muted", anchor=tk.W, height=1)
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._source_panel = source
        self._source_pack = source.pack_info()
        self._navigation = navigation
        self._footer = footer
        self._server_layout = [(widget, widget.pack_info()) for widget in
                               (auth, ras_options, search, table_header, table)]
        self.accounts = AccountsPanel(self.root, self, C)
        self.services = ServicesPanel(self.root, self, C)
        self.sql = SqlPanel(self.root, self, C)
        self.archive = ArchivePanel(self.root, self, C)
        self.postgres = PostgresPanel(self.root, self, C)
        self.cluster_infobase = ClusterInfobasePanel(self.root, self, C)
        self.sql_copy = SqlCopyPanel(self.root, self, C)
        self.postgres_copy = PostgresCopyPanel(self.root, self, C)
        self.platform_install = PlatformInstallPanel(self.root, self, C)
        self.configuration_install = ConfigurationInstallPanel(self.root, self, C)
        self.windirstat = WinDirStatPanel(self.root, self, C)
        self.home = HomePanel(self.root, self)
        self.scheduler = SchedulerPanel(self.root, self, C)
        self._pages = {'accounts': self.accounts, 'jr_archive': self.archive,
                       **{mode: self.services for mode in SERVICE_MODES},
                       **{mode: self.sql for mode in SQL_MODES},
                       **{mode: self.postgres for mode in PG_MODES}, 'cluster_infobase': self.cluster_infobase,
                       'db_copy': self.sql_copy, 'pg_copy': self.postgres_copy, 'platform_install': self.platform_install,
                       'configuration_install': self.configuration_install,
                       'windirstat': self.windirstat, 'home': self.home, 'scheduler': self.scheduler}
        self._active_tab_key, self._active_panel = 'home', self.home
        self.form_notice = FormNotice(self.root, lambda: self._clear_panel_error(self._current_panel()))
        for key, panel in self._pages.items():
            if not hasattr(panel, '_page_key'):
                panel._page_key, panel._page_kind = key, key
        # Shared legacy forms become separate instances on first use of a mode.
        self.sql._page_key = self.sql._page_kind = 'db_backup'
        self.postgres._page_key = self.postgres._page_kind = 'pg_dump'
        self.services._page_key = self.services._page_kind = 'service_create'
        for panel in (self.sql, self.postgres, self.sql_copy, self.postgres_copy, self.cluster_infobase):
            self.hub.install(panel)
        for panel in set(self._pages.values()):
            self.saved_servers.install(panel)
            install_schedule_button(panel, self.scheduler)
        self.scheduler.start_monitor()

    def _apply_theme(self):
        apply_styles(self.root, C)
        self.tree.tag_configure("even", background=C["panel"])
        self.tree.tag_configure("odd", background=C["row"])

    def _status(self, text):
        self.status_lbl.configure(text=text)

    def _panel_status(self, panel, text):
        panel._last_status = text
        if self._panel_visible(panel):
            self._status(text)

    def _panel_error(self, panel, text, widget=None, title='Запрос не запущен'):
        text = str(text)
        if panel is None:
            self._notice, self._notice_mode = (title, text), self.mode
            self._status(text)
            self.form_notice.show(title, text, self._source_panel)
            if widget is not None:
                FormNotice.reveal(widget)
            return
        panel._notice = (title, text)
        self._panel_status(panel, text)
        if hasattr(panel, 'state_label'):
            panel.state_label.configure(text=title, fg=C['danger'])
        if self._panel_visible(panel):
            self.form_notice.show(title, text, panel)
            if widget is not None:
                FormNotice.reveal(widget)

    def _clear_panel_error(self, panel):
        if panel is None:
            self._notice = None
            if self.mode in TABLES:
                self.form_notice.pack_forget()
        else:
            panel._notice = None
            if self._panel_visible(panel):
                self.form_notice.pack_forget()

    def _panel_visible(self, panel):
        return getattr(self, '_active_panel', None) is panel and self.mode not in TABLES

    def _current_panel(self):
        return self._active_panel

    def _table_busy(self):
        return self.search_running or self._discovering

    def _set_busy(self):
        state = tk.DISABLED if self._table_busy() else tk.NORMAL
        for button in (self.btn_pick, self.btn_local, self.choose_base_button, self.all_bases_button):
            button.configure(state=state)
        self._update_open_actions()
        self.btn_update.configure(state=tk.DISABLED if (self._table_busy() if self.mode in TABLES else self._current_panel().running) else tk.NORMAL)
        self.home_button.configure(state=tk.NORMAL)
        self.bookmarks.set_enabled(True)
        self.local_ras_check.configure(state=state)
        tasks = []
        if self.scheduler.active_task:
            tasks.append(('scheduler-background', 'По расписанию: ' + self.scheduler.active_task,
                          lambda: self._switch_mode('scheduler')))
        for panel in set(self._pages.values()):
            if panel.running:
                key, mode = panel._page_key, panel._page_kind
                node = self.bookmarks.layout.find(key)
                title = node['title'] if node else PAGES.get(mode, mode)
                tasks.append((key, title, lambda m=mode, k=key: self._switch_mode(m, k)))
        if self._table_busy():
            mode = 'bases' if self.search_running else 'servers'
            tasks.append(('server-search', 'Поиск баз' if self.search_running else 'Поиск серверов', lambda: self._switch_mode(mode)))
        for key, job in self._opening_bases.items():
            tasks.append((('open-base', key), 'Открытие: ' + job['row']['base'], lambda: self._switch_mode('bases')))
        self.running_tasks.update_tasks(tasks)
        self.sidebar.set_active(self.mode)

    def _can_close_tab(self, key):
        node = self.bookmarks.layout.find(key)
        if node is None or 'page' not in node:
            return True
        panel = self._pages.get(key)
        if panel and panel.running:
            self._status('Во вкладке выполняется операция. Дождитесь завершения или прервите её.')
            return False
        if isinstance(panel, WinDirStatPanel) and not panel.close(lambda: self.bookmarks._close_tab(key)):
            return False
        if key == self._active_tab_key:
            self._switch_mode('home')
        if panel:
            self.database_sizes.cancel(panel)
            for mapping in (self.hub.pins, self.hub.pending, self.hub.select_buttons, self.hub.all_buttons, self.hub.refresh_buttons, self.hub.database_menus):
                for entry in list(mapping):
                    if entry is panel or isinstance(entry, tuple) and entry[0] is panel:
                        mapping.pop(entry, None)
            del self._pages[key]
            panel.destroy()
        return True

    def _create_page(self, mode, key):
        factory = (SqlPanel if mode in SQL_MODES else PostgresPanel if mode in PG_MODES else ServicesPanel if mode in SERVICE_MODES else
                   {'accounts': AccountsPanel, 'jr_archive': ArchivePanel, 'cluster_infobase': ClusterInfobasePanel,
                    'db_copy': SqlCopyPanel, 'pg_copy': PostgresCopyPanel, 'platform_install': PlatformInstallPanel,
                    'configuration_install': ConfigurationInstallPanel,
                    'windirstat': WinDirStatPanel, 'postgres_install': PostgresInstallPanel, 'linux_disk': LinuxDiskPanel,
                    'web_publication':WebPublicationPanel,'file_transfer':FileTransferPanel}.get(mode))
        if factory is None:
            return self._pages.get(mode)
        panel = factory(self.root, self, C)
        panel._page_key, panel._page_kind = key, mode
        if isinstance(panel, (SqlPanel, PostgresPanel, SqlCopyPanel, PostgresCopyPanel, ClusterInfobasePanel)):
            self.hub.install(panel)
        self._pages[key] = panel
        self.saved_servers.install(panel)
        install_schedule_button(panel, self.scheduler)
        if self._compact_mode is not None:
            panel.set_compact(self._compact_mode)
        return panel

    def _operation_running(self):
        return self.search_running or self._discovering or bool(self._opening_bases) or any(panel.running for panel in set(self._pages.values()))

    def _save_navigation(self, layout):
        self._settings["navigation_layout"] = layout
        self._navigation_hint.configure(text=self.bookmarks.breadcrumb(getattr(self, '_active_tab_key', self.mode)))
        self._save_settings()
        if hasattr(self, '_pages'):
            self._set_busy()

    def _focus_search(self, event=None):
        if self.mode == 'configuration_install':
            panel = self._current_panel()
            index = panel.catalog_tabs.index(panel.catalog_tabs.select())
            panel.search_entries[list(panel.trees)[index]].focus_set()
        elif self.mode == 'postgres_install':
            panel=self._current_panel();index=panel.tabs.index(panel.tabs.select())
            panel.search_entries[list(panel.trees)[index]].focus_set()
        elif self.mode in ('db_copy', 'pg_copy'):
            self._current_panel().filter_entry.focus_set()
        elif self.mode in SQL_MODES:
            self.sql.filter_entry.focus_set()
        elif self.mode in PG_MODES:
            self.postgres.filter_entry.focus_set()
        elif self.mode == 'jr_archive':
            self.archive.source_entry.focus_set()
        elif self.mode == 'cluster_infobase':
            self.cluster_infobase.name_entry.focus_set()
        elif self.mode == "accounts":
            self.accounts.names.focus_set()
        elif self.mode in SERVICE_MODES:
            for widget in self.services.forms[self.mode].winfo_children():
                if isinstance(widget, tk.Entry):
                    widget.focus_set()
                    break
        else:
            self.search_entry.focus_set()

    def _resize(self, event):
        if event.widget != self.root:
            return
        # Tk measures fonts in points, so reserve proportionally more height
        # when Windows uses a larger text scale.
        scale = max(1.0, float(self.root.tk.call("tk", "scaling")) / 1.33)
        self.sidebar.set_compact(event.width < round(1160 * scale))
        threshold = round(700 * scale)
        if self._compact_mode is not None:
            threshold += round(24 * scale) if self._compact_mode else -round(24 * scale)
        compact = event.height < threshold
        if compact == self._compact_mode:
            return
        self._compact_mode = compact
        for panel in set(self._pages.values()):
            panel.set_compact(compact)
        if compact:
            self._navigation_hint.pack_forget()
        else:
            content = self._source_panel if self.mode in TABLES else self._current_panel()
            self._navigation_hint.pack(**self._navigation_hint_pack, before=content)

    def _save_ras_mode(self):
        self._settings["local_ras"] = self.local_ras_var.get()
        self._save_settings()

    def _post(self, callback, *args):
        if not self._stop.is_set():
            self._events.put((callback, args))

    def _drain_events(self):
        if self._closed:
            return
        if self._event_job is not None:
            self.root.after_cancel(self._event_job)
            self._event_job = None
        deadline = time.perf_counter() + .008
        for _ in range(100):
            try:
                callback, args = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())
            if self._closed or time.perf_counter() >= deadline:
                break
        if not self._closed:
            if self.activity.error and self.activity.error != self._last_activity_error:
                self._last_activity_error = self.activity.error
                self._status(self.activity.error + '. Текущие данные остаются в окне.')
            self._event_job = self.root.after(1 if not self._events.empty() else 40, self._drain_events)

    @staticmethod
    def _base_identity(row):
        return row.get('server', '').casefold(), str(row.get('port', '')), row.get('cluster_id', ''), row.get('base', '')

    def _remember_base(self, event=None):
        if self.mode != 'bases':
            return
        for item in self.tree.selection()[:20]:
            index = int(item)
            if index < len(self._visible_items):
                row = self._visible_items[index]
                profile = dict(kind='cluster', page='cluster_infobase', values=dict(
                    server=row['server'], port=str(row['port']), version=row.get('version', ''), cluster=row.get('cluster_id', '')))
                profile['values']['username'] = self.user_var.get()
                self.activity.remember(profile, row.get('base', ''))

    def _choose_base(self):
        if self.mode != 'bases' or self._table_busy():
            return
        selection = self.tree.selection()
        if len(selection) != 1:
            self._panel_error(None, 'Сначала выделите одну базу и нажмите «Выбрать».', self.tree)
            return
        self._clear_panel_error(None)
        row = self._visible_items[int(selection[0])]
        self._base_pin = self._base_identity(row)
        self.search_var.set('')
        self._render()
        if self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])

    def _show_all_bases(self):
        if self._table_busy():
            return
        self._base_pin = None
        self.search_var.set('')
        self._render()

    def _update_open_actions(self):
        selected = self.tree.selection()
        row = (self._visible_items[int(selected[0])] if self.mode == 'bases' and len(selected) == 1
               and int(selected[0]) < len(self._visible_items) else None)
        available = (row is not None and not self._table_busy() and len(self._opening_bases) < 4
                     and self._base_identity(row) not in self._opening_bases)
        self.open_base_button.configure(state=tk.NORMAL if available else tk.DISABLED)
        if self.mode == 'bases' and self._opening_bases:
            self.cancel_open_button.pack(side=tk.RIGHT, padx=(6, 0), before=self._table_hint)
            self.cancel_open_button.configure(state=tk.NORMAL if any(not job['cancel'].is_set() for job in self._opening_bases.values()) else tk.DISABLED)
        else:
            self.cancel_open_button.pack_forget()

    def _open_base(self, event=None):
        if self.mode != 'bases' or self._table_busy():
            return 'break'
        selection = self.tree.selection()
        if len(selection) != 1 or int(selection[0]) >= len(self._visible_items):
            self._panel_error(None, 'Выделите одну информационную базу и нажмите «Открыть».', self.tree)
            return 'break'
        row = dict(self._visible_items[int(selection[0])])
        key = self._base_identity(row)
        if key in self._opening_bases:
            self._status('Клиент для этой базы уже подготавливается к запуску.')
            return 'break'
        if len(self._opening_bases) >= 4:
            self._panel_error(None, 'Дождитесь открытия одной из баз: одновременно подготавливаются четыре клиента.')
            return 'break'
        launch_mode=choose_open_mode(self,row)
        if launch_mode is None:return 'break'
        target = next((dict(item) for item in self.all_items if str(item['server']).casefold() == row['server'].casefold()
                       and str(item['port']) == str(row['port'])), dict(server=row['server'], port=row['port']))
        target['version'] = row.get('cluster_version') or target.get('version', '')
        versions = dict(self.rac_map)
        user, password, local = self.user_var.get(), self.pwd_var.get(), self.local_ras_var.get()
        ras_port = self._settings.get('ras_ports', {}).get(str(target['server']) + ':' + str(target['port']), 1545)
        cancel = threading.Event()
        token = self.activity.begin(dict(operation='Запуск Конфигуратора 1С' if launch_mode=='DESIGNER' else 'Запуск клиента 1С', server=row['server'], database=row['base'], executor='Windows'))
        self._opening_bases[key] = dict(row=row, cancel=cancel, messages=[], token=token)
        self._clear_panel_error(None)
        self._status('Подготовка открытия базы: ' + row['base'])
        self._set_busy()
        def notify(item):
            if item.get('text'):
                self._post(self._opening_message, key, item['text'])
        def worker():
            try:
                plan = prepare_client(row, target, versions, user, password, local, ras_port, notify, cancel,mode=launch_mode)
                result = start_client(plan, cancel)
                self.activity.end(token, 'Конфигуратор запущен' if launch_mode=='DESIGNER' else 'Клиент запущен')
                self._post(self._base_opened, key, result, '', False)
            except OperationCancelled as exc:
                self.activity.end(token, 'Прервано')
                self._post(self._base_opened, key, None, str(exc), True)
            except Exception as exc:
                self.activity.end(token, 'Ошибка')
                self._post(self._base_opened, key, None, str(exc), False)
        threading.Thread(target=worker, daemon=True).start()
        return 'break'

    def _opening_message(self, key, text):
        job = self._opening_bases.get(key)
        if job is None:
            return
        job['messages'].append(str(text))
        del job['messages'][:-100]
        if self.mode == 'bases':
            self._status(str(text).splitlines()[0])

    def _base_opened(self, key, result, error, cancelled):
        job = self._opening_bases.pop(key, None)
        if job is None:
            return
        if result is not None:
            self._opened_clients = [process for process in self._opened_clients if process.poll() is None]
            self._opened_clients.append(result['process'])
            cluster = result['cluster']
            for row in self._all_bases:
                if self._base_identity(row) == key:
                    row.update(cluster_host=cluster.get('host', ''), cluster_port=cluster['port'],
                               cluster_version=cluster['platform_version'])
            if self.mode == 'bases':
                self._status(('Запущен Конфигуратор ' if result.get('mode')=='DESIGNER' else 'Запущен '+result['kind']+' клиент ')+result['version']+' · '+result['address'])
        elif error and not cancelled:
            text = 'База: ' + job['row']['base'] + '\n' + error
            self._set_errors([*self._errors, '\n'.join([*job['messages'], text])])
            if self.mode == 'bases':
                self._panel_error(None, text, title='Не удалось открыть базу')
            else:
                self._notice, self._notice_mode = ('Не удалось открыть базу', text), 'bases'
        elif self.mode == 'bases':
            self._status(error or 'Открытие базы отменено.')
        self._set_busy()

    def _cancel_base_opening(self):
        for job in self._opening_bases.values():
            job['cancel'].set()
        if self.mode == 'bases':
            self._status('Прерывание подготовки клиентов 1С…')
        self._update_open_actions()

    def _cancel_ui_jobs(self):
        for job in self._opening_bases.values():
            job['cancel'].set()
        self.database_sizes.close()
        self.progress.stop()
        for job in (self._event_job, self._filter_job):
            if job is not None:
                self.root.after_cancel(job)
        self._event_job = self._filter_job = None

    def _discover(self, startup=False):
        if self._table_busy():
            return
        self._discovering = True
        self._set_busy()
        self._status("Поиск appsrvrs.lst текущего пользователя…")
        def worker():
            try:
                report, versions = discover_local_lst(), find_all_rac()
                self._post(self._discovered, report, versions, startup)
            except Exception as exc:
                self._post(self._discovery_failed, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _discovery_failed(self, error):
        self._discovering = False
        self._set_busy()
        self._status("Не удалось завершить поиск локальных списков")
        self._set_errors([error])

    def _discovered(self, report, versions, startup):
        self._discovering = False
        self.rac_map = versions
        self._set_busy()
        self._set_errors(report.errors)
        if startup:
            if (not self._lst_path or not Path(self._lst_path).is_file()
                    or not self._is_user_source(self._lst_path, report)):
                self._lst_path = next(iter(report.current_paths), "")
            self._load_lst()
            if self.mode in TABLES:
                self.search_entry.focus_set()
        else:
            self._status(f"Найдено локальных списков: {len(report.paths)}")
            self._show_local_lists(report)

    def _show_local_lists(self, report):
        dialog = tk.Toplevel(self.root)
        dialog.title("Списки серверов текущего пользователя")
        dialog.geometry("960x500")
        dialog.minsize(700, 400)
        dialog.configure(bg=C["bg"])
        dialog.transient(self.root)
        dialog.grab_set()
        self._label(dialog, f"Найдено файлов: {len(report.paths)}", 18, bold=True).pack(anchor=tk.W, padx=24, pady=(20, 6))
        self._label(dialog, "Выберите список для загрузки или откройте его расположение.", 10, "muted").pack(anchor=tk.W, padx=24)
        frame = tk.Frame(dialog, bg=C["panel"])
        frame.pack(fill=tk.BOTH, expand=True, padx=24, pady=18)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        listing = tk.Listbox(frame, bg=C["panel"], fg=C["fg"], font=("Segoe UI", 11),
                            selectbackground=C["selected"], selectforeground=C["fg"],
                            relief="flat", bd=10, highlightthickness=0, exportselection=False)
        listing.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=listing.xview)
        sb.grid(row=1, column=0, sticky="ew")
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listing.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        listing.configure(xscrollcommand=sb.set, yscrollcommand=vsb.set)
        for path in report.paths:
            listing.insert(tk.END, path)
        if report.paths:
            listing.selection_set(0)
        def selected():
            selection = listing.curselection()
            return report.paths[selection[0]] if selection else None
        def load(event=None):
            path = selected()
            if path:
                dialog.destroy()
                self._select_source(path)
        def reveal():
            path = selected()
            if path:
                try:
                    subprocess.Popen(["explorer.exe", "/select,", path])
                except OSError as exc:
                    messagebox.showerror("Проводник", str(exc), parent=dialog)
        listing.bind("<Double-1>", load)
        buttons = tk.Frame(dialog, bg=C["bg"])
        buttons.pack(fill=tk.X, padx=24, pady=(0, 20))
        self._button(buttons, "Где искали", lambda: self._show_text("Диагностика поиска", "\n".join(report.checked) + "\n\n" + "\n".join(report.errors))).pack(side=tk.LEFT)
        self._button(buttons, "В проводнике", reveal).pack(side=tk.RIGHT, padx=(8, 0))
        self._button(buttons, "Загрузить список", load, primary=True).pack(side=tk.RIGHT)

    @staticmethod
    def _is_user_source(path, report):
        return (Path(path_key(path)).is_relative_to(Path(path_key(Path.home())))
                or path_key(path) in {path_key(item) for item in report.current_paths})

    def _select_source(self, path):
        if not self._is_user_source(path, discover_local_lst()):
            messagebox.showwarning(APP_NAME, "Выберите список из профиля текущего пользователя\n"
                                   "или из его настроенного каталога 1С.", parent=self.root)
            return
        self._lst_path = path
        self._settings["lst_path"] = path
        self._save_settings()
        self._all_bases = []
        self._switch_mode("servers")
        self._load_lst()

    def _pick_lst(self):
        if self._table_busy():
            return
        initial = str(Path(self._lst_path).parent) if self._lst_path else str(Path.home())
        path = filedialog.askopenfilename(parent=self.root, title="Выберите appsrvrs.lst", initialdir=initial,
                                         filetypes=[("Списки серверов", "*.lst"), ("Все файлы", "*.*")])
        if path:
            self._select_source(path)

    def _load_lst(self):
        self.all_items, self._all_bases = [], []
        if not self._lst_path:
            self.lst_lbl.configure(text="Выберите файл или откройте локальные списки")
            self._status("Локальные списки не найдены. Доступные пути — в окне «Локальные списки».")
        else:
            self.lst_lbl.configure(text=self._lst_path)
            try:
                source = self._backups.get(path_key(self._lst_path), (None, self._lst_path))[1]
                servers = read_lst(source)
                self.all_items = [dict(record, server=server) for server in sorted(servers, key=str.casefold)
                                  for record in servers[server]]
                self._status(f"Загружено записей: {len(self.all_items)}")
            except (OSError, UnicodeError) as exc:
                self._status("Не удалось прочитать список серверов")
                self._set_errors([f"{self._lst_path}: {exc}"])
        self._render()

    def _on_search(self, *args):
        if self._filter_job:
            self.root.after_cancel(self._filter_job)
        self._filter_job = self.root.after(140, self._render)

    def _render(self):
        if self._filter_job:
            self.root.after_cancel(self._filter_job)
            self._filter_job = None
        self.bookmarks.set_active(getattr(self, '_active_tab_key', self.mode))
        if self.mode not in TABLES:
            return
        # Allocate primary controls before the hint, which may shrink at high DPI.
        self._table_hint.pack_forget()
        for button in (self.open_base_button, self.choose_base_button, self.all_bases_button):
            button.pack_forget()
            if self.mode == 'bases':
                button.pack(side=tk.RIGHT, padx=(6, 0))
        self._table_hint.pack(side=tk.RIGHT)
        self._table_hint.configure(text=('Двойной щелчок — открыть базу' if self.mode == 'bases' else
                                         'Двойной щелчок — открыть MMC') + '    ·    Ctrl+C — копировать')
        definitions = TABLES[self.mode]
        columns = [entry[0] for entry in definitions]
        self.tree.configure(displaycolumns=columns)
        for key, title, width in definitions:
            if self._sort_col == key:
                title += "  ↓" if self._sort_reverse else "  ↑"
            self.tree.heading(key, text=title, command=lambda col=key: self._sort_tree(col))
            if self._rendered_mode != self.mode:
                self.tree.column(key, width=width, minwidth=70, anchor=tk.CENTER if key == "port" else tk.W,
                                 stretch=key in ("descr", "base", "cluster", "address"))
        self._rendered_mode = self.mode
        query = self.search_var.get().casefold().strip()
        source = self.all_items if self.mode == "servers" else self._all_bases
        if self.mode == 'bases':
            for item in source:
                item['address'] = infobase_address(
                    dict(host=item.get('cluster_host'), port=item.get('cluster_port')),
                    item.get('base', ''), item.get('server', ''))
        if self.mode == 'bases' and self._base_pin is not None:
            source = [item for item in source if self._base_identity(item) == self._base_pin]
        fields = columns if self.mode == "servers" else ["server", "base", "cluster", "address"]
        items = [item for item in source if not query or any(query in str(item.get(key, "")).casefold() for key in fields)]
        if self._sort_col in columns:
            key = self._sort_col
            items.sort(key=lambda item: int(item[key]) if key == "port" else text_key(item.get(key, "")),
                       reverse=self._sort_reverse)
        self._visible_items = items
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for index, item in enumerate(items):
            self.tree.insert("", tk.END, iid=str(index), values=[item.get(key, "") for key in COLUMNS],
                             tags=("odd" if index % 2 else "even",))
        title = "Серверы" if self.mode == "servers" else "Базы"
        self.lbl_count.configure(text=f"{title}   {len(items)} / {len(source)}")
        if items:
            self.empty_lbl.place_forget()
        else:
            if self.search_running:
                text = "Загрузка информационных баз…"
            elif query and source:
                text = "Совпадений нет\nПопробуйте изменить фильтр"
            elif self.mode == "bases":
                text = "Список баз пуст\nНажмите «Обновить» для загрузки"
            else:
                text = "Список серверов пуст\nВыберите файл appsrvrs.lst"
            self.empty_lbl.configure(text=text)
            self.empty_lbl.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self._update_open_actions()
    def _switch_mode(self, mode, tab_id=None):
        key = tab_id or mode
        self.form_notice.pack_forget()
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        self.progress_pct.configure(text='', width=5, height=1)
        for panel in set(self._pages.values()):
            panel.pack_forget()
        if mode not in TABLES:
            self._source_panel.pack_forget()
            for widget, _ in self._server_layout:
                widget.pack_forget()
            for button in (self.btn_pick, self.btn_local, self.btn_update):
                button.pack_forget()
            panel = self._pages.get(key)
            if panel is None or panel.running and getattr(panel, '_page_kind', mode) != mode:
                panel = self._create_page(mode, key)
            if not panel.running:
                if getattr(panel, '_page_kind', mode) != mode:
                    self._clear_panel_error(panel)
                panel._page_key, panel._page_kind = key, mode
            self._active_panel = panel
            # Keep the existing typed convenience references for shortcuts.
            alias = ('sql' if mode in SQL_MODES else 'postgres' if mode in PG_MODES else 'services' if mode in SERVICE_MODES else
                     {'accounts': 'accounts', 'jr_archive': 'archive', 'cluster_infobase': 'cluster_infobase',
                      'db_copy': 'sql_copy', 'pg_copy': 'postgres_copy', 'platform_install': 'platform_install', 'windirstat': 'windirstat',
                      'web_publication':'web_publication','file_transfer':'file_transfer'}.get(mode))
            if alias:
                setattr(self, alias, panel)
            if hasattr(panel, 'show_mode') and not panel.running:
                panel.show_mode(mode)
            panel.pack(fill=tk.BOTH, expand=True)
        elif self.mode not in TABLES:
            self._source_panel.pack(**self._source_pack)
            for widget, layout in self._server_layout:
                widget.pack(**layout)
            self.btn_pick.pack(side=tk.RIGHT, padx=(8, 0))
            self.btn_local.pack(side=tk.RIGHT)
            self.btn_update.pack(side=tk.RIGHT, before=self.favorites_button)
        if mode in TABLES:
            self._active_panel = None
        self._active_tab_key = key
        self.mode, self._sort_col, self._sort_reverse = mode, None, False
        self.home_button.configure(bg=C['selected'] if mode == 'home' else C['button'])
        self._navigation_hint.configure(text='Главная' if mode == 'home' else self.bookmarks.breadcrumb(key))
        self.search_var.set("")
        self._render()
        self._status({'windirstat': 'Оригинальный portable WinDirStat внутри вкладки · анализ дисков и папок Windows',
                      'postgres_install':'PostgreSQL с патчами 1С · ИТС → выбранный экземпляр Windows или Ubuntu/Debian',
                      'linux_disk':'Анализ занятого места на Linux по SSH · каталоги, крупные файлы и карта диска',
                      'scheduler': 'Последовательности по расписанию и журнал запусков · фоновый исполнитель работает отдельно от окна консоли',
                      'platform_install': 'Войдите в ИТС, выберите версию и дистрибутив Windows или Linux',
                      'configuration_install': 'Типовые российские конфигурации 1С · setup на этом компьютере, база в выбранном кластере',
                      'web_publication':'Публикация 1С и управление IIS / Apache · локально Windows, удалённо WinRM / SSH',
                      'file_transfer':'Windows ↔ Windows / Linux · источник не удаляется, передача выполняется в фоне',
                      'home': 'Избранное, последние базы и история операций', "servers": "Список серверов",
                      "bases": "Обновите список баз или отфильтруйте загруженные данные",
                      "accounts": "Введите ФИО сотрудников. Создание выполняется на этом компьютере.",
                      "jr_archive": "Укажите параметры ЖР. Архивирование начнётся по кнопке.",
                      "cluster_infobase": "Выберите кластер сервера 1С и заполните параметры информационной базы",
                      "db_copy": "Выберите исходную и целевую базу одного SQL Server и папку нового бэкапа",
                      "pg_copy": "Подключите источник и получателя PostgreSQL, выберите две базы и папку нового бэкапа",
                      **{key: "Подключитесь к SQL Server и выберите базу" for key in SQL_MODES},
                      **{key: "Подключитесь к PostgreSQL; пути относятся к компьютеру выполнения" for key in PG_MODES},
                      **{key: "Управление службами на этом компьютере" for key in SERVICE_MODES}}[mode])
        if mode == "accounts":
            self.accounts.names.focus_set()
        panel = self._current_panel()
        if panel is None and getattr(self, '_notice_mode', None) == mode and getattr(self, '_notice', None):
            self.form_notice.show(*self._notice, before=self._source_panel)
        if panel is not None:
            if getattr(panel, '_notice', None):
                self.form_notice.show(*panel._notice, before=panel)
            if getattr(panel, '_last_status', ''):
                self._status(panel._last_status)
            if hasattr(panel, 'restore_progress'):
                panel.restore_progress()
            if getattr(panel, '_pending_review', None):
                panel.on_activated()
        self._set_busy()

    def _sort_tree(self, column):
        selected = {id(self._visible_items[int(key)]) for key in self.tree.selection()}
        self._sort_reverse = not self._sort_reverse if self._sort_col == column else False
        self._sort_col = column
        self._render()
        self.tree.selection_set([str(index) for index, item in enumerate(self._visible_items) if id(item) in selected])

    def _copy_selection(self, event=None):
        if self.mode in SQL_MODES or self.mode in ('jr_archive', 'cluster_infobase'):
            return self._current_panel().copy_results(event)
        if self.mode == "accounts":
            return self.accounts.copy_results(event)
        if self.mode in SERVICE_MODES:
            self.services.copy_command()
            return "break"
        selected = self.tree.selection()
        if selected:
            columns = self.tree.cget("displaycolumns")
            rows = ["\t".join(str(self._visible_items[int(iid)].get(key, "")) for key in columns) for iid in selected]
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(rows))
            self._status(f"Скопировано строк: {len(rows)}")
        return "break"

    def _base_context(self, event):
        if self.mode != 'bases':
            return 'break'
        iid = self.tree.identify_row(event.y)
        if not iid or int(iid) >= len(self._visible_items):
            return 'break'
        row = self._visible_items[int(iid)]
        self.tree.selection_set(iid)
        menu = self.base_context_menu
        menu.delete(0, tk.END)
        available = (not self._table_busy() and not self.cluster_infobase.running and
                     row.get('cluster_id') and row.get('infobase_id'))
        menu.add_command(label='Удалить из кластера…', state=tk.NORMAL if available else tk.DISABLED,
                         command=lambda: self._base_context_action(iid, row, 'delete'))
        menu.add_command(label='Скопировать название', command=lambda: self._base_context_action(iid, row, 'copy'))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def _base_context_action(self, iid, row, action):
        if self.mode != 'bases' or int(iid) >= len(self._visible_items) or self._visible_items[int(iid)] is not row:
            self._panel_error(None, 'Список баз изменился. Откройте меню базы заново.')
            return
        if action == 'copy':
            self.root.clipboard_clear()
            self.root.clipboard_append(row.get('base', ''))
            self._status('Название базы скопировано: ' + row.get('base', ''))
            return
        if self._table_busy() or self.cluster_infobase.running or not row.get('infobase_id') or not row.get('cluster_id'):
            return
        self.tree.selection_set(iid)
        self._switch_mode('cluster_infobase')
        panel = self.cluster_infobase
        target, cluster = panel.target(), panel.selected_cluster()
        if (not target or not cluster or panel.target_key(target) != panel.target_key(row) or
                cluster['cluster'] != row['cluster_id']):
            self._panel_error(panel, 'Сначала обновите список выбранного кластера')
            return
        for index, base in enumerate(panel.bases):
            if base['infobase'] == row['infobase_id']:
                panel.tree.selection_set(str(index))
                panel.delete_selected()
                return
        self._panel_error(panel, 'База не найдена в выбранном кластере. Обновите список.')

    def _on_update_clicked(self):
        if self.mode == 'home':
            self.home.refresh()
            return
        if self.mode in TABLES and self._table_busy():
            return
        if self.mode not in TABLES:
            panel = self._current_panel()
            if panel.running:
                return
            if self.mode == 'configuration_install':
                panel.load_catalog()
                return
            if self.mode=='web_publication':panel.inspect();return
            if self.mode=='file_transfer':panel.list_source();return
            if (panel, 'source') in self.hub.refresh_buttons:
                role = 'source'
                focused = self.root.focus_get()
                target = getattr(panel, 'target_area', None)
                if target is not None and focused is not None and str(focused).startswith(str(target) + '.'):
                    role = 'target'
                self.hub.refresh_databases(panel, role)
            return
        self._load_lst() if self.mode == "servers" else self._do_rac_search()

    def _do_rac_search(self):
        if self._table_busy() or self.mode not in TABLES:
            return
        if not self.all_items:
            self._panel_error(None, "Сначала загрузите список серверов: выберите appsrvrs.lst или нажмите «Локальные списки».")
            return
        if not self.rac_map and not self.local_ras_var.get():
            self.rac_map = find_all_rac()
        if not self.rac_map and not self.local_ras_var.get():
            messagebox.showwarning(APP_NAME, "rac.exe не найден. Установите компоненты администрирования 1С.", parent=self.root)
            return
        user, password = self.user_var.get(), self.pwd_var.get()
        try:
            save_cache(user, password)
        except OSError as exc:
            messagebox.showwarning(APP_NAME, f"Не удалось сохранить учётные данные: {exc}", parent=self.root)
        items, versions = list(self.all_items), dict(self.rac_map)
        ports = self._settings.get("ras_ports", {})
        ports = dict(ports) if isinstance(ports, dict) else {}
        manager = LocalRasManager(self._stop) if self.local_ras_var.get() else None
        self._ras_manager = manager
        self._clear_panel_error(None)
        self.search_running = True
        self._all_bases = []
        self._set_errors([])
        self._set_busy()
        self.progress.configure(value=0)
        self.progress_pct.configure(text="0%")
        self._render()
        self._status("Запуск временных локальных RAS…" if manager else "Подключение к серверам администрирования…")
        def notify(stage, done, total):
            self._post(self._progress, stage, done, total)
        def worker():
            try:
                found, errors = collect_bases(items, versions, user, password, notify, self._stop,
                                              ras_ports=ports, local_ras=manager)
            except Exception as exc:
                found, errors = [], [str(exc)]
            finally:
                if manager:
                    manager.close()
            self._post(self._finalize_rac_search, found, errors)
        threading.Thread(target=worker, daemon=True).start()

    def _progress(self, stage, done, total):
        if self.mode not in TABLES:
            return
        self.progress.configure(maximum=total, value=done)
        self.progress_pct.configure(text=f"{int(done / total * 100)}%")
        self._status(f"{stage}: {done} / {total}")

    def _finalize_rac_search(self, found, errors):
        self.search_running = False
        self._all_bases = found
        self._set_busy()
        self._set_errors(errors)
        self._render()
        if self.mode in TABLES:
            self.progress.configure(value=0)
            self.progress_pct.configure(text="")
            self._status(f"Загружено баз: {len(found)}" + (f" · Ошибок: {len(errors)} — см. подробности" if errors else ""))

    def _set_errors(self, errors):
        self._errors = errors
        if errors:
            self.btn_errors.pack(side=tk.RIGHT, padx=(12, 0), before=self.progress)
        else:
            self.btn_errors.pack_forget()

    def _show_errors(self):
        self._show_text("Подробности", "\n\n".join(self._errors))

    def _show_text(self, title, text):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("900x500")
        dialog.configure(bg=C["bg"])
        dialog.transient(self.root)
        view = tk.Text(dialog, wrap=tk.WORD, bg='#0b0f0c', fg='#75ed91', font=("Consolas", 10),
                       relief="flat", padx=18, pady=18)
        scroll = ttk.Scrollbar(dialog, command=view.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        view.configure(yscrollcommand=scroll.set)
        view.pack(fill=tk.BOTH, expand=True)
        configure_log(view)
        insert_log(view, text)
        view.configure(state=tk.DISABLED)

    def _on_dbl(self, event):
        if self.mode == 'bases':
            if self.tree.identify_region(event.x, event.y) not in ('cell', 'tree'):
                return 'break'
            item = self.tree.identify_row(event.y)
            if item and not self._table_busy():
                self.tree.selection_set(item)
                self._open_base()
            return 'break'
        if self.search_running or self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        item = self._visible_items[int(iid)]
        self._do_connect(item)

    def _do_connect(self, item):
        report = discover_local_lst()
        if not report.current_paths:
            messagebox.showwarning(APP_NAME, "В текущем профиле Windows не найден appsrvrs.lst.\n"
                                   "Запустите консоль администрирования 1С и зарегистрируйте сервер.", parent=self.root)
            return
        self._status(f"Подготовка MMC: {item['server']}:{item['port']}…")
        try:
            for name in report.current_paths:
                path = Path(name)
                key = path_key(path)
                if key not in self._backups:
                    backup = Path(str(path) + ".bak")
                    if not backup.exists():
                        shutil.copy2(path, backup)
                    self._backups[key] = (path, backup)
                write_lst_entry(path, item["server"], item["port"], item.get("version", ""), item.get("descr", ""))
            version = item.get("version", "")
            rac = self.rac_map.get(version)
            dll = Path(rac).with_name("radmin.dll") if rac else None
            if version and (dll is None or not dll.is_file()):
                dll = next((root / version / "bin" / "radmin.dll" for root in platform_roots()
                            if (root / version / "bin" / "radmin.dll").is_file()), None)
            if dll and dll.is_file():
                code = ctypes.windll.shell32.ShellExecuteW(None, "runas", "regsvr32.exe",
                                                         f'/s /i:user "{dll}"', None, 1)
                if code <= 32:
                    raise OSError("Не удалось зарегистрировать radmin.dll")
            if not open_mmc():
                raise OSError("Консоль MMC для 1С не найдена")
            self._status(f"MMC: {item['server']}:{item['port']}")
        except (OSError, ValueError) as exc:
            errors = self._restore_lists()
            messagebox.showerror(APP_NAME, str(exc) + ("\n" + "\n".join(errors) if errors else ""), parent=self.root)

    def _restore_lists(self):
        errors = []
        for key, (path, backup) in list(self._backups.items()):
            try:
                shutil.copy2(backup, path)
                backup.unlink()
                del self._backups[key]
            except OSError as exc:
                errors.append(f"Не удалось восстановить {path}. Копия: {backup}. {exc}")
        return errors

    def _save_settings(self):
        try:
            save_settings(self._settings)
        except OSError as exc:
            self._status(f"Не удалось сохранить настройки: {exc}")

    def _on_close(self):
        if self._opening_bases:
            self._status('Дождитесь запуска клиента 1С или нажмите «Прервать открытие» во вкладке «ИБ».')
            return
        if self.scheduler._saving or self.scheduler.configuring_background:
            self._status('Дождитесь завершения сохранения расписания или настройки фонового запуска.')
            return
        if self.scheduler.dirty and not messagebox.askyesno(
                'Несохранённое расписание', 'В планировщике есть несохранённые изменения. Закрыть консоль без их сохранения?',
                parent=self.root, default='no'):
            return
        if any(panel.running for panel in set(self._pages.values())):
            self._status("Дождитесь завершения фоновых задач или прервите их во вкладках с зелёным индикатором.")
            return
        for panel in set(self._pages.values()):
            if isinstance(panel, WinDirStatPanel) and not panel.close(self._on_close):
                self._status('Ожидание закрытия WinDirStat…')
                return
        self._stop.set()
        if self._ras_manager:
            self._ras_manager.close()
        self._settings["lst_path"] = self._lst_path
        self._settings.pop("col_order", None)
        self._settings.pop("col_widths", None)
        self._save_settings()
        errors = self._restore_lists()
        if errors:
            messagebox.showerror(APP_NAME, "\n".join(errors), parent=self.root)
        self._closed = True
        self._cancel_ui_jobs()
        self.bookmarks.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if '--scheduler-worker' in sys.argv:
        from scheduler_worker import main as scheduler_main
        position = sys.argv.index('--scheduler-worker')
        if position + 1 >= len(sys.argv):
            raise SystemExit(2)
        raise SystemExit(scheduler_main(sys.argv[position + 1]))
    elif ensure_administrator():
        App().run()
