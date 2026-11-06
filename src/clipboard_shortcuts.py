"""Standard entry shortcuts also work with a non-Latin keyboard layout."""
import tkinter as tk


# Keep native widget errors in Tcl. Tk's bindings deliberately catch missing
# selections and invalid indices; forwarding them through a Python command
# leaves a pending _tkinter exception that can abort the next mainloop call.
ENTRY_PROXY = r'''
if {[lindex $args 0] ni {insert delete}} {
    tailcall $native {*}$args
}
$before
set code [catch {$native {*}$args} result options]
if {[llength [info commands $after]]} {
    $after $code
}
return -options $options $result
'''


class EntryUndo:
    """Undo for Tk/ttk entries, including paste/cut and editable comboboxes."""
    def __init__(self, widget):
        self.widget = widget
        self.undo, self.redo = [], []
        self.pending, self.job, self.editing = None, None, 0
        self.before, self.alive = None, True
        self.original = widget._w + '_ka_original'
        widget.tk.call('namespace', 'eval', '::ka_edit', '')
        if not widget.tk.call('info', 'commands', '::ka_edit::entry'):
            widget.tk.call('proc', '::ka_edit::entry', ('native', 'before', 'after', 'args'), ENTRY_PROXY)
        self.before_command = widget.register(self.before_edit)
        self.after_command = widget.register(self.after_edit)
        widget.tk.call('rename', widget._w, self.original)
        widget.tk.call('interp', 'alias', '', widget._w, '', '::ka_edit::entry', self.original,
                       self.before_command, self.after_command)
        self.variable = widget.cget('textvariable')
        self.trace = None
        if self.variable:
            # Do not wrap an existing variable in StringVar: its destructor
            # would unset the application's original variable.
            self.trace = widget.register(self.external_change)
            widget.tk.call('trace', 'add', 'variable', self.variable, 'write', self.trace)
        widget.bind('<<Undo>>', lambda event: self.restore(False))
        widget.bind('<<Redo>>', lambda event: self.restore(True))
        widget.bind('<Destroy>', self.destroy, add='+')

    def state(self):
        return str(self.widget.tk.call(self.original, 'get')), int(self.widget.tk.call(self.original, 'index', 'insert'))

    def external_change(self, *args):
        if not self.editing:
            self.clear()

    def clear(self):
        if self.job:
            self.widget.after_cancel(self.job)
        self.pending, self.job = None, None
        self.undo.clear()
        self.redo.clear()

    def before_edit(self):
        if not self.editing:
            self.before = self.state()
        self.editing += 1

    def after_edit(self, code):
        self.editing -= 1
        if self.alive and not self.editing and self.state()[0] != self.before[0]:
            if self.pending is None:
                self.pending = self.before
                self.job = self.widget.after_idle(self.commit)

    def commit(self):
        if self.job:
            self.widget.after_cancel(self.job)
        self.job = None
        if self.pending is not None and self.pending[0] != self.state()[0]:
            self.undo.append(self.pending)
            # Bound per-field memory even for unusually large pasted values.
            while len(self.undo) > 100 or (len(self.undo) > 1 and sum(len(row[0]) for row in self.undo) > 1024 * 1024):
                self.undo.pop(0)
            self.redo.clear()
        self.pending = None

    def restore(self, redo):
        if str(self.widget.cget('state')) != 'normal':
            return 'break'
        self.commit()
        source, destination = (self.redo, self.undo) if redo else (self.undo, self.redo)
        if source:
            destination.append(self.state())
            value, cursor = source.pop()
            self.editing += 1
            try:
                self.widget.tk.call(self.original, 'delete', 0, 'end')
                self.widget.tk.call(self.original, 'insert', 0, value)
                self.widget.tk.call(self.original, 'icursor', cursor)
            finally:
                self.editing -= 1
        return 'break'

    def destroy(self, event):
        if event.widget is not self.widget:
            return
        self.alive = False
        self.clear()
        if self.trace:
            self.widget.tk.call('trace', 'remove', 'variable', self.variable, 'write', self.trace)
            self.widget.deletecommand(self.trace)
        self.widget._ka_undo = None
        self.widget.tk.deletecommand(self.widget._w)
        self.widget.deletecommand(self.before_command)
        self.widget.deletecommand(self.after_command)
        # The native widget command is already removed by Tk during destruction.
        if self.widget.tk.call('info', 'commands', self.original):
            self.widget.tk.call('rename', self.original, '')


def install_edit_shortcuts(root):
    """Include dynamically created dialogs and editable comboboxes on first map."""
    def mapped(event):
        # Native ttk dropdown/popdown widgets have no Python wrapper.
        if not isinstance(event.widget, tk.Misc):
            return
        if isinstance(event.widget, (tk.Entry, tk.Text)) or event.widget.winfo_class() in ('TEntry', 'TCombobox', 'Spinbox', 'TSpinbox'):
            bind_entry_shortcuts(event.widget)
    root.bind_all('<Map>', mapped, add='+')


def bind_entry_shortcuts(widget):
    if isinstance(widget, tk.Misc):
        if getattr(widget, '_ka_shortcuts', False):
            return
        widget._ka_shortcuts = True
        if isinstance(widget, tk.Text):
            if str(widget.cget('state')) == 'normal':
                widget.configure(undo=True, maxundo=100, autoseparators=True)
        elif str(widget.cget('state')) == 'normal':
            widget._ka_undo = EntryUndo(widget)

    def shortcut(event):
        # Tk's Latin keysym bindings can miss Ctrl+V on a Russian Windows
        # layout. Windows keycodes here are the physical virtual-key codes.
        if event.state & (0x8 | 0x20000):  # Do not intercept AltGr / Ctrl+Alt.
            return
        action = {65: 'SelectAll', 67: 'Copy', 88: 'Cut', 86: 'Paste', 90: 'Undo', 89: 'Redo'}.get(event.keycode)
        if action is None:
            action = {'a': 'SelectAll', 'c': 'Copy', 'x': 'Cut', 'v': 'Paste',
                      'cyrillic_ef': 'SelectAll', 'cyrillic_es': 'Copy',
                      'cyrillic_che': 'Cut', 'cyrillic_em': 'Paste',
                      'z': 'Undo', 'y': 'Redo', 'cyrillic_ya': 'Undo', 'cyrillic_en': 'Redo'}.get(event.keysym.casefold())
        if action == 'Undo' and event.state & 1:
            action = 'Redo'
        if action:
            widget.event_generate('<<' + action + '>>')
            return 'break'  # Prevent a second paste from the Entry class binding.
    widget.bind('<Control-KeyPress>', shortcut, add='+')
