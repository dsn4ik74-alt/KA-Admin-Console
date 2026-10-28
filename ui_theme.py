"""Shared desktop palette and a lightweight, responsive workspace rail."""
import tkinter as tk
from tkinter import ttk


COLORS = {
    'bg': '#f3f3fa', 'panel': '#ffffff', 'row': '#f8f8fd', 'border': '#e6e7f2',
    'fg': '#20223d', 'muted': '#747990', 'accent': '#5749e9', 'hover': '#4436d0',
    'accent_fg': '#ffffff', 'button': '#f0f0fa', 'button_hover': '#e7e5fc',
    'selected': '#e9e6ff', 'danger': '#ce465f', 'danger_hover': '#b43750',
    'success': '#149b79', 'success_bg': '#e5f6ef', 'warning': '#b37a1c',
    'warning_bg': '#fff5df', 'rail': '#ffffff',
}


class RoundedSurface(tk.Canvas):
    """Native vector card; only its outline changes when the window resizes."""
    def __init__(self, parent, colors, padding=16, fit_height=False):
        super().__init__(parent, bg=parent.cget('bg'), bd=0, highlightthickness=0, height=100)
        self.colors, self.padding, self.fit_height = colors, padding, fit_height
        self.body = tk.Frame(self, bg=colors['panel'])
        self.outline = self.create_polygon(0, 0, 0, 0, smooth=True, splinesteps=24,
                                          fill=colors['panel'], outline=colors['border'])
        self.window = self.create_window(padding, padding, window=self.body, anchor='nw')
        self._pending = None
        self.bind('<Configure>', self.queue_layout)
        self.body.bind('<Configure>', self.queue_layout)
        self.bind('<Destroy>', self.closed, add='+')

    def queue_layout(self, event=None):
        if self._pending is None:
            self._pending = self.after_idle(self.layout)

    def layout(self):
        self._pending = None
        width, height, inset = self.winfo_width(), self.winfo_height(), self.padding
        if self.fit_height:
            requested = self.body.winfo_reqheight() + inset*2
            if int(self.cget('height')) != requested:
                self.configure(height=requested)
        self.itemconfigure(self.window, width=max(1, width-inset*2), height=max(1, height-inset*2))
        right, bottom = max(1, width-1), max(1, height-2)
        radius = min(18, right/2, bottom/2)
        self.coords(self.outline, radius, 1, right-radius, 1, right, 1, right, radius,
                    right, bottom-radius, right, bottom, right-radius, bottom,
                    radius, bottom, 1, bottom, 1, bottom-radius, 1, radius, 1, 1)

    def closed(self, event):
        if event.widget is self and self._pending:
            self.after_cancel(self._pending)
            self._pending = None


def apply_styles(root, colors):
    style = ttk.Style(root)
    style.theme_use('clam')
    row = max(30, round(26 * float(root.tk.call('tk', 'scaling')) / 1.33))
    style.configure('Treeview', font=('Segoe UI', 10), rowheight=row,
                    background=colors['panel'], foreground=colors['fg'],
                    fieldbackground=colors['panel'], borderwidth=0,
                    bordercolor=colors['panel'], lightcolor=colors['panel'], darkcolor=colors['panel'])
    style.configure('Treeview.Heading', font=('Segoe UI', 9, 'bold'),
                    background=colors['row'], foreground=colors['muted'],
                    borderwidth=0, relief='flat', padding=(10, 9))
    style.configure('Copy.Treeview', font=('Segoe UI', 10), rowheight=row)
    style.configure('Copy.Treeview.Heading', font=('Segoe UI', 9, 'bold'), padding=(7, 8))
    style.map('Treeview', background=[('selected', colors['accent'])],
              foreground=[('selected', colors['accent_fg'])])
    style.map('Treeview.Heading', background=[('active', colors['button'])],
              foreground=[('active', colors['accent'])])
    for orientation in ('Vertical', 'Horizontal'):
        style.configure(f'{orientation}.TScrollbar', background=colors['border'],
                        troughcolor=colors['panel'], bordercolor=colors['panel'],
                        arrowcolor=colors['muted'], relief='flat', borderwidth=0,
                        lightcolor=colors['border'], darkcolor=colors['border'], arrowsize=10)
        style.map(f'{orientation}.TScrollbar', background=[('active', colors['button_hover'])])
    style.configure('TProgressbar', background=colors['accent'], troughcolor=colors['button'],
                    borderwidth=0, bordercolor=colors['button'],
                    lightcolor=colors['accent'], darkcolor=colors['accent'])
    style.configure('TCombobox', font=('Segoe UI', 10), padding=(7, 5),
                    fieldbackground=colors['panel'], background=colors['button'],
                    foreground=colors['fg'], arrowcolor=colors['muted'],
                    bordercolor=colors['border'], lightcolor=colors['panel'], darkcolor=colors['panel'])
    style.map('TCombobox', fieldbackground=[('disabled', colors['button']), ('readonly', colors['panel'])],
              foreground=[('disabled', colors['muted']), ('readonly', colors['fg'])],
              bordercolor=[('focus', colors['accent'])])
    style.configure('TNotebook', background=colors['panel'], borderwidth=0,
                    tabmargins=(0, 0, 0, 8))
    style.configure('TNotebook.Tab', font=('Segoe UI', 10), padding=(13, 8),
                    background=colors['panel'], foreground=colors['muted'], borderwidth=0)
    style.map('TNotebook.Tab', background=[('selected', colors['selected']), ('active', colors['button'])],
              foreground=[('selected', colors['accent']), ('active', colors['fg'])])
    root.option_add('*TCombobox*Listbox.background', colors['panel'])
    root.option_add('*TCombobox*Listbox.foreground', colors['fg'])
    root.option_add('*TCombobox*Listbox.selectBackground', colors['selected'])
    root.option_add('*TCombobox*Listbox.selectForeground', colors['fg'])


