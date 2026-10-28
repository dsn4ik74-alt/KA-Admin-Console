"""The local Windows account tab. Results remain in memory only."""
import os
import threading
import tkinter as tk
from tkinter import ttk

from account_creation import DEFAULT_EMAIL_DOMAIN, run_account_batch
from table_sorting import TableSorter

ACCOUNT_COLUMNS = (
    ("full_name", "ФИО", 200), ("username", "Логин", 135),
    ("password", "Пароль", 155), ("status", "Статус", 165),
    ("email", "Адрес", 210), ("company", "Компания", 150),
    ("message", "Подробности", 420),
)
STATUSES = {"ready": "Проверено", "exists": "Уже существует", "duplicate": "Дубликат",
            "created": "Создана · RDP…", "created_rdp": "Создана · RDP",
            "created_no_rdp": "Создана без RDP", "error": "Ошибка"}


class AccountsPanel(tk.Frame):
    def __init__(self, parent, app, colors):
        super().__init__(parent, bg=colors["bg"])
        self.app, self.colors = app, colors
        self.running = False
        self.rows = {}
        self._batch = 0
        self._build()

    def _build(self):
        app, c = self.app, self.colors
        self.action_bar = tk.Frame(self, bg=c["panel"], padx=16, pady=12,
                                   highlightthickness=1, highlightbackground=c['border'])
        self.action_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 12))
        self.create_button = app._button(self.action_bar, "Создать УЗ", self.start, primary=True)
        self.create_button.pack(side=tk.LEFT, padx=(0, 8))
        self.preview_button = app._button(self.action_bar, "Проверить ФИО", lambda: self.start(preview=True))
        self.preview_button.pack(side=tk.LEFT)
        self.copy_button = app._button(self.action_bar, "Копировать", self.copy_results)
        self.copy_button.pack(side=tk.RIGHT)
        self.clear_button = app._button(self.action_bar, "Очистить", self.clear_results)
        self.clear_button.pack(side=tk.RIGHT, padx=(0, 8))
        heading = tk.Frame(self, bg=c["panel"], padx=16, pady=12,
                            highlightthickness=1, highlightbackground=c['border'])
        heading.pack(fill=tk.X, padx=22, pady=(0, 12))
        self.heading = heading
        self._heading_pack = heading.pack_info()
        app._label(heading, "Локальные учётные записи Windows", 15, bold=True).pack(anchor=tk.W)
        host = os.environ.get("COMPUTERNAME", "текущий компьютер")
        self.heading_note = app._label(heading, f"Компьютер: {host}  ·  Группа: удалённый рабочий стол  ·  Пароль не истекает",
                                      10, "muted")
        self.heading_note.pack(anchor=tk.W, pady=(5, 0))
        self._heading_note_pack = self.heading_note.pack_info()
        panes = tk.PanedWindow(self, orient=tk.HORIZONTAL, bg=c["bg"], bd=0,
                               sashwidth=10, sashrelief=tk.FLAT)
        panes.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        self.panes = panes
        left = tk.Frame(panes, bg=c["panel"], padx=16, pady=16,
                         highlightthickness=1, highlightbackground=c['border'])
        right = tk.Frame(panes, bg=c["panel"], padx=16, pady=16,
                          highlightthickness=1, highlightbackground=c['border'])
        panes.add(left, minsize=280, width=340, stretch="never")
        panes.add(right, minsize=400, stretch="always")
        # Reserve actions before the expanding input gets any height.
        actions = tk.Frame(left, bg=c["panel"])
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        app._label(left, "Сотрудники", 13, bold=True).pack(anchor=tk.W)
        self.input_hint = app._label(left, 'По одному ФИО на строку. Компания — в кавычках.\n'
                                    'Иванов Иван Иванович "Компания"', 9, "muted", justify=tk.LEFT, wraplength=300)
        self.input_hint.pack(anchor=tk.W, pady=(6, 12))
        self._input_hint_pack = self.input_hint.pack_info()
        input_frame = tk.Frame(left, bg=c["bg"])
        self.input_frame = input_frame
        input_frame.pack(fill=tk.BOTH, expand=True)
        self.names = tk.Text(input_frame, width=30, height=10, wrap=tk.WORD, font=("Segoe UI", 11),
                             bg=c["bg"], fg=c["fg"], insertbackground=c["accent"], relief="flat",
                             selectbackground=c['selected'], selectforeground=c['fg'],
                             padx=10, pady=10, undo=True)
        scroll = ttk.Scrollbar(input_frame, command=self.names.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.names.configure(yscrollcommand=scroll.set)
        self.names.pack(fill=tk.BOTH, expand=True)
        app._label(actions, "Домен для адресов", 10, "muted").pack(anchor=tk.W, pady=(0, 5))
        self.domain = tk.StringVar(value=DEFAULT_EMAIL_DOMAIN)
        self.domain_entry = app._entry(actions, self.domain)
        self.domain_entry.pack(fill=tk.X)

        result_footer = tk.Frame(right, bg=c["panel"])
        result_footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self.detail = app._label(result_footer, "Выберите строку для просмотра подробностей.", 10, "muted",
                                 anchor=tk.W, justify=tk.LEFT, wraplength=550, height=2)
        self.detail.pack(fill=tk.X, pady=(0, 4))
        self.detail.bind("<Configure>", lambda event: self.detail.configure(wraplength=max(200, event.width - 8)))
        app._label(result_footer, "Результаты хранятся в этом окне. Скопируйте их перед закрытием.",
                   9, "muted", wraplength=550, justify=tk.LEFT).pack(anchor=tk.W)
        toolbar = tk.Frame(right, bg=c["panel"])
        toolbar.pack(fill=tk.X, pady=(0, 12))
        self.count = app._label(toolbar, "Результаты · 0", 13, bold=True)
        self.count.pack(side=tk.LEFT)
        table = tk.Frame(right, bg=c["panel"])
        table.pack(fill=tk.BOTH, expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(table, columns=[col[0] for col in ACCOUNT_COLUMNS],
                                  show="headings", selectmode="extended")
        for key, title, width in ACCOUNT_COLUMNS:
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=80, stretch=False)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.tag_configure("normal", background=c["panel"])
        self.table.tag_configure("problem", foreground=c['danger'])
        self.sorter = TableSorter(self.table, colors=c)
        self.table.bind("<Control-c>", self.copy_results)
        self.table.bind("<Control-C>", self.copy_results)
        self.table.bind("<<TreeviewSelect>>", self._show_detail)

    def set_compact(self, compact):
        for button in (self.create_button,self.preview_button,self.copy_button,self.clear_button):
            button.configure(padx=5 if compact else 12)
        if compact:
            self.heading.pack_forget()
        elif not self.heading.winfo_manager():
            self.heading.pack(**self._heading_pack, before=self.panes)
        for widget, layout in ((self.heading_note, self._heading_note_pack), (self.input_hint, self._input_hint_pack)):
            if compact:
                widget.pack_forget()
            elif not widget.winfo_manager():
                options = dict(layout)
                if widget == self.input_hint:
                    options["before"] = self.input_frame
                widget.pack(**options)

    def start(self, preview=False):
        if self.running:
            return
        names = self.names.get("1.0", "end-1c")
        if not names.strip():
            self.app._panel_error(self, "Введите хотя бы одно ФИО — по одному сотруднику на строку")
            self.names.focus_set()
            return
        self._batch += 1
        batch = self._batch
        self.app._clear_panel_error(self)
        self.running = True
        self.app._set_busy()
        for widget in (self.names, self.domain_entry, self.preview_button, self.create_button, self.clear_button):
            widget.configure(state=tk.DISABLED)
        domain = self.domain.get().strip()
        self.app._panel_status(self, "Проверка ФИО…" if preview else "Создание локальных учётных записей…")
        token = self.app.activity.begin(dict(operation='Проверка ФИО' if preview else 'Создание учётных записей',
            server=os.environ.get('COMPUTERNAME', 'localhost'), executor='Windows'))
        journal=self.app.activity.logs.writer(token[0])

        def worker():
            error = ""
            failed = []
            def receive(row):
                journal.hide([row.get('password')])
                journal.write(f"{row.get('username','')}: {row.get('status','')} · {row.get('message','')}")
                if row.get('status') in ('error', 'created_no_rdp', 'duplicate'):
                    failed.append(True)
                self.app._post(self.accept_row, batch, row)
            try:
                run_account_batch(names, domain,
                                  receive, preview=preview)
            except Exception as exc:
                error = str(exc)
            status='Ошибка' if error else 'Есть ошибки' if failed else 'Успешно'
            journal.write('ОШИБКА: '+error if error else 'Результат: '+status)
            journal.close()
            self.app.activity.end(token,status)
            self.app._post(self.finish, batch, preview, error)
        threading.Thread(target=worker, daemon=True).start()

    def accept_row(self, batch, row):
        iid = f"{batch}:{row['line']}"
        self.rows[iid] = dict(row)
        values = [STATUSES.get(row.get(key), row.get(key, "")) if key == "status" else row.get(key, "")
                  for key, _, _ in ACCOUNT_COLUMNS]
        tags = ("problem",) if row.get("status") in ("error", "created_no_rdp", "duplicate") else ("normal",)
        if self.table.exists(iid):
            self.table.item(iid, values=values, tags=tags)
        else:
            self.table.insert("", tk.END, iid=iid, values=values, tags=tags)
        self.table.see(iid)
        self.count.configure(text=f"Результаты · {len(self.rows)}")
        self.sorter.apply()

    def finish(self, batch, preview, error):
        self.running = False
        for widget in (self.names, self.domain_entry, self.preview_button, self.create_button, self.clear_button):
            widget.configure(state=tk.NORMAL)
        self.app._set_busy()
        rows = [row for iid, row in self.rows.items() if iid.startswith(f"{batch}:")]
        if error:
            self.detail.configure(text=error)
            self.app._panel_status(self, "Операция завершилась с ошибкой. Полученные результаты остаются в таблице.")
            self.app._panel_error(self, error, title='Ошибка выполнения')
        else:
            created = sum(row["status"] in ("created", "created_rdp", "created_no_rdp") for row in rows)
            failed = sum(row["status"] in ("error", "created_no_rdp") for row in rows)
            summary = (f"Проверено строк: {len(rows)} · Ошибок: {failed}" if preview else
                       f"Обработано: {len(rows)} · Создано: {created} · Ошибок: {failed}")
            self.app._panel_status(self, summary)

    def _show_detail(self, event=None):
        selected = self.table.selection()
        if selected:
            row = self.rows[selected[0]]
            self.detail.configure(text=f"{row.get('full_name', '')}: {row.get('message', '')}")

    def copy_results(self, event=None):
        selected = self.table.selection() or self.table.get_children()
        if selected:
            lines = ["\t".join(title for _, title, _ in ACCOUNT_COLUMNS)]
            for iid in selected:
                values = self.table.item(iid, "values")
                lines.append("\t".join(str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                                       for value in values))
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append("\n".join(lines))
            self.app._panel_status(self, f"Скопировано результатов: {len(selected)}")
        return "break"

    def clear_results(self):
        if self.running:
            return
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        self.rows.clear()
        self.count.configure(text="Результаты · 0")
        self.detail.configure(text="Выберите строку для просмотра подробностей.")
        self.app._panel_status(self, "Таблица результатов очищена")
