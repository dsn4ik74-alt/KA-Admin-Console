"""Software-license inventory and hardware validation in a separate workspace."""
import tkinter as tk
from search_query import matches as matches_query
from tkinter import filedialog, ttk

from administration_ui import JobPanel
from admin_connections_ui import show_help, with_ssh_trust
from ring_license import validate_payload
from ring_license_jobs import operate, probe_windows
from ssh_forms import SshFields
from table_sorting import TableSorter


EXECUTIONS=('Этот компьютер · Windows','Удалённый Windows · WinRM + SMB','Удалённый Linux · SSH')


class RingLicensePanel(SshFields,JobPanel):
    def __init__(self,parent,app,colors):
        super().__init__(parent,app,colors,'Лицензии 1С · ring','Проверить все')
        self.enable_cancellation()
        self.execution=tk.StringVar(value=EXECUTIONS[0])
        app._label(self.form,'Где находятся и используются лицензии',10,'muted').pack(anchor='w',pady=(6,4))
        self.execution_choice=ttk.Combobox(self.form,textvariable=self.execution,values=EXECUTIONS,state='readonly',width=1)
        self.execution_choice.pack(fill=tk.X);self.inputs.append(self.execution_choice)
        self.win_group=tk.Frame(self.form,bg=colors['panel']);saved=self.form;self.form=self.win_group
        self.win_host=tk.StringVar();self.win_user=tk.StringVar();self.win_password=tk.StringVar()
        self.win_port=tk.StringVar(value='5985');self.win_ssl=tk.BooleanVar(value=False)
        self.field('Windows · имя сервера / IP',self.win_host)
        self.field('Логин Windows · ДОМЕН\\пользователь',self.win_user)
        self.field('Пароль Windows',self.win_password,show='•')
        self.field('Порт WinRM',self.win_port);self.check('WinRM по HTTPS · доверенный сертификат',self.win_ssl)
        self.button('Проверить WinRM',self.check_windows)
        self.form=saved
        self.ssh_group=tk.Frame(self.form,bg=colors['panel']);self.form=self.ssh_group
        self.ssh_fields(elevate=True);self.elevate=tk.BooleanVar(value=False)
        self.check('Проверять с правами sudo / root',self.elevate)
        self.form=saved
        self.connection_anchor=tk.Frame(self.form,bg=colors['panel']);self.connection_anchor.pack(fill=tk.X)
        self.path=tk.StringVar()
        self.path_entry=self.field('Каталог лицензий на выбранном компьютере · пусто = стандартные пути 1С',self.path)
        self.browse_button=self.button('Выбрать локальную папку…',self.browse)
        self.button('Получить / обновить список',lambda:self.run_check('list'))
        self.button('Как подключиться к Windows / Linux',lambda:show_help(app))
        self.note('Встроены ring 0.20.0+4, License Tools 0.15.2+8 и отдельная Java. На удалённый сервер передаётся временный комплект; после проверки он удаляется. Для Windows нужны WinRM и SMB (административная общая папка диска), для Linux — SSH / SFTP, x86-64.')
        self.note('Проверяются программные лицензии и их привязка к оборудованию выбранного компьютера. Ring не показывает занятые / свободные места, HASP-ключи или срок подписки ИТС. Пустой список также возможен при недостаточных правах.')
        self.note('Лицензии не изменяются; передача статистики 1С отключена. Для проверки сервера выбирайте сам сервер, а не копируйте его LIC-файлы на компьютер консоли.')
        header=tk.Frame(self.right,bg=colors['panel']);header.grid(row=0,column=0,sticky='ew',pady=(0,8))
        self.count=app._label(header,'Лицензии · 0',12,bold=True);self.count.pack(side=tk.LEFT)
        self.selected_button=app._button(header,'Проверить выбранные',self.check_selected)
        self.selected_button.pack(side=tk.RIGHT);self.inputs.append(self.selected_button)
        search=tk.Frame(self.right,bg=colors['panel']);search.grid(row=1,column=0,sticky='ew',pady=(0,8))
        app._label(search,'Поиск',10,'muted').pack(side=tk.LEFT,padx=(0,8))
        self.filter=tk.StringVar();self.search_entry=app._entry(search,self.filter)
        self.search_entry.pack(side=tk.LEFT,fill=tk.X,expand=True)
        table=self.split_log()
        self.tabs=ttk.Notebook(table);self.tabs.grid(row=0,column=0,sticky='nsew')
        listing=tk.Frame(self.tabs,bg=colors['panel']);listing.rowconfigure(0,weight=1);listing.columnconfigure(0,weight=1)
        self.tabs.add(listing,text='Лицензии')
        columns=(('name','Лицензия',245),('status','Проверка привязки',190),('product','Продукт',220),('owner','Владелец',150))
        self.tree=ttk.Treeview(listing,columns=[c[0] for c in columns],show='headings',selectmode='extended')
        for key,title,width in columns:
            self.tree.heading(key,text=title);self.tree.column(key,width=width,minwidth=140 if key=='name' else 100,stretch=True)
        self.tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(listing,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns');self.tree.configure(yscrollcommand=scroll.set)
        horizontal=ttk.Scrollbar(listing,orient='horizontal',command=self.tree.xview);horizontal.grid(row=1,column=0,sticky='ew');self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.tag_configure('failed',foreground=colors['danger']);self.tree.tag_configure('valid',foreground=colors['success'])
        self.sorter=TableSorter(self.tree,colors=colors)
        detail=tk.Frame(self.tabs,bg=colors['panel']);detail.rowconfigure(0,weight=1);detail.columnconfigure(0,weight=1)
        self.tabs.add(detail,text='Сведения о выбранной')
        self.details=tk.Text(detail,font=('Consolas',10),wrap=tk.WORD,width=20,height=5,bd=0,padx=12,pady=12,
            bg=colors['row'],fg=colors['fg'],state=tk.DISABLED,selectbackground=colors['text_selected'])
        self.details.grid(row=0,column=0,sticky='nsew')
        detail_scroll=ttk.Scrollbar(detail,command=self.details.yview);detail_scroll.grid(row=0,column=1,sticky='ns');self.details.configure(yscrollcommand=detail_scroll.set)
        self.rows={};self.inventory_loaded=False
        self.tree.bind('<<TreeviewSelect>>',lambda event:self.selection_changed())
        self.tree.bind('<Double-1>',lambda event:self.tabs.select(detail) if self.tree.selection() else None)
        self.filter.trace_add('write',lambda *args:self.render())
        self.execution.trace_add('write',lambda *args:self.change_execution())
        for variable in (self.path,self.win_host,self.win_user,self.win_port,self.win_ssl,self.win_password,
                         self.ssh_host,self.ssh_port,self.ssh_user,self.ssh_password,self.ssh_use_key,self.ssh_key,self.sudo_password,self.elevate):
            variable.trace_add('write',lambda *args:self.invalidate())
        self.change_execution()

    def system(self):
        return dict(zip(EXECUTIONS,('local','windows','linux')))[self.execution.get()]

    def secret_values(self):
        return (self.win_password.get(),self.ssh_password.get(),self.sudo_password.get())

    def change_execution(self):
        system=self.system()
        self.win_group.pack_forget();self.ssh_group.pack_forget()
        if system!='local':
            (self.win_group if system=='windows' else self.ssh_group).pack(fill=tk.X,before=self.connection_anchor)
        self.browse_button.configure(state=tk.NORMAL if system=='local' and not self.running else tk.DISABLED)
        self.invalidate()

    def invalidate(self):
        if self.running:return
        self.rows.clear();self.inventory_loaded=False;self.render();self.selection_changed()

    def browse(self):
        if self.running or self.system()!='local':return
        path=filedialog.askdirectory(parent=self.app.root,title='Каталог программных лицензий 1С')
        if path:self.path.set(path)

    def connection(self):
        if self.system()=='linux':return self.ssh_values()
        return dict(remote=self.system()=='windows',host=self.win_host.get().strip(),user=self.win_user.get().strip(),
            password=self.win_password.get(),port=self.win_port.get().strip(),ssl=self.win_ssl.get())

    def check_windows(self):
        if self.running:return
        connection=self.connection()
        try:
            from ring_license_jobs import validate_windows
            connection=validate_windows(connection)
        except ValueError as exc:
            self.app._panel_error(self,str(exc));return
        def ready(result):
            self.append('Windows: '+result['computer']+' · '+result['user']+' · '+result['description'])
            self.finish('Подключение Windows проверено')
        if self.start():self.work(lambda event:probe_windows(connection,event),ready)

    def execute(self,event=None):
        self.run_check('check')

    def check_selected(self):
        names=[self.rows[i]['name'] for i in self.tree.selection() if i in self.rows]
        if not names:
            self.app._panel_error(self,'Выберите одну или несколько лицензий в таблице.');return
        self.run_check('check',names)

    def run_check(self,action,names=None):
        if self.running:return
        if action=='check' and names is None:
            if self.rows:names=list(self.rows)
            elif self.inventory_loaded:
                self.app._panel_error(self,'Загруженный список лицензий пуст. При необходимости нажмите «Получить / обновить список».');return
        cache={name:dict(code=row['info_code'],text=row['info']) for name in (names or [])
               if (row:=self.rows.get(name)) is not None}
        try:
            system=self.system();connection=self.connection()
            values=validate_payload(dict(action=action,path=self.path.get(),names=names or [],info_cache=cache))
            if system=='windows':
                from ring_license_jobs import validate_windows
                validate_windows(connection)
            if system=='linux':
                from linux_platform import ssh_settings
                connection=ssh_settings(connection)
        except (ValueError,KeyError) as exc:
            self.app._panel_error(self,str(exc));return
        if not names:self.rows.clear();self.inventory_loaded=False;self.render()
        elevate=self.elevate.get() if system=='linux' else False
        def ready(result):
            for row in result['rows']:self.rows[row['name']]=row
            if not names:self.inventory_loaded=True
            self.render();self.selection_changed()
            if system=='linux':self.app.saved_servers.remember_ssh(self,connection)
            failed=sum(row.get('validation_code') not in (None,0) or row.get('info_code',0)!=0 for row in result['rows'])
            self.finish(('Проверено' if result['checked'] else 'Найдено')+': '+str(len(result['rows']))+
                ('. Требуют внимания: '+str(failed) if failed else '.'))
            if not result['rows']:
                self.append('Лицензии не найдены. Проверьте каталог и права учётной записи выбранного компьютера.','warning')
        key=(system,values['path'],connection.get('host',''),str(connection.get('port','')),connection.get('user',''),elevate)
        def retry():
            current=self.connection()
            current_key=(self.system(),self.path.get().strip(),current.get('host',''),str(current.get('port','')),current.get('user',''),
                         self.elevate.get() if self.system()=='linux' else False)
            if current_key!=key:
                self.app._panel_error(self,'Параметры сервера изменились. Запустите проверку лицензий заново.');return
            self.run_check(action,names)
        with_ssh_trust(self,lambda event:operate(system,values,connection,event,self.cancel_event,elevate),ready,retry,
            history=dict(operation='Проверка лицензий 1С' if action=='check' else 'Список лицензий 1С',
                server=connection.get('host','') if system!='local' else 'Этот компьютер',path=values['path'],executor=system))

    def events(self,events):
        regular=[];changed=False
        for item in events:
            if item.get('kind')=='license_row':
                row=item['row'];self.rows[row['name']]=row;changed=True
            else:regular.append(item)
        super().events(regular)
        if changed:self.render()

    def render(self):
        selected=set(self.tree.selection());self.tree.delete(*self.tree.get_children())
        query=self.filter.get().casefold().strip()
        visible={}
        for name,row in self.rows.items():
            if not matches_query([row.get(c,'') for c in self.tree['columns']],query):continue
            tag='valid' if row.get('validation_code')==0 else 'failed' if row.get('validation_code') is not None or row.get('info_code') else ''
            self.tree.insert('',tk.END,iid=name,values=[row.get(c,'—') for c in self.tree['columns']],tags=(tag,));visible[name]=row
        self.sorter.apply(visible)
        self.tree.selection_set([name for name in selected if name in visible])
        self.count.configure(text='Лицензии · '+str(len(visible))+' / '+str(len(self.rows)))
        self.selected_button.configure(state=tk.NORMAL if self.tree.selection() and not self.running else tk.DISABLED)

    def selection_changed(self):
        names=self.tree.selection();row=self.rows.get(names[0]) if names else None
        text=('Лицензия: '+row['name']+'\n'+row['status']+'\nПроверка: '+row.get('checked_at','')+
            '\n\nСВЕДЕНИЯ RING\n'+row['info']+'\n\nПРОВЕРКА ПРИВЯЗКИ\n'+(row['validation'] or 'Ещё не выполнялась.')) if row else 'Выберите лицензию в таблице.'
        self.details.configure(state=tk.NORMAL);self.details.delete('1.0',tk.END);self.details.insert('1.0',text);self.details.configure(state=tk.DISABLED)
        self.selected_button.configure(state=tk.NORMAL if row and not self.running else tk.DISABLED)

    def finish(self,text='',error=''):
        super().finish(text,error)
        self.execution_choice.configure(state='readonly')
        self.browse_button.configure(state=tk.NORMAL if self.system()=='local' else tk.DISABLED)
        self.selection_changed()

    def clear_results(self):
        if self.running:return
        super().clear_results();self.rows.clear();self.inventory_loaded=False;self.render();self.selection_changed()