class WorkspaceSidebar(tk.Frame):
    """Shortcuts coexist with user bookmarks; never lock while a task runs."""
    def __init__(self, parent, app, colors, version):
        super().__init__(parent, bg=colors['rail'], width=170)
        self.app, self.c, self.compact = app, colors, None
        self.version_text = 'КА · ' + version
        self.pack_propagate(False)
        self.buttons, self.labels = {}, []
        self.brand = tk.Label(self, text='КА', bg=colors['accent'], fg='#ffffff',
                              font=('Segoe UI', 18, 'bold'), padx=10, pady=6, cursor='hand2')
        self.brand.pack(anchor='w', padx=14, pady=(16, 5))
        self.brand.bind('<Button-1>', lambda event: app._switch_mode('home'))
        self.caption = tk.Label(self, text='Консоль администратора', bg=colors['rail'],
                                fg=colors['muted'], font=('Segoe UI', 8))
        self.caption.pack(anchor='w', padx=14, pady=(0, 18))
        self.menu = tk.Frame(self, bg=colors['rail'])
        self.menu.pack(fill=tk.X, padx=9)
        entries = (
            ('ОБЗОР', None, None), ('Главная', '⌂', 'home'),
            ('ИНСТРУМЕНТЫ', None, None), ('Базы 1С', '1С', 'bases'),
            ('SQL Server', 'MS', 'db_backup'), ('PostgreSQL', 'PG', 'pg_dump'),
            ('Windows', 'W', 'service_create'), ('Linux', 'LX', 'linux_disk'),
            ('Планировщик', '◷', 'scheduler'),
        )
        for title, icon, mode in entries:
            if mode is None:
                label = tk.Label(self.menu, text=title, font=('Segoe UI', 8, 'bold'),
                                 bg=colors['rail'], fg=colors['muted'], anchor='w')
                label.pack(fill=tk.X, padx=9, pady=(15, 8))
                self.labels.append(label)
                continue
            button = tk.Button(self.menu, text=icon+'   '+title, command=lambda m=mode: app._switch_mode(m),
                               font=('Segoe UI', 10), anchor='w', relief='flat', bd=0,
                               bg=colors['rail'], fg=colors['muted'], activebackground=colors['selected'],
                               activeforeground=colors['accent'], cursor='hand2', padx=10, pady=9,
                               takefocus=True)
            button.pack(fill=tk.X, pady=3)
            button.title, button.symbol = title, icon
            self.buttons[mode] = button
        self.version = tk.Label(self, text='КА · '+version, bg=colors['rail'], fg=colors['muted'],
                                font=('Segoe UI', 8), anchor='w')
        self.version.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=16)
        self.set_compact(False)

    def set_compact(self, compact):
        if compact == self.compact:
            return
        self.compact = compact
        self.configure(width=62 if compact else 170)
        self.brand.configure(font=('Segoe UI', 13 if compact else 18, 'bold'), padx=6 if compact else 10)
        if compact:
            self.caption.pack_forget()
            self.version.configure(text='КА', anchor='center')
        else:
            self.caption.pack(anchor='w', padx=14, pady=(0, 18), before=self.menu)
            self.version.configure(text=self.version_text, anchor='w')
        for label in self.labels:
            label.configure(text='—' if compact else ('ОБЗОР' if label is self.labels[0] else 'ИНСТРУМЕНТЫ'))
        for button in self.buttons.values():
            button.configure(text=button.symbol if compact else button.symbol+'   '+button.title,
                             anchor='center' if compact else 'w', padx=2 if compact else 10)

    def set_active(self, mode):
        if mode.startswith('pg_') or mode.startswith('ibcmd_') or mode == 'postgres_install':
            active = 'pg_dump'
        elif mode.startswith('db_'):
            active = 'db_backup'
        elif mode.startswith('service_') or mode in ('accounts', 'windirstat'):
            active = 'service_create'
        elif mode in ('servers', 'bases', 'cluster_infobase', 'platform_install', 'configuration_install', 'jr_archive'):
            active = 'bases'
        else:
            active = mode
        for key, button in self.buttons.items():
            button.configure(bg=self.c['accent'] if key == active else self.c['rail'],
                             fg='#ffffff' if key == active else self.c['muted'])
