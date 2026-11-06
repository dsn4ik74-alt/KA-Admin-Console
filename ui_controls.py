"""Desktop controls with native Tk editing and commands.

Only borders, icons and button surfaces are drawn. Entry selection, keyboard
editing, accessibility names, focus, disabled state and commands remain native.
No graphics libraries, web engine or clipboard calls are needed at runtime.
"""
import math
import struct
import tkinter as tk
import tkinter.font as tkfont
import zlib
from collections import OrderedDict

_ROUNDED_BYTES=OrderedDict()


def _rgb(value):
    value=value.lstrip('#')
    return tuple(int(value[i:i+2],16) for i in (0,2,4))


def _png(width,height,rows):
    def chunk(name,data):
        return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data))
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,6,0,0,0))+
            chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+row for row in rows)))+chunk(b'IEND',b''))


def rounded_image(master,width,height,fill,border=None,radius=9,shared=False):
    width,height=max(2,int(width)),max(2,int(height))
    radius=min(radius,width/2,height/2)
    key=(width,height,fill,border,radius)
    root=master.winfo_toplevel()
    if shared:
        photos=getattr(root,'_ka_surfaces',None)
        if photos is None:photos=root._ka_surfaces=OrderedDict()
        if key in photos:
            photos.move_to_end(key);return photos[key]
    if key in _ROUNDED_BYTES:
        _ROUNDED_BYTES.move_to_end(key)
        result=tk.PhotoImage(master=master,data=_ROUNDED_BYTES[key])
        if shared:
            photos[key]=result
            while len(photos)>64:photos.popitem(last=False)
        return result
    inside=_rgb(fill);edge=_rgb(border or fill)
    def pixel(x,y):
        dx=max(radius-x-.5,0);dy=max(radius-y-.5,0)
        distance=math.hypot(dx,dy)-radius
        alpha=min(1,max(0,.5-distance))
        blend=min(1,max(0,1.5+distance)) if border else 0
        return bytes((*[round(a*(1-blend)+b*blend) for a,b in zip(inside,edge)],round(alpha*255)))
    rows=[];corner=min(math.ceil(radius)+1,width//2)
    for y in range(height):
        mirrored=min(y,height-1-y)
        left=b''.join(pixel(x,mirrored) for x in range(corner))
        middle=pixel(corner,mirrored)*(width-corner*2)
        right=b''.join(left[i:i+4] for i in range(len(left)-4,-1,-4))
        rows.append(left+middle+right)
    data=_png(width,height,rows);_ROUNDED_BYTES[key]=data
    while len(_ROUNDED_BYTES)>128:_ROUNDED_BYTES.popitem(last=False)
    result=tk.PhotoImage(master=master,data=data)
    if shared:
        photos[key]=result
        while len(photos)>64:photos.popitem(last=False)
    return result


# Every icon uses the same 24-unit grid and line weight. No font glyph substitutes.
ICONS={
    'home': [[(3,11),(12,3),(21,11)],[(6,10),(6,21),(10,21),(10,15),(14,15),(14,21),(18,21),(18,10)]],
    'server': [[(4,3),(20,3),(20,10),(4,10),(4,3)],[(4,14),(20,14),(20,21),(4,21),(4,14)],[(7,6),(8,6)],[(7,17),(8,17)],[(12,6),(17,6)],[(12,17),(17,17)]],
    'database': [[(4,6),(4,18)],[(20,6),(20,18)],[(4,12),(7,15),(17,15),(20,12)]],
    'refresh': [[(20,9),(20,3)],[(20,9),(14,9)],[(4,15),(4,21)],[(4,15),(10,15)],[(20,8),(17,4),(11,3),(6,5),(3,10)],[(4,16),(7,20),(13,21),(18,19),(21,14)]],
    'search': [[(16,16),(21,21)]],
    'plus': [[(12,4),(12,20)],[(4,12),(20,12)]],
    'close': [[(6,6),(18,18)],[(6,18),(18,6)]],
    'check': [[(4,12),(9,17),(20,6)]],
    'copy': [[(9,8),(20,8),(20,21),(9,21),(9,8)],[(15,4),(4,4),(4,16)]],
    'trash': [[(3,6),(21,6)],[(9,6),(9,3),(15,3),(15,6)],[(6,6),(7,21),(17,21),(18,6)],[(10,10),(10,17)],[(14,10),(14,17)]],
    'folder': [[(3,7),(3,4),(10,4),(13,7),(21,7),(21,20),(3,20),(3,7)],[(3,9),(21,9)]],
    'download': [[(12,3),(12,15)],[(7,10),(12,15),(17,10)],[(4,16),(4,21),(20,21),(20,16)]],
    'upload': [[(12,17),(12,5)],[(7,10),(12,5),(17,10)],[(4,16),(4,21),(20,21),(20,16)]],
    'play': [[(7,4),(20,12),(7,20),(7,4)]],
    'stop': [[(6,6),(18,6),(18,18),(6,18),(6,6)]],
    'clock': [[(12,6),(12,12),(16,14)]],
    'log': [[(5,3),(19,3),(19,21),(5,21),(5,3)],[(8,7),(16,7)],[(8,11),(16,11)],[(8,15),(14,15)]],
    'user': [[(4,21),(4,18),(8,15),(16,15),(20,18),(20,21)]],
    'settings': [[(6,4),(6,20)],[(12,4),(12,20)],[(18,4),(18,20)],[(3,8),(9,8)],[(9,16),(15,16)],[(15,10),(21,10)]],
    'grid': [[(3,3),(9,3),(9,9),(3,9),(3,3)],[(15,3),(21,3),(21,9),(15,9),(15,3)],[(3,15),(9,15),(9,21),(3,21),(3,15)],[(15,15),(21,15),(21,21),(15,21),(15,15)]],
    'terminal': [[(3,4),(21,4),(21,20),(3,20),(3,4)],[(7,9),(10,12),(7,15)],[(13,15),(17,15)]],
    'star': [[(12,2),(15,8),(22,9),(17,14),(18,21),(12,18),(6,21),(7,14),(2,9),(9,8),(12,2)]],
    'arrow': [[(4,12),(20,12)],[(14,6),(20,12),(14,18)]],
    'chevron': [[(8,4),(16,12),(8,20)]],
    'down': [[(6,9),(12,15),(18,9)]],
    'shield': [[(12,2),(21,6),(20,15),(16,20),(12,23),(8,20),(4,15),(3,6),(12,2)],[(8,12),(11,15),(16,9)]],
    'link': [[(9,8),(13,4),(18,4),(21,7),(21,11),(17,15)],[(7,9),(3,13),(3,17),(6,20),(10,20),(14,16)],[(8,15),(16,7)]],
}
_ICON_BYTES={}


def icon(master,name,size=18,color='#748086'):
    root=master.winfo_toplevel()
    cache=getattr(root,'_ka_icons',None)
    if cache is None:cache=root._ka_icons={}
    key=(name,size,color)
    if key in cache:return cache[key]
    if key in _ICON_BYTES:
        result=tk.PhotoImage(master=master,data=_ICON_BYTES[key]);cache[key]=result;return result
    size=max(8,int(size));scale=3;side=size*scale;rgb=_rgb(color);paths=ICONS.get(name,ICONS['grid'])
    segments=[((a[0]*side/24,a[1]*side/24),(b[0]*side/24,b[1]*side/24)) for p in paths for a,b in zip(p,p[1:])]
    circles={'search':[(10,10,7)],'clock':[(12,12,9)],'user':[(12,7,4)],'database':[(12,6,8,3),(12,18,8,3)]}.get(name,[])
    radius=.8*side/24
    def covered(x,y):
        for (ax,ay),(bx,by) in segments:
            length=(bx-ax)**2+(by-ay)**2
            t=max(0,min(1,((x-ax)*(bx-ax)+(y-ay)*(by-ay))/max(1,length)))
            if (x-ax-t*(bx-ax))**2+(y-ay-t*(by-ay))**2<=radius**2:return True
        for circle in circles:
            cx,cy,rx=circle[:3];ry=circle[3] if len(circle)>3 else rx
            d=math.hypot((x-cx*side/24)/(rx*side/24),(y-cy*side/24)/(ry*side/24))
            if abs(d-1)<=.8/min(rx,ry):return True
        return False
    rows=[]
    for y in range(size):
        row=bytearray()
        for x in range(size):
            count=sum(covered(x*scale+sx+.5,y*scale+sy+.5) for sy in range(scale) for sx in range(scale))
            row.extend((*rgb,round(count*255/(scale*scale))))
        rows.append(bytes(row))
    data=_png(size,size,rows);_ICON_BYTES[key]=data
    result=tk.PhotoImage(master=master,data=data);cache[key]=result
    return result


def action_icon(text):
    text=str(text).lower()
    if text.startswith('+'):return 'plus'
    for words,name in ((('прервать','останов'), 'stop'), (('удал','очист'), 'trash'),
                       (('копир',), 'copy'), (('обнов','сброс'), 'refresh'),
                       (('создат','добав','новая'), 'plus'), (('избран',), 'star'),
                       (('истор','расписан','планиров'), 'clock'), (('парамет','настрой','вид '), 'settings'),
                       (('журнал','подроб'), 'log'), (('проводник','папк','файл'), 'folder'),
                       (('скачат','сохран','бэкап','выгруз'), 'download'), (('восстанов','загруз','установ'), 'upload'),
                       (('подключ','ssh'), 'link'), (('провер','выбрат','готово'), 'check'),
                       (('главн',), 'home'), (('откры','запуст'), 'play')):
        if any(word in text for word in words):return name
    return None


class LandscapeButton(tk.Button):
    """A real Button with a DPI-aware rounded surface and native text rendering."""
    def __init__(self,parent,text,command,colors,primary=False):
        self._ready=False;self.c=colors;self.primary=primary;self._logical_text=text
        self._padx,self._pady=11,6;self._logical_bg=colors['accent' if primary else 'button']
        self._allocated=None;self._pending=None;self._last_picture=None
        self._metrics_key=None;self._metrics=None
        self._disabled_cover=None
        super().__init__(parent,text='',command=command,font=('Segoe UI',10),compound=tk.CENTER,
                         relief=tk.FLAT,bd=0,padx=0,pady=0,bg=parent.cget('bg'),activebackground=parent.cget('bg'),
                         highlightthickness=1,highlightbackground=parent.cget('bg'),highlightcolor='#91a6af',
                         fg=colors['fg'],disabledforeground='#a3aaae',cursor='hand2',takefocus=True)
        self._ready=True
        self._base_bg=self._logical_bg;self._hover_bg=colors['hover' if primary else 'button_hover']
        self.bind('<Configure>',self._resized,add='+')
        self.bind('<Enter>',lambda e:self.configure(bg=self._hover_bg) if str(self.cget('state'))!='disabled' else None)
        self.bind('<Leave>',lambda e:self.configure(bg=self._base_bg))
        self.bind('<Destroy>',self._closed,add='+')
        self._refresh()

    def cget(self,key):
        if self._ready:
            if key=='text':return self._logical_text
            if key in ('bg','background'):return self._logical_bg
            if key=='padx':return self._padx
            if key=='pady':return self._pady
        return super().cget(key)

    def configure(self,cnf=None,**kw):
        if not getattr(self,'_ready',False):return super().configure(cnf,**kw)
        if cnf is not None:
            if not isinstance(cnf,dict):return super().configure(cnf,**kw)
            kw={**cnf,**kw}
        if not kw:return super().configure()
        for key,attr in (('text','_logical_text'),('padx','_padx'),('pady','_pady'),('bg','_logical_bg'),('background','_logical_bg')):
            if key in kw:setattr(self,attr,kw.pop(key))
        # The surface and its focus outline share the actual parent's background.
        for key in ('activebackground','highlightbackground'):kw.pop(key,None)
        if kw:super().configure(**kw)
        self._refresh()

    config=configure

    def _refresh(self):
        if not self.winfo_exists():return
        font_name=super().cget('font');scaling=float(self.tk.call('tk','scaling'))
        metrics_key=(font_name,scaling,self._logical_text,self._padx,self._pady)
        if metrics_key!=self._metrics_key:
            font=tkfont.Font(root=self,font=font_name);scale=scaling/(96/72)
            size=max(14,round(17*scale));name=action_icon(self._logical_text)
            prefix=' '*max(1,math.ceil((size+6)/max(1,font.measure(' ')))) if name else ''
            display=prefix+self._logical_text.lstrip('☆⌂↻＋+ ').strip() if name else self._logical_text
            width=font.measure(display)+round(float(self._padx)*2*scale)+4
            height=font.metrics('linespace')+round(float(self._pady)*2*scale)+3
            self._metrics=(font,size,name,display,width,height)
            self._metrics_key=metrics_key
            super().configure(text=display,width=width,height=height)
        font,size,name,display,width,height=self._metrics
        actual_width,actual_height=self._allocated or (width,height)
        actual_width=max(2,actual_width);actual_height=max(2,actual_height)
        disabled=str(super().cget('state'))=='disabled'
        fill=self._logical_bg
        if self.primary:
            danger=self._base_bg==self.c['danger']
            fill=('#f9e5e5' if fill==self._base_bg else '#f2d5d7') if danger else '#f5dc61' if fill==self._base_bg else '#efd35a'
            if disabled:fill='#f7eeee' if danger else '#fbf4cd'
            fg=(self.c['danger'] if danger else '#49421f') if not disabled else '#aaa58d'
            super().configure(fg=fg,activeforeground=fg)
        else:
            fg=super().cget('fg') if not disabled else '#a7afb3'
        outline=None if self.primary else '#e5e8e9'
        picture_key=(actual_width,actual_height,fill,outline,name,fg,size)
        if picture_key!=self._last_picture:
            self._picture=rounded_image(self,actual_width,actual_height,fill,outline,radius=actual_height/2)
            if name:
                asset=icon(self,name,size,fg)
                # Icon and native text form one centered group, including fill-X buttons.
                x=max(8,(actual_width-font.measure(display))//2-1)
                self.tk.call(self._picture,'copy',asset,'-to',x,max(0,(actual_height-size)//2),'-compositingrule','overlay')
            super().configure(image=self._picture)
            self._last_picture=picture_key
        if disabled:
            # Tk 8.6 stipples native image buttons when disabled. Cover only its
            # drawing; the real Button stays disabled, including invoke/keyboard.
            if self._disabled_cover is None:
                self._disabled_cover=tk.Label(self,bd=0,highlightthickness=0,compound=tk.CENTER,takefocus=False)
            self._disabled_cover.configure(image=self._picture,text=display,font=super().cget('font'),
                                           fg=fg,bg=self.master.cget('bg'))
            self._disabled_cover.place(x=1,y=1,relwidth=1,width=-2,relheight=1,height=-2)
        elif self._disabled_cover is not None:self._disabled_cover.place_forget()

    def _resized(self,event):
        size=(max(2,event.width-2),max(2,event.height-2))
        if size!=self._allocated:
            self._allocated=size
            if self._pending is None:self._pending=self.after_idle(self._redraw)

    def _redraw(self):
        self._pending=None;self._refresh()

    def _closed(self,event):
        if event.widget is self and self._pending:
            self.after_cancel(self._pending);self._pending=None


def field_parent(widget):
    shell=getattr(widget,'_field_shell',None)
    return shell.master if shell is not None else widget.master


def geometry_widget(widget):
    return getattr(widget,'_field_shell',widget)


def reveal_field(widget):
    parent=widget.master
    while parent is not None:
        show=getattr(parent,'_open_for_field',None)
        if show:show()
        parent=parent.master


class LandscapeEntry(tk.Entry):
    """Native Entry inside a rounded shell; geometry methods target its shell."""
    def __init__(self,parent,variable,colors,width=None,show=None):
        self._ready=False;self.c=colors;self._pending=None;self._destroying=False;self._variable=variable
        self._hint_trace=None;self._hint_label=None;self._search_icon=None
        self._field_shell=tk.Canvas(parent,bg=parent.cget('bg'),bd=0,highlightthickness=0,width=180,height=34)
        self._field_shell._field_shell=True
        self._border=colors['border'];self._focus_border='#7d9ba9';self._focused=False
        self._drawn=None;self._image_item=self._field_shell.create_image(0,0,anchor='nw')
        super().__init__(self._field_shell,textvariable=variable,width=width,show=show,font=('Segoe UI',10),
                         bg=colors['panel'],fg=colors['fg'],insertbackground=colors['fg'],relief='flat',bd=0,
                         highlightthickness=0,selectbackground=colors['text_selected'],selectforeground=colors['fg'],
                         disabledbackground='#f4f5f5',disabledforeground=colors['muted'],readonlybackground=colors['panel'])
        self._ready=True
        font=tkfont.Font(root=self,font=self.cget('font'));height=font.metrics('linespace')+15
        self._field_shell.configure(width=self.winfo_reqwidth()+18,height=height)
        tk.Place.place_configure(self,x=9,y=7,relwidth=1,width=-18,relheight=1,height=-14)
        self._field_shell.bind('<Configure>',self._resized,add='+')
        self.bind('<FocusIn>',lambda e:self._focus(True),add='+')
        self.bind('<FocusOut>',lambda e:self._focus(False),add='+')
        self._field_shell.bind('<Button-1>',lambda e:self.focus_set())
        self._field_shell.bind('<Destroy>',self._closed,add='+')

    def _focus(self,value):self._focused=value;self._render();self._hint_changed()

    def _resized(self,event):
        if self._pending is None:self._pending=self.after_idle(self._render)

    def _render(self):
        self._pending=None
        if not self._field_shell.winfo_exists():return
        width,height=self._field_shell.winfo_width(),self._field_shell.winfo_height()
        state=str(super().cget('state'))
        fill=super().cget('disabledbackground' if state=='disabled' else 'readonlybackground' if state=='readonly' else 'bg')
        edge=self._focus_border if self._focused else self._border
        key=(width,height,fill,edge)
        if key!=self._drawn:
            self._picture=rounded_image(self._field_shell,width,height,fill,edge,7,shared=True)
            self._field_shell.itemconfigure(self._image_item,image=self._picture);self._drawn=key

    def set_hint(self,text,search=True):
        """Visual placeholder; it is never inserted into the actual value."""
        if self._hint_label is None:
            self._hint_label=tk.Label(self._field_shell,bg=self.c['panel'],fg='#9aa3a7',font=self.cget('font'),anchor='w',bd=0)
            self._hint_label.bind('<Button-1>',lambda e:self.focus_set())
            self._hint_trace=self._variable.trace_add('write',lambda *a:self._hint_changed())
        self._hint_label.configure(text=text)
        if search and self._search_icon is None:
            self._search_icon=icon(self,'search',17,'#939da2')
            self._field_shell.create_image(11,8,image=self._search_icon,anchor='nw')
            tk.Place.place_configure(self,x=35,width=-44)
        self._hint_changed()

    def _hint_changed(self):
        if self._hint_label is None:return
        if self._variable.get() or self._focused:self._hint_label.place_forget()
        else:self._hint_label.place(x=35 if self._search_icon else 10,y=7,relwidth=1,width=-48 if self._search_icon else -20)

    def configure(self,cnf=None,**kw):
        if not getattr(self,'_ready',False):return super().configure(cnf,**kw)
        if cnf is not None:
            if not isinstance(cnf,dict):return super().configure(cnf,**kw)
            kw={**cnf,**kw}
        if not kw:return super().configure()
        for key,attr in (('highlightbackground','_border'),('highlightcolor','_focus_border')):
            if key in kw:setattr(self,attr,kw.pop(key))
        kw.pop('highlightthickness',None);kw.pop('bd',None);kw.pop('borderwidth',None)
        if kw:super().configure(**kw)
        self._render()

    config=configure

    def cget(self,key):
        if getattr(self,'_ready',False) and key in ('highlightbackground','highlightcolor'):
            return self._border if key=='highlightbackground' else self._focus_border
        return super().cget(key)

    def focus_set(self):reveal_field(self);return super().focus_set()
    def pack_configure(self,*args,**kw):return self._field_shell.pack_configure(*args,**kw)
    pack=pack_configure
    def grid_configure(self,*args,**kw):return self._field_shell.grid_configure(*args,**kw)
    grid=grid_configure
    def place_configure(self,*args,**kw):return self._field_shell.place_configure(*args,**kw)
    place=place_configure
    def pack_forget(self):return self._field_shell.pack_forget()
    forget=pack_forget
    def grid_forget(self):return self._field_shell.grid_forget()
    def grid_remove(self):return self._field_shell.grid_remove()
    def place_forget(self):return self._field_shell.place_forget()
    def pack_info(self):return self._field_shell.pack_info()
    def grid_info(self):return self._field_shell.grid_info()
    def place_info(self):return self._field_shell.place_info()
    def winfo_manager(self):return self._field_shell.winfo_manager()

    def _closed(self,event):
        if event.widget is self._field_shell and self._pending:
            self.after_cancel(self._pending);self._pending=None

    def destroy(self):
        if self._destroying:return super().destroy()
        self._destroying=True
        if self._hint_trace:
            try:self._variable.trace_remove('write',self._hint_trace)
            except tk.TclError:pass
            self._hint_trace=None
        if self._field_shell.winfo_exists():self._field_shell.destroy()


class ActionFlow:
    """Wrap footer actions into real rows before any button can be clipped."""
    def __init__(self,bar):
        self.bar=bar;self.pending=None;self.signature=None;self.original=None;self.rows=[];self.layout_key=None
        bar.bind('<Configure>',self.queue,add='+');bar.bind('<Map>',self.queue,add='+')
        bar.bind('<Destroy>',self.closed,add='+')
        for child in bar.winfo_children():
            if isinstance(child,tk.Button):child.bind('<Configure>',self.queue,add='+')

    def queue(self,event=None):
        if self.pending is None:self.pending=self.bar.after_idle(self.layout)

    def layout(self):
        self.pending=None
        if not self.bar.winfo_exists() or not self.bar.winfo_ismapped():return
        if self.original is None:self.original=[(widget,widget.pack_info()) for widget in self.bar.pack_slaves()]
        buttons=[w for w,p in self.original if isinstance(w,tk.Button)]
        width=max(1,self.bar.winfo_width()-int(self.bar.cget('padx'))*2)
        sizes=tuple(w.winfo_reqwidth() for w in buttons)
        signature=(width,sizes)
        if signature==self.signature:return
        self.signature=signature
        labels=[w for w,p in self.original if not isinstance(w,tk.Button)]
        wrapped=sum(sizes)+8*len(buttons)+(100 if labels else 0)>width
        if not wrapped and self.layout_key==('single',):return
        left=[w for w,p in self.original if isinstance(w,tk.Button) and str(p.get('side'))=='left']
        right=[w for w,p in self.original if isinstance(w,tk.Button) and w not in left]
        ordered=left+list(reversed(right));groups=[];current=[];used=0
        for button in ordered:
            request=button.winfo_reqwidth()+8
            if current and used+request>width:groups.append(current);current=[];used=0
            current.append(button);used+=request
        if current:groups.append(current)
        layout_key=tuple(tuple(str(w) for w in group) for group in groups) if wrapped else ('single',)
        if layout_key==self.layout_key:return
        self.layout_key=layout_key
        for widget,p in self.original:widget.pack_forget()
        for row in self.rows:row.destroy()
        self.rows=[]
        if not wrapped:
            for widget,p in self.original:widget.pack(**p)
            return
        # The application's persistent status bar already shows this message.
        # Reserve the compact footer's height for commands rather than a duplicate row.
        for group in groups:
            row=tk.Frame(self.bar,bg=self.bar.cget('bg'));row.pack(fill=tk.X,pady=2);self.rows.append(row)
            for button in reversed(group):button.pack(in_=row,side=tk.RIGHT,padx=(8,0))
        # Pack-in rows are descendants of the buttons' native parent. Keep the
        # actual controls above those row windows, otherwise they intercept clicks.
        for button in buttons:button.lift()

    def closed(self,event):
        if event.widget is self.bar and self.pending:
            self.bar.after_cancel(self.pending);self.pending=None


def install_action_flow(panel):
    bar=getattr(panel,'actions',None)
    if bar is None:bar=getattr(panel,'action_bar',None)
    if bar is not None and not hasattr(bar,'_ka_action_flow'):bar._ka_action_flow=ActionFlow(bar)


def decorate_search_fields(panel):
    fields=[getattr(panel,name,None) for name in ('filter_entry','search_entry','base_search_entry')]
    fields.extend(getattr(panel,'search_entries',{}).values())
    for field in fields:
        if hasattr(field,'set_hint'):field.set_hint('Поиск по названию…')


def install_native_controls(root,colors):
    """Style native checkbox state images and combobox elements once per root."""
    from tkinter import ttk
    style=ttk.Style(root)
    style.configure('TCombobox',padding=(9,3,8,3),arrowsize=14,borderwidth=0)
    root.option_add('*TCombobox*Listbox.background','#3d4245')
    root.option_add('*TCombobox*Listbox.foreground','#ffffff')
    root.option_add('*TCombobox*Listbox.selectBackground','#59646a')
    root.option_add('*TCombobox*Listbox.selectForeground','#ffffff')
    if getattr(root,'_ka_native_controls',False):return
    root._ka_native_controls=True
    switch_off=rounded_image(root,32,18,'#ffffff','#b5bec2',9)
    switch_on=rounded_image(root,32,18,'#708e9a','#708e9a',9)
    for picture,x,fill in ((switch_off,3,'#a7b0b4'),(switch_on,17,'#ffffff')):
        dot=rounded_image(root,12,12,fill,None,6)
        root.tk.call(picture,'copy',dot,'-to',x,3,'-compositingrule','overlay')
    root._ka_control_images=[switch_off,switch_on]
    for name,value in (('indicatorOn',False),('image',str(switch_off)),('selectImage',str(switch_on)),
                       ('compound','left'),('offRelief','flat'),('overRelief','flat'),('padX',5),('padY',3)):
        root.option_add('*Checkbutton.'+name,value)
    normal=rounded_image(root,24,32,colors['panel'],colors['border'],7)
    focus=rounded_image(root,24,32,colors['panel'],'#7693a0',7)
    disabled=rounded_image(root,24,32,'#f4f5f5','#e4e7e8',7)
    arrow=icon(root,'down',14,'#899397')
    root._ka_control_images.extend((normal,focus,disabled,arrow))
    if 'KA.combo.field' not in style.element_names():
        style.element_create('KA.combo.field','image',normal,('disabled',disabled),('focus',focus),border=7,sticky='nsew')
        style.element_create('KA.combo.arrow','image',arrow,sticky='')
    style.layout('TCombobox',[('KA.combo.field',dict(sticky='nswe',children=[
        ('KA.combo.arrow',dict(side='right',sticky='ns')),
        ('Combobox.padding',dict(sticky='nswe',children=[('Combobox.textarea',dict(sticky='nswe'))]))]))])
