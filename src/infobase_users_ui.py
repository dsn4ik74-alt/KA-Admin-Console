"""User and access-group administration across explicitly selected 1C bases."""
from copy import deepcopy
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

from administration_ui import JobPanel
from configuration_install import designer_for_version, one_c_command
from infobase_users import batch_create, operate, target_from_row, validate_change, validate_target
from search_query import matches
from table_sorting import TableSorter
from postgres_runner import OperationCancelled
from ui_controls import ActionFlow


class InfobaseUsersPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Создать пользователя в базе', 'Создать пользователя…')
        self.enable_cancellation()
        self.user, self.password = tk.StringVar(), tk.StringVar()
        self.address, self.version = tk.StringVar(), tk.StringVar()
        self.targets, self.snapshots, self.user_rows = {}, {}, {}
        self._credentials = None
        self._connected_signature = None
        self.field('Администратор информационных баз 1С', self.user)
        self.field('Пароль администратора ИБ', self.password, show='•')
        self.button('Подключить выбранные базы / обновить', self.connect)
        self.note('Это пользователь самой информационной базы с правами администрирования пользователей. '
                  'Пароль СУБД и администратор кластера здесь не подходят. Пароли в этой вкладке не сохраняются.')
        self.note('Базы добавьте из загруженного списка ИБ либо по адресу ниже. Для разных учётных данных '
                  'откройте отдельную вкладку. Требуется внешний COM-соединитель версии базы на этом компьютере Windows.')
        self.field('Адрес базы · Srvr="сервер:1541";Ref="имя";', self.address)
        self.field('Версия платформы · для ручного адреса', self.version)
        self.button('Добавить адрес в список', self.add_manual)
        self.platform_consent = tk.BooleanVar(value=False)
        self.check('Разрешить работу с ролями платформы, если нет поддерживаемого БСП', self.platform_consent)
        self.note('В базах БСП используются карточки пользователей и группы доступа. При иной структуре '
                  'конфигурации предлагается Конфигуратор. Роли платформы не заменяют прикладные профили прав.')
        head=tk.Frame(self.right,bg=colors['panel']);head.grid(row=0,column=0,sticky='ew',pady=(0,8))
        self.load_button=app._button(head,'Базы из вкладки ИБ',self.load_cached)
        self.load_button.pack(side=tk.LEFT)
        self.connect_button=app._button(head,'Подключить / обновить',self.connect)
        self.connect_button.pack(side=tk.LEFT,padx=6)
        self.edit_button=app._button(head,'Изменить…',self.edit_selected);self.edit_button.pack(side=tk.LEFT)
        self.delete_button=app._button(head,'Удалить…',self.delete_selected);self.delete_button.pack(side=tk.LEFT,padx=6)
        self.designer_button=app._button(head,'Конфигуратор',self.open_designer);self.designer_button.pack(side=tk.RIGHT)
        self._header_flow=ActionFlow(head)
        self.inputs.extend((self.load_button,self.connect_button,self.edit_button,self.delete_button,self.designer_button))
        search=tk.Frame(self.right,bg=colors['panel']);search.grid(row=1,column=0,sticky='ew',pady=(0,8))
        self.filter=tk.StringVar();self.search_entry=app._entry(search,self.filter)
        self.search_entry.pack(side=tk.LEFT,fill=tk.X,expand=True)
        self.count=app._label(search,'Баз: 0 · пользователей: 0',9,'muted');self.count.pack(side=tk.RIGHT,padx=8)
        table=self.split_log()
        self.tabs=ttk.Notebook(table);self.tabs.grid(row=0,column=0,sticky='nsew')
        self.base_tree=self.make_tree('Базы',(('name','База',180),('address','Адрес кластера',350),
            ('mode','Механизм пользователей',250),('status','Результат подключения',230)))
        self.users_tree=self.make_tree('Пользователи',(('name','Логин',170),('full_name','Полное имя',240),
            ('base','База',200),('rights','Назначенные права',340)))
        self.rights_tree=self.make_tree('Доступные права',(('name','Группа / роль',260),('base','База',200),('kind','Механизм',170)))
        self.base_tree.bind('<Double-1>',lambda event:self.show_base_users())
        self.users_tree.bind('<Double-1>',lambda event:self.edit_selected())
        self.users_tree.bind('<Delete>',lambda event:self.delete_selected())
        self.filter.trace_add('write',lambda *args:self.render())
        for tree in (self.base_tree,self.users_tree,self.rights_tree):
            tree.bind('<Control-a>',lambda event,w=tree:self.select_all(w))
            tree.bind('<Control-A>',lambda event,w=tree:self.select_all(w))
        self.load_cached(quiet=True)

    def button(self,title,command):
        widget=self.app._button(self.form,title,command)
        widget.pack(fill=tk.X,pady=(8,0));self.inputs.append(widget)
        return widget

    def make_tree(self,title,columns):
        frame=tk.Frame(self.tabs,bg=self.c['panel']);frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.tabs.add(frame,text=title)
        tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show='headings',selectmode='extended')
        for key,label,width in columns:
            tree.heading(key,text=label);tree.column(key,width=width,minwidth=90,stretch=True,anchor='center')
        tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(frame,command=tree.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        tree.tag_configure('error',foreground=self.c['danger']);tree.tag_configure('ok',foreground=self.c['success'])
        TableSorter(tree,colors=self.c)
        return tree

    def select_all(self,tree):tree.selection_set(tree.get_children());return 'break'

    def load_cached(self,quiet=False):
        if self.running:return
        added=0
        for row in getattr(self.app,'_all_bases',[]):
            try:target=validate_target(target_from_row(row))
            except ValueError:continue
            key=target['address'].casefold()
            if key not in self.targets:self.targets[key]=target;added+=1
        self.render()
        if not quiet:
            self.tabs.select(0)
            self.app._panel_status(self,'Добавлено баз: '+str(added)+'. Выделите нужные строки и подключитесь.')
            if not self.targets:self.app._panel_error(self,'Сначала получите список на вкладке «ИБ» либо добавьте адрес вручную.')

    def add_manual(self):
        if self.running:return
        try:target=validate_target(dict(address=self.address.get(),version=self.version.get()))
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        key=target['address'].casefold();self.targets[key]=target
        self.render();self.tabs.select(0);self.base_tree.selection_set(key);self.base_tree.see(key)
        self.app._clear_panel_error(self)

    def signature(self):return (self.user.get(),self.password.get())

    def selected_targets(self):
        return [key for key in self.base_tree.selection() if key in self.targets]

    def connect(self):
        if self.running:return
        keys=self.selected_targets()
        if not keys:
            self.tabs.select(0);self.app._panel_error(self,'Выделите одну или несколько баз. Ctrl / Shift — выбрать несколько.',self.base_tree);return
        credentials=dict(user=self.user.get(),password=self.password.get());signature=self.signature()
        targets=[(key,dict(self.targets[key])) for key in keys];versions=dict(self.app.rac_map)
        runner=operate
        if not self.start():return
        def operation(event):
            output={}
            for key,target in targets:
                if self.cancel_event.is_set():break
                event(dict(kind='stage',text='Подключение: '+target['name']))
                try:
                    output[key]=runner(target,credentials,'inspect',event,versions=versions)
                    event(dict(kind='message',text=target['name']+': '+output[key].get('configuration','')+' · '+
                        output[key].get('version','')+'\n'+output[key]['reason']))
                except OperationCancelled:raise
                except Exception as exc:
                    text=str(exc)
                    if credentials['password']:text=text.replace(credentials['password'],'[скрыто]')
                    output[key]={'error':text};event(dict(kind='error',text=target['name']+': '+text))
            return output
        def success(output):
            # Old connections must not survive a change of credentials.
            if self._connected_signature != signature:self.snapshots.clear()
            self._credentials=credentials;self._connected_signature=signature
            self.snapshots.update(output);self.render()
            failed=sum('error' in value for value in output.values())
            self.finish('Подключено баз: '+str(len(output)-failed),str(failed)+' баз не подключены. Подробности в журнале.' if failed else '')
            if not failed:self.tabs.select(1)
        self.work(operation,success)

    def render(self):
        query=self.filter.get()
        selections={tree:tree.selection() for tree in (self.base_tree,self.users_tree,self.rights_tree)}
        for tree in selections:
            children=tree.get_children()
            if children:tree.delete(*children)
        self.user_rows={}
        total=0
        for key,target in self.targets.items():
            snapshot=self.snapshots.get(key,{})
            mechanism='Группы доступа · БСП' if snapshot.get('mode')=='bsp' else 'Роли платформы' if snapshot.get('mode') else 'Не определён'
            status=snapshot.get('error') or ('Подключено' if snapshot.get('mode') else 'Не подключено')
            if matches([target,mechanism,status],query):
                self.base_tree.insert('',tk.END,iid=key,values=(target['name'],target['address'],mechanism,status),
                    tags=('error',) if snapshot.get('error') else ('ok',) if snapshot else ())
            groups={g['id']:g['name'] for g in snapshot.get('groups',[])}
            for user in snapshot.get('users',[]):
                total+=1;iid=key+'|'+user['id'];rights=', '.join(groups.get(g,g) for g in user.get('groups',[])) if snapshot.get('mode')=='bsp' else ', '.join(user.get('roles',[]))
                self.user_rows[iid]=(key,user)
                if matches([target['name'],user['name'],user['full_name'],rights],query):
                    self.users_tree.insert('',tk.END,iid=iid,values=(user['name'],user['full_name'],target['name'],rights))
            rights=snapshot.get('groups',[]) if snapshot.get('mode')=='bsp' else snapshot.get('roles',[])
            for i,right in enumerate(rights):
                title=right['name']+((' · '+right['title']) if right.get('title') else '')
                if matches([title,target['name'],mechanism],query):
                    self.rights_tree.insert('',tk.END,iid=key+'|'+str(i),values=(title,target['name'],mechanism))
        for tree,selected in selections.items():tree.selection_set([iid for iid in selected if tree.exists(iid)])
        self.count.configure(text='Баз: '+str(len(self.targets))+' · пользователей: '+str(total))

    def show_base_users(self):
        selected=self.selected_targets()
        if selected:self.tabs.select(1)

    def require_snapshot(self,keys):
        if self._connected_signature != self.signature():raise ValueError('Параметры входа изменились. Подключитесь к базам повторно.')
        for key in keys:
            if key not in self.snapshots or self.snapshots[key].get('error'):raise ValueError('Сначала подключите выбранные базы.')
            mode=self.snapshots[key]['mode']
            if mode=='platform' and not self.platform_consent.get():
                raise ValueError(self.snapshots[key]['reason']+' Для работы с ролями платформы установите соответствующий флажок в параметрах.')

    def execute(self):
        if self.running:return
        keys=self.selected_targets()
        try:
            if not keys:raise ValueError('Выделите базы на вкладке «Базы».')
            self.require_snapshot(keys)
            if len({self.snapshots[key]['mode'] for key in keys})!=1:raise ValueError('Выберите базы с одинаковым механизмом прав.')
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        UserEditor(self,keys)

    def selected_user(self):
        selected=self.users_tree.selection()
        if len(selected)!=1 or selected[0] not in self.user_rows:raise ValueError('Выберите одного пользователя на вкладке «Пользователи».')
        key,user=self.user_rows[selected[0]];self.require_snapshot([key])
        return key,dict(user)

    def edit_selected(self):
        if self.running:return
        try:key,user=self.selected_user()
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        UserEditor(self,[key],user)

    def delete_selected(self):
        if self.running:return
        try:key,user=self.selected_user()
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        mode=self.snapshots[key]['mode']
        text='Удалить пользователя '+user['name']+' из базы '+self.targets[key]['name']+'?'
        if mode=='bsp':text+='\nКарточка будет помечена на удаление, вход и участие в группах доступа удалены.'
        if not messagebox.askyesno('Удаление пользователя 1С',text,parent=self.app.root,default=messagebox.NO):return
        self.submit([key],dict(action='delete',mode=mode,id=user['id'],etag=user['etag']))

    def submit(self,keys,change):
        try:
            self.require_snapshot(keys);change=validate_change(change)
        except ValueError as exc:self.app._panel_error(self,str(exc));return False
        credentials=dict(self._credentials);versions=dict(self.app.rac_map)
        plans=[dict(target=dict(self.targets[key]),mode=self.snapshots[key]['mode']) for key in keys]
        runner=operate;batch=batch_create
        if not self.start():return False
        def operation(event):
            if change['action']=='create':return batch(plans,credentials,change,event,versions=versions,runner=runner)
            return runner(plans[0]['target'],credentials,'change',event,versions=versions,change=change,expected_mode=change['mode'])
        def success(result):
            self.finish('Изменения выполнены. Обновляется список пользователей.')
            self._refresh_after_change=keys
            self.refresh_keys(keys)
        self.work(operation,success,dict(operation='Пользователи 1С · '+change['action'],server='; '.join(p['target']['address'] for p in plans),executor='Windows'))
        return True

    def refresh_keys(self,keys):
        self.filter.set('');self.render();self.base_tree.selection_set([key for key in keys if self.base_tree.exists(key)]);self.connect()

    def open_designer(self):
        if self.running:return
        selected=self.users_tree.selection() if self.tabs.index(self.tabs.select())==1 else ()
        keys=[self.user_rows[selected[0]][0]] if len(selected)==1 and selected[0] in self.user_rows else self.selected_targets()
        if len(keys)!=1:self.app._panel_error(self,'Выберите одну базу для открытия Конфигуратора.');return
        target=dict(self.targets[keys[0]]);versions=dict(self.app.rac_map)
        if not self.start():return
        def operation(event):return designer_for_version(target['version'],versions=versions)
        def success(path):
            try:
                subprocess.Popen(one_c_command([path,'DESIGNER','/IBConnectionString',target['address']]),
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
                self.finish('Конфигуратор открыт. Вход — через стандартное окно 1С.')
            except Exception as exc:self.finish('',str(exc))
        self.work(operation,success)


class UserEditor(tk.Toplevel):
    def __init__(self,panel,keys,user=None):
        super().__init__(panel.app.root)
        self.panel,self.keys,self.user=panel,list(keys),deepcopy(user)
        self.title('Изменить пользователя 1С' if user else 'Создать пользователя 1С')
        self.transient(panel.app.root);self.configure(bg=panel.c['panel']);self.geometry('720x680');self.minsize(540,440)
        self.signature=panel._connected_signature;self.snapshots={key:deepcopy(panel.snapshots[key]) for key in keys}
        self.mode=self.snapshots[keys[0]]['mode'];self.values={}
        self.checked=set(user.get('groups' if self.mode=='bsp' else 'roles',[])) if user else set()
        first=self.snapshots[keys[0]];rights=first['groups'] if self.mode=='bsp' else first['roles']
        if not user and len(keys)>1:
            # Group IDs belong to individual databases; compare unique names.
            common={right['name'] for right in rights}
            for snapshot in self.snapshots.values():
                available=snapshot['groups'] if self.mode=='bsp' else snapshot['roles']
                names=[right['name'] for right in available]
                common.intersection_update(name for name in names if names.count(name)==1)
            rights=[right for right in rights if right['name'] in common]
        self.rights={r['id'] if self.mode=='bsp' else r['name']:r for r in rights}
        body=tk.Frame(self,bg=panel.c['panel'],padx=18,pady=12);body.pack(fill=tk.BOTH,expand=True)
        actions=tk.Frame(body,bg=panel.c['panel']);actions.pack(side=tk.BOTTOM,fill=tk.X,pady=(6,0))
        panel.app._button(actions,'Изменить выбор',self.toggle).pack(side=tk.LEFT)
        panel.app._button(actions,'Отмена',self.destroy).pack(side=tk.RIGHT)
        panel.app._button(actions,'Сохранить' if user else 'Создать',self.save,primary=True).pack(side=tk.RIGHT,padx=8)
        self._action_flow=ActionFlow(actions)
        self.error=panel.app._label(body,'',9,'danger',anchor='w',justify=tk.LEFT,wraplength=650)
        self.error.pack(side=tk.BOTTOM,fill=tk.X,pady=5)
        panel.app._label(body,'Баз: '+str(len(keys))+' · '+('Группы доступа БСП' if self.mode=='bsp' else 'Роли платформы'),11,bold=True).pack(anchor='w',pady=(0,8))
        fields=tk.Frame(body,bg=panel.c['panel']);fields.pack(fill=tk.X);fields.columnconfigure(1,weight=1)
        for i,(name,title,default) in enumerate((('name','Логин',''),('full_name','Полное имя',''),('password','Новый пароль',''),('os_user','Пользователь ОС',''))):
            panel.app._label(fields,title,10,'muted').grid(row=i,column=0,sticky='w',padx=(0,12),pady=4)
            var=tk.StringVar(value=(user or {}).get(name,default));self.values[name]=var
            entry=panel.app._entry(fields,var,show='•' if name=='password' else None);entry.grid(row=i,column=1,sticky='ew',pady=4)
        self.set_password=tk.BooleanVar(value=not user)
        self.standard=tk.BooleanVar(value=(user or {}).get('standard_auth',True))
        self.os_auth=tk.BooleanVar(value=(user or {}).get('os_auth',False))
        self.show=tk.BooleanVar(value=(user or {}).get('show_in_list',True))
        for title,var in (('Задать новый пароль (иначе текущий сохраняется)',self.set_password),('Аутентификация 1С',self.standard),
                          ('Аутентификация ОС',self.os_auth),('Показывать в списке выбора',self.show)):
            tk.Checkbutton(body,text=title,variable=var,bg=panel.c['panel'],fg=panel.c['fg'],
                activebackground=panel.c['panel'],activeforeground=panel.c['accent'],
                selectcolor=panel.c['bg'],font=('Segoe UI',10),bd=0,highlightthickness=0).pack(anchor='w',pady=2)
        panel.app._label(body,'Права · двойной щелчок или пробел — включить / выключить',10,'muted').pack(anchor='w',pady=(10,5))
        self.query=tk.StringVar();self.search=panel.app._entry(body,self.query);self.search.pack(fill=tk.X,pady=(0,5))
        container=tk.Frame(body,bg=panel.c['panel']);container.pack(fill=tk.BOTH,expand=True);container.columnconfigure(0,weight=1);container.rowconfigure(0,weight=1)
        self.tree=ttk.Treeview(container,columns=('chosen','name'),show='headings',selectmode='extended')
        self.tree.heading('chosen',text='Выбрано');self.tree.column('chosen',width=75,minwidth=70,stretch=False,anchor='center')
        self.tree.heading('name',text='Группа доступа' if self.mode=='bsp' else 'Роль платформы');self.tree.column('name',width=400,minwidth=100)
        self.tree.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(container,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns');self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<Double-1>',self.toggle);self.tree.bind('<space>',self.toggle)
        self.tree.bind('<Control-a>',lambda event:panel.select_all(self.tree));self.tree.bind('<Control-A>',lambda event:panel.select_all(self.tree))
        self.query.trace_add('write',lambda *args:self.render());self.render()
        self.bind('<Escape>',lambda event:self.destroy())

    def render(self):
        selected=self.tree.selection();children=self.tree.get_children()
        if children:self.tree.delete(*children)
        for key,right in self.rights.items():
            title=right['name']+((' · '+right['title']) if right.get('title') else '')
            if matches(title,self.query.get()):self.tree.insert('',tk.END,iid=key,values=('✓' if key in self.checked else '',title))
        self.tree.selection_set([key for key in selected if self.tree.exists(key)])

    def toggle(self,event=None):
        if event is not None and getattr(event,'type',None)==tk.EventType.ButtonPress:
            key=self.tree.identify_row(event.y)
            if not key:return 'break'
            selected=[key]
        else:selected=self.tree.selection()
        for key in selected:
            if key in self.checked:self.checked.remove(key)
            else:self.checked.add(key)
        self.render();return 'break'

    def save(self):
        panel=self.panel
        if panel.running:self.error.configure(text='Дождитесь завершения операции этой вкладки.');return
        if panel.signature()!=self.signature or any(panel.snapshots.get(key)!=value for key,value in self.snapshots.items()):
            self.error.configure(text='Подключение или список изменились. Закройте карточку и откройте заново.');return
        change={key:var.get() for key,var in self.values.items()}
        change.update(action='update' if self.user else 'create',mode=self.mode,set_password=self.set_password.get(),
            standard_auth=self.standard.get(),os_auth=self.os_auth.get(),show_in_list=self.show.get(),roles=[],groups=[])
        change['groups' if self.mode=='bsp' else 'roles']=sorted(self.checked)
        if self.mode=='bsp':change['group_names']=[self.rights[key]['name'] for key in sorted(self.checked)]
        if self.user:change.update(id=self.user['id'],etag=self.user['etag'])
        try:change=validate_change(change)
        except ValueError as exc:self.error.configure(text=str(exc));return
        names='\n'.join(panel.targets[key]['name'] for key in self.keys)
        note='\nВсе назначенные права будут сняты.' if self.user and not self.checked else ''
        if not messagebox.askyesno('Подтверждение изменения пользователей',
            ('Изменить' if self.user else 'Создать')+' пользователя '+change['name']+'?\nБазы:\n'+names+note,
            parent=self,default=messagebox.NO):return
        if panel.submit(self.keys,change):self.destroy()
