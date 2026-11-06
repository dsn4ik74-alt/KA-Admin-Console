"""SSH disk usage explorer with directory totals, largest files and a treemap."""
from pathlib import PurePosixPath
from collections import OrderedDict
import csv
import io
import json
import time
import tkinter as tk
from tkinter import messagebox,simpledialog,ttk

from administration_ui import JobPanel
from sql_server import format_size
from ssh_forms import SshFields
from ssh_jobs import run_script
from table_sorting import TableSorter
from ui_geometry import delayed_wrap


class LinuxDiskPanel(SshFields,JobPanel):
    def __init__(self,parent,app,colors):
        super().__init__(parent,app,colors,'Анализ диска Linux · SSH','Анализировать')
        self.enable_cancellation();self.ssh_fields();self.path=tk.StringVar(value='/')
        self.scan_mode=tk.StringVar(value='Полный анализ')
        self.app._label(self.form,'Режим анализа',10,'muted',anchor=tk.W).pack(fill=tk.X,pady=(8,4))
        self.mode_choice=ttk.Combobox(self.form,textvariable=self.scan_mode,values=('Полный анализ','Быстрый обзор (du)'),state='readonly',width=1)
        self.mode_choice.pack(fill=tk.X);self.inputs.append(self.mode_choice)
        self.mode_choice.bind('<MouseWheel>',lambda event:'break');self.mode_choice.bind('<Shift-MouseWheel>',lambda event:'break')
        self.cross=tk.BooleanVar(value=False);self.elevate=tk.BooleanVar(value=False)
        self.path_entry=self.field('Каталог на Linux',self.path)
        self.check('Включая другие файловые системы',self.cross)
        self.check('Читать с правами sudo / root',self.elevate)
        self.button('Папка выше',self.up)
        self.note('Размер на диске учитывает разреженные файлы и жёсткие ссылки. Содержимое файлов не передаётся в консоль. Двойной щелчок по каталогу — анализировать его.')
        self.note('Быстрый обзор показывает размеры каталогов и файлов без списка крупнейших файлов, расширений и количества файлов. Возврат в просмотренную папку использует результат из памяти; «Анализировать» всегда обновляет данные с сервера.')
        table=self.split_log();table.rowconfigure(0,weight=0);table.rowconfigure(1,weight=1)
        toolbar=tk.Frame(table,bg=colors['panel']);toolbar.grid(row=0,column=0,sticky='ew',pady=(0,6))
        self.table_copy=app._button(toolbar,'Копировать таблицу',self.copy_table);self.table_copy.pack(side=tk.LEFT,padx=(0,8))
        self.delete_button=app._button(toolbar,'Удалить…',self.delete_selected);self.delete_button.pack(side=tk.LEFT)
        self.inputs.extend((self.table_copy,self.delete_button));self.copy_button.configure(text='Копировать журнал')
        self.tabs=ttk.Notebook(table);self.tabs.grid(row=1,column=0,sticky='nsew')
        self.trees={};self.sorters={};self.rows={}
        for key,title,columns in (
            ('children','Каталоги и файлы',(('name','Имя',300),('size','На диске',120),('apparent','Размер файлов',130),('files','Файлов',85))),
            ('largest','Крупные файлы',(('path','Полный путь',390),('size','На диске',130))),
            ('extensions','Типы файлов',(('extension','Расширение',240),('size','На диске',140),('files','Файлов',100)))):
            frame=tk.Frame(self.tabs,bg=colors['panel']);self.tabs.add(frame,text=title)
            frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
            tree=ttk.Treeview(frame,columns=[c[0] for c in columns],show='headings',selectmode='browse')
            for key_col,caption,width in columns:tree.heading(key_col,text=caption);tree.column(key_col,width=width,minwidth=75)
            tree.grid(row=0,column=0,sticky='nsew')
            scroll=ttk.Scrollbar(frame,command=tree.yview);scroll.grid(row=0,column=1,sticky='ns');tree.configure(yscrollcommand=scroll.set)
            horizontal=ttk.Scrollbar(frame,orient='horizontal',command=tree.xview);horizontal.grid(row=1,column=0,sticky='ew');tree.configure(xscrollcommand=horizontal.set)
            self.trees[key]=tree;self.sorters[key]=TableSorter(tree,numeric=('size','apparent','files'),colors=colors)
            tree.bind('<<TreeviewSelect>>',lambda event:self.update_table_actions())
            tree.bind('<Control-c>',self.copy_table);tree.bind('<Control-C>',self.copy_table)
        self.trees['children'].bind('<Double-1>',self.open_directory)
        self.canvas=tk.Canvas(self.tabs,bg=colors['bg'],height=220,highlightthickness=0)
        self.tabs.add(self.canvas,text='Карта диска');self.canvas.bind('<Configure>',lambda event:self.draw())
        self.summary=app._label(self.right,'Выберите каталог Linux и запустите анализ.',10,'muted',anchor=tk.W)
        self.summary.grid(row=1,column=0,sticky='ew',pady=(6,4));delayed_wrap(self.summary)
        self.result=None;self._views=OrderedDict();self._active_key=None;self._result_key=None;self._deleting=False
        self.tabs.bind('<<NotebookTabChanged>>',lambda event:self.update_table_actions())
        for variable in (self.ssh_host,self.ssh_port,self.ssh_user,self.ssh_use_key,self.ssh_key):variable.trace_add('write',lambda *args:self.update_table_actions())
        self.update_table_actions()

    def finish(self,text='',error=''):
        if error and self._deleting:self.invalidate_views('Удаление завершилось с ошибкой. Обновите анализ для проверки состояния файлов.')
        self._deleting=False
        super().finish(text,error);self.mode_choice.configure(state='readonly');self._active_key=None
        self.update_table_actions()

    def events(self,events):
        if any(event.get('cancellable') is False for event in events):self.cancel_button.configure(state=tk.DISABLED,text='Удаление…')
        super().events(events)

    def active_table(self):
        selected=self.tabs.select()
        return next(((key,tree) for key,tree in self.trees.items() if str(tree.master)==selected),(None,None))

    def deletion_row(self):
        key,tree=self.active_table()
        if self.running or not self.result or key not in ('children','largest'):return None
        if not self._result_key or self._result_key[:6]!=self.view_key(self.result['path'])[:6]:return None
        selected=tree.selection();row=self.rows.get(key,{}).get(selected[0]) if selected else None
        return row if row and row.get('identity') and row.get('path')!=self.result['path'] else None

    def update_table_actions(self):
        key,tree=self.active_table()
        self.table_copy.configure(state=tk.NORMAL if not self.running and tree is not None and tree.get_children() else tk.DISABLED)
        self.delete_button.configure(state=tk.NORMAL if self.deletion_row() else tk.DISABLED)

    def copy_table(self,event=None):
        key,tree=self.active_table()
        if not self.running and tree is not None and tree.get_children():
            output=io.StringIO();writer=csv.writer(output,delimiter='\t',lineterminator='\r\n')
            columns=tree['columns'];writer.writerow([tree.heading(column,'text') for column in columns])
            for item in tree.get_children():writer.writerow([tree.set(item,column) for column in columns])
            self.clipboard_clear();self.clipboard_append(output.getvalue())
            self.app._panel_status(self,'Таблица скопирована с заголовками')
        return 'break'

    def invalidate_views(self,message):
        self._views.clear();self.result=None;self._result_key=None
        for key,tree in self.trees.items():tree.delete(*tree.get_children());self.rows[key]={}
        self.draw();self.summary.configure(text=message)

    def delete_selected(self):
        row=self.deletion_row()
        if not row:return
        path=row['path'];directory=bool(row.get('directory'));connection=self.ssh_values()
        # Validate scope before showing the irreversible operation for review.
        from linux_disk_delete import validate_paths
        try:root,path=validate_paths(self.result['path'],path)
        except ValueError as exc:self.app._panel_error(self,str(exc));return
        prompt=connection['host']+'\n'+path+'\n\n'+('Каталог и всё его содержимое будут удалены безвозвратно. Введите полный путь для подтверждения:' if directory else 'Файл будет удалён безвозвратно. Продолжить?')
        if directory:
            if simpledialog.askstring('Удалить каталог?',prompt,parent=self.app.root)!=path:return
        elif not messagebox.askyesno('Удалить файл?',prompt,parent=self.app.root,default='no'):return
        payload=dict(root=root,path=path,identity=dict(row['identity']),recursive=directory);elevate=self.elevate.get()
        def deleted(result):
            self.invalidate_views('Удалено: '+result['deleted']+'. Для актуальных размеров нажмите «Анализировать».')
            self._deleting=False;self.append('Удалено: '+result['deleted']);self.finish('Удаление завершено. Обновите анализ при необходимости.')
        if self.start():
            self._deleting=True
            self.work(lambda emit:run_script('linux_disk_delete',connection,payload,emit,self.cancel_event,elevate=elevate),deleted,
                      history=dict(operation='Удаление Linux',server=connection['host'],path=path,executor='SSH'))

    def view_key(self,path,connection=None):
        connection=connection or self.ssh_values()
        return (connection['host'].casefold(),str(connection['port']),connection['user'],connection.get('use_key',False),
                connection.get('key','') if connection.get('use_key') else '',
                json.dumps(connection.get('host_keys',{}),sort_keys=True),self.cross.get(),self.elevate.get(),
                self.scan_mode.get(),str(PurePosixPath(path)))

    def execute(self,event=None):
        if self.running:return
        connection=self.ssh_values();path=self.path.get().strip()
        if not path.startswith('/'):
            self.app._panel_error(self,'Укажите абсолютный каталог Linux',self.path_entry);return
        payload=dict(path=path,cross_filesystems=self.cross.get(),mode='quick' if self.scan_mode.get().startswith('Быстрый') else 'full');elevate=self.elevate.get()
        self._active_key=self.view_key(path,connection)
        def loaded(result):
            self.app.saved_servers.remember_ssh(self,connection)
            self.loaded(result)
        if self.start():
            self.work(lambda emit:run_script('linux_disk_scan',connection,payload,emit,self.cancel_event,elevate=elevate),loaded)

    def loaded(self,result,cached_at=None):
        self.result=result;self.path.set(result['path'])
        key=self._active_key or self.view_key(result['path'])
        self._result_key=key
        if cached_at is None:
            weight=len(json.dumps(result,ensure_ascii=True).encode('ascii'))
            self._views[key]=(result,time.strftime('%H:%M:%S'),weight);self._views.move_to_end(key)
            while len(self._views)>4 or sum(view[2] for view in self._views.values())>8*1024*1024:self._views.popitem(last=False)
        for key,tree in self.trees.items():
            tree.delete(*tree.get_children());rows={}
            for i,row in enumerate(result[key]):
                row=dict(row)
                tree.insert('',tk.END,iid=str(i),values=[format_size(row.get(c)) if c in ('size','apparent') else '—' if row.get(c) is None else row.get(c,'') for c in tree['columns']]);rows[str(i)]=row
            self.rows[key]=rows;self.sorters[key].apply(rows)
        self.summary.configure(text=('Из памяти · '+cached_at+' · ' if cached_at else '')+'На диске: '+format_size(result['size'])+
            ' · файлов: '+('—' if result['files'] is None else str(result['files']))+' · свободно: '+format_size(result['available'])+
            (' · быстрый обзор (du)' if result.get('mode')=='quick' else ' · пропущено: '+str(result['skipped']))+
            (' · неполный результат, см. журнал' if result.get('partial') else ''))
        self.draw();self.finish('Результат из памяти · '+cached_at if cached_at else
                               'Обзор завершён с предупреждениями — часть данных недоступна.' if result.get('partial') else
                               'Анализ завершён. Размеры — снимок на время сканирования.')

    def navigate(self,path):
        if self.running:return
        self.path.set(path);key=self.view_key(path)
        if key in self._views:
            result,stamp,_=self._views[key];self._views.move_to_end(key);self._active_key=key
            if self.start():self.loaded(result,cached_at=stamp)
        else:self.execute()

    def open_directory(self,event=None):
        selected=self.trees['children'].selection()
        row=self.rows.get('children',{}).get(selected[0]) if selected else None
        if row and row['directory'] and not self.running:self.navigate(row['path'])

    def up(self):
        if not self.running:self.navigate(str(PurePosixPath(self.path.get()).parent))

    def draw(self):
        self.canvas.delete('all')
        if not self.result:return
        rows=[r for r in self.result['children'][:60] if r['size']>0]
        total=sum(r['size'] for r in rows)
        if not total:return
        width,height=max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height());x=y=0.;w=float(width);h=float(height)
        palette=('#2563eb','#0891b2','#0d9488','#7c3aed','#d97706','#dc2626','#4f46e5')
        for index,row in enumerate(rows):
            fraction=row['size']/total
            if w>=h:rw=w*fraction;rh=h
            else:rw=w;rh=h*fraction
            tag='item'+str(index)
            self.canvas.create_rectangle(x,y,x+rw,y+rh,fill=palette[index%len(palette)],outline='white',tags=tag)
            if rw>65 and rh>35:self.canvas.create_text(x+5,y+5,anchor='nw',text=row['name']+'\n'+format_size(row['size']),fill='white',width=max(20,rw-10),tags=tag)
            if row['directory']:
                self.canvas.tag_bind(tag,'<Double-1>',lambda event,path=row['path']:self.open_path(path))
            self.canvas.tag_bind(tag,'<Button-1>',lambda event,r=row:self.summary.configure(text=r['path']+' · '+format_size(r['size'])))
            total-=row['size']
            if w>=h:x+=rw;w-=rw
            else:y+=rh;h-=rh

    def open_path(self,path):
        if not self.running:self.navigate(path)
