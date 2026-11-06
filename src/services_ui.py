"""Local service forms with live command preview and asynchronous execution."""
import os
import threading
import tkinter as tk
from log_colors import configure_log, insert_log
from tkinter import filedialog, messagebox, ttk
from clipboard_shortcuts import bind_entry_shortcuts
from ui_theme import ParameterSheet
from publication_config import full_text_match,state
from table_sorting import TableSorter

from service_management import (agent_ports, create_command, delete_command, edited_create_command,
                                installed_agents, list_services, rename_command, run_service_command)


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
        self._services=[];self._services_loaded=False;self._listing=False;self._reload_services=False;self._selected_service_name=None
        self._service_filter_job=None
        self._build()
        self.show_mode(self.mode)
        self.bind('<Destroy>',self._closed_inventory,add='+')

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
        heading.configure(bg=c['bg'],highlightthickness=0,padx=0,pady=5)
        self.title.configure(bg=c['bg'],font=('Segoe UI',18))
        self.parameters_button=app._button(heading,'Параметры службы',lambda:self.settings_sheet.show())
        self.parameters_button.pack(side=tk.RIGHT,before=self.title)

        body = tk.Frame(self, bg=c["bg"])
        self.body = body
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        body.columnconfigure(0, weight=1, minsize=330)
        body.rowconfigure(0, weight=1)
        self.settings_sheet=ParameterSheet(self,app,c)
        self.settings_sheet.caption.configure(text='Настройка службы')
        self._parameters_seen=False
        left = tk.Frame(self.settings_sheet.content, bg=c["panel"], padx=0, pady=0,
                         highlightthickness=1, highlightbackground=c['border'])
        left.configure(highlightthickness=0);left.pack(fill=tk.BOTH,expand=True)
        right = tk.Frame(body, bg=c["panel"], padx=18, pady=16,
                          highlightthickness=1, highlightbackground=c['border'])
        right.grid(row=0, column=0, sticky="nsew")
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
        self.service_panes=tk.PanedWindow(right,orient=tk.VERTICAL,bg=c['panel'],bd=0,sashwidth=7,showhandle=True,relief=tk.FLAT)
        self.service_panes.pack(fill=tk.BOTH,expand=True)
        self.service_inventory=tk.Frame(self.service_panes,bg=c['panel'])
        inventory_bar=tk.Frame(self.service_inventory,bg=c['panel']);inventory_bar.pack(fill=tk.X,pady=(0,6))
        self.service_filter=tk.StringVar();self.service_search=app._entry(inventory_bar,self.service_filter,width=18)
        self.service_search.pack(side=tk.LEFT,fill=tk.X,expand=True,padx=(0,6));self.service_search.set_hint('Поиск служб · имя, отображаемое имя…')
        self.refresh_services_button=app._button(inventory_bar,'Обновить список',self.refresh_services);self.refresh_services_button.pack(side=tk.RIGHT)
        self.inputs.extend((self.service_search,self.refresh_services_button))
        table=tk.Frame(self.service_inventory,bg=c['panel']);table.pack(fill=tk.BOTH,expand=True);table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.services_tree=ttk.Treeview(table,columns=('name','display_name','state'),show='headings',selectmode='browse',height=6)
        for column,title,width in (('name','Имя службы',250),('display_name','Отображаемое имя',380),('state','Состояние',140)):
            self.services_tree.heading(column,text=title,anchor='center');self.services_tree.column(column,width=width,minwidth=100,anchor='center')
        self.services_tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(table,command=self.services_tree.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(table,orient=tk.HORIZONTAL,command=self.services_tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.services_tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        self.services_sorter=TableSorter(self.services_tree,colors=c)
        self.services_tree.tag_configure('on',foreground=c['success']);self.services_tree.tag_configure('off',foreground=c['danger'])
        self.services_tree.bind('<<TreeviewSelect>>',lambda event:self.select_service())
        self.services_tree.bind('<Double-1>',lambda event:self.select_service(force=True) if self.services_tree.identify_region(event.x,event.y) in ('cell','tree') else None)
        self.service_count=app._label(self.service_inventory,'Список служб ещё не загружен.',9,'muted',anchor='w');self.service_count.pack(fill=tk.X,pady=4)
        self.service_filter.trace_add('write',self.queue_service_filter)
        self.command_area=tk.Frame(self.service_panes,bg=c['panel']);self.service_panes.add(self.command_area,minsize=90,stretch='always')
        right=self.command_area
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
        self.title.configure(font=('Segoe UI',15 if compact else 18))

    def workspace_activated(self):
        if hasattr(self,'settings_sheet') and not self._parameters_seen:
            self._parameters_seen=True
            if self.mode=='service_create':self.settings_sheet.show()
            else:self.settings_sheet.hide()

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
            if str(self.service_inventory) in tuple(map(str,self.service_panes.panes())):self.service_panes.forget(self.service_inventory)
            self.preview.configure(height=5)
        else:
            self.preview_tools.pack_forget()
            if str(self.service_inventory) not in tuple(map(str,self.service_panes.panes())):
                self.service_panes.forget(self.command_area)
                self.service_panes.add(self.service_inventory,minsize=100,height=260,stretch='always')
                self.service_panes.add(self.command_area,minsize=90,height=160,stretch='always')
            self.preview.configure(height=2)
            if not self._services_loaded:self.refresh_services()
        self._refresh()

        self._show_result()

    def refresh_services(self):
        if self._listing:self._reload_services=True;return
        self._listing=True;self.refresh_services_button.configure(state=tk.DISABLED);self.service_count.configure(text='Получение служб Windows…',fg=self.c['muted'])
        operation=list_services
        def worker():
            try:rows=operation();error=''
            except Exception as exc:rows=[];error=str(exc)
            self.app._post(self.services_loaded,rows,error)
        threading.Thread(target=worker,daemon=True).start()

    def services_loaded(self,rows,error=''):
        if not self.winfo_exists():return
        self._listing=False;self.refresh_services_button.configure(state=tk.DISABLED if self.running else tk.NORMAL)
        if error:self.service_count.configure(text=error,fg=self.c['danger'])
        else:self._services=list(rows);self._services_loaded=True;self.render_services()
        if self._reload_services:self._reload_services=False;self.refresh_services()

    def queue_service_filter(self,*args):
        if self._service_filter_job is not None:self.after_cancel(self._service_filter_job)
        self._service_filter_job=self.after(120,self._apply_service_filter)

    def _apply_service_filter(self):
        self._service_filter_job=None;self.render_services()

    def render_services(self):
        if self._service_filter_job is not None:self.after_cancel(self._service_filter_job)
        self._service_filter_job=None
        selected=self.services_tree.selection();name=self.services_tree.set(selected[0],'name') if selected else ''
        self.services_tree.delete(*self.services_tree.get_children());self._visible_services=[]
        for row in self._services:
            text,tag=state(row.get('state'))
            shown=dict(row,state=text)
            if not full_text_match(shown,self.service_filter.get()):continue
            key=str(len(self._visible_services));self._visible_services.append(row)
            self.services_tree.insert('',tk.END,iid=key,values=(row['name'],row['display_name'],text),tags=(tag,))
            if row['name']==name:self.services_tree.selection_set(key);self.services_tree.focus(key)
        self.services_sorter.apply();self.service_count.configure(text='Показано: '+str(len(self._visible_services))+' / '+str(len(self._services)),fg=self.c['muted'])

    def select_service(self,force=False):
        if self.running:return
        selection=self.services_tree.selection()
        if not selection:return
        row=self._visible_services[int(selection[0])]
        if not force and self._selected_service_name==(self.mode,row['name']):return
        self._selected_service_name=(self.mode,row['name'])
        if self.mode=='service_rename':
            self.rename_name.set(row['name']);self.display_name.set(row['display_name']);self.settings_sheet.show()
        elif self.mode=='service_delete':self.delete_name.set(row['name'])

    def _closed_inventory(self,event):
        if event.widget is self and self._service_filter_job is not None:self.after_cancel(self._service_filter_job);self._service_filter_job=None

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
        if command.action=='delete' and not messagebox.askyesno('Удалить службу Windows?',command.name+'\n\nУдалить регистрацию этой службы? Файлы программы и данные не удаляются.',parent=self.app.root,default='no'):return
        self.app._clear_panel_error(self)
        if hasattr(self,'settings_sheet'):self.settings_sheet.hide()
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

        operation=run_service_command
        def worker():
            try:
                output = operation(command)
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
        elif self.mode in ('service_rename','service_delete'):
            if command.action=='delete' and self.delete_name.get()==command.name:self.delete_name.set('');self._selected_service_name=None
            self.refresh_services()
