"""Identical password-first SSH fields for all administration forms."""
import tkinter as tk
from tkinter import filedialog


def add_auth_fields(panel):
    panel.field('Пароль SSH', panel.ssh_password, show='•')
    caption = panel.form.pack_slaves()[-2]
    panel.check('По SSH-ключу', panel.ssh_use_key)
    group = tk.Frame(panel.form, bg=panel.c['panel'])
    saved, panel.form = panel.form, group
    panel.field('Закрытый ключ на компьютере консоли · пусто = агент / стандартные ключи', panel.ssh_key)
    def choose():
        if panel.running:
            return
        path = filedialog.askopenfilename(parent=panel.app.root, title='Закрытый ключ SSH')
        if path:
            panel.ssh_key.set(path)
    button = getattr(panel, 'browse_button', None) or panel.button
    button('Выбрать ключ…', choose)
    panel.form = saved
    anchor = saved.pack_slaves()[-1]  # checkbox before the not-yet-packed key group
    def changed(*args):
        caption.configure(text='Пароль ключа SSH · если защищён' if panel.ssh_use_key.get() else 'Пароль SSH')
        if panel.ssh_use_key.get():
            group.pack(fill=tk.X, after=anchor)
        else:
            group.pack_forget()
    panel.ssh_use_key.trace_add('write', changed)
    changed()
    panel.ssh_auth_group = group
