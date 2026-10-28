import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from clipboard_shortcuts import bind_entry_shortcuts, install_edit_shortcuts
import test_service_ui


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        install_edit_shortcuts(self.root)
        self.addCleanup(self.root.destroy)

    def field(self, cls=tk.Entry, **kwargs):
        widget = cls(self.root, **kwargs)
        widget.pack()
        self.root.update()
        widget.focus_force()
        self.root.update()
        return widget

    def key(self, widget, code, shift=False):
        widget.event_generate('<KeyPress>', keycode=code, state=4 | int(shift))
        self.root.update()

    def test_paste_cut_undo_redo_and_selection_replacement_in_all_editable_entries(self):
        for cls in (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, ttk.Spinbox):
            widget = self.field(cls)
            widget.insert(0, 'Исходный сервер')
            self.root.update()
            widget.selection_range(0, 'end')
            self.root.clipboard_clear()
            self.root.clipboard_append('Новый сервер')
            self.key(widget, 86)
            self.assertEqual(widget.get(), 'Новый сервер', cls)
            self.key(widget, 90)
            self.assertEqual(widget.get(), 'Исходный сервер', cls)
            self.key(widget, 90, shift=True)
            self.assertEqual(widget.get(), 'Новый сервер', cls)
            widget.selection_range(0, 'end')
            self.key(widget, 88)
            self.assertEqual(widget.get(), '', cls)
            self.key(widget, 90)
            self.assertEqual(widget.get(), 'Новый сервер', cls)
            widget.destroy()

    def test_paste_into_empty_password_without_selection_does_not_escape_mainloop(self):
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(args)
        value = tk.StringVar()
        widget = self.field(textvariable=value, show='*')
        self.root.clipboard_clear()
        self.root.clipboard_append('fixture-only-Пароль!42')
        self.root.after(1, lambda: widget.event_generate('<KeyPress>', keycode=86, state=4))
        self.root.after(50, self.root.quit)
        self.root.mainloop()
        self.assertFalse(errors)
        self.assertEqual(value.get(), 'fixture-only-Пароль!42')
        self.key(widget, 90)
        self.assertEqual(value.get(), '')

    def test_native_tcl_catch_handles_missing_selection_and_invalid_indices(self):
        widget = self.field()
        # Tk's own paste/cut bindings use catch for absent selection. A Python
        # command proxy must not leave a pending exception in _tkinter.
        for operation in (('index', 'sel.first'), ('delete', 'sel.first', 'sel.last'),
                          ('insert', 'not-an-index', 'fixture')):
            self.assertEqual(self.root.tk.call('catch', (widget._w, *operation)), 1)
        widget.insert(0, 'Still usable')
        self.root.update()
        self.assertEqual(widget.get(), 'Still usable')
        self.root.after(1, self.root.quit)
        self.root.mainloop()  # Also detect a stale _tkinter exception after catch.

    def test_no_selection_empty_clipboard_and_readonly_shortcuts_in_native_loop(self):
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(args)
        for cls in (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, ttk.Spinbox):
            widget = self.field(cls)
            self.root.clipboard_clear()
            for code in (86, 67, 88, 90, 89):
                self.key(widget, code)
            widget.configure(state='readonly')
            self.root.clipboard_append('Must not change readonly')
            self.key(widget, 86)
            self.assertEqual(widget.get(), '')
            widget.destroy()
        self.root.after(1, self.root.quit)
        self.root.mainloop()
        self.assertFalse(errors)

    def test_new_connection_clears_undo_and_disabled_fields_cannot_change(self):
        value = tk.StringVar(value='Before')
        widget = self.field(textvariable=value, show='*')
        widget.insert('end', '-edited')
        self.root.update()
        value.set('Another connection')
        self.key(widget, 90)
        self.assertEqual(value.get(), 'Another connection')
        widget.insert('end', '-edited')
        self.root.update()
        widget.configure(state='disabled')
        self.key(widget, 90)
        self.assertEqual(value.get(), 'Another connection-edited')
        widget.configure(state='normal')
        self.key(widget, 90)
        self.assertEqual(value.get(), 'Another connection')

    def test_multiline_names_and_dynamically_opened_dialogs_support_undo(self):
        names = self.field(tk.Text)
        names.insert('1.0', 'Иванов Иван Иванович')
        names.edit_separator()
        names.insert('end', '\nПетров Пётр Петрович')
        self.root.update()
        self.key(names, 90)
        self.assertEqual(names.get('1.0', 'end-1c'), 'Иванов Иван Иванович')
        self.key(names, 89)
        self.assertIn('Петров', names.get('1.0', 'end-1c'))
        dialog = tk.Toplevel(self.root)
        entry = ttk.Entry(dialog)
        entry.pack()
        self.root.update()
        entry.insert(0, 'First')
        self.root.update()
        entry.insert('end', 'Second')
        self.root.update()
        entry.focus_force()
        self.key(entry, 90)
        self.assertEqual(entry.get(), 'First')
        dialog.destroy()

    def test_rebinding_is_idempotent_and_empty_undo_is_harmless(self):
        widget = self.field()
        binding = widget.bind('<Control-KeyPress>')
        bind_entry_shortcuts(widget)
        self.assertEqual(widget.bind('<Control-KeyPress>'), binding)
        self.key(widget, 90)
        self.assertEqual(widget.get(), '')

    def test_select_all_works_in_entries_passwords_combos_and_multiline_text(self):
        for cls, options in ((tk.Entry, {}), (tk.Entry, {'show': '*'}), (ttk.Entry, {}),
                             (ttk.Combobox, {}), (tk.Text, {})):
            widget = self.field(cls, **options)
            text = 'Сервер и база\nВторая строка' if cls is tk.Text else 'Сервер и база'
            widget.insert('1.0' if cls is tk.Text else 0, text)
            self.root.update()
            self.key(widget, 65)
            if cls is tk.Text:
                self.assertEqual(widget.get('sel.first', 'sel.last').rstrip('\n'), text)
            else:
                self.assertEqual(widget.get()[widget.index('sel.first'):widget.index('sel.last')], text)
            widget.destroy()

    def test_destroying_entry_does_not_unset_shared_application_variable(self):
        value = tk.StringVar(value='Keep-after-widget-destroy')
        widget = self.field(textvariable=value)
        widget.destroy()
        self.assertEqual(value.get(), 'Keep-after-widget-destroy')

    def test_native_combobox_dropdown_does_not_raise_map_callback_errors(self):
        errors = []
        self.root.report_callback_exception = lambda *args: errors.append(args)
        widget = self.field(ttk.Combobox, values=('First', 'Second'), state='readonly')
        widget.tk.call('ttk::combobox::Post', widget._w)
        self.root.update()
        widget.tk.call('ttk::combobox::Unpost', widget._w)
        self.assertFalse(errors)


class AppUndoTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_empty_password_paste_and_undo_work_in_all_application_forms(self):
        app = self.app
        errors = []
        app.root.report_callback_exception = lambda *args: errors.append(args)
        app.root.deiconify()
        for mode, panel in app._pages.items():
            if not hasattr(panel, 'form'):
                continue
            app._switch_mode(mode)
            app.root.update()
            fields = [widget for widget in panel.inputs if isinstance(widget, tk.Entry)
                      and widget.cget('show') and widget.winfo_ismapped()
                      and str(widget.cget('state')) == 'normal']
            for widget in fields:
                widget.delete(0, 'end')
                app.root.update()
                widget.selection_clear()
                app.root.clipboard_clear()
                app.root.clipboard_append('fixture-only-safe-pass')
                # Virtual paste exercises the same class binding even when a
                # long form has scrolled this particular field out of view.
                with patch.object(panel, 'work') as work:
                    widget.event_generate('<<Paste>>')
                    app.root.update()
                    self.assertEqual(widget.get(), 'fixture-only-safe-pass', mode)
                    widget.event_generate('<<Undo>>')
                    self.assertEqual(widget.get(), '', mode)
                    work.assert_not_called()
        app.root.after(1, app.root.quit)
        app.root.mainloop()
        self.assertFalse(errors)

    def test_real_sql_copy_server_field_can_undo_and_redo_paste(self):
        panel = self.app.sql_copy
        self.app._switch_mode('db_copy')
        self.app.root.deiconify()
        self.app.root.update()
        panel.server.set('First-server')
        entry = next(widget for widget in panel.inputs if isinstance(widget, tk.Entry)
                     and str(widget.cget('textvariable')) == str(panel.server))
        entry.focus_force()
        entry.selection_range(0, 'end')
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append('Second-server')
        for code, expected in ((86, 'Second-server'), (90, 'First-server'), (89, 'Second-server')):
            entry.event_generate('<KeyPress>', keycode=code, state=4)
            self.app.root.update()
            self.assertEqual(panel.server.get(), expected)
