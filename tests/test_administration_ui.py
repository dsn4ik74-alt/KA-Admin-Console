import unittest
from unittest.mock import patch

import test_service_ui
from sql_server import restore_plan
from test_sql_server import HEADER, FILES, TARGET


class AdministrationUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def load_database(self):
        panel = self.app.sql
        self.app._switch_mode('db_backup')
        panel.loaded(panel.connection(), ([dict(name='Target', recovery_model_desc='FULL')], dict(machine='REMOTE', server='REMOTE')))
        panel.tree.selection_set(panel.tree.get_children()[0])
        return panel

    def test_navigation_keeps_existing_layout_and_new_panels_independent(self):
        with patch('administration_ui.query') as query, patch('administration_ui.archive_logs') as archive:
            for mode in ('db_backup', 'accounts', 'jr_archive', 'db_restore', 'service_delete', 'servers'):
                self.app._switch_mode(mode)
                self.assertEqual(self.app.mode, mode)
                if mode == 'db_restore':
                    self.assertEqual(self.app.sql.options.winfo_manager(), '')
                if mode not in ('servers',):
                    self.assertEqual(self.app._source_panel.winfo_manager(), '')
            query.assert_not_called()
            archive.assert_not_called()

    def test_remote_picker_never_reuses_client_drive_path_and_can_cancel(self):
        panel = self.load_database()
        panel.server.set('REMOTE')
        # Editing endpoint invalidates an already loaded list.
        self.assertEqual(panel.tree.get_children(), ())
        self.assertIsNone(panel.connected)
        panel = self.load_database()
        panel.server_path.set(r'D:\previous')
        with patch('administration_ui.filedialog.askdirectory', return_value='D:/Backups') as dialog:
            panel.choose_path()
        dialog.assert_called_once()
        self.assertEqual(panel.server_path.get(), '')
        self.assertEqual(panel.client_path.get(), 'D:/Backups')
        with patch('administration_ui.filedialog.askdirectory', return_value='//REMOTE/Backup'):
            panel.choose_path()
        self.assertEqual(panel.server_path.get(), r'\\REMOTE\Backup')
        self.app._switch_mode('db_restore')
        self.assertEqual(panel.server_path.get(), '')
        with patch('administration_ui.filedialog.askopenfilename', return_value=''):
            panel.choose_path()
        self.assertEqual(panel.server_path.get(), '')

    def test_row_click_and_keyboard_selection_do_not_open_picker(self):
        panel = self.load_database()
        self.app.root.deiconify()
        for mode, dialog_name in (('db_backup', 'askdirectory'), ('db_restore', 'askopenfilename')):
            with self.subTest(mode=mode), patch('administration_ui.filedialog.' + dialog_name, return_value='') as dialog:
                self.app._switch_mode(mode)
                self.app.root.update()
                row = panel.tree.get_children()[0]
                x, y, width, height = panel.tree.bbox(row, '#1')
                panel.tree.event_generate('<ButtonPress-1>', x=x+10, y=y+height//2)
                panel.tree.event_generate('<ButtonRelease-1>', x=x+10, y=y+height//2)
                panel.tree.focus_set()
                panel.tree.event_generate('<KeyPress-Down>')
                self.app.root.update()
                dialog.assert_not_called()
                self.assertEqual(panel.selected(), 'Target')
                panel.picker_button.invoke()
                dialog.assert_called_once()

    def test_size_columns_remain_correct_when_filtering(self):
        panel = self.load_database()
        panel.databases = [dict(name='Target', recovery_model_desc='FULL', data_bytes=2 * 1024**4,
                                log_bytes=256 * 1024**2, total_bytes=2 * 1024**4 + 256 * 1024**2),
                           dict(name='Unknown', recovery_model_desc='SIMPLE')]
        panel.render()
        first, second = panel.tree.get_children()
        self.assertEqual(panel.tree.item(first, 'values')[1:4], ('2 ТБ', '256 МБ', '2 ТБ'))
        self.assertEqual(panel.tree.item(second, 'values')[1:4], ('—', '—', '—'))
        panel.filter_var.set('target')
        self.assertEqual(len(panel.tree.get_children()), 1)
        self.assertEqual(panel.selected(), 'Target')

    def test_selected_database_updates_cards_without_opening_dialog(self):
        panel = self.load_database()
        panel.databases = [dict(name='Target', recovery_model_desc='FULL', data_bytes=2 * 1024**3,
                                log_bytes=256 * 1024**2, total_bytes=2304 * 1024**2),
                           dict(name='Other', recovery_model_desc='SIMPLE')]
        panel.render()
        with patch('administration_ui.filedialog.askdirectory') as picker:
            self.app.root.update()
            self.assertEqual(panel.metric_values['data_bytes'].get(), '2 ГБ')
            self.assertEqual(panel.metric_values['log_bytes'].get(), '256 МБ')
            self.assertEqual(panel.metric_values['total_bytes'].get(), '2,25 ГБ')
            self.assertEqual(panel.metric_values['count'].get(), '2 / 2')
            panel.filter_var.set('other')
            panel.tree.selection_set(panel.tree.get_children()[0])
            self.app.root.update()
            self.assertEqual(panel.metric_values['count'].get(), '1 / 2')
            self.assertEqual(panel.metric_values['data_bytes'].get(), '—')
            self.assertIn('Other', panel.selected_label.cget('text'))
            picker.assert_not_called()

    def test_restore_preflight_cancel_does_not_execute_mutations(self):
        panel = self.load_database()
        self.app._switch_mode('db_restore')
        panel.server_path.set(r'D:\Full.bak')
        plan = restore_plan('Target', r'D:\Full.bak', HEADER, FILES, TARGET)
        with patch('administration_ui.threading.Thread') as thread, \
                patch('administration_ui.inspect_restore', return_value=plan) as inspect, \
                patch('administration_ui.query') as mutate, \
                patch('administration_ui.messagebox.askyesno', return_value=False) as confirm:
            panel.execute()
            self.assertFalse(panel.running)
            self.assertTrue(self.app.bookmarks.enabled)
            self.app._switch_mode('accounts')
            self.assertEqual(self.app.mode, 'accounts')
            thread.assert_not_called()
            inspect.assert_not_called()
            self.app._switch_mode('db_restore')
            self.app.root.update()
            confirm.assert_called_once()
            self.assertNotIn('100%', self.app.progress_pct.cget('text'))
            mutate.assert_not_called()
        self.assertFalse(panel.running)
        self.assertTrue(self.app.bookmarks.enabled)
        self.assertIn('Целевая база не изменялась', panel.output.get('1.0', 'end-1c'))

    def test_restore_confirmation_then_failure_unlocks_and_reports_error(self):
        panel = self.load_database()
        self.app._switch_mode('db_restore')
        panel.server_path.set(r'D:\Full.bak')
        plan = restore_plan('Target', r'D:\Full.bak', HEADER, FILES, TARGET)
        with patch('administration_ui.threading.Thread') as thread, \
                patch('administration_ui.inspect_restore', return_value=plan), \
                patch('administration_ui.query', side_effect=RuntimeError('SQL restore failed')) as execute, \
                patch('administration_ui.messagebox.askyesno', return_value=True):
            panel.execute()
            thread.call_args.kwargs['target']()
            self.app._drain_events()
            self.assertTrue(panel.running)
            thread.call_args.kwargs['target']()
            self.app._drain_events()
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[1][0]['sql'], plan.sql)
        self.assertFalse(panel.running)
        self.assertIn('SQL restore failed', panel.output.get('1.0', 'end-1c'))
        self.assertNotIn('База Target восстановлена', panel.output.get('1.0', 'end-1c'))

    def test_backup_failure_keeps_own_buttons_and_parameters(self):
        panel = self.load_database()
        panel.server_path.set(r'D:\Backups')
        with patch('administration_ui.threading.Thread') as thread, \
                patch('administration_ui.query', side_effect=RuntimeError('Backup access denied')) as execute:
            panel.execute()
            self.assertFalse(panel.start())
            self.assertTrue(self.app.archive.start())
            self.app.archive.finish('Independent task finished')
            thread.call_args.kwargs['target']()
            self.app._drain_events()
            execute.assert_called_once()
        self.assertFalse(panel.running)
        self.assertEqual(panel.server_path.get(), r'D:\Backups')
        panel.copy_results()
        self.assertIn('Backup access denied', self.app.root.clipboard_get())
        panel.clear_results()
        self.assertEqual(panel.output.get('1.0', 'end-1c'), '')


if __name__ == '__main__':
    unittest.main()
