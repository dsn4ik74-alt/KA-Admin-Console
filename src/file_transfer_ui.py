"""Windows Robocopy and all local/SMB/SFTP source/destination combinations."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from administration_ui import JobPanel
from admin_connections_ui import EndpointFields, show_help, with_ssh_trust
from file_transfer import scan, transfer, validate_pair, selection_paths, endpoint
from sql_server import format_size
from table_sorting import TableSorter


class FileTransferPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Передача файлов · Robocopy / SFTP / SMB', 'Начать передачу')
        self.enable_cancellation()
        self.source = EndpointFields(self, 'Источник', r'C:\temp\source')
        self.destination = EndpointFields(self, 'Получатель', r'C:\temp\target')
        self.queued_paths=[];self.listed_rows={}
        self.engine = tk.StringVar(value='Автоматически')
        self.engine_choice = ttk.Combobox(self.form, textvariable=self.engine, state='readonly', width=1,
                                         values=('Автоматически', 'Robocopy · Windows', 'Потоковая передача · SFTP / SMB'))
        self.engine_choice.pack(fill=tk.X); self.inputs.append(self.engine_choice)
        self.pattern = tk.StringVar(value='*'); self.field('Маска файлов · например *.bak', self.pattern)
        self.use_queue=tk.BooleanVar(value=False)
        self.check('Передавать только очередь «К отправке»',self.use_queue)
        self.threads = tk.StringVar(value='8'); self.threads_entry=self.field('Потоки Robocopy · 1–128', self.threads)
        self.overwrite, self.verify = tk.BooleanVar(value=False), tk.BooleanVar(value=True)
        self.check('Заменять существующие файлы получателя', self.overwrite)
        self.check('Проверять SHA-256 при потоковой передаче', self.verify)
        for text, action in (('Список файлов источника', self.list_source), ('Предпросмотр Robocopy · без копирования', self.preview),
                             ('Как включить WinRM / SMB / SSH', lambda: show_help(app))):
            button = app._button(self.form, text, action); button.pack(fill=tk.X, pady=(7, 0)); self.inputs.append(button)
            if text.startswith('Предпросмотр'):self.preview_button=button
        self.note('Без очереди копируется содержимое указанной папки. В очереди папки сохраняют свои имена; маска применяется ко всем файлам. '
                  'Несколько папок можно выделить в списке источника через Ctrl/Shift. Источник не удаляется. '
                  'Windows ↔ Windows: Robocopy; остальные направления: поток через консоль, без полной временной копии на диске. '
                  'Robocopy сравнивает размер и время; SHA-256 относится к потоковому режиму.')
        table = self.split_log()
        self.transfer_tabs=ttk.Notebook(table);self.transfer_tabs.grid(row=0,column=0,sticky='nsew')
        self.source_page=tk.Frame(self.transfer_tabs,bg=colors['panel']);self.queue_page=tk.Frame(self.transfer_tabs,bg=colors['panel'])
        self.transfer_tabs.add(self.source_page,text='Источник');self.transfer_tabs.add(self.queue_page,text='К отправке · 0')
        bar=tk.Frame(self.source_page,bg=colors['panel']);bar.pack(fill=tk.X,pady=(0,6))
        self.source_caption=app._label(bar,'Выделите файлы и папки · Ctrl / Shift',9,'muted',anchor='w')
        self.source_caption.pack(side=tk.LEFT,fill=tk.X,expand=True)
        button=app._button(bar,'В очередь',self.add_selected);button.pack(side=tk.RIGHT);self.inputs.append(button)
        table=tk.Frame(self.source_page,bg=colors['panel']);table.pack(fill=tk.BOTH,expand=True)
        table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.tree = ttk.Treeview(table, columns=('relative', 'size'), show='headings',selectmode='extended')
        self.tree.heading('relative', text='Файл / папка'); self.tree.heading('size', text='Размер')
        self.tree.column('relative', width=440); self.tree.column('size', width=110, anchor='e')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview); scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.sorter = TableSorter(self.tree, numeric=('size',), colors=colors)
        horizontal=ttk.Scrollbar(table,orient=tk.HORIZONTAL,command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky='ew');self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.column('size',minwidth=85,stretch=False)
        self.queue_buttons=[]
        toolbar=tk.Frame(self.queue_page,bg=colors['panel']);toolbar.pack(fill=tk.X,pady=(0,6))
        for column in range(3):toolbar.columnconfigure(column,weight=1)
        for index,(label,action) in enumerate((('Добавить путь',self.add_path),('Файлы…',self.add_files),('Папку…',self.add_folder),
                                               ('Убрать выбранное',self.remove_paths),('Очистить очередь',self.clear_queue))):
            button=app._button(toolbar,label,action);button.grid(row=index//3,column=index%3,sticky='ew',padx=(0,5),pady=(0,4))
            self.inputs.append(button);self.queue_buttons.append(button)
        area=tk.Frame(self.queue_page,bg=colors['panel']);area.pack(fill=tk.BOTH,expand=True)
        area.rowconfigure(0,weight=1);area.columnconfigure(0,weight=1)
        self.queue_tree=ttk.Treeview(area,columns=('path',),show='headings',selectmode='extended')
        self.queue_tree.heading('path',text='Файлы и папки на источнике');self.queue_tree.column('path',width=650,minwidth=240)
        self.queue_tree.grid(row=0,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(area,command=self.queue_tree.yview);vertical.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(area,orient=tk.HORIZONTAL,command=self.queue_tree.xview);horizontal.grid(row=1,column=0,sticky='ew')
        self.queue_tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.queue_tree.bind('<Delete>',lambda event:self.remove_paths())
        for tree in (self.tree,self.queue_tree):
            tree.bind('<Control-a>',lambda event,tree=tree:self.select_all(tree))
        self._source_identity=self.source_identity()
        for variable in (self.source.kind,self.source.host,self.source.user,self.source.port):
            variable.trace_add('write',lambda *args:self.source_changed())
        for variable in (self.engine,self.source.kind,self.destination.kind):variable.trace_add('write',lambda *args:self.options_changed())
        self.options_changed()

    def secret_values(self):
        return [self.source.password.get(), self.destination.password.get()]

    def finish(self, text='', error=''):
        super().finish(text, error)
        self.source.refresh(); self.destination.refresh()
        self.engine_choice.configure(state='readonly')
        self.options_changed()

    def options_changed(self):
        windows='Linux · SSH' not in (self.source.kind.get(),self.destination.kind.get())
        choices=('Автоматически','Robocopy · Windows','Потоковая передача · SFTP / SMB') if windows else ('Автоматически','Потоковая передача · SFTP / SMB')
        self.engine_choice.configure(values=choices)
        if self.engine.get() not in choices:self.engine.set('Автоматически')
        self.preview_button.configure(state=tk.NORMAL if windows and not self.running else tk.DISABLED)
        self.threads_entry.configure(state=tk.NORMAL if windows and self.engine.get()!='Потоковая передача · SFTP / SMB' and not self.running else tk.DISABLED)
        for button in self.queue_buttons[1:3]:button.configure(state=tk.NORMAL if self.source.kind.get()!='Linux · SSH' and not self.running else tk.DISABLED)

    @staticmethod
    def select_all(tree):
        tree.selection_set(tree.get_children());return 'break'

    def source_identity(self):
        return (self.source.kind.get(),self.source.host.get().strip(),self.source.user.get().strip(),self.source.port.get())

    def source_changed(self):
        identity=self.source_identity()
        if identity==self._source_identity:return
        self._source_identity=identity;self.queued_paths=[];self.listed_rows={}
        self.tree.delete(*self.tree.get_children());self.render_queue()
        self.source_caption.configure(text='Источник изменён. Обновите список файлов.')

    def render_queue(self):
        self.queue_tree.delete(*self.queue_tree.get_children())
        for index,path in enumerate(self.queued_paths):self.queue_tree.insert('',tk.END,iid=str(index),values=(path,))
        self.transfer_tabs.tab(self.queue_page,text='К отправке · '+str(len(self.queued_paths)))

    def add_paths(self,paths):
        if self.running or not paths:return
        try:
            rows=selection_paths(self.source.values(),self.queued_paths+list(paths))
        except (ValueError,TypeError) as exc:self.app._panel_error(self,str(exc));return
        self.queued_paths=[row['path'] for row in rows];self.use_queue.set(True);self.render_queue();self.transfer_tabs.select(self.queue_page)

    def add_path(self):
        self.add_paths([self.source.path.get()])

    def add_selected(self):
        if self.running:return
        selected=[self.listed_rows[iid]['path'] for iid in self.tree.selection() if iid in self.listed_rows]
        if not selected:self.app._panel_error(self,'Выделите файлы или папки в списке источника · Ctrl / Shift.');return
        self.add_paths(selected)

    def picker_options(self):
        if self.running or self.source.kind.get()=='Linux · SSH':return None
        try:
            values=endpoint(self.source.values())
        except ValueError as exc:self.app._panel_error(self,str(exc));return None
        return dict(parent=self.app.root,initialdir=values['path'])

    def accept_picked(self,paths):
        if self.source.kind.get()=='Windows · SMB':
            if any(not path.replace('/','\\').startswith('\\\\') for path in paths):
                self.app._panel_error(self,'Для удалённого Windows выберите UNC-путь на указанном сервере либо добавьте объекты из списка источника.');return
        self.add_paths(paths)

    def add_files(self):
        options=self.picker_options()
        if options is not None:self.accept_picked(filedialog.askopenfilenames(title='Добавить файлы в очередь',**options))

    def add_folder(self):
        options=self.picker_options()
        if options is not None:
            path=filedialog.askdirectory(title='Добавить папку в очередь',**options)
            if path:self.accept_picked([path])

    def remove_paths(self):
        if self.running:return
        selected=set(self.queue_tree.selection())
        self.queued_paths=[path for index,path in enumerate(self.queued_paths) if str(index) not in selected];self.render_queue()

    def clear_queue(self):
        if self.running:return
        self.queued_paths=[];self.render_queue()

    def list_source(self):
        if self.running:return
        values, pattern = self.source.values(), self.pattern.get()
        def done(rows):
            self.source.remember()
            self.tree.delete(*self.tree.get_children())
            raw = {};self.listed_rows={}
            for index, row in enumerate(rows):
                if row['directory'] and not row['relative']:continue
                key = str(index)
                self.tree.insert('', tk.END, iid=key, values=(row['relative'] or '/', 'Папка' if row['directory'] else format_size(row['size'])))
                raw[key] = dict(relative=row['relative'], size=row['size'])
                self.listed_rows[key]=dict(row)
            self.sorter.apply(raw)
            self.source_caption.configure(text='Выделите файлы и папки · Ctrl / Shift')
            self.transfer_tabs.select(self.source_page)
            self.finish('Список прочитан. Выделите объекты и нажмите «В очередь» либо передайте весь источник.')
        with_ssh_trust(self, lambda event: scan(values, pattern, event, self.cancel_event), done, self.list_source)

    def options(self, preview=False):
        try:threads=int(self.threads.get()) if self.threads_entry.cget('state')!='disabled' else 8
        except ValueError:raise ValueError('Укажите число потоков Robocopy: от 1 до 128') from None
        return dict(engine={'Автоматически':'auto', 'Robocopy · Windows':'robocopy', 'Потоковая передача · SFTP / SMB':'stream'}[self.engine.get()],
                    pattern=self.pattern.get().strip() or '*', threads=threads,
                    overwrite=self.overwrite.get(), verify=self.verify.get(), preview=preview)

    def preview(self):
        self.execute(preview=True)

    def execute(self, preview=False):
        if self.running:return
        try:
            options = self.options(preview)
            if self.use_queue.get():
                options['paths']=list(self.queued_paths)
                sources=selection_paths(self.source.values(),options['paths'])
                target=endpoint(self.destination.values())
                for source in sources:validate_pair(source,target)
                source=sources[0]
            else:source,target=validate_pair(self.source.values(),self.destination.values())
            if preview:
                options['engine'] = 'robocopy'
                if source['kind'] == 'linux' or target['kind'] == 'linux':
                    raise ValueError('Предпросмотр Robocopy доступен только для Windows ↔ Windows. Для остальных используйте список источника.')
        except (ValueError, TypeError) as exc:
            self.app._panel_error(self, str(exc)); return
        if not preview and options['overwrite'] and not messagebox.askyesno('Передать файлы?',
                ('Объектов в очереди: '+str(len(options['paths'])) if 'paths' in options else source['path'])+'\n→ '+target['path']+'\n\n'+('Существующие файлы будут заменены.' if options['overwrite'] else 'Существующие файлы будут пропущены.')+
                '\nИсточник не удаляется.', parent=self.app.root, default='no'):
            return
        def done(result):
            self.source.remember();self.destination.remember()
            text = 'Robocopy: код '+str(result['code']) if result['engine'] == 'Robocopy' else f'Передано: {result["copied"]}; пропущено: {result["skipped"]}'
            if 'items' in result:text+='; объектов: '+str(result['items'])
            self.finish(('Предпросмотр завершён. ' if preview else 'Передача завершена. ')+text)
        with_ssh_trust(self, lambda event: transfer(source, target, options, event, self.cancel_event), done,
                      lambda: self.execute(preview=preview), history=dict(operation='Предпросмотр Robocopy' if preview else 'Передача файлов',
                      server=source.get('host') or 'Этот компьютер', target=target.get('host') or 'Этот компьютер',
                      path=target['path'], executor='Консоль · Robocopy / SFTP / SMB'))
