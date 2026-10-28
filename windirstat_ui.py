"""A tab hosting the original portable WinDirStat in an independent process."""
import json
from pathlib import Path
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from windirstat_host import NativeHost, launch, prepare_executable, vendor_directory


class WinDirStatPanel(tk.Frame):
    running = False  # Disk scanning never locks unrelated console operations.

    def __init__(self, parent, app, colors):
        super().__init__(parent, bg=colors['bg'])
        self.app, self.c = app, colors
        self.process = self.native = self.hwnd = None
        self.starting = self.closing = False
        self.embedded = self.external = False
        self._disposed = self._startup_notice = False
        self._start_cancel = threading.Event()
        self._seen_dialogs = set()
        self._resume_at = self._close_deadline = 0.
        self._fit_at = 0.
        self._poll_job = self._resize_job = self._on_closed = None
        self.path = tk.StringVar()
        bar = tk.Frame(self, bg=colors['panel'], padx=12, pady=8)
        bar.pack(fill=tk.X, padx=22, pady=(0, 8))
        app._label(bar, 'WinDirStat', 12, bold=True).pack(side=tk.LEFT, padx=(0, 12))
        self.path_entry = app._entry(bar, self.path)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pick = app._button(bar, 'Папка…', self.choose_folder)
        self.pick.pack(side=tk.LEFT, padx=6)
        self.open_button = app._button(bar, 'Открыть WinDirStat', self.open, primary=True)
        self.open_button.pack(side=tk.LEFT)
        self.close_button = app._button(bar, 'Закрыть', self.close)
        self.close_button.pack(side=tk.LEFT, padx=6)
        app._button(bar, 'О программе', self.about).pack(side=tk.LEFT)
        self.state = app._label(self, 'Оригинальный WinDirStat 2.9.3. Выберите папку либо откройте штатный выбор дисков.', 9, 'muted', anchor=tk.W)
        self.state.pack(fill=tk.X, padx=22, pady=(0, 6))
        tools = tk.Frame(self, bg=colors['panel'])
        tools.pack(fill=tk.X, padx=22, pady=(0,6))
        self.native_pick = app._button(tools, 'Выбрать диск / папку…', lambda:self.native_command(32805))
        self.native_pick.pack(side=tk.LEFT)
        self.explorer = app._button(tools, 'Выделенное в Проводнике', lambda:self.native_command(32779))
        self.explorer.pack(side=tk.LEFT,padx=6)
        self.window_button = app._button(tools, 'Отдельное окно', self.toggle_window)
        self.window_button.pack(side=tk.LEFT)
        self.menu_bar = tk.Frame(self, bg=colors['panel'])
        self.menu_bar.pack(fill=tk.X, padx=22)
        self.host = tk.Frame(self, bg=colors['panel'], highlightthickness=1, highlightbackground=colors['border'])
        self.host.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        self.placeholder = app._label(self.host, 'Таблицы, карта диска и инструменты WinDirStat появятся здесь', 11, 'muted')
        self.placeholder.place(relx=.5, rely=.5, anchor=tk.CENTER)
        self.host.bind('<Configure>', self.schedule_resize)
        self.bind('<Destroy>', self.destroyed, add='+')
        self.update_buttons()

    def set_compact(self, compact):
        pass

    def show_mode(self, mode):
        self.schedule_resize()

    def status(self, text):
        self.state.configure(text=text)
        self.app._panel_status(self,text)

    def error(self, text):
        self.status(text)
        self.app._panel_error(self,text,title='WinDirStat')

    def choose_folder(self):
        value = filedialog.askdirectory(parent=self.app.root, title='Папка или диск для WinDirStat')
        if value:
            self.path.set(value)

    def update_buttons(self):
        active = self.starting or self.process is not None
        self.open_button.configure(state=tk.DISABLED if active else tk.NORMAL)
        self.pick.configure(state=tk.DISABLED if active else tk.NORMAL)
        self.path_entry.configure(state=tk.DISABLED if active else tk.NORMAL)
        self.close_button.configure(state=tk.NORMAL if active and not self.closing else tk.DISABLED)
        available = self.hwnd and self.native and not self.closing and not self.starting
        ready = available and (self.embedded or self.external) and not self._seen_dialogs
        for button in (self.native_pick,self.explorer):
            button.configure(state=tk.NORMAL if ready else tk.DISABLED)
        self.window_button.configure(state=tk.NORMAL if available else tk.DISABLED)
        self.window_button.configure(text='Вернуть во вкладку' if self.external else 'Отдельное окно')

    def open(self):
        if self.starting or self.process is not None:
            return
        value = self.path.get().strip()
        if value and not Path(value).is_dir():
            self.error('Укажите существующую папку или диск')
            return
        self.app._clear_panel_error(self)
        self._start_cancel = cancel = threading.Event()
        self._startup_notice = False
        self.external = self.embedded = False
        self._seen_dialogs.clear()
        self.starting = True
        self.status('Запуск WinDirStat. Если путь пуст, выберите диски в его окне.')
        self.update_buttons()
        def worker():
            try:
                executable = prepare_executable()
                if cancel.is_set():
                    self.app._post(self.started,None,'Запуск отменён')
                    return
                process = launch(executable, value)
                if cancel.is_set():
                    process.terminate()
                    self.app._post(self.started,process,None)
                    return
                self.app._post(self.started, process, None)
            except Exception as exc:
                self.app._post(self.started, None, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def started(self, process, error):
        if self._disposed:
            if process is not None and process.poll() is None:
                try:process.terminate()
                except OSError:pass
            return
        self.starting = False
        if error:
            if self._start_cancel.is_set():self.status('Запуск WinDirStat отменён')
            else:self.error('Не удалось запустить WinDirStat: ' + error)
            self.closing = False
            if self._on_closed:
                callback, self._on_closed = self._on_closed, None
                self.after_idle(callback)
        else:
            self.process = process
            try:self.native = NativeHost()
            except Exception as exc:
                process.terminate()
                self.error('Не удалось подготовить окно WinDirStat: ' + str(exc))
                self.closing = True
            self._deadline = time.monotonic() + 30
            self.poll()
        self.update_buttons()

    def poll(self):
        self._poll_job = None
        if self.process is None:
            return
        code = self.process.poll()
        if code is not None:
            self.process, self.hwnd, self.native = None, None, None
            self.closing = False
            self.embedded = self.external = False
            self._seen_dialogs.clear()
            for widget in self.menu_bar.winfo_children():
                widget.destroy()
            self.placeholder.place(relx=.5, rely=.5, anchor=tk.CENTER)
            if code and not self._start_cancel.is_set():self.error('WinDirStat завершился с кодом ' + str(code) + '. Можно повторить запуск.')
            else:self.status('WinDirStat закрыт')
            self.update_buttons()
            if self._on_closed:
                callback, self._on_closed = self._on_closed, None
                self.after_idle(callback)
            return
        try:
            self.poll_window()
        except Exception as exc:
            try:self.detach()
            except Exception:pass
            self.external = not self.embedded
            self.error('Ошибка окна WinDirStat: ' + str(exc) + '. Можно закрыть WinDirStat и повторить запуск.')
        if self.closing and time.monotonic() >= self._close_deadline:
            try:self.process.terminate()
            except OSError:pass
            self._close_deadline = time.monotonic() + 5
        self.update_buttons()
        self._poll_job = self.after(250, self.poll)

    def poll_window(self):
        if self.native is None:return
        if self.hwnd and not self.native.dll.IsWindow(self.hwnd):
            self.hwnd = None
            self.embedded = False
            self.clear_menus()
        if not self.hwnd:self.hwnd = self.native.find_window(self.process.pid)
        dialogs = set(self.native.dialogs(self.process.pid))
        if self.closing:
            for dialog in dialogs:self.native.close(dialog)
            if self.hwnd:self.native.close(self.hwnd)
            return
        if dialogs:
            if self.embedded:self.detach()
            for dialog in dialogs-self._seen_dialogs:self.native.show_dialog(dialog,self.hwnd)
            if dialogs-self._seen_dialogs:self.status('Выберите параметры в окне WinDirStat. Остальные вкладки консоли доступны.')
            self._seen_dialogs = dialogs
            self._deadline = time.monotonic()+30
            self._resume_at = time.monotonic()+.75
            return
        self._seen_dialogs.clear()
        if self.embedded and self.host.winfo_ismapped() and time.monotonic() >= self._fit_at:
            self._fit_at = time.monotonic()+1
            self.native.fit_columns(self.hwnd,self.process.pid)
        if self.hwnd and not self.embedded and not self.external and self.host.winfo_ismapped() and time.monotonic() >= self._resume_at:
            if self.native.ready(self.hwnd):
                try:
                    self.native.attach(self.hwnd,self.host.winfo_id())
                except Exception as exc:
                    self.external = True
                    self.native.detach(self.hwnd)
                    self.error('WinDirStat открыт отдельным окном: ' + str(exc))
                    return
                self.embedded = True
                self.placeholder.place_forget()
                self.build_menus()
                self.schedule_resize()
                if self._startup_notice:
                    self.app._clear_panel_error(self)
                    self._startup_notice = False
                self.status('WinDirStat работает внутри вкладки. Другие разделы консоли доступны.')
                return
        if not self.embedded and not self.external and time.monotonic()>self._deadline and not self._startup_notice:
            self._startup_notice = True
            self.error('WinDirStat не подготовил окно за 30 секунд. Нажмите «Отдельное окно» либо «Закрыть» и повторите запуск.')

    def clear_menus(self):
        for widget in self.menu_bar.winfo_children():widget.destroy()

    def detach(self):
        if self.hwnd and self.native:
            self.native.detach(self.hwnd)
            self.embedded = False
            self._resume_at = time.monotonic()+.75
            self.clear_menus()
            self.placeholder.configure(text='WinDirStat открыт отдельным окном')
            self.placeholder.place(relx=.5,rely=.5,anchor=tk.CENTER)

    def toggle_window(self):
        if not self.hwnd or not self.native or self.closing:return
        if self.external:
            self.external = False
            self._resume_at = 0
            self._deadline = time.monotonic()+30
            self.status('Возврат WinDirStat во вкладку…')
        else:
            try:self.detach()
            except Exception as exc:
                self.error('Не удалось открыть отдельное окно WinDirStat: '+str(exc))
                return
            self.external = True
            self.status('WinDirStat открыт отдельным окном. Сканирование продолжается.')
        self.update_buttons()

    def native_command(self,command):
        if not self.hwnd or not self.native or self.closing:return
        try:
            if self._seen_dialogs or not self.native.ready(self.hwnd):
                self.status('Дождитесь готовности WinDirStat или закройте его диалог.')
                return
            # Keep native shell/file dialogs in WinDirStat's own window/thread.
            if command in (32805,32779):self.detach()
            self.native.command(self.hwnd,command)
        except Exception as exc:self.error(str(exc))

    def popup(self,button,index,menu):
        try:
            command=self.native.popup(self.hwnd,self.host.winfo_id(),index,menu,
                                      button.winfo_rootx(),button.winfo_rooty()+button.winfo_height())
            if command:self.native_command(command)
        except Exception as exc:self.error('Не удалось открыть меню WinDirStat: '+str(exc))

    def build_menus(self):
        self.clear_menus()
        for index, title, menu in self.native.menus(self.hwnd):
            button = tk.Button(self.menu_bar, text=title, relief=tk.FLAT, bd=0, padx=8, pady=4,
                               bg=self.c['panel'], fg=self.c['fg'], activebackground=self.c['selected'], font=('Segoe UI', 9))
            button.configure(command=lambda b=button, i=index, m=menu:self.popup(b,i,m))
            button.pack(side=tk.LEFT)

    def schedule_resize(self, event=None):
        if self._disposed:return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(40, self.resize)

    def resize(self):
        self._resize_job = None
        if self.embedded and self.hwnd and self.native and self.host.winfo_ismapped() and self.host.winfo_width()>1 and self.host.winfo_height()>1:
            self.native.resize(self.hwnd, self.host.winfo_width(), self.host.winfo_height())
            self._fit_at = 0.

    def close(self, on_closed=None):
        if on_closed:
            self._on_closed = on_closed
        if not self.starting and self.process is None:
            self._on_closed = None
            return True
        self.closing = True
        self._start_cancel.set()
        self._close_deadline = time.monotonic()+8
        if self.hwnd:
            try:self.native.close(self.hwnd)
            except OSError:pass
        self.update_buttons()
        self.status('Закрытие WinDirStat…')
        return False

    def destroyed(self, event):
        if event.widget is self:
            self._disposed = True
            self._start_cancel.set()
            for job in (self._poll_job, self._resize_job):
                if job:
                    self.after_cancel(job)
            if self.hwnd and self.native:
                self.native.close(self.hwnd)

    def about(self):
        root = vendor_directory()
        window = tk.Toplevel(self.app.root)
        window.title('WinDirStat · лицензия и исходный код')
        window.geometry('760x520')
        window.transient(self.app.root)
        text = tk.Text(window, wrap=tk.WORD, font=('Segoe UI', 10), padx=12, pady=12)
        text.pack(fill=tk.BOTH, expand=True)
        text.insert('1.0', (root / 'NOTICE.txt').read_text(encoding='utf-8') + '\n\n' + (root / 'LICENSE.txt').read_text(encoding='utf-8'))
        text.configure(state=tk.DISABLED)
        def save_sources():
            manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            path = filedialog.asksaveasfilename(parent=window, title='Сохранить исходники WinDirStat', initialfile=manifest['source_file'], defaultextension='.zip', filetypes=[('ZIP', '*.zip')])
            if path:
                try:
                    shutil.copyfile(root / manifest['source_file'], path)
                except OSError as exc:
                    messagebox.showerror('WinDirStat', str(exc), parent=window)
        self.app._button(window, 'Сохранить исходники WinDirStat', save_sources).pack(side=tk.LEFT, padx=12, pady=8)
        self.app._button(window, 'Закрыть', window.destroy).pack(side=tk.RIGHT, padx=12, pady=8)
