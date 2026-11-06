"""Publication, sites, IIS pools/bindings and versioned server configuration."""
import tkinter as tk
from tkinter import messagebox, ttk

from administration_ui import JobPanel
from admin_connections_ui import show_help, with_ssh_trust
from ssh_forms import SshFields
from web_publication import descriptor, operate
from infobase_address import infobase_address
from console_core import version_key


def prompt(app, title, fields):
    window=tk.Toplevel(app.root);window.title(title);window.configure(bg=app.colors['panel'])
    window.transient(app.root);window.resizable(False,False)
    frame=tk.Frame(window,bg=app.colors['panel'],padx=18,pady=14);frame.pack(fill=tk.BOTH,expand=True)
    variables={};result={}
    for name,label,value in fields:
        app._label(frame,label,10,'muted').pack(anchor='w',pady=(5,3))
        variable=variables[name]=tk.StringVar(value=value)
        app._entry(frame,variable,width=54).pack(fill=tk.X)
    def accept():
        result.update({name:variable.get().strip() for name,variable in variables.items()});window.destroy()
    app._button(frame,'Применить',accept,primary=True).pack(side=tk.RIGHT,pady=(12,0))
    app._button(frame,'Отмена',window.destroy).pack(side=tk.RIGHT,padx=8,pady=(12,0))
    window.grab_set();app.root.wait_window(window)
    return result or None


