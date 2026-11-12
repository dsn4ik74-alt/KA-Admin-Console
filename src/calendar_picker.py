"""A small local-date picker; selecting a day changes only the supplied field."""
import calendar
from datetime import date
import tkinter as tk


MONTHS = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
          'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')


class CalendarPicker(tk.Toplevel):
    def __init__(self, field, variable, colors):
        super().__init__(field.winfo_toplevel())
        self.variable, self.colors = variable, colors
        try:
            selected = date.fromisoformat(variable.get().strip())
        except ValueError:
            selected = date.today()
        self.year, self.month = selected.year, selected.month
        self.title('Выберите дату')
        self.configure(bg=colors['panel'], padx=12, pady=10)
        self.resizable(False, False)
        self.transient(field.winfo_toplevel())
        self.bind('<Escape>', lambda event: self.destroy())
        header = tk.Frame(self, bg=colors['panel'])
        header.pack(fill=tk.X, pady=(0, 8))
        self.previous = self.button(header, '‹', lambda: self.shift_month(-1))
        self.previous.pack(side=tk.LEFT)
        self.title_label = tk.Label(header, bg=colors['panel'], fg=colors['fg'],
                                    font=('Segoe UI', 11, 'bold'), width=20)
        self.title_label.pack(side=tk.LEFT, expand=True)
        self.following = self.button(header, '›', lambda: self.shift_month(1))
        self.following.pack(side=tk.RIGHT)
        self.days = tk.Frame(self, bg=colors['panel'])
        self.days.pack(fill=tk.BOTH)
        footer = tk.Frame(self, bg=colors['panel'])
        footer.pack(fill=tk.X, pady=(8, 0))
        self.button(footer, 'Сегодня', lambda: self.choose(date.today())).pack(side=tk.LEFT)
        self.button(footer, 'Отмена', self.destroy).pack(side=tk.RIGHT)
        self.render()
        self.update_idletasks()
        x = max(0, min(field.winfo_rootx(), self.winfo_screenwidth()-self.winfo_reqwidth()))
        y = field.winfo_rooty()+field.winfo_height()+4
        if y+self.winfo_reqheight() > self.winfo_screenheight():
            y = max(0, field.winfo_rooty()-self.winfo_reqheight()-4)
        self.geometry('+'+str(x)+'+'+str(y))

    def button(self, parent, text, command, selected=False):
        c = self.colors
        return tk.Button(parent, text=text, command=command, font=('Segoe UI', 10),
            bg=c['accent'] if selected else c['button'],
            fg=c['accent_fg'] if selected else c['fg'],
            activebackground=c['hover'] if selected else c['button_hover'],
            activeforeground=c['accent_fg'] if selected else c['fg'],
            bd=0, padx=9, pady=5, relief=tk.FLAT, cursor='hand2')

    def render(self):
        for child in self.days.winfo_children():
            child.destroy()
        self.day_buttons = {}
        self.title_label.configure(text=MONTHS[self.month-1]+' '+str(self.year))
        self.previous.configure(state=tk.DISABLED if (self.year, self.month)==(1, 1) else tk.NORMAL)
        self.following.configure(state=tk.DISABLED if (self.year, self.month)==(9999, 12) else tk.NORMAL)
        for col, name in enumerate(('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')):
            tk.Label(self.days, text=name, bg=self.colors['panel'], fg=self.colors['muted'],
                     font=('Segoe UI', 9)).grid(row=0, column=col, padx=2, pady=(0, 5))
        for row, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(self.year, self.month), 1):
            for col, day in enumerate(week):
                if not day:
                    continue
                value = date(self.year, self.month, day)
                button = self.button(self.days, str(day), lambda value=value: self.choose(value),
                                     selected=value.isoformat()==self.variable.get().strip())
                button.grid(row=row, column=col, sticky='ew', padx=2, pady=2)
                self.day_buttons[day] = button

    def shift_month(self, offset):
        index = (self.year-1)*12+self.month-1+offset
        if 0 <= index < 9999*12:
            year, month = divmod(index, 12)
            self.year, self.month = year+1, month+1
            self.render()

    def choose(self, value):
        self.variable.set(value.isoformat())
        self.destroy()
