"""Persistent, nonmodal form errors that never grab the mouse or keyboard."""
import tkinter as tk

from ui_geometry import delayed_wrap


class FormNotice(tk.Frame):
    def __init__(self, parent, dismiss):
        super().__init__(parent, bg='#fff1f2', padx=12, pady=8,
                         highlightthickness=1, highlightbackground='#f0a6b0')
        close = tk.Button(self, text='×', command=dismiss, bg='#fff1f2', fg='#9f2338',
                          activebackground='#ffe0e5', relief='flat', bd=0,
                          font=('Segoe UI', 13), cursor='hand2', padx=6)
        close.pack(side=tk.RIGHT, anchor=tk.N)
        self.heading = tk.Label(self, bg='#fff1f2', fg='#9f2338', anchor=tk.W,
                                font=('Segoe UI', 10, 'bold'))
        self.heading.pack(fill=tk.X)
        self.message = tk.Label(self, bg='#fff1f2', fg='#792637', anchor=tk.W,
                                justify=tk.LEFT, font=('Segoe UI', 10), wraplength=820)
        self.message.pack(fill=tk.X, pady=(2, 0))
        delayed_wrap(self.message, minimum=200)

    def show(self, heading, text, before):
        self.heading.configure(text='⚠  ' + heading)
        self.message.configure(text=text if len(text) <= 900 else text[:450] + '\n…\n' + text[-450:] +
                               '\nПолный текст — в журнале операции.')
        self.pack(fill=tk.X, padx=22, pady=(0, 10), before=before)

    @staticmethod
    def reveal(widget):
        """Reveal an invalid field inside a scrolled form without grabbing focus."""
        widget.focus_set()
        parent = widget.master
        while parent is not None:
            if isinstance(parent, tk.Canvas):
                bounds = parent.bbox('all')
                if bounds and bounds[3] > parent.winfo_height():
                    offset = widget.winfo_rooty() - parent.winfo_rooty() + parent.canvasy(0)
                    parent.yview_moveto(max(0, offset - 20) / max(1, bounds[3]))
                break
            parent = parent.master
