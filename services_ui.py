"""Local service forms with live command preview and asynchronous execution."""
import os
import threading
import tkinter as tk
from log_colors import configure_log, insert_log
from tkinter import filedialog, ttk
from clipboard_shortcuts import bind_entry_shortcuts

from service_management import (agent_ports, create_command, delete_command, edited_create_command,
                                installed_agents, rename_command, run_service_command)


SERVICE_MODES = {"service_create": ("Создать службу 1С", "Создать службу"),
                 "service_rename": ("Переименовать службу", "Переименовать"),
                 "service_delete": ("Удалить службу", "Удалить службу")}


class ServicesPanel(tk.Frame):
    def __init__(self, parent, app, colors):
        super().__init__(parent, bg=colors["bg"])
        self.app, self.c = app, colors
        self.running = False
        self.mode = "service_create"
        self.agents = installed_agents()
        self._updating = False
        self._automatic_data = True
        self._command = None
        self._results = {}
        self._create_draft = None
        self._writing_preview = False
        self._build()
        self.show_mode(self.mode)

    def _build(self):
        app, c = self.app, self.c
        heading = tk.Frame(self, bg=c["panel"], padx=18, pady=12,
                            highlightthickness=1, highlightbackground=c['border'])
        self.heading = heading
        self.title = app._label(heading, "", 15, bold=True)
        self.title.pack(anchor=tk.W)
        host = os.environ.get("COMPUTERNAME", "текущий компьютер")
        app._label(heading, f"Службы Windows  /  {host}", 10, "muted").pack(anchor=tk.W, pady=(3, 0))

        self.actions = tk.Frame(self, bg=c["panel"], padx=18, pady=12,
                                highlightthickness=1, highlightbackground=c['border'])
        self.actions.pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 12))
        self.execute_button = app._button(self.actions, "", self.execute, primary=True)
        self.execute_button.pack(side=tk.RIGHT)
        self.copy_button = app._button(self.actions, "Копировать команду", self.copy_command)
        self.copy_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.state_label = app._label(self.actions, "Проверьте параметры перед выполнением", 10, "muted", anchor=tk.W)
        self.state_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        heading.pack(fill=tk.X, padx=22, pady=(0, 12))
        self._heading_pack = heading.pack_info()

        body = tk.Frame(self, bg=c["bg"])
        self.body = body
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        body.columnconfigure(0, weight=1, minsize=330)
        body.columnconfigure(1, weight=2, minsize=350)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=c["panel"], padx=18, pady=16,
                         highlightthickness=1, highlightbackground=c['border'])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right = tk.Frame(body, bg=c["panel"], padx=18, pady=16,
                          highlightthickness=1, highlightbackground=c['border'])
        right.grid(row=0, column=1, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        # Forms scroll vertically instead of pushing the action bar out of view.
        canvas = tk.Canvas(left, bg=c["panel"], highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        self.form = tk.Frame(canvas, bg=c["panel"])
        window = canvas.create_window(0, 0, anchor=tk.NW, window=self.form)
        self.form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))

        self.forms = {}
        self.inputs = []
        for mode in SERVICE_MODES:
            self.forms[mode] = tk.Frame(self.form, bg=c["panel"])
        create = self.forms["service_create"]
        app._label(create, "Параметры агента 1С", 12, bold=True).pack(anchor=tk.W, pady=(0, 12))
        self.port = tk.StringVar(value="1540")
        self._field(create, "Порт агента", self.port)
        app._label(create, "1540 → 1541, 1560–1591  ·  4540 → 4541, 4560–4591",
                   9, "muted", wraplength=330, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 10))
        versions = sorted(self.agents, key=lambda value: tuple(map(int, value.split('.'))), reverse=True)
        self.version = tk.StringVar(value=versions[0] if versions else "8.3.25.1286")
        app._label(create, "Версия платформы", 10, "muted").pack(anchor=tk.W, pady=(0, 5))
        self.version_entry = ttk.Combobox(create, textvariable=self.version, values=versions, font=("Segoe UI", 11))
        self.version_entry.pack(fill=tk.X, ipady=5, pady=(0, 12))
        self.inputs.append(self.version_entry)
        self.data_path = tk.StringVar(value=r"G:\srvinfo_1541")
        self._field(create, "Каталог данных службы", self.data_path)
        path_actions = tk.Frame(create, bg=c["panel"])
        path_actions.pack(fill=tk.X, pady=(6, 12))
        self.browse_button = app._button(path_actions, "Выбрать…", self.browse_data)
        self.browse_button.pack(side=tk.LEFT)
        self.auto_button = app._button(path_actions, "Авто", self.reset_data)
        self.auto_button.pack(side=tk.LEFT, padx=(6, 0))
        self.inputs.extend((self.browse_button, self.auto_button))
        self.parameters = app._label(create, "", 10, "accent", justify=tk.LEFT, anchor=tk.W, wraplength=330)
        self.parameters.pack(fill=tk.X, pady=(0, 10))
        app._label(create, "Имя, путь ragent.exe и автозапуск подставляются автоматически.",
                   9, "muted", wraplength=330, justify=tk.LEFT).pack(anchor=tk.W)

        rename = self.forms["service_rename"]
        app._label(rename, "Новое отображаемое имя", 12, bold=True).pack(anchor=tk.W, pady=(0, 12))
        self.rename_name = tk.StringVar()
        self.display_name = tk.StringVar()
        self._field(rename, "Имя службы (ServiceName)", self.rename_name)
        self._field(rename, "Новое отображаемое имя", self.display_name)
        app._label(rename, "Изменяется DisplayName. Внутреннее имя службы сохраняется.",
                   10, "muted", wraplength=330, justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 0))

        delete = self.forms["service_delete"]
        app._label(delete, "Удаление регистрации службы", 12, bold=True).pack(anchor=tk.W, pady=(0, 12))
        self.delete_name = tk.StringVar()
        self._field(delete, "Имя службы (ServiceName)", self.delete_name)
        app._label(delete, "Удаляется выбранная служба Windows. Файлы платформы и каталог данных сохраняются.",
                   10, "muted", wraplength=330, justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 0))

        app._label(right, "Команда и результат", 12, bold=True).pack(anchor=tk.W, pady=(0, 10))
        self.description = app._label(right, "", 10, "muted", anchor=tk.W, justify=tk.LEFT, wraplength=480)
        self.description.pack(fill=tk.X, pady=(0, 10))
        self.preview_tools = tk.Frame(right, bg=c['panel'])
        self.preview_tools.pack(fill=tk.X, pady=(0, 6))
        self.reset_command_button = app._button(self.preview_tools, 'Из полей', self.reset_command)
        self.reset_command_button.configure(font=('Segoe UI', 9), padx=7, pady=3)
        self.reset_command_button.pack(side=tk.RIGHT)
        app._label(self.preview_tools, 'Команду можно изменить вручную', 9, 'muted',
                   wraplength=240, justify=tk.LEFT, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.inputs.append(self.reset_command_button)
        self.preview = self._text(right, height=5)
        self.preview.configure(state=tk.NORMAL, undo=True, maxundo=100, autoseparators=True,
                               insertbackground='#75ed91')
        bind_entry_shortcuts(self.preview)
        self.preview.bind('<<Modified>>', self._preview_changed, add='+')
        self.preview.pack(fill=tk.X, pady=(0, 12))
        app._label(right, "Ответ Windows по этой операции", 10, "muted").pack(anchor=tk.W, pady=(0, 6))
        result_frame = tk.Frame(right, bg=c["bg"])
        result_frame.pack(fill=tk.BOTH, expand=True)
        self.output = self._text(result_frame, height=3)
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        output_scroll = ttk.Scrollbar(result_frame, command=self.output.yview)
        output_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.output.configure(yscrollcommand=output_scroll.set)
        for variable in (self.port, self.version, self.rename_name, self.display_name, self.delete_name):
            variable.trace_add("write", self._refresh)
        self.data_path.trace_add("write", self._data_changed)

    def _field(self, parent, title, variable):
        self.app._label(parent, title, 10, "muted").pack(anchor=tk.W, pady=(0, 5))
        entry = self.app._entry(parent, variable)
        entry.pack(fill=tk.X, pady=(0, 10))
        self.inputs.append(entry)
        return entry

    def set_compact(self, compact):
        if compact:
            self.heading.pack_forget()
        elif not self.heading.winfo_manager():
            self.heading.pack(**self._heading_pack, before=self.body)

    def _text(self, parent, height):
        widget = tk.Text(parent, height=height, width=20, font=("Consolas", 10), wrap=tk.WORD,
                       bg='#0b0f0c', fg='#75ed91', relief="flat", bd=0, padx=12, pady=10,
                       state=tk.DISABLED, selectbackground='#20462d', selectforeground='#c9ffd6')
        configure_log(widget)
        return widget

    @staticmethod
    def _replace(widget, text, level=None):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        insert_log(widget, text, level)
        widget.configure(state=tk.DISABLED)

    def show_mode(self, mode):
        self._capture_draft()
        self.mode = mode
        for form in self.forms.values():
            form.pack_forget()
        self.forms[mode].pack(fill=tk.X)
        self.title.configure(text=SERVICE_MODES[mode][0])
        self.execute_button.configure(text=SERVICE_MODES[mode][1])
        self.execute_button._base_bg = self.c["danger"] if mode == "service_delete" else self.c["accent"]
        self.execute_button._hover_bg = self.c["danger_hover"] if mode == "service_delete" else self.c["hover"]
        self.execute_button.configure(bg=self.execute_button._base_bg)
        if mode == 'service_create':
            self.preview_tools.pack(fill=tk.X, pady=(0, 6), before=self.preview)
        else:
            self.preview_tools.pack_forget()
        self._refresh()

        self._show_result()

    def _show_result(self):
        text, status, color = self._results.get(self.mode, ('', '', 'muted'))
        self._replace(self.output, text, 'error' if color == 'danger' else None)
        if status:
            self.state_label.configure(text=status, fg=self.c[color])

    def _data_changed(self, *args):
        if not self._updating:
            self._automatic_data = False
            self._refresh()

    def _capture_draft(self):
        if not self._writing_preview and self.mode == 'service_create' and self.preview.edit_modified():
            self._create_draft = self.preview.get('1.0', 'end-1c')
            self.preview.edit_modified(False)

    def _preview_changed(self, event=None):
        if self._writing_preview or self.running or not self.preview.edit_modified():
            return
        self._capture_draft()
        self._refresh()

    def _set_preview(self, text):
        self._writing_preview = True
        try:
            if self.preview.get('1.0', 'end-1c') != text:
                self._replace(self.preview, text)
                self.preview.edit_reset()
            self.preview.edit_modified(False)
            self.preview.configure(state=tk.NORMAL if self.mode == 'service_create' and not self.running else tk.DISABLED)
        finally:
            self._writing_preview = False

    def reset_command(self):
        if self.running or self.mode != 'service_create':
            return
        self._create_draft = None
        self.preview.edit_modified(False)
        self._refresh()

    def _refresh(self, *args):
        if self._updating:
            return
        self._capture_draft()
        self._updating = True
        try:
            if self.mode == 'service_create' and self._create_draft is not None:
                command = edited_create_command(self._create_draft)
                description = f'Ручная команда · {command.name}\n{command.executable}'
                self.parameters.configure(text='Используется команда справа.\n«Из полей» вернёт автоматическое заполнение.')
            elif self.mode == "service_create":
                port, registration, first, last = agent_ports(self.port.get())
                if self._automatic_data:
                    self.data_path.set(rf"G:\srvinfo_{registration}")
                self.parameters.configure(text=f"Регистрация: {registration}\nРабочие порты: {first}–{last}")
                command = create_command(self.port.get(), self.version.get(), self.data_path.get(), self.agents)
                description = f"{command.name}\n{command.arguments[-1]}\n{command.executable}"
            elif self.mode == "service_rename":
                command = rename_command(self.rename_name.get(), self.display_name.get())
                description = f"Служба: {command.name}\nНовое имя: {self.display_name.get().strip()}"
            else:
                command = delete_command(self.delete_name.get())
                description = f"Будет удалена служба: {command.name}"
            self._command = command
            self.description.configure(text=description)
            self._set_preview(self._create_draft if self.mode == 'service_create' and self._create_draft is not None else command.preview)
            self.state_label.configure(text='Ручная команда готова' if self.mode == 'service_create' and self._create_draft is not None else 'Параметры готовы', fg=self.c["accent"])
        except ValueError as exc:
            self._command = None
            self.description.configure(text=str(exc))
            self._set_preview(self._create_draft if self.mode == 'service_create' and self._create_draft is not None else "Заполните поля для формирования команды")
            self.state_label.configure(text="Проверьте поля формы", fg=self.c["muted"])
        finally:
            self._updating = False
        self.execute_button.configure(state=tk.NORMAL if self._command and not self.running else tk.DISABLED)

    def reset_data(self):
        self._automatic_data = True
        self._refresh()

    def browse_data(self):
        path = filedialog.askdirectory(parent=self.app.root, title="Каталог данных службы", mustexist=True)
        if path:
            self.data_path.set(path.replace('/', '\\'))

    def copy_command(self):
        self._capture_draft()
        self._refresh()
        if self._command:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(self.preview.get('1.0', 'end-1c'))
            self.app._panel_status(self, "Команда службы скопирована")

    def execute(self):
        if self.running:
            return
        self._capture_draft()
        self._refresh()
        command = self._command
        if command is None:
            self.app._panel_error(self, self.description.cget('text'))
            return
        self.app._clear_panel_error(self)
        self.running = True
        for widget in (*self.inputs, self.execute_button):
            widget.configure(state=tk.DISABLED)
        self.preview.configure(state=tk.DISABLED)
        self.app._set_busy()
        self._results[self.mode] = (f"Выполняется: {command.name}…", 'Выполняется…', 'accent')
        self._show_result()
        self.app._panel_status(self, "Выполнение команды службы…")
        token = self.app.activity.begin(dict(operation='Службы Windows · ' + command.action,
            server=os.environ.get('COMPUTERNAME', 'localhost'), database=command.name, executor='Windows'))

        def worker():
            try:
                output = run_service_command(command)
                self.app.activity.end(token, 'Успешно')
                self.app._post(self.finish, command, output, "")
            except Exception as exc:
                self.app.activity.end(token, 'Ошибка')
                self.app._post(self.finish, command, "", str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def finish(self, command, output, error):
        self.running = False
        for widget in self.inputs:
            widget.configure(state=tk.NORMAL)
        self.app._set_busy()
        self._results['service_' + command.action] = (f"{command.preview}\n\n{error or output}",
                                                      'Ошибка выполнения' if error else 'Готово',
                                                      'danger' if error else 'accent')
        self._refresh()
        self._show_result()
        messages = {"create": "Служба создана. Немедленный запуск не выполнялся.",
                    "rename": "Отображаемое имя службы изменено.",
                    "delete": "Команда удаления выполнена. Windows завершит удаление после освобождения службы."}
        self.app._panel_status(self, f"Ошибка управления службой: {command.name}" if error else messages[command.action])
        if error:
            self.app._panel_error(self, error, title='Ошибка выполнения')
