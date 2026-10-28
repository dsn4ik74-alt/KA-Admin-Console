"""Sequence editor and a day/run/step journal; all long work stays outside Tk."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from log_colors import configure_log, insert_log
from operation_progress import duration
from scheduler_actions import LABELS, capture, destructive, describe, validate_step
from scheduler_store import SchedulerStore
from scheduler_windows import control, current_user


KINDS = {'Один раз': 'once', 'Ежедневно': 'daily', 'По дням недели': 'weekly', 'Через интервал': 'interval'}
FIELD_NAMES = {
 'connection':'Подключение источника', 'target_connection':'Подключение получателя', 'host':'Сервер',
 'port':'Порт', 'username':'Логин СУБД', 'password':'Пароль', 'sslmode':'SSL',
 'execution':'Компьютер выполнения · local Windows / ssh Linux', 'ssh_host':'Адрес SSH',
 'ssh_port':'Порт SSH', 'ssh_user':'Пользователь SSH', 'ssh_password':'Пароль SSH / ключа',
 'ssh_use_key':'Использовать SSH-ключ', 'ssh_key':'Файл SSH-ключа', 'database':'База данных',
 'source_database':'База источника', 'target_database':'База получателя', 'databases':'Выбранные базы · список JSON',
 'source':'Источник', 'destination':'Получатель', 'path':'Папка / файл бэкапа', 'data_path':'Рабочая папка ibcmd',
 'ib_user':'Пользователь информационной базы 1С', 'ib_password':'Пароль информационной базы 1С',
 'dump_jobs':'Потоки бэкапа', 'restore_jobs':'Потоки восстановления', 'compression':'Сжатие',
 'retention_days':'Хранить бэкапы · суток, 0 хранить все', 'format':'Формат · dump / directory / sql.gz',
 'dt_keep_remote':'Оставить DT на компьютере выполнения', 'windows_auth':'Аутентификация Windows',
 'server':'Сервер', 'encrypt':'Шифрование соединения', 'trust_certificate':'Доверять сертификату сервера',
 'copy_only':'COPY_ONLY', 'verify':'Проверять бэкап перед восстановлением', 'target':'Сервер 1С', 'cluster':'UUID кластера', 'user':'Пользователь',
 'local':'Временный локальный RAS', 'ras_port':'Порт RAS', 'values':'Параметры информационной базы',
 'name':'Имя', 'descr':'Описание', 'security-level':'Защищённое соединение', 'db-server':'Сервер СУБД',
 'dbms':'Тип СУБД', 'db-name':'База СУБД', 'db-user':'Пользователь СУБД', 'db-pwd':'Пароль СУБД',
 'license-distribution':'Выдача лицензий · allow / deny', 'locale':'Язык', 'date-offset':'Смещение дат',
 'create-database':'Создать базу при отсутствии', 'scheduled-jobs-deny':'Блокировка заданий · on / off',
 'infobase':'UUID информационной базы', 'denied':'Блокировка регламентных заданий',
 'exclusive_confirmed':'Подтверждаю: доступ других процессов 1С остановлен вручную',
 'base_name':'Имя базы', 'months':'Хранить ЖР · месяцев', 'delete':'Удалять ЖР после проверки архива',
 'code':'Код PowerShell', 'names':'ФИО · по одному на строку', 'email_domain':'Почтовый домен',
 'command':'Команда службы', 'arguments':'Аргументы команды · JSON', 'action':'Действие',
 'executable':'Исполняемый файл', 'owner':'Владелец базы', 'its_user':'Логин ИТС', 'its_password':'Пароль ИТС',
 'settings':'Параметры установки', 'folder':'Папка загрузки', 'linux':'Установка на Linux',
}


def moment(value):
    return datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M:%S') if value else '—'


class SchedulerPanel(tk.Frame):
    def __init__(self, parent, app, colors):
        super().__init__(parent, bg=colors['bg'])
        self.app, self.c, self.running = app, colors, False
        local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
        self.store = SchedulerStore(Path(os.environ.get('KA_SCHEDULER_DIR',
                                    str(local / 'KAConsole' / 'scheduler'))) / 'scheduler.sqlite3')
        self._store_lock = threading.Lock()
        self.ready = False
        self.job_id, self.revision, self.steps = None, None, []
        self._poll_job = None
        self._monitor_job = None
        self.active_task = ''
        self._reading = False
        self._journal_key, self._last_event = None, 0
        self._history_signature = None
        self.history_limit = 100
        self._jobs_signature = None
        self.dirty = False
        self._draft_revision = 0
        self._saving = False
        self.configuring_background = False
        self.jobs_by_id = {}
        self.name = tk.StringVar(value='Новая последовательность')
        self.kind = tk.StringVar(value='Один раз')
        initial = datetime.now() + timedelta(hours=1)
        self.date = tk.StringVar(value=initial.strftime('%Y-%m-%d'))
        self.clock = tk.StringVar(value=initial.strftime('%H:%M'))
        self.interval = tk.StringVar(value='60')
        self.days = [tk.BooleanVar(value=True) for _ in range(7)]
        self.enabled = tk.BooleanVar(value=True)
        self.catch_up = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Фоновый исполнитель ещё не настроен')
        self._built = False
        for var in (self.name, self.kind, self.date, self.clock, self.interval, self.enabled, self.catch_up, *self.days):
            var.trace_add('write', lambda *args: self.mark_dirty())
        self.bind('<Destroy>', self.destroyed, add='+')

    def _build(self):
        c, app = self.c, self.app
        bar = tk.Frame(self, bg=c['panel'], padx=10, pady=8)
        bar.pack(fill=tk.X, padx=22, pady=(0, 8))
        app._label(bar, 'Планировщик', 14, bold=True).pack(side=tk.LEFT)
        app._button(bar, 'Фоновый запуск…', self.background_dialog).pack(side=tk.RIGHT)
        app._label(bar, '', 9).pack(side=tk.RIGHT)
        tk.Label(bar, textvariable=self.status, bg=c['panel'], fg=c['muted'], font=('Segoe UI', 9)).pack(side=tk.RIGHT, padx=10)
        footer = tk.Frame(self, bg=c['panel'], padx=10, pady=8)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=22, pady=(0, 10))
        app._button(footer, 'Сохранить расписание', self.save, primary=True).pack(side=tk.RIGHT)
        app._button(footer, 'Запустить сейчас', self.run_now).pack(side=tk.RIGHT, padx=6)
        app._button(footer, 'Прервать запуск', self.cancel_run).pack(side=tk.RIGHT, padx=6)
        self.notice = app._label(footer, 'Шаги выполняются по порядку. При ошибке цепочка останавливается.', 9, 'muted', wraplength=420, justify=tk.LEFT)
        self.notice.pack(side=tk.LEFT, fill=tk.X, expand=True)
        split = tk.PanedWindow(self, orient=tk.VERTICAL, sashwidth=8, bg=c['border'], opaqueresize=False)
        split.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 10))
        top = tk.PanedWindow(split, orient=tk.HORIZONTAL, sashwidth=8, bg=c['border'], opaqueresize=False)
        split.add(top, minsize=190, height=300, stretch='always')
        left = tk.Frame(top, bg=c['panel'], padx=8, pady=8)
        right = tk.Frame(top, bg=c['panel'], padx=10, pady=8)
        top.add(left, minsize=220, width=430, stretch='always')
        top.add(right, minsize=380, width=740, stretch='always')
        buttons = tk.Frame(left, bg=c['panel'])
        buttons.pack(fill=tk.X)
        for title, callback in (('+ Новая', self.new), ('Открыть', self.open_selected), ('Вкл./выкл.', self.toggle), ('Удалить', self.delete)):
            app._button(buttons, title, callback).pack(side=tk.LEFT, padx=(0, 4))
        self.jobs_tree = self.tree(left, [('name','Последовательность',190), ('status','Статус',150),
            ('last','Последний запуск',145), ('next','Следующий запуск',145), ('errors','Ошибок',60), ('duration','Длительность',100)])
        self.jobs_tree.bind('<Double-1>', lambda event: self.open_selected())
        fields = tk.Frame(right, bg=c['panel'])
        fields.pack(fill=tk.X)
        app._entry(fields, self.name, width=28).grid(row=0, column=0, columnspan=3, sticky='ew', padx=(0, 6))
        combo = ttk.Combobox(fields, textvariable=self.kind, values=list(KINDS), state='readonly', width=18)
        combo.grid(row=0, column=3, sticky='ew')
        combo.bind('<MouseWheel>', lambda event: 'break')
        for col, (label, variable, width) in enumerate((('Дата · ГГГГ-ММ-ДД',self.date,12), ('Время · ЧЧ:ММ',self.clock,7), ('Интервал · минуты',self.interval,8))):
            app._label(fields, label, 9, 'muted').grid(row=1,column=col,sticky='w',pady=(6,0))
            app._entry(fields, variable, width=width).grid(row=2,column=col,sticky='ew',padx=(0,6))
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(3, weight=1)
        toggles = tk.Frame(right, bg=c['panel'])
        toggles.pack(fill=tk.X, pady=4)
        for label, variable in (('Включена',self.enabled), ('Запускать пропущенное',self.catch_up)):
            self.check(toggles,label,variable).pack(side=tk.LEFT)
        for label, variable in zip(('Пн','Вт','Ср','Чт','Пт','Сб','Вс'),self.days):
            self.check(toggles,label,variable).pack(side=tk.LEFT)
        actions = tk.Frame(right, bg=c['panel'])
        actions.pack(fill=tk.X, pady=(4,4))
        for title, callback in (('+ Действие…',self.action_menu), ('Параметры…',self.edit_step), ('↑',lambda:self.move(-1)), ('↓',lambda:self.move(1)), ('Убрать',self.remove_step)):
            app._button(actions,title,callback).pack(side=tk.LEFT,padx=(0,4))
        self.steps_tree = self.tree(right,[('order','№',35),('title','Действие',230),('summary','Сервер / база / путь',380)])
        self.steps_tree.bind('<Double-1>',lambda event:self.edit_step())
        bottom = tk.PanedWindow(split, orient=tk.HORIZONTAL, sashwidth=8, bg=c['border'], opaqueresize=False)
        split.add(bottom, minsize=130, height=270, stretch='always')
        history = tk.Frame(bottom, bg=c['panel'], padx=8, pady=8)
        log = tk.Frame(bottom,bg=c['panel'],padx=8,pady=8)
        bottom.add(history,minsize=270,width=540,stretch='always')
        bottom.add(log,minsize=250,width=630,stretch='always')
        history_bar=tk.Frame(history,bg=c['panel']);history_bar.pack(fill=tk.X)
        app._label(history_bar,'История · день → запуск → действия',10,bold=True).pack(side=tk.LEFT)
        app._button(history_bar,'Ещё 100',self.more_history).pack(side=tk.RIGHT)
        self.history_tree = self.tree(history,[('start','Начало',145),('end','Окончание',145),('duration','Длительность',95),('status','Статус',120),('errors','Ошибок',60)],show='tree headings')
        self.history_tree.column('#0',width=230,minwidth=100)
        self.history_tree.heading('#0',text='День / последовательность / шаг')
        self.history_tree.bind('<<TreeviewSelect>>',self.select_journal)
        row=tk.Frame(log,bg=c['panel']);row.pack(fill=tk.X)
        app._label(row,'Журнал операции',10,bold=True).pack(side=tk.LEFT)
        app._button(row,'Результат шага…',self.show_result).pack(side=tk.RIGHT)
        self.output=tk.Text(log,wrap=tk.NONE,width=30,height=5,font=('Consolas',10),state=tk.DISABLED)
        configure_log(self.output)
        vertical=ttk.Scrollbar(log,command=self.output.yview);vertical.pack(side=tk.RIGHT,fill=tk.Y)
        horizontal=ttk.Scrollbar(log,orient=tk.HORIZONTAL,command=self.output.xview);horizontal.pack(side=tk.BOTTOM,fill=tk.X)
        self.output.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.output.pack(fill=tk.BOTH,expand=True)

    def ensure_built(self):
        if not self._built:
            self._build()
            self._built=True

    def tree(self,parent,columns,show='headings'):
        frame=tk.Frame(parent,bg=self.c['panel']);frame.pack(fill=tk.BOTH,expand=True)
        tree=ttk.Treeview(frame,columns=[key for key,title,width in columns],show=show,selectmode='browse',height=4)
        for key,title,width in columns:
            tree.heading(key,text=title);tree.column(key,width=width,minwidth=45,stretch=key in ('name','summary','title'))
        vertical=ttk.Scrollbar(frame,command=tree.yview);vertical.pack(side=tk.RIGHT,fill=tk.Y)
        horizontal=ttk.Scrollbar(frame,orient=tk.HORIZONTAL,command=tree.xview);horizontal.pack(side=tk.BOTTOM,fill=tk.X)
        tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        tree.pack(fill=tk.BOTH,expand=True)
        return tree

    def check(self,parent,title,variable):
        return tk.Checkbutton(parent,text=title,variable=variable,bg=self.c['panel'],fg=self.c['fg'],
                              activebackground=self.c['panel'],font=('Segoe UI',9),bd=0,highlightthickness=0)

    def mark_dirty(self):
        self.dirty=True
        self._draft_revision+=1

    def show_mode(self, mode):
        self.ensure_built()
        self.refresh()

    def set_compact(self, compact):
        if self._built:self.notice.configure(wraplength=230 if compact else 420)

    def destroyed(self,event):
        if event.widget is self and self._poll_job:
            self.after_cancel(self._poll_job);self._poll_job=None
        if event.widget is self and self._monitor_job:
            self.after_cancel(self._monitor_job);self._monitor_job=None

    def start_monitor(self):
        if self._monitor_job is not None:return
        self._monitor_job=self.after(3000,self.monitor)

    def monitor(self):
        self._monitor_job=None
        def read():
            try:
                if self.store.path.is_file():
                    heartbeat,name=self.store.health()
                    active=name if time.time()-heartbeat.get('at',0)<20 else ''
                    self.app._post(self.monitored,active)
            except Exception:
                pass
        threading.Thread(target=read,daemon=True).start()
        self.start_monitor()

    def monitored(self,active):
        if not self.winfo_exists():return
        if active!=self.active_task:
            self.active_task=active;self.app._set_busy()

    def async_call(self, operation, success=None, failure=None):
        def work():
            try:
                result=operation()
                self.app._post(success or (lambda result:None),result)
            except Exception as exc:
                self.app._post(failure or self.error,str(exc))
        threading.Thread(target=work,daemon=True).start()

    def ensure_store(self):
        with self._store_lock:
            if not self.ready:
                self.store.initialize();self.ready=True

    def error(self,text):
        self.notice.configure(text=text,fg=self.c['danger'])
        self.app._panel_error(self,text,title='Планировщик')

    def refresh(self):
        if self._poll_job:
            self.after_cancel(self._poll_job);self._poll_job=None
        if not self.winfo_exists():
            return
        if not self._reading:
            self._reading=True
            key=self._journal_key;after=self._last_event
            def read():
                self.ensure_store()
                jobs=self.store.jobs();runs,steps=self.store.history(self.history_limit)
                heartbeat=self.store.meta('heartbeat') or {}
                rows=self.store.journal(key[0],after,key[1]) if key else []
                return jobs,runs,steps,heartbeat,key,rows
            def done(result):
                self._reading=False
                if not self.winfo_exists():return
                self.render_snapshot(result)
            def work():
                try:self.app._post(done,read())
                except Exception as exc:
                    self.app._post(self.refresh_error,str(exc))
            threading.Thread(target=work,daemon=True).start()
        self._poll_job=self.after(1500,self.refresh_if_visible)

    def refresh_if_visible(self):
        self._poll_job=None
        if self.winfo_manager():self.refresh()

    def refresh_error(self,text):
        self._reading=False;self.status.set('Ошибка чтения расписаний: '+text)

    def render_snapshot(self,result):
        jobs,runs,steps,heartbeat,key,rows=result
        alive=time.time()-heartbeat.get('at',0)<20
        self.status.set(('● Выполняется последовательность' if heartbeat.get('run_id') else '● Фоновый исполнитель работает') if alive else '○ Фоновый исполнитель не работает · настройте фоновый запуск')
        signature=json.dumps(jobs,sort_keys=True)+(str(int(time.time()/5)) if any(row['status']=='Выполняется' for row in jobs) else '')
        if signature!=self._jobs_signature:
            selected=self.jobs_tree.selection()
            self.jobs_by_id={row['id']:row for row in jobs}
            for iid in self.jobs_tree.get_children():
                if iid not in self.jobs_by_id:self.jobs_tree.delete(iid)
            for row in jobs:
                elapsed=(row['last_finished'] or time.time())-row['last_started'] if row['last_started'] else None
                values=(row['name'],row['status'] if row['enabled'] or row['status']=='Выполняется' else row['status']+' · выкл.',
                        moment(row['last_started']),moment(row['next_at']) if row['enabled'] else 'Выключена',row['errors'],duration(elapsed) if elapsed is not None else '—')
                if self.jobs_tree.exists(row['id']):self.jobs_tree.item(row['id'],values=values)
                else:self.jobs_tree.insert('','end',iid=row['id'],values=values)
            if selected and self.jobs_tree.exists(selected[0]):self.jobs_tree.selection_set(selected[0])
            self._jobs_signature=signature
        signature=json.dumps([runs,steps],sort_keys=True)+(str(int(time.time()/5)) if any(row['status']=='Выполняется' for row in runs) else '')
        if signature!=self._history_signature:
            desired=set()
            by_run={}
            for step in steps:by_run.setdefault(step['run_id'],[]).append(step)
            def upsert(parent,iid,text,values,opened=False):
                desired.add(iid)
                if self.history_tree.exists(iid):
                    self.history_tree.item(iid,text=text,values=values)
                else:self.history_tree.insert(parent,'end',iid=iid,text=text,values=values,open=opened)
            days={}
            for run in runs:
                day=datetime.fromtimestamp(run['started']).strftime('%Y-%m-%d')
                days.setdefault(day,[]).append(run)
            for day,entries in days.items():
                errors=sum(row['errors'] for row in entries)
                day_id='day:'+day
                upsert('',day_id,day+f' · {len(entries)} запусков',('','','','',errors),day==datetime.now().strftime('%Y-%m-%d'))
                for run in entries:
                    elapsed=(run['finished'] or time.time())-run['started']
                    upsert(day_id,run['id'],run['name'],(moment(run['started']),moment(run['finished']),duration(elapsed),run['status'],run['errors']))
                    for step in by_run.get(run['id'],[]):
                        elapsed=(step['finished'] or time.time())-step['started'] if step['started'] else 0
                        upsert(run['id'],run['id']+':'+str(step['number']),f"{step['number']}. {step['title']}",
                            (moment(step['started']),moment(step['finished']),duration(elapsed) if step['started'] else '—',step['status'],step['errors']))
            for day_id in self.history_tree.get_children():
                if day_id not in desired:self.history_tree.delete(day_id);continue
                for run_id in self.history_tree.get_children(day_id):
                    if run_id not in desired:self.history_tree.delete(run_id)
            for index,day in enumerate(days):self.history_tree.move('day:'+day,'',index)
            for entries in days.values():
                for index,run in enumerate(entries):self.history_tree.move(run['id'],self.history_tree.parent(run['id']),index)
            self._history_signature=signature
        if key==self._journal_key and rows:
            at_bottom=self.output.yview()[1]>.98
            self.output.configure(state=tk.NORMAL)
            for row in rows:
                insert_log(self.output,datetime.fromtimestamp(row['at']).strftime('%H:%M:%S')+'  '+row['text']+'\n',row['level'])
                self._last_event=row['id']
            # Bound Tcl memory; the complete journal remains on disk and reloadable.
            if int(self.output.index('end-1c').split('.')[0])>12000:self.output.delete('1.0','2000.0')
            self.output.configure(state=tk.DISABLED)
            if at_bottom:self.output.see(tk.END)

    def confirm_discard(self):
        if self._saving:
            self.notice.configure(text='Дождитесь завершения сохранения расписания');return False
        return not self.dirty or messagebox.askyesno('Несохранённые изменения','Отбросить изменения текущей последовательности?',parent=self.app.root,default='no')

    def more_history(self):
        self.history_limit+=100
        self.refresh()

    def new(self):
        if not self.confirm_discard():return
        self.job_id,self.revision,self.steps=None,None,[]
        self.name.set('Новая последовательность');self.enabled.set(True)
        self.date.set((datetime.now()+timedelta(hours=1)).strftime('%Y-%m-%d'))
        self.clock.set((datetime.now()+timedelta(hours=1)).strftime('%H:%M'))
        self.render_steps();self.dirty=False

    def open_selected(self):
        selected=self.jobs_tree.selection()
        if not selected or not self.confirm_discard():return
        def loaded(row):
            self.job_id,self.revision,self.steps=row['id'],row['revision'],row['steps']
            self.name.set(row['name']);self.enabled.set(bool(row['enabled']));self.catch_up.set(bool(row['catch_up']))
            rule=row['rule'];date,clock=rule['start'].split(' ')
            self.date.set(date);self.clock.set(clock);self.kind.set(next(label for label,kind in KINDS.items() if kind==rule['kind']))
            self.interval.set(str(rule.get('minutes',60)))
            for i,var in enumerate(self.days):var.set(i in rule.get('days',range(7)))
            self.render_steps();self.dirty=False
        self.async_call(lambda:self.store.load(selected[0]),loaded)

    def render_steps(self):
        self.ensure_built()
        self.steps_tree.delete(*self.steps_tree.get_children())
        for i,step in enumerate(self.steps):self.steps_tree.insert('','end',iid=str(i),values=(i+1,step['title'],step.get('summary','')))

    def queue_step(self,panel,action=None):
        try:step=capture(panel,action)
        except (ValueError,KeyError,TypeError) as exc:
            self.app._panel_error(panel,str(exc));return
        self.add_step(step)
        self.app._switch_mode('scheduler')
        self.steps_tree.selection_set(str(len(self.steps)-1))
        self.notice.configure(text='Действие добавлено. Задайте время и сохраните последовательность.',fg=self.c['fg'])

    def add_step(self,step):
        self.steps.append(step);self.render_steps();self.mark_dirty()

    def database_action(self,panel,action,role='source'):
        try:
            is_pg=hasattr(panel,'host')
            if is_pg:
                connection=panel.connection()
                if role=='target':connection.update(panel.target_connection())
                names=panel.selected() if role=='source' else [panel.target_tree.set(row,'name') for row in panel.target_tree.selection()]
            else:
                connection=panel.connection()
                names=[panel.selected()] if role=='source' else [panel.target_tree.set(row,'name') for row in panel.target_tree.selection()]
            if action.endswith('create'):
                from database_dialog import ask_create_database
                settings=ask_create_database(self.app.root,'PostgreSQL' if is_pg else 'SQL Server',connection.get('host',connection.get('server','')),
                                             connection.get('username',''),self.c)
                if not settings:return
                parameters=dict(connection=connection,database=settings['name'],owner=settings.get('owner',''))
            else:
                if len(names)!=1 or not names[0]:raise ValueError('Выберите одну базу в нужном списке')
                parameters=dict(connection=connection,database=names[0])
            step=dict(action=action,title=LABELS[action],parameters=parameters)
            from scheduler_actions import bundle_paths
            step['parameters']=bundle_paths(step['parameters'])
            validate_step(step);step['summary']=describe(step)
        except (ValueError,KeyError,TypeError) as exc:self.app._panel_error(panel,str(exc));return
        self.add_step(step);self.app._switch_mode('scheduler')

    def action_menu(self):
        menu=tk.Menu(self,tearoff=False)
        pages=('db_backup','db_restore','db_copy','pg_dump','pg_restore','pg_copy','ibcmd_dump','ibcmd_restore','cluster_infobase','jr_archive','service_create','service_rename','service_delete','accounts','platform_install')
        for page in pages:menu.add_command(label=LABELS[page]+'…',command=lambda page=page:self.configure_action(page))
        menu.add_separator();menu.add_command(label='Своя команда PowerShell…',command=self.command_step)
        menu.tk_popup(self.winfo_pointerx(),self.winfo_pointery())

    def configure_action(self,page):
        self.app._switch_mode(page)
        self.app._status('Настройте действие и нажмите «В планировщик ▾». Оно добавится в текущую последовательность.')

    def command_step(self):
        step=dict(action='command',title='Команда PowerShell',summary='Локально · Windows',parameters={'code':''})
        self.edit_parameters(step,lambda updated:(self.steps.append(updated),self.render_steps(),self.mark_dirty()))

    def edit_step(self):
        selected=self.steps_tree.selection()
        if not selected:return
        index=int(selected[0])
        original=self.steps[index]
        def done(step):
            position=next((i for i,item in enumerate(self.steps) if item is original),None)
            if position is None:
                self.error('Действие уже удалено из последовательности. Изменения не применены.');return
            self.steps[position]=step;self.render_steps();self.mark_dirty()
        self.edit_parameters(deepcopy(self.steps[index]),done)

    def edit_parameters(self,step,done):
        dialog=tk.Toplevel(self);dialog.title('Параметры действия');dialog.geometry('680x600');dialog.transient(self.app.root)
        footer=tk.Frame(dialog);footer.pack(side=tk.BOTTOM,fill=tk.X,padx=12,pady=10)
        self.app._label(dialog,'Параметры сохраняются только для этого шага. Пароли скрыты.',10).pack(anchor=tk.W,padx=12,pady=8)
        canvas=tk.Canvas(dialog,highlightthickness=0);canvas.pack(side=tk.LEFT,fill=tk.BOTH,expand=True)
        scroll=ttk.Scrollbar(dialog,command=canvas.yview);scroll.pack(side=tk.RIGHT,fill=tk.Y);canvas.configure(yscrollcommand=scroll.set)
        form=tk.Frame(canvas);window=canvas.create_window(0,0,window=form,anchor='nw')
        form.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
        values=[]
        title=tk.StringVar(value=step['title']);self.app._entry(form,title).pack(fill=tk.X,padx=12,pady=5)
        def walk(obj,prefix=''):
            for key,value in obj.items():
                if key in ('ssh_host_keys','host_keys','restore_candidates','distribution','remote_prepared'):continue
                path=prefix+FIELD_NAMES.get(key,key.replace('_',' '))
                if isinstance(value,dict):walk(value,path+' / ');continue
                label=path
                self.app._label(form,label,9,'muted').pack(anchor=tk.W,padx=12,pady=(7,2))
                if isinstance(value,bool):
                    var=tk.BooleanVar(value=value);self.check(form,'Да',var).pack(anchor=tk.W,padx=12)
                    values.append((obj,key,var.get));continue
                if isinstance(value,list) or key in ('code','names'):
                    widget=tk.Text(form,height=6 if key=='code' else 3,width=30,undo=True,wrap=tk.WORD)
                    widget.insert('1.0',json.dumps(value,ensure_ascii=False) if isinstance(value,list) else value)
                    widget.pack(fill=tk.X,padx=12)
                    getter=(lambda w=widget:json.loads(w.get('1.0','end-1c'))) if isinstance(value,list) else (lambda w=widget:w.get('1.0','end-1c'))
                    values.append((obj,key,getter));continue
                var=tk.StringVar(value=str(value));secret=any(word in key.lower() for word in ('password','pwd','secret'))
                self.app._entry(form,var,show='•' if secret else None).pack(fill=tk.X,padx=12)
                values.append((obj,key,(lambda v=var:int(v.get())) if type(value)is int else var.get))
        walk(step['parameters'])
        self.app._label(form,'Путь ${previous.backup_path} использует бэкап предыдущего шага.',9,'muted').pack(anchor=tk.W,padx=12,pady=12)
        def accept():
            try:
                for owner,key,getter in values:owner[key]=getter()
                step['title']=title.get().strip();validate_step(step);step['summary']=describe(step)
            except (ValueError,KeyError,TypeError) as exc:messagebox.showerror('Параметры действия',str(exc),parent=dialog);return
            done(step);dialog.destroy()
        self.app._button(footer,'Сохранить действие',accept,primary=True).pack(side=tk.RIGHT)
        self.app._button(footer,'Отмена',dialog.destroy).pack(side=tk.RIGHT,padx=6)

    def move(self,offset):
        selected=self.steps_tree.selection()
        if not selected:return
        index=int(selected[0]);other=index+offset
        if 0<=other<len(self.steps):
            self.steps[index],self.steps[other]=self.steps[other],self.steps[index]
            self.render_steps();self.steps_tree.selection_set(str(other));self.mark_dirty()

    def remove_step(self):
        selected=self.steps_tree.selection()
        if selected:self.steps.pop(int(selected[0]));self.render_steps();self.mark_dirty()

    def save(self, enable_override=None):
        if self._saving:return
        enabled = self.enabled.get() if enable_override is None else enable_override
        try:
            rule=dict(kind=KINDS[self.kind.get()],start=self.date.get().strip()+' '+self.clock.get().strip(),
                      minutes=int(self.interval.get()),days=[i for i,var in enumerate(self.days) if var.get()])
            from scheduler_store import next_time
            following = next_time(rule,time.time(),inclusive=True)
            if enabled and following is None:
                raise ValueError('Указанное время однократного запуска уже прошло: ' + rule['start'] +
                                 '. Выберите будущее время или снимите «Включена», чтобы сохранить без запуска.')
            if not self.steps:raise ValueError('Добавьте действия в последовательность')
            for step in self.steps:validate_step(step)
        except (ValueError,KeyError,TypeError) as exc:self.error(str(exc));return
        changes=[f"{i}. {step['title']} — {step.get('summary','')}" for i,step in enumerate(self.steps,1) if destructive(step)]
        if changes and not messagebox.askyesno('Разрешить действия по расписанию?',
            'Следующие действия могут изменять данные, завершать подключения или удалять объекты:\n\n'+'\n'.join(changes)+
            '\n\nПри каждом запуске они выполнятся без дополнительных вопросов. При ошибке цепочка остановится. Сохранить?',
            parent=self.app.root,icon='warning',default='no'):return
        # Snapshot Tk variables now, never read them from a worker thread.
        name,steps,catch_up,job_id,revision=self.name.get(),deepcopy(self.steps),self.catch_up.get(),self.job_id,self.revision
        self._saving=True;draft_revision=self._draft_revision
        def operation():
            self.ensure_store()
            return self.store.save(name,rule,steps,enabled,job_id,catch_up,revision)
        def saved(key):
            self._saving=False
            self.job_id=key;self.revision=(revision+1) if revision else 1
            cached=self.jobs_by_id.get(key)
            if cached and cached['revision']<=self.revision:
                cached.update(enabled=enabled,revision=self.revision,name=name)
            changed=self._draft_revision!=draft_revision
            if not changed:self.enabled.set(enabled)
            self.dirty=changed
            self.app._clear_panel_error(self)
            self.notice.configure(text='Расписание сохранено. Для работы после выхода из Windows включите фоновый запуск.',fg=self.c['accent'])
            self.refresh()
        def failed(text):
            self._saving=False;self.error(text)
        self.async_call(operation,saved,failed)

    def toggle(self):
        if self._saving:return
        selected=self.jobs_tree.selection()
        if selected:
            row=self.jobs_by_id.get(selected[0])
            if not row:return
            row=dict(row)
            value=not row['enabled']
            if value and self.job_id==row['id'] and self.dirty:
                if messagebox.askyesno('Сохранить изменения и включить?',
                    'Дата, время или действия справа изменены и ещё не сохранены.\n\n'
                    'Сохранить текущие параметры и включить это расписание?',parent=self.app.root,default='no'):
                    self.save(enable_override=True)
                return
            self._saving=True
            draft_revision,dirty=self._draft_revision,self.dirty
            def done(result):
                self._saving=False
                cached=self.jobs_by_id.get(row['id'])
                if cached and cached['revision']<=result['revision']:cached.update(result)
                if self.job_id==row['id'] and self.revision==row['revision']:
                    self.revision=result['revision']
                    if self._draft_revision==draft_revision:
                        self.enabled.set(result['enabled']);self.dirty=dirty
                self.app._clear_panel_error(self)
                self.notice.configure(text='Расписание включено' if value else 'Расписание выключено',fg=self.c['accent'])
                self.refresh()
            def failed(text):
                self._saving=False;self.error(text)
            self.async_call(lambda:self.store.enable(row['id'],value,revision=row['revision']),done,failed)

    def delete(self):
        selected=self.jobs_tree.selection()
        if selected and messagebox.askyesno('Удалить расписание?','История выполненных запусков сохранится.',parent=self.app.root,default='no'):
            self.async_call(lambda:self.store.delete(selected[0]),lambda result:self.refresh())

    def run_now(self):
        selected=self.jobs_tree.selection();key=selected[0] if selected else self.job_id
        if not key:self.error('Сначала сохраните и выберите последовательность');return
        if self.dirty:self.error('Сначала сохраните изменения последовательности');return
        name=self.jobs_by_id.get(key,{}).get('name',self.name.get())
        if not messagebox.askyesno('Запустить последовательность?',name+'\n\nБудут выполнены все сохранённые действия по порядку.',parent=self.app.root,default='no'):return
        def operation():
            control('start',self.store.path)
            self.store.request(key)
        self.async_call(operation,lambda result:self.refresh())

    def selected_run(self):
        selected=self.history_tree.selection()
        if not selected or selected[0].startswith('day:'):return None
        parts=selected[0].split(':');return parts[0],int(parts[1]) if len(parts)>1 else None

    def cancel_run(self):
        run=self.selected_run()
        if not run:self.error('Выберите выполняющийся запуск в истории');return
        if messagebox.askyesno('Прервать последовательность?','Следующие шаги не начнутся. Текущая операция получит запрос отмены; уже сделанные изменения не откатываются автоматически.',parent=self.app.root,default='no'):
            self.async_call(lambda:self.store.cancel(run[0]),lambda result:self.notice.configure(text='Запрошено прерывание последовательности'))

    def select_journal(self,event=None):
        key=self.selected_run()
        if key==self._journal_key:return
        self._journal_key=key;self._last_event=0
        self.output.configure(state=tk.NORMAL);self.output.delete('1.0',tk.END);self.output.configure(state=tk.DISABLED)
        self.refresh()

    def show_result(self):
        key=self.selected_run()
        if not key or key[1] is None:self.error('Выберите конкретный шаг в истории');return
        def display(result):
            dialog=tk.Toplevel(self);dialog.title('Результат действия');dialog.geometry('800x430')
            text=tk.Text(dialog,wrap=tk.NONE,font=('Consolas',10));text.pack(fill=tk.BOTH,expand=True)
            text.insert('1.0',json.dumps(result,ensure_ascii=False,indent=2) if result else 'Результат ещё не получен')
            text.configure(state=tk.DISABLED)
        self.async_call(lambda:self.store.result(*key),display)

    def background_dialog(self):
        if self.configuring_background:return
        dialog=tk.Toplevel(self);dialog.title('Фоновый запуск планировщика');dialog.geometry('640x340');dialog.transient(self.app.root)
        self.app._label(dialog,'Работа после закрытия консоли и выхода из Windows',12,bold=True).pack(anchor=tk.W,padx=16,pady=12)
        description=('Windows будет запускать отдельный исполнитель при включении компьютера. Используется текущая учётная запись Windows; '
            'параметры задач и защищённые пароли останутся доступны. Нужен пароль Windows, не PIN. Пароль передаётся штатному планировщику Windows и не сохраняется в файлах консоли. '
            'Компьютер должен быть включён; при смене пароля фоновый запуск нужно настроить заново.')
        self.app._label(dialog,description,10,'muted',wraplength=600,justify=tk.LEFT).pack(fill=tk.X,padx=16)
        try:user=current_user()
        except OSError as exc:self.error(str(exc));dialog.destroy();return
        self.app._label(dialog,'Учётная запись: '+user,10).pack(anchor=tk.W,padx=16,pady=(12,4))
        password=tk.StringVar();self.app._entry(dialog,password,show='•').pack(fill=tk.X,padx=16,pady=(0,10))
        state=self.app._label(dialog,'',9,'muted',wraplength=600,justify=tk.LEFT);state.pack(fill=tk.X,padx=16)
        actions=tk.Frame(dialog);actions.pack(side=tk.BOTTOM,fill=tk.X,padx=16,pady=12)
        def apply(action):
            value=password.get();password.set('')
            if action=='register' and not value:state.configure(text='Введите пароль Windows');return
            for button in actions.winfo_children():button.configure(state=tk.DISABLED)
            self.configuring_background=True
            state.configure(text='Настройка фонового запуска…')
            def operation():
                self.ensure_store()
                if action=='remove':self.store.meta('stop',True)
                return control(action,self.store.path,value)
            def done(result):
                self.configuring_background=False
                if dialog.winfo_exists():dialog.destroy()
                self.notice.configure(text='Фоновый запуск включён' if action=='register' else 'Фоновый запуск отключён. Активная цепочка завершится штатно.',fg=self.c['accent'])
                self.refresh()
            def work():
                try:self.app._post(done,operation())
                except Exception as exc:self.app._post(failed,str(exc))
            def failed(text):
                self.configuring_background=False
                if dialog.winfo_exists():
                    for button in actions.winfo_children():button.configure(state=tk.NORMAL)
                    state.configure(text=text,fg=self.c['danger'])
            threading.Thread(target=work,daemon=True).start()
        self.app._button(actions,'Включить / обновить',lambda:apply('register'),primary=True).pack(side=tk.RIGHT)
        self.app._button(actions,'Отключить',lambda:apply('remove')).pack(side=tk.LEFT)


def install_schedule_button(panel, scheduler):
    if getattr(panel,'_scheduler_button',None) is not None:return
    mode=getattr(panel,'_page_kind',getattr(panel,'mode',''))
    if mode not in LABELS:return
    parent=getattr(panel,'form',None)
    if parent is None:parent=getattr(panel,'actions',None)
    if parent is None:parent=getattr(panel,'action_bar',None)
    if parent is None:return
    button=panel.app._button(parent,'В планировщик ▾',lambda:menu())
    if hasattr(panel,'form'):
        children=parent.pack_slaves()
        button.pack(fill=tk.X,pady=(0,6),**({'before':children[0]} if children else {}))
    else:button.pack(side=tk.RIGHT,padx=6)
    panel._scheduler_button=button
    def menu():
        action=getattr(panel,'mode',getattr(panel,'_page_kind',mode))
        popup=tk.Menu(button,tearoff=False)
        popup.add_command(label='Добавить текущее действие',command=lambda:scheduler.queue_step(panel,action))
        if action=='cluster_infobase':
            for extra in ('cluster_drop','cluster_jobs'):
                popup.add_command(label=LABELS[extra],command=lambda extra=extra:scheduler.queue_step(panel,extra))
        if action.startswith(('db_','pg_','ibcmd_')):
            prefix='pg_' if hasattr(panel,'host') else 'sql_'
            for side,title in (('source','источника'),('target','получателя')) if hasattr(panel,'target_tree') else (('source',''),):
                popup.add_command(label='Создать базу '+title+'…',command=lambda side=side:scheduler.database_action(panel,prefix+'create',side))
                popup.add_command(label='Удалить выбранную базу '+title+'…',command=lambda side=side:scheduler.database_action(panel,prefix+'delete',side))
        popup.tk_popup(button.winfo_rootx(),button.winfo_rooty()+button.winfo_height())
