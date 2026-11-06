"""Publication properties, visible WS/HS switches and remote directory browsing."""
from datetime import datetime
import os
from pathlib import PureWindowsPath, PurePosixPath
import tkinter as tk
from tkinter import messagebox,ttk
import webbrowser

from publication_config import full_text_match,state
from sql_server import format_size
from table_sorting import TableSorter


class PublicationWindow(tk.Toplevel):
    def __init__(self,panel,row,title):
        super().__init__(panel.app.root)
        self.panel,self.app,self.row=panel,panel.app,dict(row)
        self.context=panel.context_key();self.pending=False;self.buttons=[]
        self.configure(bg=panel.c['panel']);self.title(title+' · /'+row['name'])
        self.geometry(f'{min(1040,self.winfo_screenwidth()-80)}x{min(740,self.winfo_screenheight()-100)}')
        self.minsize(680,440);self.transient(self.app.root)
        self.status=self.app._label(self,'',9,'muted',anchor='w',justify=tk.LEFT)
        self.status.pack(side=tk.BOTTOM,fill=tk.X,padx=14,pady=10)
        self.status.bind('<Configure>',lambda event:self.status.configure(wraplength=max(200,event.width)))
        panel.detail_windows.add(self)
        self.bind('<Destroy>',self.destroyed,add='+')

    def button(self,parent,title,command,**options):
        button=self.app._button(parent,title,command,**options);self.buttons.append(button);return button

    def request(self,action,callback,**values):
        if self.pending or self.panel.running:return
        try:
            if self.context!=self.panel.context_key():raise ValueError('Подключение изменилось. Откройте свойства публикации заново.')
            payload=self.panel.publication_payload(self.row);payload.update(action=action,**values)
        except ValueError as exc:self.task_finished('',str(exc));return
        self.pending=True;self.status.configure(text='Выполняется…',fg=self.panel.c['muted'])
        for button in self.buttons:button.configure(state=tk.DISABLED)
        def done(result):
            if self.winfo_exists():
                self.pending=False;callback(result)
            self.panel.finish(result.get('text','Сведения получены.'))
        if self.panel.perform(payload,done,history=action=='publication_services') is False:
            self.task_finished('','Запрос не запущен. Проверьте подключение и выбранную публикацию.')

    def task_finished(self,text,error=''):
        if not self.winfo_exists():return
        self.pending=False
        for button in self.buttons:button.configure(state=tk.NORMAL)
        self.status.configure(text=error or text,fg=self.panel.c['danger'] if error else self.panel.c['muted'])

    def destroyed(self,event):
        if event.widget is self:self.panel.detail_windows.discard(self)


