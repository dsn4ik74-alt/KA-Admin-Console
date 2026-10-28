import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

import test_service_ui


class PostgresUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_wheel_scrolls_form_without_changing_any_dropdown(self):
        panel = self.load_databases()
        self.app.root.deiconify()
        self.app.root.geometry('960x540+20+20')
        self.app.root.update()
        canvas = panel.form.master
        dropdowns = [widget for widget in panel.inputs if isinstance(widget, ttk.Combobox)]
        for widget in dropdowns:
            with self.subTest(widget=str(widget)):
                value = widget.get()
                for modifiers in ('', 'Shift-'):
                    for delta in (-120, 120, -120):
                        widget.event_generate('<' + modifiers + 'MouseWheel>', delta=delta)
                        self.app.root.update()
                        self.assertEqual(widget.get(), value)
        canvas.yview_moveto(0)
        before = canvas.yview()
        dropdowns[0].event_generate('<MouseWheel>', delta=-120)
        self.app.root.update()
        self.assertGreater(canvas.yview()[0], before[0])
        self.assertEqual(panel.connected, panel.connection_key())

    def test_dropdown_still_commits_selection_with_left_click(self):
        panel = self.load_databases()
        self.app.root.deiconify()
        self.app.root.update()
        combo = panel.format_widget
        combo.tk.call('ttk::combobox::Post', str(combo))
        self.app.root.update()
        listing = str(combo) + '.popdown.f.l'
        x, y, width, height = combo.tk.call(listing, 'bbox', 1)
        combo.tk.call('event', 'generate', listing, '<ButtonPress-1>', '-x', x+5, '-y', y+height//2)
        combo.tk.call('event', 'generate', listing, '<ButtonRelease-1>', '-x', x+5, '-y', y+height//2)
        self.app.root.update()
        self.assertEqual(panel.format.get(), 'SQL gzip .sql.gz · psql')

    def load_databases(self, mode='pg_dump'):
        self.app._switch_mode(mode)
        panel = self.app.postgres
        panel.loaded(panel.connection_key(), [dict(name='One', size_bytes=1048576, owner='postgres'),
                                             dict(name='Two', size_bytes=2 * 1024**3, owner='postgres')])
        panel.tree.selection_set(panel.tree.get_children()[0])
        return panel

    def test_selection_never_opens_explorer_and_backup_multiselect(self):
        panel = self.load_databases()
        with patch('postgres_ui.filedialog.askdirectory') as picker, patch('postgres_ui.run_job') as job:
            panel.tree.selection_set(panel.tree.get_children())
            self.app.root.update()
            self.assertEqual(panel.selected(), ['One', 'Two'])
            picker.assert_not_called()
            job.assert_not_called()
        self.assertEqual(panel.tree.item(panel.tree.get_children()[0], 'values')[1], '1 МБ')
        self.app._switch_mode('pg_restore')
        self.assertEqual(len(panel.selected()), 1)
        self.assertEqual(str(panel.tree.cget('selectmode')), 'browse')
        self.app._switch_mode('ibcmd_dump')
        self.assertEqual(str(panel.tree.cget('selectmode')), 'browse')

    def test_local_picker_and_remote_path_are_separate(self):
        panel = self.load_databases()
        with patch('postgres_ui.filedialog.askdirectory', return_value='C:/Backups') as picker:
            panel.choose_path()
        picker.assert_called_once()
        self.assertEqual(panel.path.get(), 'C:/Backups')
        panel.execution.set('SSH · Linux')
        self.assertEqual(panel.path.get(), '')
        self.assertIsNone(panel.connected)
        self.assertEqual(panel.tree.get_children(), ())
        self.assertEqual(panel.ssh_form.winfo_manager(), 'pack')
        self.assertTrue(panel.tool_values['ibcmd'].get().startswith('/opt/'))
        panel.path.set('/BACKUP')
        with patch('postgres_ui.filedialog.askdirectory') as picker:
            panel.choose_path()
        picker.assert_not_called()
        self.assertEqual(panel.path.get(), '/BACKUP')
        panel.execution.set('Локально · Windows')
        self.assertEqual(panel.ssh_form.winfo_manager(), '')
        self.assertEqual(panel.tool_values['psql'].get(), panel._tool_sets['local']['psql'])

    def test_each_mode_keeps_its_path_and_log_and_clear_only_current(self):
        panel = self.load_databases()
        for mode in ('pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore'):
            self.app._switch_mode(mode)
            panel.path.set(mode + '-path')
            panel.append(mode + '-log')
        for mode in ('pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore'):
            self.app._switch_mode(mode)
            self.assertEqual(panel.path.get(), mode + '-path')
            self.assertIn(mode + '-log', panel.output.get('1.0', 'end-1c'))
            self.assertEqual(panel.ib_options.winfo_manager(), 'pack' if mode.startswith('ibcmd') else '')
        panel.clear_results()
        self.assertEqual(panel.output.get('1.0', 'end-1c'), '')
        self.app._switch_mode('pg_dump')
        self.assertIn('pg_dump-log', panel.output.get('1.0', 'end-1c'))

    def test_restore_cancel_and_ibcmd_exclusive_guard_never_start_job(self):
        panel = self.load_databases('pg_restore')
        panel.path.set('C:/file.dump')
        with patch('postgres_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'start') as start:
            panel.execute()
        confirm.assert_called_once()
        start.assert_not_called()
        self.app._switch_mode('ibcmd_restore')
        panel.path.set('C:/file.dt')
        with patch('postgres_ui.messagebox.askyesno') as confirm, patch.object(panel, 'start') as start:
            panel.execute()
        confirm.assert_not_called()
        start.assert_not_called()

    def test_backup_execution_captures_settings_and_disables_during_work(self):
        panel = self.load_databases()
        panel.path.set('C:/Backups')
        panel.password.set('test-password')
        self.assertIsNone(panel.connected)
        panel = self.load_databases()
        panel.tree.selection_set(panel.tree.get_children())
        with patch.object(panel, 'work') as work:
            panel.execute()
        self.assertTrue(panel.running)
        self.assertEqual(panel.execute_button.cget('state'), 'disabled')
        self.assertEqual(panel.execution.get(), 'Локально · Windows')
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', return_value=['C:/Backups/file.dump']) as job:
            self.assertEqual(operation(Mock()), ['C:/Backups/file.dump'])
        payload = job.call_args.args[0]
        self.assertEqual(payload['databases'], ['One', 'Two'])
        self.assertEqual(payload['connection']['password'], 'test-password')
        self.assertNotIn('test-password', panel.output.get('1.0', 'end-1c'))
        success(['C:/Backups/file.dump'])
        self.assertFalse(panel.running)
        self.assertEqual(str(panel.format_widget.cget('state')), 'readonly')

    def test_close_waits_for_postgres_job_but_navigation_remains_available(self):
        panel = self.load_databases()
        panel.running = True
        with patch.object(self.app.root, 'destroy') as destroy, patch.object(self.app, '_save_settings') as save:
            self.app._on_close()
            self.app._switch_mode('accounts')
        destroy.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.app.mode, 'accounts')
        panel.running = False

    def test_auto_clients_match_server_and_manual_paths_are_preserved(self):
        panel = self.load_databases()
        panel.bundled = {major: {name: f'C:/embedded/{major}/{name}.exe' for name in ('psql', 'pg_dump', 'pg_restore')}
                         for major in (14, 16, 17, 18)}
        panel.client_set.set('Автоматически')
        for major in (14, 16, 17, 18):
            panel.loaded(panel.connection_key(), [dict(name='One', owner='postgres', server_version=major * 10000)])
            self.assertEqual(panel.tool_values['pg_dump'].get(), f'C:/embedded/{major}/pg_dump.exe')
            self.assertEqual(panel.client_set.get(), 'Автоматически')
            self.assertEqual(panel.connected, panel.connection_key())
            self.assertEqual(len(panel.tree.get_children()), 1)
            self.assertEqual(len(panel.connection()['restore_candidates']), 4)
        panel.tool_values['pg_dump'].set('C:/my-tools/pg_dump.exe')
        self.assertEqual(panel.client_set.get(), 'Свои пути')
        self.assertNotIn('restore_candidates', panel.connection())
        panel.loaded(panel.connection_key(), [dict(name='One', owner='postgres', server_version=160015)])
        self.assertEqual(panel.tool_values['pg_dump'].get(), 'C:/my-tools/pg_dump.exe')
        panel.client_set.set('Встроенные PostgreSQL 16')
        self.assertEqual(panel.tool_values['pg_dump'].get(), 'C:/embedded/16/pg_dump.exe')
        panel.execution.set('SSH · Linux')
        self.assertEqual(panel.tool_values['pg_dump'].get(), 'pg_dump')
        self.assertNotIn('restore_candidates', panel.connection())
        self.assertEqual(panel.client_form.winfo_manager(), '')

    def test_restores_log_area_can_be_resized_by_dragging_sash(self):
        self.app.root.deiconify()
        self.app.root.geometry('1380x860+20+20')
        for mode in ('db_backup', 'db_restore', 'pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore'):
            with self.subTest(mode=mode):
                self.app._switch_mode(mode)
                panel = self.app.sql if mode.startswith('db_') else self.app.postgres
                self.app.root.update()
                split = panel.log_split
                split.sash_place(0, 0, split.winfo_height() // 2)
                self.app.root.update()
                x, y = split.sash_coord(0)
                before = panel.output.winfo_height()
                # Native Tk hit testing identifies the sash/handle, and real events drag it.
                drag_x = split.winfo_width() // 2
                split.event_generate('<ButtonPress-1>', x=drag_x, y=y+3)
                split.event_generate('<B1-Motion>', x=drag_x, y=max(70, y-70))
                split.event_generate('<ButtonRelease-1>', x=drag_x, y=max(70, y-70))
                self.app.root.update()
                self.assertGreater(panel.output.winfo_height(), before)
                self.assertTrue(panel.execute_button.winfo_ismapped())


if __name__ == '__main__':
    unittest.main()
