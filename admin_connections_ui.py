"""Connection fields and explicit SSH trust for publication and file transfer."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ssh_connection import UnknownHostKey
from clipboard_shortcuts import bind_entry_shortcuts


HELP = '''WINDOWS · управление веб-сервером по WinRM
На сервере откройте PowerShell от администратора:
  Enable-PSRemoting -Force
Используйте доменное имя и доменную учётную запись с правами администратора.
По умолчанию WinRM HTTP: 5985, HTTPS: 5986. HTTPS требует настроенного
слушателя и доверенного сертификата. Приложение не отключает проверку TLS.
Для подключения по IP без домена предпочтителен WinRM HTTPS.
Если используется Negotiate по HTTP вне домена, на компьютере консоли
добавьте только конкретный адрес сервера в TrustedHosts:
  Set-Item WSMan:\\localhost\\Client\\TrustedHosts -Value "IP_СЕРВЕРА" -Concatenate
Эта команда выполняется вручную от администратора. Не используйте *.
При ошибках прав проверьте членство пользователя и ограничения удалённого UAC.

WINDOWS · файлы через SMB
Нужны доступ к общей папке и TCP 445. Включите общий доступ к нужной папке
и разрешите доступ выбранной учётной записи (права общей папки и NTFS).
Путь: \\\\сервер\\общая_папка\\каталог. C:\\каталог преобразуется в
\\\\сервер\\C$\\каталог и требует доступа к административной общей папке.
При необходимости добавьте правило брандмауэра с ограничением адреса консоли:
  New-NetFirewallRule -DisplayName "SMB from console" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 445 -RemoteAddress IP_КОНСОЛИ
Приложение не отключает чужие SMB-подключения при конфликте учётных записей.

LINUX · Ubuntu / Debian · SSH и SFTP
На сервере:
  sudo apt-get update
  sudo apt-get install openssh-server
  sudo systemctl enable --now ssh
Разрешите TCP 22 от адреса консоли в вашем брандмауэре.
Вводите логин и пароль; для входа по ключу включите соответствующий флажок.
При первом соединении сравните показанный отпечаток с ключом сервера.
Для публикации на Apache нужны root либо права sudo. Для передачи файлов
достаточно прав чтения источника и записи в каталог получателя.

Пути публикаций относятся к веб-серверу. Пути передачи относятся к каждому
из выбранных компьютеров. Linux ↔ Linux передаётся через память консоли:
временная полная копия на её диске не создаётся.
'''


def show_help(app):
    window = tk.Toplevel(app.root)
    window.title('Подключение · WinRM, SMB, SSH')
    window.geometry('920x650')
    frame = tk.Frame(window, padx=12, pady=12)
    frame.pack(fill=tk.BOTH, expand=True)
    text = tk.Text(frame, wrap=tk.WORD, font=('Segoe UI', 10), padx=10, pady=10)
    scroll = ttk.Scrollbar(frame, command=text.yview)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    text.configure(yscrollcommand=scroll.set)
    text.pack(fill=tk.BOTH, expand=True)
    text.insert('1.0', HELP)
    text.configure(state=tk.DISABLED)


def with_ssh_trust(panel, operation, success, retry, history=None):
    """Return unknown keys to the UI before retrying any unstarted operation."""
    def work(event):
        try:
            return operation(event)
        except UnknownHostKey as exc:
            return dict(_host_key=exc.key, settings_error='Требуется подтверждение ключа SSH; действие не начиналось')
    def done(result):
        if isinstance(result, dict) and '_host_key' in result:
            panel.finish('Требуется подтверждение ключа SSH')
            key = result['_host_key']
            def ask():
                if messagebox.askyesno('Ключ SSH', key['host']+'\n'+key['fingerprint']+'\n\nДоверять серверу?',
                                       parent=panel.app.root, default='no'):
                    panel.app._settings.setdefault('ssh_hosts', {})[key['host']] = key
                    panel.app._save_settings()
                    retry()
            if not panel.defer_until_visible(ask):
                ask()
        else:
            success(result)
    if panel.start():
        panel.work(work, done, history=history)


class EndpointFields:
    TYPES = {'Этот компьютер': 'local', 'Windows · SMB': 'windows', 'Linux · SSH': 'linux'}
    def __init__(self, panel, title, path):
        self.panel = panel
        self.frame = tk.Frame(panel.form,bg=panel.c['panel'],padx=10,pady=8,
                              highlightthickness=1,highlightbackground=panel.c['border'])
        self.frame.pack(fill=tk.X, pady=(5, 12))
        panel.app._label(self.frame,title,11,'accent',True).pack(anchor='w',pady=(0,6))
        self.kind = tk.StringVar(value='Этот компьютер')
        self.host, self.user, self.password = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.port, self.key, self.use_key = tk.StringVar(value='22'), tk.StringVar(), tk.BooleanVar(value=False)
        self.path = tk.StringVar(value=path)
        self.choice = ttk.Combobox(self.frame, textvariable=self.kind, values=list(self.TYPES), state='readonly', width=1)
        self.choice.pack(fill=tk.X)
        panel.inputs.append(self.choice)
        self.remote_widgets, self.ssh_widgets = [], []
        self.remote_group=tk.Frame(self.frame,bg=panel.c['panel'])
        self.ssh_group=tk.Frame(self.frame,bg=panel.c['panel'])
        for label, variable, show, group in (
                ('Адрес / IP сервера', self.host, None, self.remote_widgets),
                ('Логин · для Windows можно ДОМЕН\\логин', self.user, None, self.remote_widgets),
                ('Пароль / пароль ключа', self.password, '•', self.remote_widgets),
                ('Порт SSH', self.port, None, self.ssh_widgets),
                ('Закрытый ключ SSH · необязательно', self.key, None, self.ssh_widgets)):
            parent=self.remote_group if group is self.remote_widgets else self.ssh_group
            if variable is self.key:
                self.key_group=tk.Frame(parent,bg=panel.c['panel']);self.key_group.pack(fill=tk.X)
                parent=self.key_group
            panel.app._label(parent, label, 9, 'muted').pack(anchor='w', pady=(7, 3))
            if variable is self.host:
                entry=ttk.Combobox(parent,textvariable=variable,width=1,postcommand=self.refresh_hosts)
                self.host_entry=entry;bind_entry_shortcuts(entry)
                entry.bind('<<ComboboxSelected>>',self.choose_host)
            else:
                entry = panel.app._entry(parent, variable, show=show)
            entry.pack(fill=tk.X)
            panel.inputs.append(entry); group.append(entry)
        self.key_check = tk.Checkbutton(self.ssh_group, text='По SSH-ключу', variable=self.use_key,
                                       bg=panel.c['panel'], fg=panel.c['fg'], bd=0, highlightthickness=0)
        self.key_check.pack(anchor='w', pady=(6, 0),before=self.key_group); panel.inputs.append(self.key_check)
        self.ssh_widgets.append(self.key_check)
        self.path_group=tk.Frame(self.frame,bg=panel.c['panel']);self.path_group.pack(fill=tk.X)
        panel.app._label(self.path_group, 'Путь · источник — файл или папка; получатель — папка', 9, 'muted',wraplength=300,justify='left').pack(anchor='w', pady=(8, 3))
        entry = panel.app._entry(self.path_group, self.path); entry.pack(fill=tk.X); panel.inputs.append(entry)
        bar = tk.Frame(self.path_group, bg=panel.c['panel']); bar.pack(fill=tk.X, pady=(6, 0))
        self.pickers=[]
        for label, command in (('Папка…', self.choose_folder), ('Файл…', self.choose_file)):
            button = panel.app._button(bar, label, command); button.pack(side=tk.LEFT, padx=(0, 5)); panel.inputs.append(button);self.pickers.append(button)
        self.kind.trace_add('write', lambda *args: self.refresh())
        self.use_key.trace_add('write',lambda *args:self.refresh())
        self.refresh()

    def refresh(self):
        self.choice.configure(state='readonly' if not self.panel.running else tk.DISABLED)
        remote, linux = self.kind.get() != 'Этот компьютер', self.kind.get() == 'Linux · SSH'
        self.remote_group.pack_forget();self.ssh_group.pack_forget()
        if remote:self.remote_group.pack(fill=tk.X,before=self.path_group)
        if linux:self.ssh_group.pack(fill=tk.X,before=self.path_group)
        self.key_group.pack_forget()
        if self.use_key.get():self.key_group.pack(fill=tk.X)
        for widgets, enabled in ((self.remote_widgets, remote), (self.ssh_widgets, linux)):
            for widget in widgets:
                widget.configure(state=tk.NORMAL if enabled and not self.panel.running else tk.DISABLED)
        for button in self.pickers:button.configure(state=tk.NORMAL if not linux and not self.panel.running else tk.DISABLED)

    def values(self):
        return dict(kind=self.TYPES[self.kind.get()], host=self.host.get().strip(), user=self.user.get().strip(),
                    password=self.password.get(), port=self.port.get(), use_key=self.use_key.get(), key=self.key.get().strip(),
                    path=self.path.get().strip(), host_keys={**self.panel.app._settings.get('its_ssh_hosts', {}),
                                                          **self.panel.app._settings.get('ssh_hosts', {})})

    def refresh_hosts(self):
        self.saved={}
        if self.kind.get()=='Linux · SSH':
            for row in self.panel.app.activity.snapshot('connections'):
                if row['kind']=='ssh':
                    values=row['values'];caption=values['host']+' · '+values.get('user','')
                    self.saved[caption]=row
        self.host_entry.configure(values=sorted(self.saved))

    def choose_host(self,event=None):
        row=getattr(self,'saved',{}).get(self.host.get())
        if row:
            values=row['values'];secrets=self.panel.app.activity.credentials(row)
            for variable,key in ((self.host,'host'),(self.user,'user'),(self.port,'port'),(self.key,'key'),(self.use_key,'use_key')):
                variable.set(values.get(key,False if key=='use_key' else ''))
            self.password.set(secrets.get('password',''))

    def remember(self):
        if self.kind.get()=='Linux · SSH':
            self.panel.app.saved_servers.remember_ssh(self.panel,self.values())

    def choose_folder(self):
        if self.kind.get() == 'Linux · SSH':
            self.panel.app._panel_error(self.panel, 'Проводник открыт на компьютере консоли. Путь Linux введите вручную.')
            return
        value = filedialog.askdirectory(parent=self.panel.app.root)
        if value:
            self.path.set(value)

    def choose_file(self):
        if self.kind.get() == 'Linux · SSH':
            self.panel.app._panel_error(self.panel, 'Путь файла на Linux введите вручную.')
            return
        value = filedialog.askopenfilename(parent=self.panel.app.root)
        if value:
            self.path.set(value)