class WebPublicationPanel(SshFields,JobPanel):
    def __init__(self,parent,app,colors):
        super().__init__(parent,app,colors,'Публикация ИБ · IIS / Apache','Опубликовать ИБ')
        self.enable_cancellation()
        self.system=tk.StringVar(value='Windows');self.web_server=tk.StringVar(value='IIS')
        self.where=tk.StringVar(value='Этот компьютер')
        self.combos=[]
        for title,variable,choices in (('ОС веб-сервера',self.system,('Windows','Linux')),
                ('Веб-сервер',self.web_server,('IIS','Apache 2.4')),('Выполнение',self.where,('Этот компьютер','Удалённый сервер'))):
            app._label(self.form,title,10,'muted').pack(anchor='w',pady=(7,3))
            widget=ttk.Combobox(self.form,textvariable=variable,values=choices,state='readonly',width=1)
            widget.pack(fill=tk.X);self.inputs.append(widget);self.combos.append(widget)
        self.windows_group=tk.Frame(self.form,bg=colors['panel'])
        saved=self.form;self.form=self.windows_group
        self.remote_host=tk.StringVar();self.remote_user=tk.StringVar();self.remote_password=tk.StringVar()
        self.winrm_port=tk.StringVar(value='5985');self.winrm_ssl=tk.BooleanVar(value=False)
        self.field('Windows · адрес / IP WinRM',self.remote_host);self.field('Windows · логин',self.remote_user)
        self.field('Windows · пароль',self.remote_password,show='•');self.field('Порт WinRM',self.winrm_port)
        self.check('WinRM по HTTPS · доверенный сертификат',self.winrm_ssl)
        self.form=saved
        self.linux_group=tk.Frame(self.form,bg=colors['panel']);self.form=self.linux_group
        self.ssh_fields(elevate=True);self.form=saved
        self.connection_anchor=tk.Frame(self.form,bg=colors['panel']);self.connection_anchor.pack(fill=tk.X)
        self.button('Как включить WinRM / SMB / SSH',lambda:show_help(app))
        self.button('Подключиться / обновить сайты',self.inspect)
        self.button('Установить IIS / Apache на сервер',lambda:self.action('install'))
        self.button('Перезапустить службу · все сайты',lambda:self.action('server_restart'))
        self.site=tk.StringVar();self.platform=tk.StringVar();self.platform_choice=tk.StringVar()
        app._label(self.form,'Сайт / VirtualHost',10,'muted').pack(anchor='w',pady=(9,3))
        self.site_widget=ttk.Combobox(self.form,textvariable=self.site,state='readonly',width=1)
        self.site_widget.pack(fill=tk.X);self.inputs.append(self.site_widget)
        app._label(self.form,'Платформа на веб-сервере',10,'muted').pack(anchor='w',pady=(9,3))
        self.platform_widget=ttk.Combobox(self.form,textvariable=self.platform_choice,state='readonly',width=1)
        self.platform_widget.pack(fill=tk.X);self.inputs.append(self.platform_widget)
        self.field('Каталог bin платформы · можно указать вручную',self.platform)
        self.alias=tk.StringVar(value='demo');self.connection=tk.StringVar()
        self.directory=tk.StringVar(value=r'C:\inetpub\wwwroot\demo');self.pool=tk.StringVar()
        self.field('Имя публикации в URL',self.alias)
        self.field('Строка ИБ · Srvr="сервер:1541";Ref="база";',self.connection)
        self.button('Выбрать ИБ из списка консоли…',self.choose_infobase)
        self.field('Каталог публикации на веб-сервере',self.directory)
        self.field('Пул IIS · пусто = отдельный пул для версии',self.pool)
        self.main_conf=tk.StringVar(value=r'C:\Apache24\conf\httpd.conf')
        self.apache_tool=tk.StringVar(value=r'C:\Apache24\bin\httpd.exe')
        self.apache_service=tk.StringVar(value='Apache2.4')
        self.field('Основной конфиг Apache · проверка перед применением',self.main_conf)
        self.field('Apache Windows · httpd.exe на веб-сервере',self.apache_tool)
        self.field('Apache Windows · имя службы',self.apache_service)
        self.web,self.services,self.http,self.odata=[tk.BooleanVar(value=v) for v in (True,True,True,False)]
        self.replace,self.os_auth=tk.BooleanVar(value=False),tk.BooleanVar(value=False)
        for title,variable in (('Веб-клиент',self.web),('Web-сервисы конфигурации',self.services),
                ('HTTP-сервисы конфигурации',self.http),('Стандартный OData',self.odata),
                ('Аутентификация Windows · IIS',self.os_auth),('Обновить существующую публикацию',self.replace)):
            self.check(title,variable)
        self.note('Версия модуля веб-сервера должна совпадать с платформой ИБ, разрядность — с IIS/Apache. '
                  'Перед изменением файлов создаётся резервная копия. Снятие публикации не удаляет базу и её файлы. '
                  'Редактор applicationHost.config даёт расширенные параметры IIS; отдельные функции MMC не повторяются кнопками.')
        table=self.split_log();self.tabs=ttk.Notebook(table);self.tabs.grid(row=0,column=0,sticky='nsew')
        self.trees={};self.rows={};self.catalog={};self.loaded_config=None;self.context=None;self.selected_certificate=''
        definitions=(('sites','Сайты',('name','state','bindings','path')),
                     ('publications','Публикации',('name','site','path','pool')),
                     ('pools','Пулы IIS',('name','state','runtime','pipeline')),
                     ('bindings','Привязки IIS',('site','protocol','binding')),
                     ('certificates','Сертификаты IIS',('subject','expires','thumbprint')))
        labels={'name':'Имя','state':'Состояние','bindings':'Привязки','path':'Каталог','site':'Сайт',
                'pool':'Пул','runtime':'.NET','pipeline':'Режим','protocol':'Протокол','binding':'IP : порт : имя',
                'subject':'Сертификат','expires':'Действует до','thumbprint':'Отпечаток'}
        for key,title,columns in definitions:
            page=tk.Frame(self.tabs,bg=colors['panel']);self.tabs.add(page,text=title)
            bar=tk.Frame(page,bg=colors['panel']);bar.pack(fill=tk.X,pady=(0,7))
            actions=({'sites':(('Создать','site_create'),('Запустить','site_start'),('Остановить','site_stop'),('Удалить','site_delete')),
                      'publications':(('Выбрать','select_publication'),('Снять публикацию','unpublish')),
                      'pools':(('Создать','pool_create'),('Запустить','pool_start'),('Остановить','pool_stop'),('Перезапустить','pool_recycle'),('Удалить','pool_delete')),
                      'bindings':(('Добавить','binding_add'),('Удалить','binding_delete')),'certificates':()}[key])
            bar.columnconfigure((0,1,2),weight=1)
            for index,(title,action) in enumerate(actions):
                b=app._button(bar,title,lambda a=action:self.action(a));b.configure(font=('Segoe UI',9),padx=6,pady=4)
                b.grid(row=index//3,column=index%3,sticky='ew',padx=(0,4),pady=2);self.inputs.append(b)
            frame=tk.Frame(page);frame.pack(fill=tk.BOTH,expand=True);frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
            tree=self.trees[key]=ttk.Treeview(frame,columns=columns,show='headings',selectmode='browse')
            for column in columns:tree.heading(column,text=labels[column]);tree.column(column,width=170,minwidth=70)
            tree.grid(row=0,column=0,sticky='nsew')
            scroll=ttk.Scrollbar(frame,command=tree.yview);scroll.grid(row=0,column=1,sticky='ns')
            horizontal=ttk.Scrollbar(frame,orient=tk.HORIZONTAL,command=tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
            tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
            tree.bind('<<TreeviewSelect>>',lambda event,k=key:self.select_row(k))
        page=tk.Frame(self.tabs,bg=colors['panel']);self.tabs.add(page,text='Конфигурации')
        bar=tk.Frame(page,bg=colors['panel']);bar.pack(fill=tk.X)
        self.config_kind=tk.StringVar(value='default.vrd')
        choice=ttk.Combobox(bar,textvariable=self.config_kind,state='readonly',width=24,
                            values=('default.vrd','web.config','Конфиг сайта Apache','Основной конфиг Apache','applicationHost.config IIS'))
        bar.columnconfigure(0,weight=1)
        choice.configure(width=16);choice.grid(row=0,column=0,sticky='ew');self.inputs.append(choice);self.config_choice=choice
        for column,(title,command) in enumerate((('Прочитать',self.read_config),('Сохранить',self.save_config)),1):
            button=app._button(bar,title,command);button.configure(font=('Segoe UI',9),padx=6)
            button.grid(row=0,column=column,padx=(4,0));self.inputs.append(button)
        generate=app._button(bar,'Создать VRD из формы',self.generate_vrd)
        generate.grid(row=1,column=0,columnspan=3,sticky='ew',pady=(5,0));self.inputs.append(generate)
        self.config_path=tk.StringVar()
        path=app._entry(page,self.config_path);path.pack(fill=tk.X,pady=6);self.inputs.append(path)
        self.editor=tk.Text(page,wrap=tk.NONE,font=('Consolas',10),undo=True,bg=colors['panel'],fg=colors['fg'])
        scroll=ttk.Scrollbar(page,command=self.editor.yview);scroll.pack(side=tk.RIGHT,fill=tk.Y)
        self.editor.configure(yscrollcommand=scroll.set);self.editor.pack(fill=tk.BOTH,expand=True);self.inputs.append(self.editor)
        self.editor.bind('<<Modified>>',self.editor_changed)
        self._editor_vrd=False
        self.config_kind.trace_add('write',self.config_kind_changed)
        self.site.trace_add('write',lambda *args:self.site_selected())
        self.platform_choice.trace_add('write',lambda *args:self.platform_selected())
        for variable in (self.system,self.web_server,self.where):variable.trace_add('write',self.mode_changed)
        for variable in (self.remote_host,self.remote_user,self.winrm_port,self.winrm_ssl,self.ssh_host,self.ssh_port,self.ssh_user,self.ssh_use_key,self.ssh_key):
            variable.trace_add('write',self.mode_changed)
        self.mode_changed()

    def secret_values(self):return [self.remote_password.get(),self.ssh_password.get(),self.sudo_password.get()]

    def mode_changed(self,*args):
        if self.running:return
        linux=self.system.get()=='Linux'
        if linux and self.web_server.get()!='Apache 2.4':self.web_server.set('Apache 2.4')
        if linux and self.where.get()!='Удалённый сервер':self.where.set('Удалённый сервер')
        self.combos[1].configure(values=('Apache 2.4',) if linux else ('IIS','Apache 2.4'),state='readonly')
        self.combos[2].configure(state=tk.DISABLED if linux else 'readonly')
        self.windows_group.pack_forget();self.linux_group.pack_forget()
        if linux:self.linux_group.pack(fill=tk.X,before=self.connection_anchor)
        elif self.where.get()=='Удалённый сервер':self.windows_group.pack(fill=tk.X,before=self.connection_anchor)
        if linux and self.directory.get().startswith('C:'):self.directory.set('/var/www/1c/'+self.alias.get())
        if linux and self.main_conf.get().startswith('C:'):self.main_conf.set('/etc/apache2/apache2.conf')
        if not linux and self.directory.get().startswith('/'):self.directory.set('C:\\inetpub\\wwwroot\\'+self.alias.get())
        if not linux and self.main_conf.get().startswith('/'):self.main_conf.set('C:\\Apache24\\conf\\httpd.conf')
        if self.context is not None and self.context!=self.context_key():
            self.context=None;self.catalog={};self.loaded_config=None;self.site.set('');self.render()
            self.site_widget.configure(values=());self.platform_widget.configure(values=())
            self.platform_choice.set('');self.platform.set('')

    def finish(self,text='',error=''):
        super().finish(text,error)
        for combo in (*self.combos,self.site_widget,self.platform_widget,self.config_choice):combo.configure(state='readonly')
        self.mode_changed()

    def base_payload(self):
        site=next((row for row in self.catalog.get('sites',[]) if self.site.get()==self.site_caption(row)),{})
        return dict(os='linux' if self.system.get()=='Linux' else 'windows',server='iis' if self.web_server.get()=='IIS' else 'apache',
                    site=site.get('id',''),site_etag=site.get('etag',''),site_number=site.get('number'),alias=self.alias.get().strip(),connection=self.connection.get().strip(),
                    directory=self.directory.get().strip(),platform=self.platform.get().strip(),pool=self.pool.get().strip(),
                    main_conf=self.main_conf.get().strip(),apache_tool=self.apache_tool.get().strip(),apache_service=self.apache_service.get().strip(),
                    replace=self.replace.get(),os_auth=self.os_auth.get())

    def connection_values(self):
        if self.system.get()=='Linux':return self.ssh_values()
        return dict(remote=self.where.get()=='Удалённый сервер',host=self.remote_host.get().strip(),user=self.remote_user.get().strip(),
                    password=self.remote_password.get(),port=self.winrm_port.get(),ssl=self.winrm_ssl.get())

    def perform(self,payload,success,history=True):
        if self.running:return
        try:connection=self.connection_values()
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        key=self.context_key()
        if payload['action'] in ('publish','unpublish','site_start','site_stop','site_delete','binding_add','binding_delete','pool_create','pool_start','pool_stop','pool_recycle','pool_delete') and self.context!=key:
            self.app._panel_error(self,'Сначала подключитесь и обновите список сайтов для выбранного сервера.');return
        def retry():
            if self.context_key()!=key:self.app._panel_error(self,'Параметры подключения изменились. Запустите действие заново.');return
            self.perform(payload,success,history)
        with_ssh_trust(self,lambda event:operate(payload,connection,event,self.cancel_event),success,
                      retry,history=dict(operation='Веб-сервер · '+payload['action'],
                      server=connection.get('host') or 'Этот компьютер',database=payload.get('alias',''),path=payload.get('directory',''),executor=payload['os']) if history else None)

    def inspect(self):
        p=self.base_payload();p['action']='inspect'
        self.perform(p,self.inspected,False)

    @staticmethod
    def site_caption(row):return row['name']+' · '+row.get('bindings','')

    def inspected(self,result):
        result['platforms']=sorted(result.get('platforms',[]),key=lambda row:version_key(row['version']))
        self.catalog=result;self.context=self.context_key()
        if self.system.get()=='Linux':self.app.saved_servers.remember_ssh(self,self.ssh_values())
        self.site_widget.configure(values=[self.site_caption(row) for row in result.get('sites',[])])
        self.platform_widget.configure(values=[row['version']+' · '+row['bin'] for row in result.get('platforms',[])])
        if result.get('sites'):self.site.set(self.site_caption(result['sites'][0]))
        if result.get('platforms'):self.platform_choice.set(result['platforms'][-1]['version']+' · '+result['platforms'][-1]['bin'])
        self.render();self.finish('Подключено. Выберите сайт и параметры публикации.')

    def render(self):
        selected=next((r for r in self.catalog.get('sites',[]) if self.site.get()==self.site_caption(r)),{})
        for key,tree in self.trees.items():
            tree.delete(*tree.get_children())
            rows=self.catalog.get(key,[])
            if key in ('publications','bindings') and selected:rows=[r for r in rows if r.get('site')==selected['id']]
            self.rows[key]=rows
            for i,row in enumerate(rows):tree.insert('',tk.END,iid=str(i),values=[row.get(col,'') for col in tree['columns']])

    def site_selected(self):
        if hasattr(self,'trees'):self.render()

    def platform_selected(self):
        for row in self.catalog.get('platforms',[]):
            if self.platform_choice.get()==row['version']+' · '+row['bin']:self.platform.set(row['bin'])

    def selected(self,key):
        selection=self.trees[key].selection()
        return self.rows[key][int(selection[0])] if selection else None

    def select_row(self,key):
        if self.running:return
        row=self.selected(key)
        if not row:return
        if key=='sites':self.site.set(self.site_caption(row))
        elif key=='pools':self.pool.set(row['name'])
        elif key=='certificates':self.selected_certificate=row['thumbprint'];self.state_label.configure(text='Сертификат выбран для HTTPS')

    def context_key(self):
        return tuple(variable.get() for variable in (self.system,self.web_server,self.where,self.remote_host,self.remote_user,
            self.winrm_port,self.winrm_ssl,self.ssh_host,self.ssh_port,self.ssh_user,self.ssh_use_key,self.ssh_key))

    def config_kind_changed(self,*args):
        self.config_path.set('');self.loaded_config=None;self._editor_vrd=False

    def select_publication(self):
        row=self.selected('publications')
        if row:
            self.alias.set(row['name']);self.directory.set(row['path']);self.pool.set(row.get('pool',''));self.replace.set(True)

    def editor_changed(self,event=None):
        if self.editor.edit_modified():
            self._editor_vrd=self.config_kind.get()=='default.vrd'
            self.editor.edit_modified(False)

    def generate_vrd(self):
        try:text=descriptor(self.alias.get().strip(),self.connection.get().strip(),self.web.get(),self.services.get(),self.http.get(),self.odata.get())
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        self.config_kind.set('default.vrd');self.editor.delete('1.0',tk.END);self.editor.insert('1.0',text);self._editor_vrd=True

    def execute(self):
        if self.running:return
        try:
            p=self.base_payload();p['action']='publish'
            p['vrd']=self.editor.get('1.0','end-1c') if self._editor_vrd and self.config_kind.get()=='default.vrd' else descriptor(
                p['alias'],p['connection'],self.web.get(),self.services.get(),self.http.get(),self.odata.get())
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        if not messagebox.askyesno('Опубликовать ИБ?',self.site.get()+'\n/'+p['alias']+'\n'+p['directory']+
                                  '\n\nКонфигурация веб-сервера будет изменена.'+('\nСуществующая публикация будет обновлена.' if p['replace'] else ''),parent=self.app.root,default='no'):return
        self.perform(p,self.completed)

    def completed(self,result):self.finish(result.get('text','Выполнено.')+' Обновите список сайтов при необходимости.')

    def action(self,action):
        if self.running:return
        if action=='select_publication':self.select_publication();return
        p=self.base_payload();p['action']=action
        if action=='unpublish':self.select_publication();p=self.base_payload();p['action']=action
        if action=='site_create':
            values=prompt(self.app,'Создать сайт',(('site_name','Имя сайта',''),('hostname','Доменное имя · необязательно для IIS',''),
                           ('port','Порт HTTP','80'),('site_path','Каталог на веб-сервере','/var/www/site' if p['os']=='linux' else r'C:\inetpub\site')))
            if not values:return
            p.update(values)
        if action=='pool_create':
            values=prompt(self.app,'Создать пул IIS',(('pool','Имя пула','KA-1C'),))
            if not values:return
            p.update(values)
        if action in ('binding_add','binding_delete'):
            row=self.selected('bindings') if action=='binding_delete' else None
            old_ip,old_port,old_host='*','80',''
            if row:
                try:old_ip,old_port,old_host=row['binding'].rsplit(':',2)
                except ValueError:pass
            values=prompt(self.app,'Привязка IIS',(('protocol','http или https',(row or {}).get('protocol','http')),
                    ('ip','IP · * = все',old_ip),('port','Порт',old_port),('hostname','Имя хоста · SNI для HTTPS',old_host),
                    ('certificate','Сертификат · выберите во вкладке «Сертификаты IIS»',self.selected_certificate)))
            if not values:return
            p.update(values)
        if not messagebox.askyesno('Изменение веб-сервера',action+'\n'+(p.get('site_name') or self.site.get())+'\n'+p.get('pool','')+
                                   '\n\nПрименить изменение?',parent=self.app.root,default='no'):return
        self.perform(p,self.completed)

    def read_config(self):
        p=self.base_payload();p['action']='read_config'
        kind={'default.vrd':'vrd','web.config':'web','Конфиг сайта Apache':'apache','Основной конфиг Apache':'apache','applicationHost.config IIS':'iis'}[self.config_kind.get()]
        p['config_kind']=kind
        site=next((r for r in self.catalog.get('sites',[]) if self.site.get()==self.site_caption(r)),{})
        sep='/' if p['os']=='linux' else '\\'
        default=(p['directory'].rstrip('/\\')+sep+('default.vrd' if kind=='vrd' else 'web.config'))
        if self.config_kind.get()=='Конфиг сайта Apache':default=site.get('config_path','')
        if self.config_kind.get()=='Основной конфиг Apache':default=p['main_conf']
        p['config_path']=self.config_path.get().strip() or default
        def done(result):
            self.loaded_config=dict(kind=kind,etag=result['etag'],path=result['path'],context=self.connection_values())
            self.config_path.set(result['path']);self.editor.delete('1.0',tk.END);self.editor.insert('1.0',result['text'])
            self._editor_vrd=kind=='vrd';self.finish('Файл прочитан. Изменения применяются кнопкой «Сохранить».')
        self.perform(p,done,False)

    def save_config(self):
        if not self.loaded_config:self.app._panel_error(self,'Сначала прочитайте файл с сервера');return
        if self.loaded_config['context']!=self.connection_values():self.app._panel_error(self,'Подключение изменилось. Прочитайте файл заново.');return
        if not messagebox.askyesno('Сохранить конфигурацию?',self.loaded_config['path']+'\n\nБудет создана резервная копия.',parent=self.app.root,default='no'):return
        p=self.base_payload();p.update(action='save_config',config_kind=self.loaded_config['kind'],config_path=self.loaded_config['path'],
                                     etag=self.loaded_config['etag'],text=self.editor.get('1.0','end-1c'))
        def done(result):self.loaded_config=None;self.completed(result)
        self.perform(p,done)

    def choose_infobase(self):
        window=tk.Toplevel(self.app.root);window.title('Выбрать ИБ');window.geometry('850x450')
        search=tk.StringVar();entry=self.app._entry(window,search);entry.pack(fill=tk.X,padx=12,pady=10)
        tree=ttk.Treeview(window,columns=('name','address'),show='headings',selectmode='browse')
        tree.heading('name',text='База');tree.heading('address',text='Адрес');tree.column('name',width=230);tree.column('address',width=570)
        tree.pack(fill=tk.BOTH,expand=True,padx=12)
        rows={}
        def render(*args):
            tree.delete(*tree.get_children());rows.clear()
            for row in self.app._all_bases:
                address=infobase_address(dict(host=row.get('cluster_host'),port=row.get('cluster_port')),row.get('base',''),row.get('server',''))
                if address and search.get().casefold() in (row['base']+' '+address).casefold():
                    key=str(len(rows));rows[key]=row,address;tree.insert('',tk.END,iid=key,values=(row['base'],address))
        def choose(event=None):
            if tree.selection():
                row,address=rows[tree.selection()[0]];self.connection.set(address);self.alias.set(row['base'])
                self.directory.set(('/var/www/1c/' if self.system.get()=='Linux' else 'C:\\inetpub\\wwwroot\\')+row['base'])
                version=row.get('version') or next((item.get('version','') for item in self.app.all_items
                    if item.get('server','').casefold()==row.get('server','').casefold() and str(item.get('port'))==str(row.get('port'))),'')
                if version:
                    platform=next((item for item in self.catalog.get('platforms',[]) if item['version']==version),None)
                    if platform:self.platform_choice.set(platform['version']+' · '+platform['bin'])
                    else:
                        self.platform.set('');self.platform_choice.set('')
                        self.app._panel_error(self,'На веб-сервере нужен модуль платформы '+version+'. Установите его или укажите правильный bin.')
                window.destroy()
        search.trace_add('write',render);tree.bind('<Double-1>',choose)
        self.app._button(window,'Выбрать',choose,primary=True).pack(anchor='e',padx=12,pady=10);render()
