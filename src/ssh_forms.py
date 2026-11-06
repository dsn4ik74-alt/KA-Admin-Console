"""Shared SSH fields and host-key confirmation for new administration pages."""
import tkinter as tk
from tkinter import messagebox

from ssh_connection import probe,UnknownHostKey
from ssh_ui import add_auth_fields


class SshFields:
    def button(self,title,command):
        button=self.app._button(self.form,title,command);button.pack(fill=tk.X,pady=(7,0));self.inputs.append(button);return button

    def ssh_fields(self,elevate=True):
        self.ssh_host=tk.StringVar();self.ssh_user=tk.StringVar(value='root');self.ssh_port=tk.StringVar(value='22')
        self.ssh_password=tk.StringVar();self.ssh_key=tk.StringVar();self.ssh_use_key=tk.BooleanVar(value=False)
        self.sudo_password=tk.StringVar()
        self.ssh_host_entry=self.server_field('Linux · адрес SSH',self.ssh_host);self.field('Порт SSH',self.ssh_port);self.field('Пользователь SSH',self.ssh_user)
        add_auth_fields(self)
        if elevate:self.field('Пароль sudo · если отличается / при входе по ключу',self.sudo_password,show='•')
        self.button('Проверить SSH',self.check_ssh)

    def ssh_values(self):
        return dict(host=self.ssh_host.get().strip(),port=self.ssh_port.get().strip(),user=self.ssh_user.get().strip(),
            password=self.ssh_password.get(),use_key=self.ssh_use_key.get(),key=self.ssh_key.get().strip(),sudo_password=self.sudo_password.get(),
            host_keys={**self.app._settings.get('its_ssh_hosts',{}),**self.app._settings.get('ssh_hosts',{})})

    def check_ssh(self):
        if self.running:return
        values=self.ssh_values()
        def operation(event):
            try:return probe(values)
            except UnknownHostKey as exc:return dict(key=exc.key)
        def ready(result):
            self.finish('Проверка SSH завершена' if 'key' not in result else 'Требуется подтверждение ключа SSH')
            if 'key' in result:
                key=result['key']
                def confirm():
                    if messagebox.askyesno('Ключ SSH',key['host']+'\n'+key['fingerprint']+'\n\nДоверять этому серверу?',parent=self.app.root,default='no'):
                        self.app._settings.setdefault('ssh_hosts',{})[key['host']]=key;self.app._save_settings();self.check_ssh()
                if not self.defer_until_visible(confirm):confirm()
            else:
                self.app.saved_servers.remember_ssh(self,values)
                self.append(result['description'])
                if hasattr(self,'ssh_checked'):self.ssh_checked(result)
        if self.start():self.work(operation,ready)
