"""Download and install an official 1C PostgreSQL build into a chosen instance."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog,messagebox,ttk

from administration_ui import JobPanel
from favorite_secrets import protect,unprotect
from postgres_install_catalog import PostgresPortal
from ssh_forms import SshFields
from table_sorting import TableSorter


class PostgresInstallPanel(SshFields,JobPanel):
    def __init__(self,parent,app,colors):
        super().__init__(parent,app,colors,'Установка PostgreSQL из ИТС','Установить PostgreSQL')
        self.enable_cancellation();self.portal=None;self.releases=[];self.distributions=[];self.loaded_release=None
        self.username=tk.StringVar();self.password=tk.StringVar();self.remember=tk.BooleanVar(value=True)
        self.field('Логин ИТС',self.username);self.field('Пароль ИТС',self.password,show='•')
        self.check('Сохранять вход с защитой Windows',self.remember);self.button('Войти / обновить версии',self.load_catalog)
        self.execution=tk.StringVar(value='Локально · Windows')
        self.mode_choice=self.choice('Где установить PostgreSQL',self.execution,('Локально · Windows','SSH · Ubuntu / Debian'))
        self.ssh_form=tk.Frame(self.form,bg=colors['panel']);saved=self.form;self.form=self.ssh_form;self.ssh_fields();self.form=saved
        self.data=tk.StringVar(value='C:\\PostgreSQL\\data_5432');self.prefix=tk.StringVar(value='C:\\Program Files\\PostgreSQL\\1C')
        self.instance=tk.StringVar(value='ka5432');self.port=tk.StringVar(value='5432');self.pg_password=tk.StringVar()
        self.locale=tk.StringVar(value='Russian_Russia.1251');self.listen=tk.StringVar(value='*');self.networks=tk.StringVar(value='127.0.0.1/32, ::1/128')
        self.field('Каталог программы · для DEB задаётся пакетами',self.prefix)
        self.ssh_anchor=self.form.pack_slaves()[-2]
        self.field('Каталог данных экземпляра · выбирается до установки',self.data)
        self.button('Выбрать каталог данных…',self.choose_data)
        self.field('Имя экземпляра / службы',self.instance)
        self.service_note=app._label(self.form,'Linux: к введённому имени добавляется только .service. Файл создаётся в /etc/systemd/system/.',
                                   9,'muted',wraplength=280,justify=tk.LEFT,anchor=tk.W)
        self.service_note.pack(fill=tk.X,pady=(3,0));self.field('Порт PostgreSQL',self.port)
        self.field('Пароль администратора postgres',self.pg_password,show='•');self.field('Локаль экземпляра',self.locale)
        self.listen_choice=self.choice('Адреса прослушивания',self.listen,('*','localhost','127.0.0.1'))
        self.field('Разрешённые сети · через запятую',self.networks)
        self.note('Для подключения сервера 1С добавьте его подсеть. Существующие каталоги данных и экземпляры не заменяются.')
        self.folder=tk.StringVar(value=str(Path.home()/'Downloads'/'1C'));self.field('Временная папка дистрибутива',self.folder)
        self.note('После установки архив и распакованные временные файлы удаляются на компьютере консоли и на ВМ. Рабочий экземпляр PostgreSQL остаётся в выбранном каталоге.')
        frame=self.split_log();self.tabs=ttk.Notebook(frame);self.tabs.grid(row=0,column=0,sticky='nsew')
        self.trees={};self.searches={};self.sorters={};self.search_entries={};self.empty_labels={};self.count_labels={}
        for key,title,columns,choose in (
            ('releases','Версии',(('version','Версия PostgreSQL 1С',200),('date','Дата',140)),self.choose_release),
            ('distributions','Дистрибутивы',(('title','Дистрибутив',430),('architecture','Архитектура',100),('format','Поставка',90)),self.choose_distribution)):
            page=tk.Frame(self.tabs,bg=colors['panel']);self.tabs.add(page,text=title);page.columnconfigure(0,weight=1);page.rowconfigure(1,weight=1)
            toolbar=tk.Frame(page,bg=colors['panel']);toolbar.grid(row=0,column=0,sticky='ew',pady=(0,6));toolbar.columnconfigure(0,weight=1)
            query=self.searches[key]=tk.StringVar();entry=app._entry(toolbar,query);entry.grid(row=0,column=0,sticky='ew');self.search_entries[key]=entry
            self.count_labels[key]=app._label(toolbar,'',9,'muted')
            self.count_labels[key].grid(row=0,column=1,padx=(6,0))
            pick=app._button(toolbar,'Выбрать',choose);pick.grid(row=0,column=2,padx=(6,0));self.inputs.append(pick)
            if key=='distributions':
                refresh=app._button(toolbar,'Обновить',self.refresh_distributions);refresh.grid(row=0,column=3,padx=(6,0));self.inputs.append(refresh)
            tree=ttk.Treeview(page,columns=[c[0] for c in columns],show='headings',selectmode='browse');tree.grid(row=1,column=0,sticky='nsew')
            for name,label,width in columns:tree.heading(name,text=label);tree.column(name,width=width,minwidth=85)
            scrollbar=ttk.Scrollbar(page,command=tree.yview);scrollbar.grid(row=1,column=1,sticky='ns');tree.configure(yscrollcommand=scrollbar.set)
            horizontal=ttk.Scrollbar(page,orient='horizontal',command=tree.xview);horizontal.grid(row=2,column=0,sticky='ew');tree.configure(xscrollcommand=horizontal.set)
            self.trees[key]=tree;self.sorters[key]=TableSorter(tree,colors=colors)
            self.empty_labels[key]=tk.Label(tree,text='',font=('Segoe UI',10),bg=colors['panel'],fg=colors['muted'],wraplength=440,justify=tk.CENTER)
            tree.bind('<Double-1>',choose);query.trace_add('write',lambda *args,k=key:self.render(k))
            tree.bind('<Button-1>',lambda event:'break' if self.running else None)
        self.trees['distributions'].bind('<<TreeviewSelect>>',self.choose_distribution)
        self.note_label=app._label(self.right,'Выберите версию и дистрибутив. Один щелчок не запускает установку.',10,'muted',anchor=tk.W,wraplength=600)
        self.note_label.grid(row=1,column=0,sticky='ew',pady=(6,0))
        self._mode='windows';self._drafts={};self._auto_prefix={'windows':self.prefix.get(),'linux':'/opt/postgresql/1c'}
        self.execution.trace_add('write',self.mode_changed)
        self.tabs.bind('<<NotebookTabChanged>>',lambda event:self.update_actions())
        try:
            saved=unprotect(app._settings['its_credentials']) if app._settings.get('its_credentials') else {}
            self.username.set(saved.get('username',''));self.password.set(saved.get('password',''))
        except ValueError:pass
        self.render('releases');self.render('distributions')

    def is_linux(self):return self.execution.get().startswith('SSH')

    def choice(self,title,variable,values):
        self.app._label(self.form,title,10,'muted',anchor=tk.W).pack(fill=tk.X,pady=(8,4))
        combo=ttk.Combobox(self.form,textvariable=variable,values=values,state='readonly',width=1,font=('Segoe UI',10))
        combo.pack(fill=tk.X);self.inputs.append(combo);return combo

    def finish(self,text='',error=''):
        super().finish(text,error)
        for combo in (self.mode_choice,self.listen_choice):combo.configure(state='readonly')
        self.update_actions()

    def mode_changed(self,*args):
        mode='linux' if self.is_linux() else 'windows'
        if mode==self._mode:return
        self._drafts[self._mode]=dict(data=self.data.get(),prefix=self.prefix.get(),locale=self.locale.get())
        self._mode=mode
        if self.is_linux():
            self.ssh_form.pack(fill=tk.X,before=self.ssh_anchor)
            draft=self._drafts.get(mode,dict(data='/var/lib/postgresql/ka5432',prefix='/opt/postgresql/1c',locale='ru_RU.UTF-8'))
        else:
            self.ssh_form.pack_forget();draft=self._drafts.get(mode,dict(data='C:\\PostgreSQL\\data_5432',prefix='C:\\Program Files\\PostgreSQL\\1C',locale='Russian_Russia.1251'))
        for key,value in draft.items():getattr(self,key).set(value)
        self.render('distributions')

    def render(self,key):
        tree=self.trees[key];tree.delete(*tree.get_children());rows={};query=self.searches[key].get().casefold().strip()
        available=0
        for i,row in enumerate(getattr(self,key)):
            if key=='distributions' and (row['os']=='windows')==self.is_linux():continue
            available+=1
            aliases=' Ubuntu Debian Linux DEB' if row.get('os')=='linux-deb' else ''
            if query and query not in (' '.join(str(v) for v in row.values())+aliases).casefold():continue
            values=dict(row)
            if key=='distributions':values['format']='MSI' if row['os']=='windows' else 'Бинарный архив' if row['format']=='binary' else 'DEB'
            tree.insert('',tk.END,iid=str(i),values=[values.get(c,'') for c in tree['columns']]);rows[str(i)]=row
        self.sorters[key].apply(rows)
        self.count_labels[key].configure(text=str(len(rows))+' из '+str(available))
        label=self.empty_labels[key]
        if rows:label.place_forget()
        else:
            label.configure(text=self.empty_text(key,query));label.place(relx=.5,rely=.5,anchor=tk.CENTER)
        if key=='distributions':
            version=self.loaded_release['version'] if self.loaded_release else ''
            self.note_label.configure(text=('PostgreSQL '+version+' · ' if version else '')+
                ('SSH · Ubuntu / Debian' if self.is_linux() else 'Локально · Windows')+
                ' · дистрибутивов: '+str(available)+'. Один щелчок не запускает установку.')
        self.update_actions()

    def empty_text(self,key,query=''):
        if query:return 'По этому запросу ничего не найдено. Очистите строку поиска.'
        if not self.portal:return 'Введите логин и пароль ИТС и нажмите «Войти / обновить версии».'
        if key=='releases':return 'Версии ещё не загружены. Нажмите «Войти / обновить версии».'
        if not self.loaded_release:return 'На вкладке «Версии» выделите версию и нажмите «Выбрать» или дважды щёлкните по ней.'
        return 'Для PostgreSQL '+self.loaded_release['version']+' на ИТС нет дистрибутива под '+('Ubuntu / Debian' if self.is_linux() else 'Windows')+'. Выберите другую версию или режим установки.'

    def update_actions(self):
        ready=not self.running and self.portal is not None and bool(self.selected('distributions'))
        self.execute_button.configure(state=tk.NORMAL if ready else tk.DISABLED)

    def selected(self,key):
        selected=self.trees[key].selection()
        return dict(getattr(self,key)[int(selected[0])]) if selected else None

    def load_catalog(self):
        if self.running:return
        username,password,remember=self.username.get().strip(),self.password.get(),self.remember.get()
        def operation(event):
            portal=PostgresPortal(self.cancel_event);portal.login(username,password)
            return portal,portal.postgres_releases(),protect(dict(username=username,password=password)) if remember else None
        def loaded(result):
            self.portal,self.releases,credentials=result;self.distributions=[];self.loaded_release=None
            if credentials:self.app._settings['its_credentials']=credentials
            else:self.app._settings.pop('its_credentials',None)
            self.app._save_settings();self.render('releases');self.render('distributions');self.tabs.select(0)
            self.finish('Доступных релизов PostgreSQL: '+str(len(self.releases)))
        if self.start():self.work(operation,loaded)

    def choose_release(self,event=None):
        row=self.selected('releases')
        self.load_distributions(row)

    def refresh_distributions(self):
        self.load_distributions(self.loaded_release or self.selected('releases'))

    def load_distributions(self,row):
        if self.running or not row or not self.portal:return
        portal=self.portal;release=dict(row)
        def loaded(rows):
            self.distributions=rows;self.loaded_release=release;self.searches['distributions'].set('')
            self.render('distributions');self.tabs.select(1)
            count=len(self.trees['distributions'].get_children())
            self.finish('PostgreSQL '+release['version']+': дистрибутивов для выбранного режима — '+str(count)+'. '+
                ('Выберите дистрибутив для установки.' if count else self.empty_text('distributions')))
        if self.start():
            self.append('Получение дистрибутивов PostgreSQL '+release['version'])
            self.work(lambda emit:portal.postgres_distributions(release),loaded)

    def choose_distribution(self,event=None):
        row=self.selected('distributions')
        self.update_actions()
        if row and not self.running:
            self.note_label.configure(text=row['title']+' · версия '+row['version']+' · '+('бинарный архив' if row['format']=='binary' else 'пакеты / установщик'))
            if self.prefix.get()==self._auto_prefix[self._mode]:
                value=('/opt/postgresql/1c/' if self.is_linux() else 'C:\\Program Files\\PostgreSQL\\')+row['version']
                self.prefix.set(value);self._auto_prefix[self._mode]=value

    def choose_data(self):
        if self.running:return
        if self.is_linux():self.app._panel_error(self,'Введите путь каталога данных на Linux. Проводник Windows относится к компьютеру консоли.');return
        path=filedialog.askdirectory(parent=self.app.root,title='Каталог данных нового экземпляра PostgreSQL')
        if path:self.data.set(path)

    def execute(self,event=None):
        if self.running:return
        from postgres_install import validate,install
        row=self.selected('distributions')
        try:
            if not self.portal or not row:raise ValueError(self.empty_text('distributions') if not self.trees['distributions'].get_children() else 'Выделите нужный дистрибутив в таблице перед установкой.')
            values=validate(dict(data=self.data.get().strip(),prefix=self.prefix.get().strip(),name=self.instance.get().strip(),port=self.port.get(),
                password=self.pg_password.get(),locale=self.locale.get().strip(),listen=self.listen.get(),networks=self.networks.get()),linux=self.is_linux())
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        service='\nСлужба: '+values['name']+'.service\nФайл: /etc/systemd/system/'+values['name']+'.service' if self.is_linux() else ''
        if not messagebox.askyesno('Установить PostgreSQL?',row['title']+'\n\nЭкземпляр: '+values['name']+'\nДанные: '+values['data']+
            '\nПорт: '+str(values['port'])+service+'\n\nБудут установлены PostgreSQL и необходимые зависимости, создана служба.',parent=self.app.root,default='no'):return
        portal=self.portal;connection=self.ssh_values() if self.is_linux() else None;folder=self.folder.get().strip()
        def installed(result):
            if connection:self.app.saved_servers.remember_ssh(self,connection)
            self.finish('PostgreSQL установлен: '+result['data']+' · порт '+str(result['port']))
        if self.start():self.work(lambda emit:install(portal,row,values,folder,emit,self.cancel_event,connection),
            installed,
            history=dict(operation='Установка PostgreSQL',server=connection['host'] if connection else 'Windows',path=values['data'],database=values['name']))
