"""Browser-like open workspaces, separate from the persistent action menu."""
from copy import deepcopy
import tkinter as tk
import tkinter.font as tkfont
from uuid import uuid4
import re

from bookmark_navigation import PAGES
from ui_controls import LandscapeButton, icon, rounded_image


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


def tab_widths(available,count,minimum=34,maximum=232,gap=3):
    """Share the row before resorting to scrolling at the favicon-only limit."""
    if count<=0:return []
    space=max(0,int(available)-gap*(count-1))
    width=max(minimum,min(maximum,space//count))
    extra=min(count,max(0,space-width*count)) if width<maximum else 0
    return [width+(index<extra) for index in range(count)]


class BrowserTab(tk.Canvas):
    """Rounded native tab with a pixel-trimmed caption and full-title tooltip."""
    def __init__(self,parent,colors,mode,title):
        self.c=colors;self.title=title;self.selected=False;self.running=False;self.phase=False
        self.scale=max(1,float(parent.tk.call('tk','scaling'))/(96/72))
        self.tab_height=round(34*self.scale)
        super().__init__(parent,bg=colors['bg'],width=round(232*self.scale),height=self.tab_height,
                         highlightthickness=0,bd=0,cursor='hand2')
        self.shape=self.create_image(0,0,anchor='nw')
        self._surface_key=None
        self.font=tkfont.Font(root=self,font=('Segoe UI',9))
        name=('database' if mode.startswith(('pg_','db_','ibcmd_')) or mode=='bases' else
              'server' if mode=='servers' or mode.startswith('service_') else
              {'accounts':'user','scheduler':'clock','file_transfer':'folder','linux_disk':'terminal',
               'platform_install':'download','configuration_install':'download','ring_license':'shield'}.get(mode,'grid'))
        self.symbol=icon(self,name,round(15*self.scale),colors['muted'])
        self.symbol_small=icon(self,name,round(12*self.scale),colors['muted'])
        self.dot=tk.Label(self,image=self.symbol,bd=0,font=('Segoe UI',8),anchor='center')
        self.label=tk.Label(self,font=self.font,anchor='w',bd=0)
        self.close=tk.Label(self,text='×',font=('Segoe UI',11),anchor='center',bd=0,cursor='hand2')
        self._tooltip_job=None;self._tooltip=None;self._hover=False;self._caption_key=None
        self.bind('<Configure>',lambda event:self.render(),add='+')
        self.bind('<Destroy>',self.closed,add='+')
        for child in (self,self.dot,self.label,self.close):
            child.bind('<Enter>',self.enter,add='+');child.bind('<Leave>',self.leave,add='+')

    def set_state(self,selected,running,phase):
        changed=self.selected!=selected or self.running!=running
        self.selected,self.running,self.phase=selected,running,phase
        background=self.c['panel'] if selected else self.c['button']
        self.label.configure(bg=background,fg=self.c['fg'])
        self.close.configure(bg=background,fg=self.c['muted'])
        self.dot.configure(bg=background,image='' if running else self.symbol_small if self.winfo_width()<round(58*self.scale) else self.symbol,text='●' if running else '',
                           fg='#16a34a' if phase else '#a7d8b5')
        if changed:self.render()

    def caption(self,available):
        key=(self.title,available)
        if key==self._caption_key:return self._caption
        title=self.title
        if self.font.measure(title)>available:
            match=re.search(r' · \d+$',title);tail=match[0] if match else ''
            if self.font.measure('…'+tail)>available:title=''
            else:
                left,right=0,len(title)-len(tail)
                while left<right:
                    middle=(left+right+1)//2
                    if self.font.measure(title[:middle]+'…'+tail)<=available:left=middle
                    else:right=middle-1
                title=title[:left]+'…'+tail
        self._caption_key,self._caption=key,title
        return title

    def render(self):
        width,height=self.winfo_width(),self.winfo_height()
        surface_key=(width,height,self.selected)
        if surface_key!=self._surface_key:
            self._surface=rounded_image(self,width,height,self.c['panel'] if self.selected else self.c['button'],
                                        self.c['border'] if self.selected else None,round(10*self.scale),shared=True)
            self.itemconfigure(self.shape,image=self._surface);self._surface_key=surface_key
        tight=width<round(58*self.scale)
        show_close=not tight or self.selected or self._hover
        edge=round((3 if tight else 6)*self.scale)
        close_width=round((12 if tight else 17)*self.scale)
        symbol_width=round((12 if tight else 15)*self.scale)
        if show_close:
            self.close.place(x=max(0,width-edge-close_width),y=4,width=close_width,height=max(1,height-8))
        else:self.close.place_forget()
        if tight:
            self.label.place_forget()
            x=edge if show_close else max(0,(width-symbol_width)//2)
            self.dot.place(x=x,y=5,width=symbol_width,height=max(1,height-10))
        else:
            self.dot.place(x=edge,y=5,width=symbol_width,height=max(1,height-10))
            x=edge+symbol_width+round(6*self.scale)
            available=max(0,width-x-close_width-edge-3)
            self.label.configure(text=self.caption(available))
            self.label.place(x=x,y=4,width=available,height=max(1,height-8))
        if not self.running:self.dot.configure(image=self.symbol_small if tight else self.symbol)

    def enter(self,event=None):
        self._hover=True;self.render();self.hide_tooltip()
        if not (getattr(event,'state',0) or 0)&0x100:
            self._tooltip_job=self.after(550,self.show_tooltip)

    def leave(self,event=None):
        x,y=self.winfo_pointerxy()
        if self.winfo_rootx()<=x<self.winfo_rootx()+self.winfo_width() and self.winfo_rooty()<=y<self.winfo_rooty()+self.winfo_height():return
        self._hover=False;self.render();self.hide_tooltip()

    def show_tooltip(self):
        self._tooltip_job=None
        if not self.winfo_ismapped() or not self._hover:return
        tip=self._tooltip=tk.Toplevel(self);tip.overrideredirect(True)
        tk.Label(tip,text=self.title,bg='#30383c',fg='#ffffff',font=('Segoe UI',9),padx=9,pady=5).pack()
        tip.update_idletasks()
        x=max(0,min(self.winfo_rootx(),self.winfo_screenwidth()-tip.winfo_reqwidth()-8))
        tip.geometry(f'+{x}+{self.winfo_rooty()+self.winfo_height()+4}')

    def hide_tooltip(self):
        if self._tooltip_job:self.after_cancel(self._tooltip_job);self._tooltip_job=None
        if self._tooltip:
            tip,self._tooltip=self._tooltip,None
            if tip.winfo_exists():tip.destroy()

    def closed(self,event):
        if event.widget is self:self.hide_tooltip()


class WorkspaceTabStrip(tk.Frame):
    def __init__(self,parent,colors,saved,activate,close,new,changed):
        super().__init__(parent,bg=colors['bg'])
        self.c=colors;self.model=WorkspaceTabs(saved)
        self.activate,self.close_tab,self.new,self.changed=activate,close,new,changed
        self.widgets={};self.active='home';self.running=set();self._blink=None;self._phase=False
        self._press=None;self._drag=False;self._menu=None
        self._layout_job=None
        self.scale=max(1,float(self.tk.call('tk','scaling'))/(96/72))
        self.home=LandscapeButton(self,'Главная',lambda:self.activate('home'),colors)
        self.home.configure(font=('Segoe UI',9),padx=7,pady=5)
        self.home.pack(side=tk.LEFT,padx=(0,5))
        self.plus=LandscapeButton(self,'+',self.new,colors);self.plus.configure(padx=4,pady=5)
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
        if self._layout_job is None:self._layout_job=self.after_idle(self._layout)

    def _layout(self):
        self._layout_job=None
        count=len(self.model.entries);gap=round(3*self.scale);minimum=round(34*self.scale)
        available=max(1,self.canvas.winfo_width())
        full=available+(self.arrows.winfo_reqwidth()+6 if self.arrows.winfo_manager() else 0)
        overflow=count*minimum+max(0,count-1)*gap>full
        if overflow and not self.arrows.winfo_manager():self.arrows.pack(side=tk.RIGHT,before=self.canvas,padx=3)
        elif not overflow and self.arrows.winfo_manager():self.arrows.pack_forget()
        widths=tab_widths(available,count,minimum,round(232*self.scale),gap)
        for row,width in zip(self.model.entries,widths):
            tab=self.widgets[row['key']]
            if int(tab.cget('width'))!=width:tab.configure(width=width)
        height=round(36*self.scale)
        if int(self.canvas.cget('height'))!=height:self.canvas.configure(height=height)
        total=sum(widths)+max(0,count-1)*gap
        self.canvas.configure(scrollregion=(0,0,max(1,total),height))
        if total<=available:self.canvas.xview_moveto(0)

    def redraw(self):
        for row in self.model.entries:
            key=row['key']
            if key not in self.widgets:
                frame=BrowserTab(self.items,self.c,row['mode'],row['title'])
                self.widgets[key]=frame
                for widget in (frame,frame.dot,frame.label):
                    widget.bind('<ButtonPress-1>',lambda event,k=key:self.press(event,k))
                    widget.bind('<B1-Motion>',self.motion)
                    widget.bind('<ButtonRelease-1>',lambda event,k=key:self.release(event,k))
                    widget.bind('<Button-2>',lambda event,k=key:self.request_close(k))
                    widget.bind('<Button-3>',lambda event,k=key:self.context(event,k))
                frame.close.bind('<Button-1>',lambda event,k=key:self.request_close(k))
            self.widgets[key].title=row['title']
            self.widgets[key].pack_forget();self.widgets[key].pack(side=tk.LEFT,padx=(0,round(3*self.scale)),pady=(0,2))
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
            frame.set_state(item==key,item in self.running,self._phase)

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
        self.widgets[key].hide_tooltip()
        self._press=(key,event.x_root);self._drag=False
        return 'break'

    def motion(self,event):
        if self._press and abs(event.x_root-self._press[1])>6:
            self._drag=True
            self.widgets[self._press[0]].hide_tooltip()
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
        if key in self.widgets:self.widgets[key].hide_tooltip()
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
        if event.widget is self:
            if self._blink is not None:self.after_cancel(self._blink);self._blink=None
            if self._layout_job is not None:self.after_cancel(self._layout_job);self._layout_job=None
