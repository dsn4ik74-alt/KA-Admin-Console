"""Windows Robocopy and all local/SMB/SFTP source/destination combinations."""
import tkinter as tk
from tkinter import messagebox, ttk

from administration_ui import JobPanel
from admin_connections_ui import EndpointFields, show_help, with_ssh_trust
from file_transfer import scan, transfer, validate_pair
from sql_server import format_size
from table_sorting import TableSorter


class FileTransferPanel(JobPanel):
    def __init__(self, parent, app, colors):
        super().__init__(parent, app, colors, 'Передача файлов · Robocopy / SFTP / SMB', 'Начать передачу')
        self.enable_cancellation()
        self.source = EndpointFields(self, 'Источник', r'C:\temp\source')
        self.destination = EndpointFields(self, 'Получатель', r'C:\temp\target')
        self.engine = tk.StringVar(value='Автоматически')
        self.engine_choice = ttk.Combobox(self.form, textvariable=self.engine, state='readonly', width=1,
                                         values=('Автоматически', 'Robocopy · Windows', 'Потоковая передача · SFTP / SMB'))
        self.engine_choice.pack(fill=tk.X); self.inputs.append(self.engine_choice)
        self.pattern = tk.StringVar(value='*'); self.field('Маска файлов · например *.bak', self.pattern)
        self.threads = tk.StringVar(value='8'); self.threads_entry=self.field('Потоки Robocopy · 1–128', self.threads)
        self.overwrite, self.verify = tk.BooleanVar(value=False), tk.BooleanVar(value=True)
        self.check('Заменять существующие файлы получателя', self.overwrite)
        self.check('Проверять SHA-256 при потоковой передаче', self.verify)
        for text, action in (('Список файлов источника', self.list_source), ('Предпросмотр Robocopy · без копирования', self.preview),
                             ('Как включить WinRM / SMB / SSH', lambda: show_help(app))):
            button = app._button(self.form, text, action); button.pack(fill=tk.X, pady=(7, 0)); self.inputs.append(button)
            if text.startswith('Предпросмотр'):self.preview_button=button
        self.note('Копируется содержимое папки источника. Исходные файлы и лишние файлы получателя не удаляются. '
                  'Windows ↔ Windows: Robocopy; остальные направления: поток через консоль, без полной временной копии на диске. '
                  'Robocopy сравнивает размер и время; SHA-256 относится к потоковому режиму.')
        table = self.split_log()
        self.tree = ttk.Treeview(table, columns=('relative', 'size'), show='headings')
        self.tree.heading('relative', text='Файл / папка'); self.tree.heading('size', text='Размер')
        self.tree.column('relative', width=440); self.tree.column('size', width=110, anchor='e')
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, command=self.tree.yview); scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.sorter = TableSorter(self.tree, numeric=('size',), colors=colors)
        horizontal=ttk.Scrollbar(table,orient=tk.HORIZONTAL,command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky='ew');self.tree.configure(xscrollcommand=horizontal.set)
        self.tree.column('size',minwidth=85,stretch=False)
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

    def list_source(self):
        if self.running:return
        values, pattern = self.source.values(), self.pattern.get()
        def done(rows):
            self.source.remember()
            self.tree.delete(*self.tree.get_children())
            raw = {}
            for index, row in enumerate(rows):
                key = str(index)
                self.tree.insert('', tk.END, iid=key, values=(row['relative'] or '/', 'Папка' if row['directory'] else format_size(row['size'])))
                raw[key] = dict(relative=row['relative'], size=row['size'])
            self.sorter.apply(raw)
            self.finish('Список прочитан. Нажмите «Начать передачу».')
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
            source, target = validate_pair(self.source.values(), self.destination.values())
            options = self.options(preview)
            if preview:
                options['engine'] = 'robocopy'
                if source['kind'] == 'linux' or target['kind'] == 'linux':
                    raise ValueError('Предпросмотр Robocopy доступен только для Windows ↔ Windows. Для остальных используйте список источника.')
        except (ValueError, TypeError) as exc:
            self.app._panel_error(self, str(exc)); return
        if not preview and options['overwrite'] and not messagebox.askyesno('Передать файлы?',
                source['path']+'\n→ '+target['path']+'\n\n'+('Существующие файлы будут заменены.' if options['overwrite'] else 'Существующие файлы будут пропущены.')+
                '\nИсточник не удаляется.', parent=self.app.root, default='no'):
            return
        def done(result):
            self.source.remember();self.destination.remember()
            text = 'Robocopy: код '+str(result['code']) if result['engine'] == 'Robocopy' else f'Передано: {result["copied"]}; пропущено: {result["skipped"]}'
            self.finish(('Предпросмотр завершён. ' if preview else 'Передача завершена. ')+text)
        with_ssh_trust(self, lambda event: transfer(source, target, options, event, self.cancel_event), done,
                      lambda: self.execute(preview=preview), history=dict(operation='Предпросмотр Robocopy' if preview else 'Передача файлов',
                      server=source.get('host') or 'Этот компьютер', target=target.get('host') or 'Этот компьютер',
                      path=target['path'], executor='Консоль · Robocopy / SFTP / SMB'))
