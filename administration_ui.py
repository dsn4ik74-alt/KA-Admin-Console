"""Scrollable SQL Server and event-log forms with persistent bottom actions."""
import os
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from archive_jr import archive_logs
from sql_server import backup_plan, create_database, delete_database, format_size, identifier, inspect_restore, list_databases, query, step
from database_dialog import ask_create_database
from postgres_runner import OperationCancelled
from table_sorting import TableSorter
from operation_progress import PERCENT, ProgressState
from ui_dispatch import JobEvents
from ui_geometry import delayed_wrap
from log_colors import configure_log, insert_log


SQL_MODES = {"db_backup": "Бэкап БД", "db_restore": "Восстановление БД"}


def server_picker_path(path, machine, endpoint, local_machine=None):
    """Explorer paths belong to the console; never assume a remote drive maps."""
    path = path.replace('/', '\\')
    if path.startswith('\\\\'):
        return path
    local = (local_machine or os.environ.get('COMPUTERNAME', '')).casefold()
    host = re.split(r'[\\,]', endpoint)[0].strip().casefold()
    if machine:
        return path if local and machine.casefold() == local else ''
    if host in ('.', '(local)', 'localhost', '127.0.0.1', '::1'):
        return path
    return ''


class JobPanel(tk.Frame):
    def __init__(self, parent, app, colors, title, action):
        super().__init__(parent, bg=colors['bg'])
        self.app, self.c = app, colors
        self.running = False
        self.cancel_event = threading.Event()
        self.cancellable = False
        self.progress_state = ProgressState()
        self._finished_progress = ''
        self._finished_value = 0
        self._pending_review = None
        self._progress_job = None
        self.bind('<Destroy>', self._progress_destroyed, add='+')
        self.inputs = []
        # Reserve actions before allocating any variable-height content.
        self.actions = tk.Frame(self, bg=colors['panel'], padx=16, pady=12,
                                highlightthickness=1, highlightbackground=colors['border'])
        self.actions.pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 12))
        self.execute_button = app._button(self.actions, action, self.execute, primary=True)
        self.execute_button.pack(side=tk.RIGHT)
        self.copy_button = app._button(self.actions, 'Копировать', self.copy_results)
        self.copy_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.clear_button = app._button(self.actions, 'Очистить', self.clear_results)
        self.clear_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.cancel_button = app._button(self.actions, 'Прервать запрос', self.interrupt)
        self.cancel_button.configure(state=tk.DISABLED)
        self.state_label = app._label(self.actions, 'Готово', 9, 'muted', anchor=tk.W)
        self.state_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.title = app._label(self, title, 17, bold=True, anchor=tk.W)
        self.title.pack(fill=tk.X, padx=22, pady=(2, 14))
        body = tk.Frame(self, bg=colors['bg'])
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        body.columnconfigure(0, weight=1, minsize=290)
        body.columnconfigure(1, weight=2, minsize=340)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=colors['panel'], padx=14, pady=14,
                         highlightthickness=1, highlightbackground=colors['border'])
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        canvas = tk.Canvas(left, bg=colors['panel'], bd=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(left, command=canvas.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        canvas.configure(yscrollcommand=scroll.set)
        self.form = tk.Frame(canvas, bg=colors['panel'])
        window = canvas.create_window(0, 0, window=self.form, anchor=tk.NW)
        self.form.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        # Bind descendants too: wheel input normally arrives at entries and labels.
        self.form.bind('<Map>', lambda event: self._bind_wheel(canvas, self.form))
        self.right = tk.Frame(body, bg=colors['panel'], padx=14, pady=14,
                              highlightthickness=1, highlightbackground=colors['border'])
        self.right.grid(row=0, column=1, sticky='nsew')
        self.right.columnconfigure(0, weight=1)

    def set_compact(self, compact):
        self._compact_actions=compact
        if not self.running or not self.cancel_event.is_set():
            self.cancel_button.configure(text='Прервать' if compact else 'Прервать запрос')
        for widget in self.actions.winfo_children():
            if isinstance(widget,tk.Button):
                if not hasattr(widget,'_regular_padx'):widget._regular_padx=int(widget.cget('padx'))
                widget.configure(padx=min(5,widget._regular_padx) if compact else widget._regular_padx)

    def _bind_wheel(self, canvas, widget):
        def scroll_form(event):
            if event.delta:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
            # Stop the ttk Combobox class binding from changing the selected value.
            return 'break'
        if not isinstance(widget, ttk.Treeview):
            widget.bind('<MouseWheel>', scroll_form)
            widget.bind('<Shift-MouseWheel>', scroll_form)
        for child in widget.winfo_children():
            self._bind_wheel(canvas, child)

    def field(self, title, variable, show=None):
        label = self.app._label(self.form, title, 10, 'muted', wraplength=280, justify=tk.LEFT, anchor=tk.W)
        label.pack(fill=tk.X, pady=(8, 4))
        delayed_wrap(label)
        entry = self.app._entry(self.form, variable, show=show)
        entry.pack(fill=tk.X)
        self.inputs.append(entry)
        return entry

    def check(self, title, variable):
        widget = tk.Checkbutton(self.form, text=title, variable=variable, bg=self.c['panel'],
                                fg=self.c['fg'], activebackground=self.c['panel'],
                                selectcolor=self.c['bg'], activeforeground=self.c['accent'],
                                font=('Segoe UI', 10), anchor=tk.W, bd=0, highlightthickness=0)
        widget.pack(fill=tk.X, pady=(8, 0))
        self.inputs.append(widget)
        return widget

    def server_field(self, title, variable):
        self.app._label(self.form, title, 10, 'muted').pack(anchor=tk.W, pady=(8, 4))
        row = tk.Frame(self.form, bg=self.c['panel'])
        row.pack(fill=tk.X)
        widget = ttk.Combobox(row, textvariable=variable, state='normal', font=('Segoe UI', 10), width=1)
        widget.pack(side=tk.LEFT, fill=tk.X, expand=True)
        widget.server_actions_row = row
        self.inputs.append(widget)
        return widget

    def note(self, text):
        label = self.app._label(self.form, text, 9, 'muted', anchor=tk.W, justify=tk.LEFT, wraplength=280)
        label.pack(fill=tk.X, pady=(10, 6))
        delayed_wrap(label)

    def build_log(self, row=0, parent=None):
        frame = tk.Frame(parent or self.right, bg=self.c['bg'])
        frame.grid(row=row, column=0, sticky='nsew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.output = tk.Text(frame, width=20, height=4, bg='#0b0f0c', fg='#75ed91',
                              font=('Consolas', 10), relief='flat', wrap=tk.NONE, padx=10, pady=10,
                              state=tk.DISABLED, selectbackground='#20462d', selectforeground='#c9ffd6')
        self.output.grid(row=0, column=0, sticky='nsew')
        configure_log(self.output)
        scroll = ttk.Scrollbar(frame, command=self.output.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.output.configure(yscrollcommand=scroll.set)
        horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.output.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.output.configure(xscrollcommand=horizontal.set)
        return frame

    def split_log(self):
        """A native draggable sash gives the journal real, resizable space."""
        self.right.rowconfigure(2, weight=1)
        self.log_split = tk.PanedWindow(self.right, orient=tk.VERTICAL, bg=self.c['border'],
                                        bd=0, sashwidth=9, sashpad=2, showhandle=True,
                                        handlesize=12, sashcursor='sb_v_double_arrow', opaqueresize=False)
        self.log_split.grid(row=2, column=0, sticky='nsew')
        table = tk.Frame(self.log_split, bg=self.c['panel'])
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        journal = tk.Frame(self.log_split, bg=self.c['panel'])
        journal.columnconfigure(0, weight=1)
        journal.rowconfigure(1, weight=1)
        self.app._label(journal, 'Журнал операции · потяните границу сверху ↕', 9, 'muted',
                        anchor=tk.W).grid(row=0, column=0, sticky='ew', pady=(4, 6))
        self.log_split.add(table, minsize=60, height=250, stretch='always')
        self.log_split.add(journal, minsize=60, height=170, stretch='always')
        self.build_log(row=1, parent=journal)
        return table

    def append(self, text, level=None):
        follow = self.output.yview()[1] >= .995
        self.output.configure(state=tk.NORMAL)
        insert_log(self.output, str(text) + '\n', level)
        # Bound log memory during very large operations.
        excess = int(self.output.index('end-1c').split('.')[0]) - 5000
        if excess > 0:
            self.output.delete('1.0', f'{excess + 1}.0')
        if follow:
            self.output.see(tk.END)
        self.output.configure(state=tk.DISABLED)

    def clear_results(self):
        if self.running:
            return
        self.output.configure(state=tk.NORMAL)
        self.output.delete('1.0', tk.END)
        self.output.configure(state=tk.DISABLED)

    def copy_results(self, event=None):
        text = self.output.get('1.0', 'end-1c')
        if text:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(text)
            self.app._panel_status(self, 'Журнал операции скопирован')
        return 'break'

    def start(self):
        if self.running:
            return False
        self.app.database_sizes.cancel(self)
        self.app._clear_panel_error(self)
        self.running = True
        self._finished_progress = ''
        self.progress_state.start()
        self.cancel_event.clear()
        if self.cancellable:
            self.cancel_button.configure(state=tk.NORMAL, text='Прервать' if getattr(self,'_compact_actions',False) else 'Прервать запрос')
        for widget in (*self.inputs, self.execute_button, self.clear_button):
            widget.configure(state=tk.DISABLED)
        self.app._set_busy()
        if self.app._panel_visible(self):
            self.app.progress.configure(mode='indeterminate')
            self.app.progress.start(12)
            self.app.progress_pct.configure(text=self.progress_state.text(), width=42, height=2)
        self._progress_tick()
        self.state_label.configure(text='Выполняется…', fg=self.c['accent'])
        return True

    def event(self, event):
        self.events([event])

    def events(self, events):
        lines, last_stage = [], None
        progress_changed = False
        for event in events:
            if event.get('kind') == 'progress':
                self.progress_state.update(event['percent'], event.get('unit', 'этапа'), event.get('estimated', False), event.get('phase', ''))
                progress_changed = True
                continue
            text = event.get('text', '')
            if not text:
                continue
            kind = event.get('kind')
            level = 'warning' if kind == 'warning' else 'error' if kind in ('error', 'fatal') else None
            if lines and lines[-1][0] == level:
                lines[-1][1].append(text)
            else:
                lines.append((level, [text]))
            if event.get('kind') == 'stage':
                self.progress_state.stage(text)
                last_stage = text
                progress_changed = True
            else:
                for match in PERCENT.finditer(text):
                    self.progress_state.update(float(match[1].replace(',', '.')))
                    progress_changed = True
        for level, texts in lines:
            self.append('\n'.join(texts), level)
        if last_stage is not None:
            self.app._panel_status(self, last_stage)
            self.state_label.configure(text=last_stage)
        if progress_changed:
            if self.progress_state.percent is None and self.app._panel_visible(self):
                self.app.progress.stop()
                self.app.progress.configure(mode='indeterminate')
                self.app.progress.start(12)
            self._show_progress()

    def _show_progress(self):
        if not self.app._panel_visible(self):
            return
        if self.progress_state.percent is not None:
            self.app.progress.stop()
            self.app.progress.configure(mode='determinate', maximum=100, value=self.progress_state.percent)
        self.app.progress_pct.configure(text=self.progress_state.text())

    def restore_progress(self):
        if self.running:
            if self.progress_state.percent is None:
                self.app.progress.configure(mode='indeterminate')
                self.app.progress.start(12)
            self.app.progress_pct.configure(width=42, height=2)
            self._show_progress()
        elif self._finished_progress:
            self.app.progress.configure(mode='determinate', value=self._finished_value)
            self.app.progress_pct.configure(text=self._finished_progress, width=42, height=2)

    def _progress_tick(self):
        self._progress_job = None
        if self.running:
            self._show_progress()
            self._progress_job = self.after(1000, self._progress_tick)

    def defer_until_visible(self, callback):
        if self.app._panel_visible(self):
            return False
        self._pending_review = callback
        self.state_label.configure(text='Требуется подтверждение · откройте вкладку')
        self.app._panel_status(self, 'Требуется подтверждение во вкладке операции')
        return True

    def on_activated(self):
        if self._pending_review and self.app._panel_visible(self):
            callback, self._pending_review = self._pending_review, None
            callback()

    def _progress_destroyed(self, event):
        if event.widget == self and self._progress_job is not None:
            self.after_cancel(self._progress_job)
            self._progress_job = None

    def history_context(self, operation, database='', target='', path='', server=''):
        from activity_ui import endpoint
        profile = self.app.hub.profile(self)
        executor = self.execution.get() if hasattr(self, 'execution') else 'Windows'
        if hasattr(self, '_execution') and self._execution != 'local':
            executor += ' · ' + self.ssh_user.get() + '@' + self.ssh_host.get()
        return dict(operation=operation, server=server or (endpoint(profile) if profile else ''),
                    database=database, target=target, path=path, executor=executor)

    def work(self, operation, success, history=None):
        delivery = JobEvents(self.app._post, self.events)
        token = self.app.activity.begin(history) if history else None
        secrets=[]
        for owner in (self,self.app):
            for name in ('password','target_password','ib_password','db_password','ssh_password','sudo_password','pwd_var','its_password','service_password'):
                variable=getattr(owner,name,None)
                if isinstance(variable,tk.Variable):secrets.append(variable.get())
        if hasattr(self, 'secret_values'):
            secrets.extend(self.secret_values())
        journal = self.app.activity.logs.writer(token[0],secrets) if token else None
        def on_event(event):
            if token and event.get('kind') == 'artifact':
                self.app.activity.artifact(token, event)
            if journal and event.get('text'):journal.write(event['text'])
            delivery.emit(event)
        on_event.cancel_event = self.cancel_event
        def worker():
            try:
                if journal:journal.write(history['operation'])
                result = operation(on_event)
                if token:
                    status = 'Требует проверки' if isinstance(result, dict) and result.get('settings_error') else 'Успешно'
                    journal.write('Результат: '+status)
                    journal.close()
                    self.app.activity.end(token,status)
                delivery.complete(self._work_completed, success, result)
            except OperationCancelled as exc:
                if token:
                    journal.write('ПРЕРВАНО: '+str(exc));journal.close()
                    self.app.activity.end(token, 'Прервано')
                delivery.complete(self.finish_cancelled, str(exc))
            except Exception as exc:
                if token:
                    journal.write('ОШИБКА: '+str(exc));journal.close()
                    self.app.activity.end(token, 'Ошибка')
                delivery.complete(self.finish, '', str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def _work_completed(self, success, result):
        try:
            success(result)
        except Exception as exc:
            # The operation has already run. Unlock the UI without retrying it.
            self.finish('', 'Не удалось обработать результат операции: ' + str(exc))

    def enable_cancellation(self):
        self.cancellable = True
        self.cancel_button.pack(side=tk.RIGHT, padx=(0, 8), before=self.clear_button)

    def interrupt(self):
        if self._pending_review:
            self._pending_review = None
            self.finish_cancelled('Операция отменена до подтверждения изменений')
            return
        if self.running and self.cancellable and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.configure(state=tk.DISABLED, text='Прерывание…')
            self.append('Запрошено прерывание. Ожидание остановки операции…')
            self.app._panel_status(self, 'Прерывание запроса…')

    def finish_cancelled(self, message):
        self.finish('', message)
        self.state_label.configure(text='Прервано', fg=self.c['danger'])
        self.app._panel_status(self, 'Операция прервана — подробности в журнале')

    def finish(self, text='', error=''):
        self.running = False
        if self._progress_job is not None:
            self.after_cancel(self._progress_job)
            self._progress_job = None
        self.cancel_button.configure(state=tk.DISABLED, text='Прервать' if getattr(self,'_compact_actions',False) else 'Прервать запрос')
        for widget in (*self.inputs, self.execute_button, self.clear_button):
            widget.configure(state=tk.NORMAL)
        complete = not error and (self.progress_state.complete_task or self.progress_state.measured)
        self._finished_value = 100 if complete else 0
        self._finished_progress = self.progress_state.finished_text(not error)
        if self.app._panel_visible(self):
            self.app.progress.stop()
            self.restore_progress()
        self.app._set_busy()
        self.state_label.configure(text='Ошибка' if error else 'Готово', fg=self.c['danger'] if error else self.c['accent'])
        self.append(('ОШИБКА: ' + error) if error else text)
        self.app._panel_status(self, 'Операция завершилась с ошибкой — подробности в журнале' if error else text)
        if error:
            self.app._panel_error(self, error, title='Ошибка выполнения')


class SqlPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Бэкап БД', 'Сделать FULL')
        self.enable_cancellation()
        self.mode = 'db_backup'
        self.databases = []
        self.machine = ''
        self.connected = None
        self.server = tk.StringVar(value='localhost')
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.windows_auth = tk.BooleanVar(value=True)
        self.encrypt = tk.BooleanVar(value=True)
        self.trust_certificate = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar()
        self.server_path = tk.StringVar()
        self.client_path = tk.StringVar()
        self.compression = tk.BooleanVar(value=True)
        self.copy_only = tk.BooleanVar(value=True)
        self.verify_restore = tk.BooleanVar(value=True)
        self.server_entry = self.server_field('SQL Server · сервер\\экземпляр или сервер,порт', self.server)
        self.check('Аутентификация Windows', self.windows_auth)
        self.field('Логин SQL Server', self.username)
        self.field('Пароль SQL Server', self.password, show='•')
        self.check('Шифровать соединение', self.encrypt)
        self.check('Доверять сертификату сервера', self.trust_certificate)
        self.connect_button = app._button(self.form, 'Подключиться / список БД', self.connect)
        self.connect_button.pack(fill=tk.X, pady=(12, 4))
        self.inputs.append(self.connect_button)
        self.create_db_button = app._button(self.form, '+ Создать базу', self.create_new_database)
        self.create_db_button.pack(fill=tk.X, pady=(4, 4))
        self.inputs.append(self.create_db_button)
        self.delete_db_button = app._button(self.form, 'Удалить выбранную базу…', self.delete_selected_database)
        self.delete_db_button.pack(fill=tk.X, pady=(4, 4))
        self.delete_db_button.configure(fg=colors['danger'])
        self.inputs.append(self.delete_db_button)
        self.path_label = app._label(self.form, 'Папка бэкапов на SQL Server', 10, 'muted')
        self.path_label.pack(anchor=tk.W, pady=(12, 4))
        self.path_entry = app._entry(self.form, self.server_path)
        self.path_entry.pack(fill=tk.X)
        self.inputs.append(self.path_entry)
        self.picker_button = app._button(self.form, 'Выбрать в проводнике…', self.choose_path)
        self.picker_button.pack(fill=tk.X, pady=(8, 0))
        self.inputs.append(self.picker_button)
        self.note('Проводник открыт на компьютере консоли. SQL Server читает и пишет от имени своей службы. Для удалённого сервера укажите его полный путь или общую UNC-папку; файл не загружается на сервер.')
        self.client_label = app._label(self.form, '', 9, 'muted', justify=tk.LEFT, anchor=tk.W, wraplength=280)
        self.client_label.pack(fill=tk.X)
        self.options = tk.Frame(self.form, bg=colors['panel'])
        self.options.pack(fill=tk.X)
        # Option widgets use the same helper but live in a hideable container.
        saved = self.form
        self.form = self.options
        self.check('Сжатие COMPRESSION', self.compression)
        self.check('COPY_ONLY', self.copy_only)
        self.note('FULL с CHECKSUM и проверкой VERIFYONLY. COPY_ONLY сохраняет основу дифференциальных копий. Имя файла: база_FULL_дата_время.bak.')
        self.form = saved
        self.verify_check = self.check('Проверять бэкап перед восстановлением', self.verify_restore)
        table_area = self.split_log()
        filter_row = tk.Frame(self.right, bg=colors['panel'])
        filter_row.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        app._label(filter_row, 'Базы данных', 12, bold=True).pack(side=tk.LEFT, padx=(0, 10))
        self.filter_entry = app._entry(filter_row, self.filter_var)
        self.filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.summary = tk.Frame(self.right, bg=colors['panel'])
        self._compact = False
        self.summary.grid(row=1, column=0, sticky='ew', pady=(2, 12))
        self.metric_values = {}
        for column, (key, title) in enumerate((('count', 'Базы в списке'), ('data_bytes', 'Данные'),
                                              ('log_bytes', 'Лог'), ('total_bytes', 'Всего'))):
            self.summary.columnconfigure(column, weight=1, uniform='metrics')
            card = tk.Frame(self.summary, bg=colors['row'], padx=10, pady=8,
                             highlightthickness=1, highlightbackground=colors['border'])
            card.grid(row=0, column=column, sticky='nsew', padx=(0, 8 if column < 3 else 0))
            app._label(card, title, 9, 'muted', anchor=tk.W).pack(fill=tk.X)
            value = tk.StringVar(value='—')
            self.metric_values[key] = value
            app._label(card, '', 14, 'accent', bold=True, textvariable=value, anchor=tk.W).pack(fill=tk.X, pady=(3, 0))
        frame = tk.Frame(table_area, bg=colors['panel'])
        frame.grid(row=0, column=0, sticky='nsew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=('name', 'data', 'log', 'total', 'recovery'),
                                 show='headings', selectmode='browse', height=4)
        for key, title, width in (('name', 'База данных', 230), ('data', 'Данные', 110),
                                  ('log', 'Лог', 110), ('total', 'Всего', 110),
                                  ('recovery', 'Восстановление', 150)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=100, stretch=key == 'name',
                             anchor=tk.W if key in ('name', 'recovery') else tk.E)
        self.tree.tag_configure('even', background=colors['panel'])
        self.tree.tag_configure('odd', background=colors['row'])
        self.sorter = TableSorter(self.tree, numeric=('data', 'log', 'total'), colors=colors)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.bind('<<TreeviewSelect>>', self.update_summary)
        self.selected_label = app._label(table_area, 'Выберите базу в списке', 10, 'accent', anchor=tk.W)
        self.selected_label.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        delayed_wrap(self.selected_label)
        self.table_note = app._label(table_area, 'Данные — MDF/NDF, лог — LDF. Размер выделенных файлов.\nПапку или файл выберите кнопкой «Выбрать в проводнике…».',
                                9, 'muted', justify=tk.LEFT, anchor=tk.W)
        self.table_note.grid(row=2, column=0, sticky='ew', pady=(4, 8))
        delayed_wrap(self.table_note)
        self.filter_var.trace_add('write', self.render)
        for variable in (self.server, self.windows_auth, self.username, self.password, self.encrypt, self.trust_certificate):
            variable.trace_add('write', self._connection_changed)
        self.right.bind('<Configure>', lambda event: self._resize_summary())

    def connection(self):
        return dict(server=self.server.get().strip(), windows_auth=self.windows_auth.get(), username=self.username.get(),
                    password=self.password.get(), encrypt=self.encrypt.get(), trust_certificate=self.trust_certificate.get())

    def _connection_changed(self, *args):
        self.connected = None
        self.machine = ''
        self.databases = []
        self.render()
        self.server_path.set('')
        self.client_path.set('')
        self.client_label.configure(text='')

    def show_mode(self, mode):
        if mode != self.mode:
            self.server_path.set('')
            self.client_path.set('')
            self.client_label.configure(text='')
        self.mode = mode
        self.title.configure(text=SQL_MODES[mode] + ' · Microsoft SQL Server')
        self.execute_button.configure(text='Сделать FULL' if mode == 'db_backup' else 'Восстановить')
        self.path_label.configure(text='Папка бэкапов на SQL Server' if mode == 'db_backup' else 'Файл FULL-бэкапа на SQL Server')
        if mode == 'db_backup':
            self.options.pack(fill=tk.X)
            self.verify_check.pack_forget()
        else:
            self.verify_check.pack(fill=tk.X, pady=(8, 0))
            self.options.pack_forget()

    def render(self, *args):
        selected = self.selected()
        query_text = self.filter_var.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        visible = 0
        values = {}
        for row in self.databases:
            if hasattr(self.app, 'hub') and not self.app.hub.visible(self, 'source', row['name']):
                continue
            if query_text in row['name'].casefold():
                key = self.tree.insert('', tk.END, values=(row['name'], format_size(row.get('data_bytes')),
                                       format_size(row.get('log_bytes')), format_size(row.get('total_bytes')),
                                       row['recovery_model_desc']), tags=('odd' if visible % 2 else 'even',))
                visible += 1
                values[key] = dict(name=row['name'], data=row.get('data_bytes'), log=row.get('log_bytes'),
                                   total=row.get('total_bytes'), recovery=row['recovery_model_desc'])
                if row['name'] == selected:
                    self.tree.selection_set(key)
        self.sorter.apply(values)
        self.update_summary()
        if hasattr(self.app, 'hub'):
            self.app.hub.restore_pending(self)

    def create_new_database(self):
        if self.running:
            return
        connection = self.connection()
        settings = ask_create_database(self.app.root, 'SQL Server', connection['server'], connection['username']
                                       if not connection['windows_auth'] else '', self.c)
        if not settings:
            return
        database = settings['name']
        def operation(event):
            create_database(connection, database, event)
            try:
                return list_databases(connection, event), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.database_created(connection, database, result),
                      history=self.history_context('Создание БД · SQL Server', database))

    def database_created(self, connection, database, result):
        refreshed, error = result
        if refreshed is not None:
            self.databases, server = refreshed
            self.machine, self.connected = server.get('machine', ''), connection
            self.filter_var.set('')
            self.render()
            for key in self.tree.get_children():
                if self.tree.set(key, 'name') == database:
                    self.tree.selection_set(key)
                    self.tree.see(key)
            self.update_summary()
        else:
            self.append('База создана, но список не обновлён: ' + error + '\nНажмите «Подключиться / список БД».')
        self.finish('База создана: ' + database)

    def delete_selected_database(self):
        if self.running:
            return
        connection, database = self.connection(), self.selected()
        if not database or self.connected != connection:
            self.app._panel_error(self, 'Подключитесь к SQL Server и выберите одну базу для удаления')
            return
        if database.casefold() in ('master', 'model', 'msdb', 'tempdb'):
            self.app._panel_error(self, 'Удаление системной базы SQL Server запрещено')
            return
        detail = (f"SQL Server: {connection['server']}\nБаза: {database}\n\n"
                  'База и её данные будут удалены. Текущие подключения будут завершены.\n'
                  'Резервная копия автоматически не создаётся.\n\nУдалить эту базу?')
        if not messagebox.askyesno('Удалить базу SQL Server?', detail, parent=self.app.root, icon='warning', default='no'):
            return
        def operation(event):
            delete_database(connection, database, event)
            try:
                return list_databases(connection, event), ''
            except Exception as exc:
                return None, str(exc)
        if self.start():
            self.work(operation, lambda result: self.database_deleted(connection, database, result),
                      history=self.history_context('Удаление БД · SQL Server', database))

    def database_deleted(self, connection, database, result):
        refreshed, error = result
        if refreshed is not None:
            self.loaded(connection, refreshed)
        else:
            self.databases = [row for row in self.databases if row['name'] != database]
            self.render()
            self.append('База удалена, но список не обновлён: ' + error + '\nНажмите «Подключиться / список БД».')
        self.finish('База удалена: ' + database)

    def update_summary(self, event=None):
        database = self.selected()
        self.metric_values['count'].set(f'{len(self.tree.get_children())} / {len(self.databases)}')
        row = next((row for row in self.databases if row['name'] == database), {})
        for key in ('data_bytes', 'log_bytes', 'total_bytes'):
            self.metric_values[key].set(format_size(row.get(key)))
        self.selected_label.configure(text='Выбрана база: ' + database if database else 'Выберите базу в списке')

    def set_compact(self, compact):
        super().set_compact(compact)
        self._compact = compact
        self._resize_summary()

    def _resize_summary(self):
        scale = max(1, float(self.tk.call('tk', 'scaling')) / 1.33)
        show = not self._compact and self.right.winfo_width() >= round(600 * scale)
        if show != getattr(self, '_summary_visible', None):
            self._summary_visible = show
            self.summary.grid() if show else self.summary.grid_remove()
        compact = self._compact
        if compact != getattr(self, '_note_compact', None):
            self._note_compact = compact
            self.table_note.grid_remove() if compact else self.table_note.grid()

    def selected(self):
        selection = self.tree.selection()
        return self.tree.item(selection[0], 'values')[0] if selection else ''

    def connect(self):
        connection = self.connection()
        if not self.start():
            return
        self.work(lambda event: list_databases(connection, event), lambda result: self.connection_loaded(connection, result))

    def connection_loaded(self, connection, result):
        self.loaded(connection, result)
        self.app.saved_servers.remember(self, connection)

    def loaded(self, connection, result):
        self.databases, server = result
        self.machine = server.get('machine', '')
        self.connected = connection
        self.render()
        self.finish(f"Подключено: {server.get('server', connection['server'])}. Доступных БД: {len(self.databases)}. Выберите базу.")

    def choose_path(self):
        if self.running:
            return
        if not self.selected():
            self.app._panel_error(self, 'Сначала подключитесь и выберите базу в списке')
            return
        if self.mode in ('db_backup', 'db_copy'):
            path = filedialog.askdirectory(parent=self.app.root, title='Папка для FULL-бэкапа', mustexist=True)
        else:
            path = filedialog.askopenfilename(parent=self.app.root, title='Полный бэкап для восстановления',
                                             filetypes=[('Бэкапы SQL Server', '*.bak'), ('Все файлы', '*.*')])
        if path:
            self.client_path.set(path)
            self.client_label.configure(text='Выбрано в проводнике: ' + path)
            mapped = server_picker_path(path, self.machine, self.server.get())
            self.server_path.set(mapped)
            if not mapped:
                self.app._panel_error(self, 'Удалённый SQL Server: укажите серверный путь к выбранному файлу или папке')
                self.path_entry.focus_set()

    def execute(self):
        if self.running:
            return
        database = self.selected()
        connection = self.connection()
        if self.connected != connection:
            self.app._panel_error(self, 'Подключение к SQL Server не установлено или его параметры изменены. Нажмите «Подключиться / список БД».')
            return
        if not database:
            self.app._panel_error(self, 'Выберите базу данных в списке справа.', self.tree)
            return
        path = self.server_path.get().strip()
        if not path:
            self.app._panel_error(self, 'Укажите путь, доступный службе SQL Server', self.path_entry)
            return
        if self.mode == 'db_backup':
            try:
                target, steps = backup_plan(database, path, compression=self.compression.get(), copy_only=self.copy_only.get())
            except ValueError as exc:
                self.app._panel_error(self, str(exc))
                return
            if self.start():
                self.progress_state.complete_task = True
                self.append(f"FULL: {connection['server']} / {database}\nФайл на SQL Server: {target}")
                def operation(event):
                    result = query(connection, steps, event)
                    from sql_server import report_backup_artifact
                    report_backup_artifact(connection, target, event, result)
                    return result
                self.work(operation, lambda result: self.finish('FULL-бэкап создан и проверен: ' + target),
                          history=self.history_context('FULL-бэкап · SQL Server', database, path=target))
        else:
            verify = self.verify_restore.get()
            details = (f"Сервер: {connection['server']}\nЦелевая база: {database}\nФайл: {path}\n"
                       + ('Проверка VERIFYONLY включена.' if verify else 'Проверка VERIFYONLY отключена.')
                       + '\nПосле восстановления: SIMPLE → сжатие журнала получателя до 1 ГБ → FULL.'
                       + '\n\nВсе текущие данные целевой базы будут заменены. Подключения будут закрыты. Продолжить?')
            try:
                approved = messagebox.askyesno('Заменить базу полным бэкапом?', details,
                                               parent=self.app.root, icon='warning', default='no')
            except tk.TclError as exc:
                self.app._panel_error(self, str(exc))
                return
            if not approved:
                self.append('Восстановление отменено. Целевая база не изменялась.')
                return
            if self.start():
                self.work(lambda event: inspect_restore(connection, database, path, event, verify=verify),
                          lambda plan: self.confirm_restore(connection, plan, approved=True),
                          history=self.history_context('Подготовка восстановления · SQL Server', database, path=path))

    def confirm_restore(self, connection, plan, *, approved=False):
        if not approved and self.defer_until_visible(lambda: self.confirm_restore(connection, plan)):
            return
        details = (f"Сервер: {connection['server']}\nЦелевая база: {plan.database}\nФайл: {plan.path}\n"
                   f"База в бэкапе: {plan.source_database}\nДата: {plan.backup_date}\nНабор FULL: {plan.position}\n\n"
                   + '\n'.join(f'{old} → {path}' for old, path in plan.moves)
                   + '\n\nЛогические имена: ' + ', '.join(plan.logical_names))
        self.append(details)
        if not approved:
            try:
                approved = messagebox.askyesno('Заменить базу полным бэкапом?',
                    details + '\n\nВсе текущие данные целевой базы будут заменены. Подключения будут закрыты. Продолжить?',
                    parent=self.app.root, icon='warning', default='no')
            except tk.TclError as exc:
                self.finish('', str(exc))
                return
        if not approved:
            # Verification finished, but the requested restore did not run.
            self.progress_state.measured = False
            self.finish('Восстановление отменено. Целевая база не изменялась.')
            return
        self.progress_state.complete_task = True
        def operation(event):
            event(dict(kind='artifact', text='', path=plan.path, size=plan.backup_size))
            from sql_server import restore_events,restore_cleanup
            emit,state=restore_events(event)
            try:
                return query(connection, [step(plan.sql, 'Восстановление FULL и переименование файлов', timeout=0)], emit)
            except OperationCancelled as exc:
                # Use an independent connection to restore access after cancelling the restore batch.
                try:
                    cleanup_sql = restore_cleanup(plan.database,state['restored'])
                    query(connection, [step(cleanup_sql, timeout=30)], lambda item: event(item))
                except Exception as cleanup:
                    event({'kind': 'message', 'text': 'Не удалось вернуть MULTI_USER: ' + str(cleanup)})
                raise OperationCancelled('Восстановление прервано. Прежние данные не возвращаются; состояние базы '
                                         'и окончание отката нужно проверить на SQL Server.') from exc
        self.work(operation,
                  lambda result: self.finish(f'База {plan.database} восстановлена. Логические имена обновлены. Журнал обработан. Модель FULL, режим MULTI_USER.'),
                  history=self.history_context('Восстановление · SQL Server', plan.database, path=plan.path))


class ArchivePanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Архивирование журнала регистрации 1С', 'Архивировать ЖР')
        self.source = tk.StringVar(value=r'C:\Program Files\1cv8\srvinfo_4\reg_4541\c6d0d422-2476-47df-af06-98bcfbca94ea\1Cv8Log')
        self.destination = tk.StringVar(value=r'F:\Archive_JR')
        self.base_name = tk.StringVar(value='GitConverter')
        self.months = tk.StringVar(value='1')
        self.delete = tk.BooleanVar(value=True)
        self.source_entry = self.field('Папка ЖР или корень кластера', self.source)
        self.folder_button('Выбрать папку ЖР…', self.source)
        self.field('Папка ZIP-архивов (можно UNC)', self.destination)
        self.folder_button('Выбрать папку архивов…', self.destination)
        self.field('Имя базы · пустое для кластера', self.base_name)
        self.field('Сохранять последние N месяцев', self.months)
        self.check('Удалять LGP/LGX после проверки ZIP', self.delete)
        self.note('Архивируются старые LGP/LGX. ZIP проверяется по содержимому и SHA-256 перед удалением. Словарь 1Cv8.lgf сохраняется. Формат ЖР SQLite (.lgd) не поддерживается.')
        self.right.rowconfigure(1, weight=1)
        app._label(self.right, 'Ход архивирования и результат', 12, bold=True).grid(row=0, column=0, sticky='w', pady=(0, 10))
        self.build_log(row=1)

    def folder_button(self, title, variable):
        def browse():
            path = filedialog.askdirectory(parent=self.app.root, title=title, mustexist=True)
            if path:
                variable.set(path.replace('/', '\\'))
        button = self.app._button(self.form, title, browse)
        button.pack(fill=tk.X, pady=(8, 0))
        self.inputs.append(button)

    def execute(self):
        values = (self.source.get(), self.destination.get(), self.base_name.get(), self.months.get(), self.delete.get())
        if self.start():
            self.append('Архивирование ЖР: ' + values[0] + '\nУдаление после проверки ZIP: ' + ('да' if values[4] else 'нет'))
            self.work(lambda event: archive_logs(*values, on_event=event),
                      lambda result: self.finish('Архивирование ЖР завершено. Подробности выше.'),
                      history=self.history_context('Архивирование ЖР', values[2], path=values[1]))