class PublicationProperties(PublicationWindow):
    def __init__(self,panel,row,services=False):
        super().__init__(panel,row,'Свойства публикации')
        self.data={};self.rules={};self._switch_job=None;self.switches=[]
        bar=tk.Frame(self,bg=panel.c['panel']);bar.pack(fill=tk.X,padx=14,pady=10)
        self.button(bar,'Обновить свойства',self.refresh).pack(side=tk.LEFT)
        self.button(bar,'Открыть папку',lambda:panel.open_directory(self.row)).pack(side=tk.RIGHT)
        self.tabs=ttk.Notebook(self);self.tabs.pack(fill=tk.BOTH,expand=True,padx=14)
        general=tk.Frame(self.tabs,bg=panel.c['panel']);self.tabs.add(general,text='Свойства')
        self.info=ttk.Treeview(general,columns=('property','value'),show='headings',selectmode='browse')
        self.info.heading('property',text='Параметр',anchor='center');self.info.heading('value',text='Значение',anchor='center')
        self.info.column('property',width=200,minwidth=150,stretch=False,anchor='w')
        self.info.column('value',width=700,minwidth=240,anchor='center');self.info.pack(fill=tk.BOTH,expand=True)
        horizontal=ttk.Scrollbar(general,orient=tk.HORIZONTAL,command=self.info.xview);horizontal.pack(fill=tk.X)
        self.info.configure(xscrollcommand=horizontal.set)
        linkbar=tk.Frame(general,bg=panel.c['panel']);linkbar.pack(fill=tk.X,pady=10)
        self.url=tk.StringVar();self.urls=ttk.Combobox(linkbar,textvariable=self.url,state='readonly',width=1)
        self.urls.pack(side=tk.LEFT,fill=tk.X,expand=True)
        self.urls.bind('<<ComboboxSelected>>',lambda event:self.render_services())
        self.button(linkbar,'Открыть ссылку',self.open_link).pack(side=tk.RIGHT,padx=(8,0))
        page=tk.Frame(self.tabs,bg=panel.c['panel']);self.tabs.add(page,text='WS / HS')
        actions=tk.Frame(page,bg=panel.c['panel']);actions.pack(fill=tk.X,pady=(8,8))
        for title,enabled in (('Включить выбранные',True),('Отключить выбранные',False)):
            self.button(actions,title,lambda value=enabled:self.change_selected(value)).pack(side=tk.RIGHT,padx=(6,0))
        self.search=tk.StringVar();search=panel.app._entry(actions,self.search,width=18)
        search.pack(side=tk.LEFT,fill=tk.X,expand=True);search.set_hint('Поиск сервиса…')
        self.search.trace_add('write',lambda *args:self.render_services())
        frame=tk.Frame(page,bg=panel.c['panel']);frame.pack(fill=tk.BOTH,expand=True);frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.tree=ttk.Treeview(frame,columns=('kind','name','address','effective','switch'),show='headings',selectmode='extended')
        for column,title,width in (('kind','Тип',55),('name','Сервис / правило',250),('address','Адрес',180),('effective','Доступность',180),('switch','Переключатель',145)):
            self.tree.heading(column,text=title,anchor='center');self.tree.column(column,width=width,minwidth=45 if column=='kind' else 110,anchor='center',stretch=column in ('name','address','effective'))
        self.tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(frame,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(frame,orient=tk.HORIZONTAL,command=self.tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=lambda *args:(scroll.set(*args),self.queue_switches()),
                            xscrollcommand=lambda *args:(horizontal.set(*args),self.queue_switches()))
        self.sorter=TableSorter(self.tree,colors=panel.c)
        for column in self.tree['columns']:
            self.tree.heading(column,command=lambda key=column:(self.sorter.toggle(key),self.queue_switches()))
        self.tree.tag_configure('on',foreground=panel.c['success']);self.tree.tag_configure('off',foreground=panel.c['danger'])
        self.tree.bind('<Control-a>',self.select_all);self.tree.bind('<Control-A>',self.select_all)
        self.tree.bind('<Control-KeyPress>',lambda event:self.select_all(event) if event.keycode==65 else None)
        self.tree.bind('<Configure>',lambda event:self.queue_switches(),add='+')
        self.tabs.bind('<<NotebookTabChanged>>',lambda event:self.queue_switches())
        self.tree.bind('<ButtonRelease-1>',lambda event:self.queue_switches(),add='+')
        self.tree.bind('<Double-1>',self.open_service_link)
        note=panel.app._label(page,'Ctrl+A — выбрать все строки. Переключатель задаёт разрешение; «Доступность» учитывает блокировку всего раздела WS.\n'
                        'Показаны правила и сервисы из публикации. Остальные сервисы конфигурации подчиняются правилу «по умолчанию».',9,'muted',anchor='w',justify=tk.LEFT)
        note.pack(fill=tk.X,pady=8);note.bind('<Configure>',lambda event:note.configure(wraplength=max(200,event.width)))
        if services:self.tabs.select(page)
        self.refresh()

    def refresh(self):self.request('publication_properties',self.loaded)

    def loaded(self,result):
        self.data=dict(result);self.row.update({key:result[key] for key in ('module_path','version','vrd_path') if key in result})
        self.panel.update_publication_info(result)
        decorated=self.panel.decorate_publication(result)
        urls=decorated.get('urls',[]);self.urls.configure(values=urls);self.url.set(urls[0] if urls else '')
        self.info.delete(*self.info.get_children())
        values=(('Публикация','/'+result.get('name','')),('Веб-сервер',self.panel.connection_label()),('Сайт',decorated.get('site_label',result.get('site',''))),('Тип',decorated.get('type_text','')),
                ('Состояние веб-сервера / пула',decorated.get('state','')),('Версия веб-расширения 1С',(result.get('version') or 'Не определена')+(' · по каталогу модуля' if result.get('version') and result.get('version_source')=='path' else '')),
                ('Модуль веб-сервера',result.get('module_path') or 'Не определён'),('Каталог публикации',result.get('path','')),
                ('Тип информационной базы',result.get('base_type','Не определён')),('Сервер / кластер 1С',result.get('cluster','')),
                ('Имя базы в кластере',result.get('base','')),('Каталог файловой базы',result.get('file_path','')),
                ('Адрес информационной базы',result.get('connection','')),('Пул IIS',result.get('pool','')),
                ('Файл публикации',result.get('vrd_path','')),('Веб-клиент',('Включено' if result.get('web_enabled') else 'Отключено') if result.get('is_1c') else 'Не определено'),
                ('Базовый путь default.vrd',result.get('base_url','')),
                ('Ошибка чтения настроек',result.get('metadata_error','')))
        for key,value in values:self.info.insert('',tk.END,values=(key,value))
        self.rules={row['id']:row for row in result.get('services',[])};self.render_services()
        error=result.get('metadata_error')
        self.status.configure(text=error or ('Свойства получены.' if result.get('is_1c') else 'В каталоге нет default.vrd; настройки WS/HS недоступны.'),fg=self.panel.c['danger'] if error else self.panel.c['muted'])

    def render_services(self):
        selected=set(self.tree.selection());self.tree.delete(*self.tree.get_children())
        for key,row in self.rules.items():
            url=self.service_url(row)
            effective='Включено' if row['effective'] else 'Отключено'
            if not full_text_match(dict(row,url=url,state=effective,setting='Включено' if row['enabled'] else 'Отключено'),self.search.get()):continue
            if row['enabled'] and not row['effective']:effective+=' · раздел WS'
            self.tree.insert('',tk.END,iid=key,values=(row['kind'],row['name'],url,effective,'Включено' if row['enabled'] else 'Отключено'),tags=('on' if row['effective'] else 'off',))
        self.sorter.apply()
        self.tree.selection_set(tuple(key for key in selected if self.tree.exists(key)));self.queue_switches()

    def select_all(self,event=None):self.tree.selection_set(self.tree.get_children());return 'break'

    def change_selected(self,enabled):self.change(list(self.tree.selection()),enabled)

    def change(self,ids,enabled):
        if self.pending or self.panel.running:return
        changes=[dict(id=key,enabled=enabled) for key in ids if key in self.rules and self.rules[key]['enabled']!=enabled]
        if not changes:self.status.configure(text='Выберите строки, для которых нужно изменить настройку.');return
        if not enabled:
            names='\n'.join(self.rules[item['id']]['name'] for item in changes[:12])
            if len(changes)>12:names+='\n… ещё '+str(len(changes)-12)
            if not messagebox.askyesno('Отключить сервисы?',names+'\n\nКоличество настроек: '+str(len(changes))+'.\nОтключение может остановить использующие их интеграции. Продолжить?',parent=self,default='no'):return
        self.request('publication_services',self.loaded,etag=self.data.get('etag',''),changes=changes)
        self.queue_switches()

    def queue_switches(self):
        if self.winfo_exists() and self._switch_job is None:self._switch_job=self.after_idle(self.draw_switches)

    def draw_switches(self):
        self._switch_job=None
        visible=[]
        if self.tree.winfo_ismapped():
            keys=dict.fromkeys(self.tree.identify_row(y) for y in range(0,self.tree.winfo_height(),8))
            for key in keys:
                if not key:continue
                box=self.tree.bbox(key,'switch')
                if box and box[0]>=0:
                    x,y,width,height=box;width=min(width,self.tree.winfo_width()-x-2)
                    if width>=40:visible.append((key,(x,y,width,height)))
        while len(self.switches)<len(visible):
            cell=tk.Frame(self.tree,bg=self.panel.c['panel'])
            button=self.app._button(cell,'',lambda:None);button.configure(font=('Segoe UI',9),padx=5,pady=2);button.pack(fill=tk.BOTH,expand=True)
            button.bind('<Control-KeyPress>',lambda event:self.select_all(event) if event.keycode==65 else None)
            self.switches.append(button)
        for index,button in enumerate(self.switches):
            if index>=len(visible):button.master.place_forget();continue
            key,(x,y,width,height)=visible[index];enabled=self.rules[key]['enabled']
            button._base_bg='#e8f5ee' if enabled else '#fbe9eb';button._hover_bg='#d1eadb' if enabled else '#f5d4d8'
            button.configure(text='Включено' if enabled else 'Отключено',bg=button._base_bg,
                             fg=self.panel.c['success'] if enabled else self.panel.c['danger'],state=tk.DISABLED if self.pending or self.panel.running else tk.NORMAL,
                             command=lambda rule=key,value=not enabled:self.change([rule],value))
            button.master.place(x=x+3,y=y+2,width=max(30,width-6),height=max(16,height-4));button.master.lift()

    def task_finished(self,text,error=''):
        super().task_finished(text,error or self.data.get('metadata_error',''));self.queue_switches()

    def open_link(self):
        if self.url.get().startswith(('http://','https://')):webbrowser.open(self.url.get())

    def service_url(self,row):
        return self.url.get().rstrip('/')+'/'+row['address'] if row.get('address') and self.url.get() else ''

    def open_service_link(self,event):
        if self.tree.identify_region(event.x,event.y)!='cell' or self.tree.identify_column(event.x)!='#3':return
        key=self.tree.identify_row(event.y);row=self.rules.get(key)
        if row:
            url=self.service_url(row)
            if url.startswith(('http://','https://')):webbrowser.open(url)

    def destroyed(self,event):
        if event.widget is self and self._switch_job:self.after_cancel(self._switch_job);self._switch_job=None
        super().destroyed(event)


class PublicationBrowser(PublicationWindow):
    def __init__(self,panel,row):
        super().__init__(panel,row,'Папка публикации')
        self.current=row['path'];self.root_path=row['path'];self.entries={}
        bar=tk.Frame(self,bg=panel.c['panel']);bar.pack(fill=tk.X,padx=14,pady=10)
        self.button(bar,'На уровень выше',self.up).pack(side=tk.LEFT)
        self.button(bar,'Обновить',self.refresh).pack(side=tk.LEFT,padx=6)
        if panel.system.get()=='Windows' and panel.where.get()=='Этот компьютер':
            self.button(bar,'Открыть Проводник',lambda:os.startfile(self.current)).pack(side=tk.RIGHT)
        self.path=tk.StringVar(value=self.current);entry=self.app._entry(self,self.path);entry.configure(state='readonly');entry.pack(fill=tk.X,padx=14,pady=(0,10))
        frame=tk.Frame(self,bg=panel.c['panel']);frame.pack(fill=tk.BOTH,expand=True,padx=14);frame.columnconfigure(0,weight=1);frame.rowconfigure(0,weight=1)
        self.tree=ttk.Treeview(frame,columns=('name','kind','size','modified'),show='headings',selectmode='browse')
        for name,title,width in (('name','Имя',320),('kind','Тип',130),('size','Размер',120),('modified','Изменён',180)):
            self.tree.heading(name,text=title,anchor='center');self.tree.column(name,width=width,minwidth=80,anchor='center')
        self.tree.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(frame,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set);self.sorter=TableSorter(self.tree,numeric=('size',),colors=panel.c)
        self.tree.bind('<Double-1>',self.open_selected)
        self.refresh()

    def refresh(self):self.request('publication_browse',self.loaded,browse_path=self.current)

    def loaded(self,result):
        self.current,self.root_path=result['path'],result['root'];self.path.set(self.current)
        self.tree.delete(*self.tree.get_children());self.entries={};raw={}
        for index,row in enumerate(sorted(result.get('entries',[]),key=lambda row:(not row['directory'],row['name'].casefold()))):
            key=str(index);self.entries[key]=row;modified=row.get('modified','')
            if isinstance(modified,(int,float)):modified=datetime.fromtimestamp(modified).strftime('%Y-%m-%d %H:%M:%S')
            self.tree.insert('',tk.END,iid=key,values=(row['name'],'Папка' if row['directory'] else 'Файл',format_size(row.get('size')) if not row['directory'] else '',modified))
            raw[key]=dict(row,kind='Папка' if row['directory'] else 'Файл')
        self.sorter.apply(raw)
        self.status.configure(text='Показано: '+str(len(self.entries))+('. Достигнут предел 10 000 объектов.' if result.get('truncated') else ''))

    def open_selected(self,event=None):
        if event is not None and self.tree.identify_region(event.x,event.y) not in ('cell','tree'):return
        selection=self.tree.selection();row=self.entries.get(selection[0]) if selection else None
        if row and row['directory'] and not self.pending:self.current=row['path'];self.refresh()

    def up(self):
        path_type=PureWindowsPath if self.panel.system.get()=='Windows' else PurePosixPath
        if path_type(self.current)!=path_type(self.root_path):self.current=str(path_type(self.current).parent);self.refresh()
