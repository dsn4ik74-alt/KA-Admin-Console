"""Browser-like open workspaces, separate from the persistent action menu."""
from copy import deepcopy
import tkinter as tk
from uuid import uuid4
import re

from bookmark_navigation import PAGES


def page_title(mode):
    return {'pg_copy':'PostgreSQL · Бэкап + восстановление','db_copy':'MSSQL · Бэкап + восстановление',
        'pg_dump':'PostgreSQL · Бэкап','pg_restore':'PostgreSQL · Восстановление',
        'db_backup':'MSSQL · Бэкап','db_restore':'MSSQL · Восстановление'}.get(mode,PAGES.get(mode,mode))


def split_legacy_tabs(saved):
    """Migrate old sidebar shortcuts into open workspaces without losing names."""
    tabs=[]
    def visit(nodes,depth=0):
        if depth>16:return []
        result=[]
        for original in nodes if isinstance(nodes,list) else []:
            if not isinstance(original,dict):continue
            node=deepcopy(original)
            if isinstance(node.get('page'),str) and node['page'] in PAGES and str(node.get('id','')).startswith('tab-'):
                tabs.append(dict(key=node['id'],mode=node['page'],title=node.get('title') or page_title(node['page'])))
            else:
                if 'children' in node:node['children']=visit(node['children'],depth+1)
                result.append(node)
        return result
    return (visit(saved) if isinstance(saved,list) else None),tabs


class WorkspaceTabs:
    def __init__(self,saved=None):
        self.entries=[]
        for row in saved[:100] if isinstance(saved,list) else []:
            if not isinstance(row,dict) or not isinstance(row.get('mode'),str) or row['mode'] not in PAGES:continue
            key=row.get('key')
            if not isinstance(key,str) or not key or key=='home' or len(key)>100 or self.find(key):continue
            self.entries.append(dict(key=key,mode=row['mode'],title=str(row.get('title') or page_title(row['mode']))[:100]))

    def find(self,key):
        return next((row for row in self.entries if row['key']==key),None)

    def add(self,mode,title=None,key=None):
        if mode not in PAGES:raise ValueError('Неизвестное действие')
        key=key or 'work-'+uuid4().hex
        if self.find(key):return self.find(key)
        base=title or page_title(mode);caption=base;number=2
        existing={row['title'] for row in self.entries}
        while caption in existing:caption=base+' · '+str(number);number+=1
        row=dict(key=key,mode=mode,title=caption[:100]);self.entries.append(row)
        return row

    def remove(self,key):
        row=self.find(key)
        if row:self.entries.remove(row)

    def move(self,key,before=None):
        row=self.find(key);target=self.find(before)
        if row is None or row is target:return False
        self.entries.remove(row)
        self.entries.insert(self.entries.index(target) if target else len(self.entries),row)
        return True

    def snapshot(self):return deepcopy(self.entries)


