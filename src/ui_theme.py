"""Shared desktop palette, cards and workspace navigation."""
import tkinter as tk
from tkinter import ttk


COLORS = {
    'bg': '#f1f2f2', 'panel': '#ffffff', 'row': '#fafbfb', 'border': '#d8dee0',
    'fg': '#30383c', 'muted': '#7c878c', 'accent': '#4c8198', 'hover': '#376c82',
    'accent_fg': '#ffffff', 'button': '#edf0f2', 'button_hover': '#e0e6ea',
    'selected': '#eef3f5', 'text_selected': '#b5cee7', 'danger': '#be3e43', 'danger_hover': '#a33035',
    'success': '#238760', 'success_bg': '#e8f5ee', 'warning': '#956500',
    'warning_bg': '#fff5d8', 'rail': '#f7f8f9',
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


class ParameterSheet(RoundedSurface):
    """A modeless parameter card inside one workspace, never a global grab."""
    def __init__(self,parent,app,colors):
        self.shadow=tk.Frame(parent,bg='#dfe3e4')
        super().__init__(parent,colors,padding=19)
        self.parent_area=parent
        self.visible=False
        self._sheet_job=None
        self._key_tag='ka-sheet-'+str(id(self))
        self.bind_class(self._key_tag,'<Escape>',lambda event:(self.hide(),'break')[1])
        self.body._open_for_field=self.show
        header=tk.Frame(self.body,bg=colors['panel']);header.pack(fill=tk.X,pady=(0,14))
        self.caption=app._label(header,'Параметры задачи',14,anchor='w')
        self.caption.pack(side=tk.LEFT)
        close=app._button(header,'×',self.hide);close.configure(padx=3,pady=2)
        close._base_bg='#fee8e8';close._hover_bg='#f9d4d6'
        close.configure(bg=close._base_bg,fg='#bb666a')
        close.pack(side=tk.RIGHT)
        footer=tk.Frame(self.body,bg=colors['panel']);footer.pack(side=tk.BOTTOM,fill=tk.X,pady=(12,0))
        app._button(footer,'Готово',self.hide,primary=True).pack(side=tk.RIGHT)
        app._label(footer,'Параметры сохраняются в этой вкладке',8,'muted',anchor='w').pack(side=tk.LEFT)
        self.content=tk.Frame(self.body,bg=colors['panel'])
        self.content.pack(fill=tk.BOTH,expand=True)
        parent.bind('<Configure>',self._sheet_resize,add='+')
        self.bind('<Escape>',lambda event:self.hide(),add='+')
        self.bind('<Destroy>',self._sheet_closed,add='+')

    def show(self):
        pending=[self.body]
        while pending:
            child=pending.pop();pending.extend(child.winfo_children())
            tags=child.bindtags()
            if self._key_tag not in tags:child.bindtags((*tags[:2],self._key_tag,*tags[2:]))
        self.visible=True;self._sheet_geometry();self.lift()

    def lift(self,aboveThis=None):
        return tk.Misc.lift(self,aboveThis)

    def hide(self):
        self.visible=False;self.place_forget();self.shadow.place_forget()

    def _sheet_resize(self,event=None):
        if self.visible and self._sheet_job is None:self._sheet_job=self.after_idle(self._sheet_geometry)

    def _sheet_geometry(self):
        self._sheet_job=None
        if not self.visible:return
        width,height=self.parent_area.winfo_width(),self.parent_area.winfo_height()
        w,h=max(260,min(520,width-28)),max(110,min(680,height-16))
        x,y=max(0,(width-w)//2),max(0,(height-h)//2)
        self.shadow.place(x=x+4,y=y+5,width=w,height=h)
        self.shadow.lift()
        self.place(x=x,y=y,width=w,height=h);self.lift()

    def _sheet_closed(self,event):
        if event.widget is self:
            if self._sheet_job:self.after_cancel(self._sheet_job);self._sheet_job=None
            self.unbind_class(self._key_tag,'<Escape>')


def apply_styles(root, colors):
    style = ttk.Style(root)
    style.theme_use('clam')
    row = max(34, round(29 * float(root.tk.call('tk', 'scaling')) / 1.33))
    style.configure('Treeview', font=('Segoe UI', 10), rowheight=row,
                    background=colors['panel'], foreground=colors['fg'],
                    fieldbackground=colors['panel'], borderwidth=0,
                    bordercolor=colors['panel'], lightcolor=colors['panel'], darkcolor=colors['panel'])
    style.configure('Treeview.Heading', font=('Segoe UI', 9),
                    background=colors['panel'], foreground=colors['fg'],
                    borderwidth=0, relief='flat', padding=(12, 10))
    style.configure('Copy.Treeview', font=('Segoe UI', 10), rowheight=row)
    style.configure('Copy.Treeview.Heading', background=colors['panel'], foreground=colors['fg'],
                    font=('Segoe UI', 9), padding=(10, 9))
    style.map('Treeview', background=[('selected', '#e7eff2')], foreground=[('selected', colors['fg'])])
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
    style.configure('TNotebook.Tab', font=('Segoe UI', 10), padding=(14, 7),
                    background=colors['panel'], foreground=colors['muted'], borderwidth=0)
    style.map('TNotebook.Tab', background=[('selected', '#eaf1f4'), ('active', colors['button'])],
              foreground=[('selected', colors['fg']), ('active', colors['fg'])])
    root.option_add('*TCombobox*Listbox.background', colors['panel'])
    root.option_add('*TCombobox*Listbox.foreground', colors['fg'])
    root.option_add('*TCombobox*Listbox.selectBackground', colors['selected'])
    root.option_add('*TCombobox*Listbox.selectForeground', colors['fg'])
    from ui_controls import install_native_controls
    install_native_controls(root, colors)


class WorkspaceSidebar(tk.Frame):
    """Compact navigation for the application's fixed sections."""
    def __init__(self, parent, app, colors, version):
        super().__init__(parent, bg=colors['rail'], width=176)
        self.app, self.c, self.compact = app, colors, None
        self.version_text = 'КА · ' + version
        self.pack_propagate(False)
        self.brand = tk.Label(self, text='КА  /  Администрирование', bg=colors['rail'], fg=colors['fg'],
                              font=('Segoe UI', 9, 'bold'), padx=0, pady=6, cursor='hand2')
        self.brand.pack(anchor='w', padx=12, pady=(12, 10))
        self.brand.bind('<Button-1>', lambda event: app._switch_mode('home'))
        self.home = app._button(self, '⌂  Главная', lambda: app._switch_mode('home'))
        self.home.configure(font=('Segoe UI', 9), anchor='w', pady=4, padx=8)
        self.home.pack(fill=tk.X, padx=9, pady=(0, 10))
        tk.Label(self, text='РАЗДЕЛЫ', bg=colors['rail'], fg=colors['muted'],
                 font=('Segoe UI', 8, 'bold'), anchor='w').pack(fill=tk.X, padx=15, pady=(0, 7))
        self.version = tk.Label(self, text='КА · '+version, bg=colors['rail'], fg=colors['muted'],
                                font=('Segoe UI', 8), anchor='w')
        self.version.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)
        self.folder_host = tk.Frame(self, bg=colors['rail'])
        self.folder_host.pack(fill=tk.BOTH, expand=True, padx=7, pady=(0, 5))
        tk.Label(self, text='Раздел → действие → вкладка', bg=colors['rail'], fg=colors['muted'],
                 font=('Segoe UI', 8), anchor='w').pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=4)
        self.set_compact(False)

    def set_compact(self, compact):
        if compact == self.compact:
            return
        self.compact = compact
        self.configure(width=164 if compact else 188)
        self.brand.configure(text='КА / Администр.' if compact else 'КА  /  Администрирование')

    def set_active(self, mode):
        self.home._base_bg = self.c['selected'] if mode == 'home' else self.c['button']
        self.home.configure(bg=self.home._base_bg, fg=self.c['accent'] if mode == 'home' else self.c['fg'])
