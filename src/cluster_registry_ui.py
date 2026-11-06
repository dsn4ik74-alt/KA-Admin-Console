"""Modeless infobase properties from an explicitly selected registry snapshot."""
from datetime import datetime
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from uuid import UUID

from branding import tk_icon
from cluster_registry import read_registry, parameters, property_meaning
from cluster_registry_local import find_local_registry
from infobase_address import infobase_address
from ui_geometry import delayed_wrap


def state_text(denied):
    return 'Запрещены' if denied is True else 'Разрешены' if denied is False else 'Не определено для этой схемы'


def show_base_properties(app, row, parent=None):
    try:
        cluster=str(UUID(row.get('cluster_id','')));base=str(UUID(row.get('infobase_id','')))
    except (ValueError,TypeError,AttributeError):
        app._panel_error(None,'Для свойств нужны UUID кластера и ИБ. Обновите список баз.');return None
    return BaseProperties(app,dict(row,cluster_id=cluster,infobase_id=base),parent)


class BaseProperties:
    def __init__(self,app,row,parent=None):
        self.app,self.row=app,row;self.busy=False;self.snapshot=None;self._generation=0;self.source_note=''
        c=app.colors
        self.window=window=tk.Toplevel(parent or app.root)
        window.title('Свойства базы · '+row.get('base',''));window.geometry('940x700');window.minsize(640,480)
        window.configure(bg=c['bg']);window.transient(app.root);tk_icon(window)
        window.bind('<Escape>',lambda event:window.destroy())
        frame=tk.Frame(window,bg=c['panel'],padx=16,pady=14);frame.pack(fill=tk.BOTH,expand=True,padx=12,pady=12)
        footer=tk.Frame(frame,bg=c['panel']);footer.pack(side=tk.BOTTOM,fill=tk.X)
        app._button(footer,'Закрыть',window.destroy).pack(side=tk.BOTTOM,anchor='e',pady=(8,0))
        app._label(frame,row.get('base',''),15,bold=True,anchor='w').pack(fill=tk.X)
        address=infobase_address(dict(host=row.get('cluster_host') or row.get('server',''),
                                     port=row.get('cluster_port','')),row.get('base',''),row.get('server',''))
        app._label(frame,address or 'UUID ИБ: '+row['infobase_id'],10,'muted',anchor='w').pack(fill=tk.X,pady=(3,6))
        self.jobs=app._label(frame,'Регламентные задания: подключите реестр кластера',12,bold=True,anchor='w')
        self.jobs.pack(fill=tk.X,pady=(0,5))
        live=row.get('scheduled_jobs_deny')
        self.live=app._label(frame,'Последнее состояние, прочитанное через RAC: '+state_text(live) if type(live) is bool else
                            'Состояние через RAC в этой строке не проверено.',9,'muted',anchor='w')
        self.live.pack(fill=tk.X,pady=(0,8))
        source=tk.Frame(frame,bg=c['panel']);source.pack(fill=tk.X)
        self.path=tk.StringVar()
        app._entry(source,self.path).pack(side=tk.LEFT,fill=tk.X,expand=True,padx=(0,6))
        self.find_button=app._button(source,'Найти локально',self.find_local);self.find_button.pack(side=tk.LEFT,padx=(0,6))
        self.choose_button=app._button(source,'Выбрать файл…',self.choose);self.choose_button.pack(side=tk.LEFT,padx=(0,6))
        self.reload_button=app._button(source,'Обновить',self.load);self.reload_button.pack(side=tk.LEFT)
        self.status=app._label(frame,'Поиск по запущенным службам локального сервера или выбор файла вручную.',9,'muted',
                               anchor='w',wraplength=830,justify=tk.LEFT)
        self.status.pack(fill=tk.X,pady=(7,9))
        delayed_wrap(self.status)
        self.show_codes=tk.BooleanVar(value=False)
        tk.Checkbutton(frame,text='Показать служебные коды блокировки сеансов',variable=self.show_codes,
                       command=lambda:self.show(self.snapshot) if self.snapshot else None,
                       bg=c['panel'],fg=c['fg'],selectcolor=c['panel'],bd=0).pack(anchor='w',pady=(0,7))
        area=tk.Frame(frame,bg=c['panel']);area.pack(fill=tk.BOTH,expand=True)
        area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        self.tree=ttk.Treeview(area,columns=('value','meaning'),show='tree headings',selectmode='browse')
        self.tree.heading('#0',text='Свойство · индекс поля от 0');self.tree.column('#0',width=295,minwidth=170)
        self.tree.heading('value',text='Значение');self.tree.column('value',width=190,minwidth=80)
        self.tree.heading('meaning',text='Расшифровка');self.tree.column('meaning',width=360,minwidth=180)
        self.tree.grid(row=0,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(area,command=self.tree.yview);vertical.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(area,orient=tk.HORIZONTAL,command=self.tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.detail=tk.Text(footer,height=2,width=20,wrap=tk.WORD,font=('Segoe UI',9),bg=c['row'],fg=c['fg'],
                            selectbackground=c['text_selected'],bd=0,padx=8,pady=5,state=tk.DISABLED)
        self.detail.pack(fill=tk.X,pady=(8,0))
        self.tree.bind('<<TreeviewSelect>>',self.selection_changed)
        note=app._label(footer,'Просмотр снимка файла. Разрешённые задания выполняются по расписанию; изменение блокировки — через RAC.',9,'muted',
                   wraplength=830,justify=tk.LEFT,anchor='w')
        note.pack(fill=tk.X,pady=(8,0));delayed_wrap(note)
        snapshots=getattr(app,'_cluster_registry_snapshots',{})
        cached=snapshots.get(row['cluster_id'])
        saved=app._settings.get('cluster_registry_paths',{})
        if cached:self.show(cached)
        path=saved.get(row['cluster_id'],'') if isinstance(saved,dict) else ''
        if cached:path=cached.path
        if path:self.path.set(path)
        self.find_local(automatic=True,fallback=path)

    def begin_read(self,text):
        self.busy=True;self.snapshot=None;self.source_note='';self._generation+=1
        for button in (self.find_button,self.choose_button,self.reload_button):button.configure(state=tk.DISABLED)
        self.tree.delete(*self.tree.get_children());self.selection_changed()
        self.jobs.configure(text='Регламентные задания: чтение файла…',fg=self.app.colors['muted'])
        self.status.configure(text=text,fg=self.app.colors['muted'])
        return self._generation

    def finish_read(self,generation):
        try:
            if not self.window.winfo_exists() or generation!=self._generation:return False
        except tk.TclError:return False
        self.busy=False
        for button in (self.find_button,self.choose_button,self.reload_button):button.configure(state=tk.NORMAL)
        return True

    def find_local(self,automatic=False,fallback=''):
        if self.busy:return
        generation=self.begin_read('Поиск реестра по порту и запущенной службе ragent…')
        row=dict(self.row)
        def worker():
            try:result=find_local_registry(row)
            except (OSError,ValueError) as exc:result=(True,[],str(exc))
            except Exception:result=(True,[],'Не удалось проверить локальные службы 1С.')
            self.app._post(self.found_local,generation,result,automatic,fallback)
        threading.Thread(target=worker,daemon=True).start()

    def found_local(self,generation,result,automatic=False,fallback=''):
        if not self.finish_read(generation):return
        local,found,message=result
        if not local:
            if automatic and fallback:self.path.set(fallback);self.load();return
            message='Автопоиск доступен для серверов этого компьютера. Выберите файл или UNC-путь вручную.'
        if len(found)==1:self.use_found(found[0]);return
        self.jobs.configure(text='Регламентные задания: выберите реестр',fg=self.app.colors['muted'])
        self.status.configure(text=message,fg=self.app.colors['muted'])
        if len(found)>1:self.choose_found(found)

    def use_found(self,found):
        self.source_note='Запущенная служба: '+found.service+' · порт агента '+str(found.agent_port)
        self.path.set(found.snapshot.path);self.loaded(self._generation,found.snapshot)

    def choose_found(self,found):
        generation=self._generation
        choice=tk.Toplevel(self.window);choice.title('Выберите реестр запущенной службы 1С');choice.geometry('820x290')
        choice.transient(self.window);tk_icon(choice)
        frame=tk.Frame(choice,bg=self.app.colors['panel'],padx=12,pady=12);frame.pack(fill=tk.BOTH,expand=True)
        self.app._label(frame,'Совпали порт и UUID. Выберите нужный источник.',10,anchor='w').pack(fill=tk.X)
        def accept(event=None):
            selected=tree.selection()
            if selected and self.window.winfo_exists() and generation==self._generation and not self.busy:
                self.use_found(found[int(selected[0])]);choice.destroy()
        self.app._button(frame,'Выбрать',accept,primary=True).pack(side=tk.BOTTOM,anchor='e',pady=(8,0))
        tree=ttk.Treeview(frame,columns=('service','path'),show='headings',selectmode='browse')
        tree.heading('service',text='Запущенная служба');tree.column('service',width=240)
        tree.heading('path',text='Файл реестра');tree.column('path',width=520)
        tree.pack(fill=tk.BOTH,expand=True)
        for index,item in enumerate(found):tree.insert('',tk.END,iid=str(index),values=(item.service,item.snapshot.path))
        tree.bind('<Double-1>',accept);choice.bind('<Escape>',lambda event:choice.destroy())

    def choose(self):
        if self.busy:return
        path=filedialog.askopenfilename(parent=self.window,title='Реестр выбранного кластера · 1CV8Clst.lst',
            filetypes=(('Реестр кластера','*.lst *.txt'),('Все файлы','*.*')))
        if path:self.path.set(path);self.load()

    def load(self):
        if self.busy:return
        path=self.path.get().strip()
        if not path:
            self.status.configure(text='Выберите файл 1CV8Clst.lst или введите его полный путь.',fg=self.app.colors['danger']);return
        generation=self.begin_read('Чтение реестра…')
        def worker():
            try:
                snapshot=read_registry(path)
                if snapshot.cluster_id!=self.row['cluster_id']:raise ValueError('Файл принадлежит другому кластеру. Выберите реестр выбранного кластера.')
                if self.row['infobase_id'] not in snapshot.bases:raise ValueError('UUID выбранной ИБ отсутствует в файле. Обновите реестр или выберите другой файл.')
                result,error=snapshot,''
            except (OSError,ValueError) as exc:result,error=None,str(exc)
            except Exception:result,error=None,'Не удалось прочитать реестр кластера.'
            self.app._post(self.loaded,generation,result,error)
        threading.Thread(target=worker,daemon=True).start()

    def loaded(self,generation,snapshot,error=''):
        if not self.finish_read(generation):return
        if error:
            self.jobs.configure(text='Регламентные задания: данные файла не загружены',fg=self.app.colors['muted'])
            self.status.configure(text=error,fg=self.app.colors['danger']);return
        if not hasattr(self.app,'_cluster_registry_snapshots'):self.app._cluster_registry_snapshots={}
        self.app._cluster_registry_snapshots[snapshot.cluster_id]=snapshot
        if not isinstance(self.app._settings.get('cluster_registry_paths'),dict):self.app._settings['cluster_registry_paths']={}
        self.app._settings.setdefault('cluster_registry_paths',{})[snapshot.cluster_id]=snapshot.path
        self.app._save_settings()  # Only the path; registry contents and passwords stay in memory.
        self.show(snapshot)

    def show(self,snapshot):
        if snapshot.cluster_id!=self.row['cluster_id'] or self.row['infobase_id'] not in snapshot.bases:return
        self.snapshot=snapshot;record=snapshot.bases[self.row['infobase_id']]
        denied=record.jobs_denied
        suffix=' · блокировка включена' if denied is True else ' · блокировка выключена' if denied is False else ''
        self.jobs.configure(text='Регламентные задания из реестра: '+state_text(denied)+suffix,
                            fg=self.app.colors['danger'] if denied is True else self.app.colors['success'] if denied is False else self.app.colors['muted'])
        timestamp=datetime.fromtimestamp(snapshot.mtime).strftime('%d.%m.%Y %H:%M:%S') if snapshot.mtime else 'не определено'
        note='Файл: '+snapshot.path+' · изменён '+timestamp+' · '+snapshot.host+':'+str(snapshot.port)
        if self.source_note:note+=' · '+self.source_note
        mismatch=False
        try:
            stored=parameters(record.values[8]).get('schjobdn','').casefold()
            if denied is not None and stored in ('y','n') and (stored=='y')!=denied:
                note+=' · SchJobDn в строке подключения отличается от отдельного флага реестра.'
                mismatch=True
        except ValueError:pass
        live=self.row.get('scheduled_jobs_deny')
        if type(live) is bool and denied is not None and live!=denied:
            note+=' · Последние данные RAC отличаются от файла. Обновите сведения перед изменением настроек.'
            mismatch=True
        self.status.configure(text=note,fg=self.app.colors['warning'] if mismatch else self.app.colors['muted'])
        self.tree.delete(*self.tree.get_children())
        self.selection_changed()
        for index,label,value in record.properties(show_codes=self.show_codes.get()):
            parent=self.tree.insert('',tk.END,text='['+str(index)+'] '+label,
                values=('' if isinstance(value,tuple) else value,property_meaning(record,index,value)))
            if isinstance(value,tuple):
                for child,item in enumerate(value):
                    child_label,child_value=item if isinstance(item,tuple) and len(item)==2 else ('Поле',item)
                    self.tree.insert(parent,tk.END,text='['+str(child)+'] '+str(child_label),values=(str(child_value),
                        property_meaning(record,index,str(child_value),child_label if index==8 else child)))
                self.tree.item(parent,open=True)

    def selection_changed(self,event=None):
        selected=self.tree.selection()
        text=''
        if selected:
            row=self.tree.item(selected[0]);values=row.get('values',())
            text=row['text']+'\n'+' · '.join(str(value) for value in values)
        self.detail.configure(state=tk.NORMAL);self.detail.delete('1.0',tk.END);self.detail.insert('1.0',text);self.detail.configure(state=tk.DISABLED)
