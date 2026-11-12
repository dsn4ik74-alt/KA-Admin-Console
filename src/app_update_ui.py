"""Modeless update dialog. Network and patch reconstruction never run in Tk."""
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox,ttk

import app_update as updates
from sql_server import format_size


class UpdateWindow(tk.Toplevel):
    def __init__(self,app,current):
        super().__init__(app.root)
        self.app,self.current,self.release,self.stage=app,current,None,None
        self.busy=False;self.cancel=threading.Event();self.handed=False;self.closing=False
        self.title('Обновление консоли');self.geometry('650x500');self.minsize(500,380)
        self.configure(bg=app.colors['bg']);self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.bind('<Destroy>',self.destroyed,add='+')
        app._label(self,'Обновление приложения',17).pack(anchor='w',padx=22,pady=(20,8))
        app._label(self,'Установленная версия: '+current,10,'muted').pack(anchor='w',padx=22)
        self.status=app._label(self,'Нажмите «Проверить», чтобы узнать о новой версии.',10,anchor='w',justify=tk.LEFT)
        self.status.pack(fill=tk.X,padx=22,pady=12);self.status.bind('<Configure>',lambda e:self.status.configure(wraplength=max(200,e.width)))
        self.progress=ttk.Progressbar(self,mode='determinate');self.progress.pack(fill=tk.X,padx=22,pady=(0,12))
        bar=tk.Frame(self,bg=app.colors['bg']);bar.pack(side=tk.BOTTOM,fill=tk.X,padx=22,pady=18)
        self.action=app._button(bar,'Проверить',self.execute,primary=True);self.action.pack(side=tk.RIGHT)
        self.full=app._button(bar,'Полное обновление',lambda:self.fetch(full=True))
        self.cancel_button=app._button(bar,'Отмена',self.close);self.cancel_button.pack(side=tk.LEFT)
        self.notes=tk.Text(self,bg=app.colors['panel'],fg=app.colors['fg'],font=('Segoe UI',10),
                           wrap=tk.WORD,bd=0,padx=12,pady=12,state=tk.DISABLED)
        self.notes.pack(fill=tk.BOTH,expand=True,padx=22)

    def execute(self):
        if self.busy:return
        if self.stage:self.install();return
        if self.release:self.fetch();return
        self.work(updates.latest,self.checked,self.current)

    def work(self,operation,success,*args):
        self.busy=True;self.cancel.clear();self.action.configure(state=tk.DISABLED);self.full.configure(state=tk.DISABLED)
        self.status.configure(text='Проверка GitHub…' if self.release is None else 'Подготовка обновления…')
        def worker():
            try:result=operation(*args);self.app._post(self.completed,success,result,None)
            except Exception as exc:self.app._post(self.completed,success,None,exc)
        threading.Thread(target=worker,daemon=True).start()

    def completed(self,success,result,error):
        self.busy=False
        if not self.winfo_exists():
            if isinstance(result,dict) and 'folder' in result:updates.cleanup(result)
            return
        self.action.configure(state=tk.NORMAL);self.full.configure(state=tk.NORMAL)
        if self.closing:
            if isinstance(result,dict) and 'folder' in result:updates.cleanup(result)
            self.destroy();return
        if error:
            self.status.configure(text=str(error),fg=self.app.colors['muted'] if isinstance(error,updates.UpdateCancelled) else self.app.colors['danger'])
            self.action.configure(text='Повторить' if self.release is None else 'Скачать обновление')
            return
        self.status.configure(fg=self.app.colors['fg']);success(result)

    def checked(self,release):
        self.release=release
        if release is None:
            self.status.configure(text='Установлена актуальная версия.');self.action.configure(text='Проверить ещё раз');return
        asset=release['patch'] or release['full']
        self.status.configure(text='Доступна версия '+release['version']+'\n'+
            ('Патч: ' if release['patch'] else 'Подходящего патча нет. Полный ZIP: ')+format_size(asset['size']))
        self.notes.configure(state=tk.NORMAL);self.notes.delete('1.0',tk.END);self.notes.insert('1.0',release['notes']);self.notes.configure(state=tk.DISABLED)
        self.action.configure(text='Скачать патч' if release['patch'] else 'Скачать полный ZIP')
        if release['patch']:self.full.pack(side=tk.RIGHT,padx=8)

    def fetch(self,full=False):
        if self.busy or not self.release:return
        if not getattr(sys,'frozen',False):
            self.status.configure(text='Автоматическая замена доступна в готовом EXE. Исходники обновляются отдельно.');return
        asset=self.release['full'] if full else self.release['patch'] or self.release['full']
        if not asset['name'].endswith('.kaupdate') and not messagebox.askyesno('Скачать полное обновление?',
                'Будет скачан весь ZIP: '+format_size(asset['size'])+'. Продолжить?',parent=self,default='no'):return
        self.work(updates.stage_update,self.ready,self.release,asset,sys.executable,self.current,self.show_progress,self.cancel)

    def show_progress(self,title,done,total):
        self.app._post(self.progressed,title,done,total)

    def progressed(self,title,done,total):
        if self.winfo_exists() and not self.closing:
            percent=round(done*100/max(1,total));self.progress['value']=percent
            self.status.configure(text=title+': '+str(percent)+'% · '+format_size(done)+' / '+format_size(total))

    def ready(self,stage):
        self.stage=stage;self.full.pack_forget();self.progress['value']=100
        self.status.configure(text='Версия '+stage['version']+' собрана и проверена. Настройки и расписания сохранятся.')
        self.action.configure(text='Обновить и перезапустить')

    def install(self):
        app=self.app
        if app.search_running or app._discovering or app._opening_bases or any(
                getattr(p,'running',False) or getattr(p,'_saving',False) or getattr(p,'configuring_background',False)
                for p in set(app._pages.values())):
            self.status.configure(text='Завершите выполняющиеся задачи перед обновлением.',fg=app.colors['danger']);return
        if not messagebox.askyesno('Установить обновление?',
                'Консоль закроется, обновит EXE и откроется снова. Продолжить?',parent=self,default='no'):return
        try:
            scheduler=app.scheduler
            if scheduler.store.path.is_file():
                scheduler.ensure_store();heartbeat,name=scheduler.store.health()
                if name and time.time()-heartbeat.get('at',0)<20:
                    raise ValueError('Дождитесь завершения задания фонового планировщика.')
            app._on_close(before_exit=self.handoff)
        except Exception as exc:self.status.configure(text=str(exc),fg=app.colors['danger'])

    def handoff(self):
        store=self.app.scheduler.store
        resume=False
        if store.path.is_file():
            heartbeat,name=store.health()
            if name and time.time()-heartbeat.get('at',0)<20:raise ValueError('Планировщик начал задание. Обновите приложение после его завершения.')
            resume=time.time()-heartbeat.get('at',0)<20
            store.meta('update_until',time.time()+600);store.meta('stop',True)
        try:updates.launch_helper(self.stage,sys.executable,resume_scheduler=resume)
        except Exception:
            if store.path.is_file():store.meta('update_until',0);store.meta('stop',False)
            raise
        self.handed=True

    def close(self):
        if self.busy:
            self.closing=True;self.cancel.set();self.status.configure(text='Отмена…');return
        self.destroy()

    def destroyed(self,event):
        if event.widget is self and self.stage and not self.handed:
            updates.cleanup(self.stage);self.stage=None