class WorkspaceTabStrip(tk.Frame):
    def __init__(self,parent,colors,saved,activate,close,new,changed):
        super().__init__(parent,bg=colors['bg'])
        self.c=colors;self.model=WorkspaceTabs(saved)
        self.activate,self.close_tab,self.new,self.changed=activate,close,new,changed
        self.widgets={};self.active='home';self.running=set();self._blink=None;self._phase=False
        self._press=None;self._drag=False;self._menu=None
        self.home=tk.Button(self,text='⌂ Главная',command=lambda:self.activate('home'),font=('Segoe UI',9),
            bg=colors['panel'],fg=colors['fg'],bd=0,relief='flat',padx=10,pady=7,cursor='hand2')
        self.home.pack(side=tk.LEFT,padx=(0,5))
        self.plus=tk.Button(self,text='+',command=self.new,font=('Segoe UI',13),bg=colors['button'],
            fg=colors['fg'],bd=0,relief='flat',padx=10,pady=1,cursor='hand2')
        self.plus.pack(side=tk.RIGHT,padx=(5,0))
        self.arrows=tk.Frame(self,bg=colors['bg'])
        for text,step in (('‹',-1),('›',1)):
            tk.Button(self.arrows,text=text,command=lambda s=step:self.canvas.xview_scroll(3*s,'units'),
                bg=colors['button'],fg=colors['fg'],bd=0,font=('Segoe UI',11),padx=5,cursor='hand2').pack(side=tk.LEFT)
        self.canvas=tk.Canvas(self,bg=colors['bg'],height=34,highlightthickness=0,bd=0,width=1)
        self.canvas.pack(side=tk.LEFT,fill=tk.X,expand=True)
        self.items=tk.Frame(self.canvas,bg=colors['bg']);self.canvas.create_window(0,0,window=self.items,anchor='nw')
        self.canvas.bind('<Configure>',self._size);self.items.bind('<Configure>',self._size)
        self.canvas.bind('<MouseWheel>',lambda event:'break')
        self.bind('<Destroy>',self.destroyed,add='+')
        self.redraw()

    def _size(self,event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        height=max(32,self.items.winfo_reqheight())
        if int(self.canvas.cget('height'))!=height:self.canvas.configure(height=height)
        overflow=self.items.winfo_reqwidth()>self.canvas.winfo_width()+2
        if overflow and not self.arrows.winfo_manager():self.arrows.pack(side=tk.RIGHT,before=self.canvas,padx=3)
        elif not overflow and self.arrows.winfo_manager():self.arrows.pack_forget()

    def redraw(self):
        for row in self.model.entries:
            key=row['key']
            if key not in self.widgets:
                frame=tk.Frame(self.items,bg=self.c['button'],highlightthickness=1,highlightbackground=self.c['border'],cursor='hand2')
                dot=tk.Label(frame,text='●',font=('Segoe UI',8),width=2,bd=0);dot.pack(side=tk.LEFT)
                label=tk.Label(frame,font=('Segoe UI',9),anchor='w',width=24,pady=7,bd=0);label.pack(side=tk.LEFT)
                close=tk.Label(frame,text='×',font=('Segoe UI',11),padx=7,pady=3,bd=0);close.pack(side=tk.LEFT)
                frame.dot,frame.label,frame.close=dot,label,close;self.widgets[key]=frame
                for widget in (frame,dot,label):
                    widget.bind('<ButtonPress-1>',lambda event,k=key:self.press(event,k))
                    widget.bind('<B1-Motion>',self.motion)
                    widget.bind('<ButtonRelease-1>',lambda event,k=key:self.release(event,k))
                    widget.bind('<Button-2>',lambda event,k=key:self.request_close(k))
                    widget.bind('<Button-3>',lambda event,k=key:self.context(event,k))
                close.bind('<Button-1>',lambda event,k=key:self.request_close(k))
            self.widgets[key].pack_forget();self.widgets[key].pack(side=tk.LEFT,padx=(0,3),pady=(0,2))
            title=row['title'];suffix=re.search(r' · \d+$',title)
            tail=suffix[0] if suffix else ''
            self.widgets[key].label.configure(text=title if len(title)<=25 else title[:24-len(tail)]+'…'+tail)
        for key in set(self.widgets)-{row['key'] for row in self.model.entries}:self.widgets.pop(key).destroy()
        self.set_active(self.active)
        self._size()

    def add(self,mode,title=None,key=None):
        row=self.model.add(mode,title,key);self.redraw();self.changed(self.model.snapshot());return row['key']

    def request_close(self,key):
        self.close_tab(key)
        return 'break'

    def remove(self,key):
        self.model.remove(key);self.running.discard(key);self.redraw();self.changed(self.model.snapshot())

    def set_active(self,key):
        self.active=key
        self.home.configure(bg=self.c['panel'] if key=='home' else self.c['button'],fg=self.c['accent'] if key=='home' else self.c['fg'])
        for item,frame in self.widgets.items():
            background=self.c['panel'] if item==key else self.c['button']
            frame.configure(bg=background,highlightbackground=self.c['accent'] if item==key else self.c['border'])
            for widget in (frame.label,frame.close):widget.configure(bg=background,fg=self.c['fg'])
            frame.dot.configure(bg=background,fg=('#16a34a' if self._phase else '#b0dec0') if item in self.running else background)

    def reveal(self,key):
        self.items.update_idletasks()
        frame=self.widgets.get(key)
        if frame is None:return
        total=max(1,self.items.winfo_reqwidth());left=frame.winfo_x();right=left+frame.winfo_width()
        visible_left=self.canvas.canvasx(0);visible_right=visible_left+self.canvas.winfo_width()
        if left<visible_left:self.canvas.xview_moveto(left/total)
        elif right>visible_right:self.canvas.xview_moveto(max(0,right-self.canvas.winfo_width())/total)

    def set_running(self,keys):
        self.running=set(keys);self.set_active(self.active)
        if self.running and self._blink is None:self.blink()
        elif not self.running and self._blink is not None:self.after_cancel(self._blink);self._blink=None

    def blink(self):
        self._phase=not self._phase;self.set_active(self.active);self._blink=self.after(650,self.blink)

    def press(self,event,key):
        self._press=(key,event.x_root);self._drag=False
        return 'break'

    def motion(self,event):
        if self._press and abs(event.x_root-self._press[1])>6:self._drag=True
        return 'break'

    def release(self,event,key):
        pressed=self._press;drag=self._drag;self._press=None;self._drag=False
        if pressed and drag:
            before=next((row['key'] for row in self.model.entries if row['key']!=pressed[0] and
                event.x_root<self.widgets[row['key']].winfo_rootx()+self.widgets[row['key']].winfo_width()/2),None)
            if self.model.move(pressed[0],before):self.redraw();self.changed(self.model.snapshot())
        elif pressed:self.activate(key)
        return 'break'

    def context(self,event,key):
        if self._menu:self._menu.destroy()
        row=self.model.find(key)
        if row is None:return
        self._menu=tk.Menu(self,tearoff=False,bg=self.c['panel'],fg=self.c['fg'])
        self._menu.add_command(label=row['title'],state=tk.DISABLED)
        self._menu.add_command(label='Открыть ещё одну',command=lambda:self.new(row['mode']))
        self._menu.add_command(label='Закрыть вкладку',command=lambda:self.close_tab(key))
        try:self._menu.tk_popup(event.x_root,event.y_root)
        finally:self._menu.grab_release()

    def destroyed(self,event):
        if event.widget is self and self._blink is not None:
            self.after_cancel(self._blink);self._blink=None
