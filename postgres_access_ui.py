"""Plain-language access editor; no connection or installation is performed."""
import tkinter as tk
from tkinter import ttk,messagebox

from postgres_access import rules,legacy_rules,configuration

ACTION={'Разрешить по паролю':'allow','Запретить':'reject'}
DATABASE={'Все базы':'all','Указать базу':'name','Физическая репликация':'replication'}
USER={'Все пользователи':'all','Указать пользователя':'name'}
CONNECTION={'С SSL или без SSL':'host','Только SSL':'hostssl','Только без SSL':'hostnossl'}
AUTH={'Автоматически по версии':'auto','SCRAM-SHA-256':'scram-sha-256','MD5 · старые клиенты':'md5'}


def caption(mapping,value):return next(key for key,item in mapping.items() if item==value)


class AccessEditor:
    def __init__(self,panel):
        self.panel=panel;self.app=panel.app;self.current=None;self.rendering=False
        self.rows=[dict(row) for row in panel.access_rules] if panel.custom_hba.get() else legacy_rules(panel.networks.get())
        self.window=tk.Toplevel(panel);self.window.title('Кто может подключаться к PostgreSQL')
        self.window.geometry(str(min(1100,max(820,self.window.winfo_screenwidth()-60)))+'x'+str(min(760,max(520,self.window.winfo_screenheight()-100))))
        self.window.minsize(820,520);self.window.transient(self.app.root)
        c=panel.c;self.window.configure(bg=c['bg'])
        outer=tk.Frame(self.window,bg=c['panel'],padx=14,pady=12);outer.pack(fill=tk.BOTH,expand=True)
        self.app._label(outer,'Доступ к новому экземпляру PostgreSQL',14,'fg',True).pack(anchor='w')
        self.app._label(outer,'Сетевые правила проверяются сверху вниз: срабатывает первое совпавшее. Без совпадения — отказ.\n'
            'Локальный вход postgres сохраняется; в Linux для локального сокета проверяется пользователь ОС.\n'
            'Права на таблицы и роли эти правила не выдают.',
            9,'muted',justify='left',anchor=tk.W,wraplength=1020).pack(fill=tk.X,pady=(6,10))
        body=tk.Frame(outer,bg=c['panel']);body.pack(fill=tk.BOTH,expand=True)
        body.columnconfigure(0,weight=1);body.columnconfigure(1,weight=0);body.rowconfigure(0,weight=1)
        left=tk.Frame(body,bg=c['panel']);left.grid(row=0,column=0,sticky='nsew',padx=(0,12))
        left.rowconfigure(1,weight=1);left.rowconfigure(4,weight=1);left.columnconfigure(0,weight=1)
        bar=tk.Frame(left,bg=c['panel']);bar.grid(row=0,column=0,sticky='ew')
        for index,(text,command) in enumerate((('Новое',self.new),('Удалить',self.delete),('↑',lambda:self.move(-1)),('↓',lambda:self.move(1)))):
            button=self.app._button(bar,text,command);button.configure(font=('Segoe UI',9),padx=7,pady=4)
            button.grid(row=0,column=index,padx=(0,4),pady=(0,6))
        table=tk.Frame(left);table.grid(row=1,column=0,sticky='nsew');table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.tree=ttk.Treeview(table,columns=('action','database','user','address','connection'),show='headings',selectmode='browse',height=7)
        for column,title,width in (('action','Доступ',95),('database','База',130),('user','Пользователь',125),('address','Откуда',160),('connection','SSL',115)):
            self.tree.heading(column,text=title);self.tree.column(column,width=width,minwidth=65)
        self.tree.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(table,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(table,orient=tk.HORIZONTAL,command=self.tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        self.tree.bind('<<TreeviewSelect>>',self.selected)
        self.app._label(left,'Так будет выглядеть pg_hba.conf',10,'fg',True).grid(row=2,column=0,sticky='w',pady=(10,4))
        self.version_note=self.app._label(left,'',9,'muted');self.version_note.grid(row=3,column=0,sticky='w')
        preview=tk.Frame(left);preview.grid(row=4,column=0,sticky='nsew');preview.rowconfigure(0,weight=1);preview.columnconfigure(0,weight=1)
        self.preview=tk.Text(preview,font=('Consolas',9),wrap=tk.NONE,height=8,bg='#080d0a',fg='#66df91')
        self.preview.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(preview,command=self.preview.yview);scroll.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(preview,orient=tk.HORIZONTAL,command=self.preview.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.preview.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        right_area=tk.Frame(body,bg=c['panel']);right_area.grid(row=0,column=1,sticky='nsew')
        right_area.rowconfigure(0,weight=1);right_area.columnconfigure(0,weight=1)
        canvas=tk.Canvas(right_area,bg=c['panel'],width=310,highlightthickness=0)
        canvas.grid(row=0,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(right_area,command=canvas.yview);scroll.grid(row=0,column=1,sticky='ns')
        canvas.configure(yscrollcommand=scroll.set)
        right=tk.Frame(canvas,bg=c['panel']);window=canvas.create_window((0,0),window=right,anchor='nw')
        right.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
        self.variables={};self.entries={}
        for key,label,mapping in (('action','Разрешить или запретить',ACTION),('database_mode','К каким базам',DATABASE),
                ('database','Имя базы',None),('user_mode','Кому',USER),('user','Имя пользователя PostgreSQL',None),
                ('address','С какого IP / подсети',None),('connection','Защита соединения',CONNECTION),('auth','Проверка пароля',AUTH)):
            self.app._label(right,label,9,'muted').pack(anchor='w',pady=(5,2))
            variable=self.variables[key]=tk.StringVar()
            entry=ttk.Combobox(right,textvariable=variable,values=list(mapping),state='readonly',width=29) if mapping else self.app._entry(right,variable,width=28)
            entry.pack(fill=tk.X);self.entries[key]=entry
            variable.trace_add('write',lambda *args:self.fields_changed())
        self.apply_button=self.app._button(right,'Добавить правило',self.apply_rule,primary=True);self.apply_button.pack(fill=tk.X,pady=(10,6))
        self.app._label(right,'IP одного компьютера: 192.168.20.10\nПодсеть: 192.168.20.0/24\nIPv6: 2001:db8::/64\n\n'
            '«Только SSL» требует настроенного SSL и сертификата PostgreSQL. Этот редактор их не устанавливает.\n'
            'MD5 не меняет формат уже заданных паролей ролей.',9,'muted',wraplength=270,justify='left').pack(fill=tk.X)
        def scroll_fields(event):
            canvas.yview_scroll(-max(1,abs(event.delta)//120) if event.delta>0 else max(1,abs(event.delta)//120),'units')
            return 'break'
        for widget in right.winfo_children():widget.bind('<MouseWheel>',scroll_fields)
        footer=tk.Frame(outer,bg=c['panel']);footer.pack(side=tk.BOTTOM,fill=tk.X,pady=(12,0),before=body)
        self.app._button(footer,'Сохранить правила',self.accept,primary=True).pack(side=tk.RIGHT)
        self.app._button(footer,'Отмена',self.window.destroy).pack(side=tk.RIGHT,padx=8)
        self.new();self.render()

    def version(self):
        row=self.panel.selected('distributions') or self.panel.loaded_release
        return row['version'] if row else '18'

    def fields_changed(self):
        if not self.entries or len(self.entries)<8:return
        self.entries['database'].configure(state=tk.NORMAL if self.variables['database_mode'].get()=='Указать базу' else tk.DISABLED)
        self.entries['user'].configure(state=tk.NORMAL if self.variables['user_mode'].get()=='Указать пользователя' else tk.DISABLED)
        self.entries['auth'].configure(state='readonly' if self.variables['action'].get()=='Разрешить по паролю' else tk.DISABLED)

    def snapshot(self):return {key:variable.get() for key,variable in self.variables.items()}

    def new(self):
        self.current=None;self.tree.selection_remove(self.tree.selection())
        values=dict(action=next(iter(ACTION)),database_mode=next(iter(DATABASE)),database='',user_mode=next(iter(USER)),user='',
                    address='',connection=next(iter(CONNECTION)),auth=next(iter(AUTH)))
        for key,value in values.items():self.variables[key].set(value)
        self.original=self.snapshot();self.apply_button.configure(text='Добавить правило')

    def selected(self,event=None):
        if self.rendering or not self.tree.selection():return
        self.current=int(self.tree.selection()[0]);row=self.rows[self.current]
        for key,mapping in (('action',ACTION),('database_mode',DATABASE),('user_mode',USER),('connection',CONNECTION),('auth',AUTH)):
            self.variables[key].set(caption(mapping,row[key]))
        for key in ('database','user','address'):self.variables[key].set(row[key])
        self.original=self.snapshot();self.apply_button.configure(text='Изменить правило')

    def apply_rule(self):
        try:
            row=self.snapshot()
            for key,mapping in (('action',ACTION),('database_mode',DATABASE),('user_mode',USER),('connection',CONNECTION),('auth',AUTH)):row[key]=mapping[row[key]]
            row=rules([row])[0]
            configuration(dict(access_rules=[row]),self.version(),self.panel.is_linux())
        except (ValueError,KeyError) as exc:
            messagebox.showerror('Проверьте правило',str(exc),parent=self.window);return False
        if self.current is None:
            if len(self.rows)>=200:messagebox.showerror('Правила доступа','Допускается не более 200 правил',parent=self.window);return False
            self.rows.append(row);self.current=len(self.rows)-1
        else:self.rows[self.current]=row
        self.render();self.original=self.snapshot();return True

    def render(self):
        self.rendering=True
        try:
            self.tree.delete(*self.tree.get_children())
            for index,row in enumerate(self.rows):
                self.tree.insert('',tk.END,iid=str(index),values=('Разрешить' if row['action']=='allow' else 'Запретить',
                    row['database'] if row['database_mode']=='name' else caption(DATABASE,row['database_mode']),
                    row['user'] if row['user_mode']=='name' else caption(USER,row['user_mode']),row['address'],caption(CONNECTION,row['connection'])))
            if self.current is not None and self.current<len(self.rows):self.tree.selection_set(str(self.current))
            text=configuration(dict(access_rules=self.rows),self.version(),self.panel.is_linux())
            self.preview.configure(state=tk.NORMAL);self.preview.delete('1.0',tk.END);self.preview.insert('1.0',text);self.preview.configure(state=tk.DISABLED)
            self.version_note.configure(text='Версия: '+self.version()+' · порядок правил сохранён')
        except ValueError as exc:self.version_note.configure(text=str(exc))
        finally:self.rendering=False

    def delete(self):
        if self.current is not None:
            self.rows.pop(self.current);self.new();self.render()

    def move(self,offset):
        if self.current is None:return
        target=self.current+offset
        if 0<=target<len(self.rows):
            self.rows[self.current],self.rows[target]=self.rows[target],self.rows[self.current];self.current=target;self.render()

    def accept(self):
        if self.panel.running:messagebox.showerror('Установка выполняется','Параметры текущей установки уже переданы. Дождитесь её завершения.',parent=self.window);return
        if self.snapshot()!=self.original and not self.apply_rule():return
        try:configuration(dict(access_rules=self.rows),self.version(),self.panel.is_linux())
        except ValueError as exc:messagebox.showerror('Правила доступа',str(exc),parent=self.window);return
        self.panel.access_rules=rules(self.rows);self.panel.custom_hba.set(True);self.panel.refresh_access();self.window.destroy()
