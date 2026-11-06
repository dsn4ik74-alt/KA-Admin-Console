"""A reviewed database name and optional PostgreSQL owner, without credentials."""
import tkinter as tk
from tkinter import messagebox, simpledialog

from postgres_runner import create_database_sql
from sql_server import identifier
from clipboard_shortcuts import bind_entry_shortcuts


class CreateDatabaseDialog(simpledialog.Dialog):
    def __init__(self, parent, vendor, server, username, colors):
        self.vendor, self.server, self.username, self.c = vendor, server, username, colors
        self.result = None
        super().__init__(parent, 'Создать базу · ' + vendor)

    def body(self, master):
        self.configure(bg=self.c['panel'])
        master.configure(bg=self.c['panel'], padx=16, pady=12)
        self.name = tk.StringVar(master=self)
        self.owner = tk.StringVar(master=self)
        def label(text):
            widget = tk.Label(master, text=text, bg=self.c['panel'], fg=self.c['fg'],
                              font=('Segoe UI', 10), wraplength=440, justify=tk.LEFT, anchor=tk.W)
            widget.pack(fill=tk.X, pady=(8, 4))
        def entry(variable):
            widget = tk.Entry(master, textvariable=variable, font=('Segoe UI', 11), width=44,
                              bg=self.c['bg'], fg=self.c['fg'], relief='solid', bd=1)
            bind_entry_shortcuts(widget)
            widget.pack(fill=tk.X, ipady=7)
            return widget
        label('Сервер: ' + self.server + '\nПодключение: ' + (self.username or 'Аутентификация Windows'))
        label('Имя новой базы')
        first = entry(self.name)
        if self.vendor == 'PostgreSQL':
            label('Владелец · пустое поле = текущий пользователь')
            entry(self.owner)
            label('Пустая база из template0, UTF-8. Локаль и размещение — по настройкам сервера.')
        else:
            label('Настройки — из model. Файлы данных и лога создаёт SQL Server в своих стандартных каталогах.')
        label('Создаётся база СУБД. Регистрация информационной базы в кластере 1С выполняется отдельно.')
        return first

    def buttonbox(self):
        box = tk.Frame(self, bg=self.c['panel'], padx=16, pady=12)
        box.pack(fill=tk.X)
        for text, command, primary in (('Создать базу', self.ok, True), ('Отмена', self.cancel, False)):
            tk.Button(box, text=text, command=command, bg=self.c['accent'] if primary else self.c['button'],
                      fg=self.c['accent_fg'] if primary else self.c['fg'], font=('Segoe UI', 10, 'bold'),
                      padx=14, pady=8, relief='flat').pack(side=tk.RIGHT, padx=(8, 0))
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)

    def validate(self):
        try:
            name, owner = self.name.get().strip(), self.owner.get().strip()
            if self.vendor == 'PostgreSQL':
                create_database_sql(name, owner)
            else:
                identifier(name)
            self.result = dict(name=name, owner=owner)
            return True
        except ValueError as exc:
            messagebox.showerror('Проверьте параметры базы', str(exc), parent=self)
            return False


def ask_create_database(parent, vendor, server, username, colors):
    return CreateDatabaseDialog(parent, vendor, server, username, colors).result
