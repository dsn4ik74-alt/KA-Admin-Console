"""Compact, clickable indicators for independent background tab operations."""
import tkinter as tk


class RunningTasks(tk.Frame):
    def __init__(self, parent, colors, activate):
        super().__init__(parent, bg=colors['bg'])
        self.c, self.activate = colors, activate
        self.entries, self._job, self._phase = {}, None, False
        self.canvas = tk.Canvas(self, bg=colors['bg'], bd=0, highlightthickness=0, width=260, height=30)
        self.canvas.pack(fill=tk.X)
        self.items = tk.Frame(self.canvas, bg=colors['bg'])
        self.canvas.create_window(0, 0, anchor=tk.NW, window=self.items)
        self.items.bind('<Configure>', lambda event: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<MouseWheel>', lambda event: self.canvas.xview_scroll(-1 if event.delta > 0 else 1, 'units'))
        self.bind('<Destroy>', self.destroyed, add='+')

    def update_tasks(self, tasks):
        keys = {key for key, _, _ in tasks}
        for key in set(self.entries) - keys:
            self.entries.pop(key).destroy()
        for key, title, action in tasks:
            if key not in self.entries:
                frame = tk.Frame(self.items, bg=self.c['button'], padx=5, pady=4, cursor='hand2')
                frame.pack(side=tk.LEFT, padx=2)
                dot = tk.Label(frame, text='●', font=('Segoe UI', 8), bg=self.c['button'], fg='#16a34a')
                dot.pack(side=tk.LEFT, padx=(0, 4))
                label = tk.Label(frame, text=title[:25], font=('Segoe UI', 9), bg=self.c['button'], fg=self.c['fg'])
                label.pack(side=tk.LEFT)
                for widget in (frame, dot, label):
                    widget.bind('<Button-1>', lambda event, callback=action: callback())
                frame.dot = dot
                frame.title_label = label
                self.entries[key] = frame
            self.entries[key].title_label.configure(text=title[:25])
        if tasks:
            if not self.winfo_manager():
                self.pack(side=tk.RIGHT, padx=(0, 6))
            if self._job is None:
                self.blink()
        else:
            self.pack_forget()
            if self._job:
                self.after_cancel(self._job)
                self._job = None

    def blink(self):
        self._phase = not self._phase
        for entry in self.entries.values():
            entry.dot.configure(fg='#16a34a' if self._phase else '#b0dec0')
        self._job = self.after(650, self.blink)

    def destroyed(self, event):
        if event.widget is self and self._job:
            self.after_cancel(self._job)
            self._job = None
