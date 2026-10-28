"""CRUD chooses the correct copy endpoint; refresh never clears its sibling."""
import unittest
from unittest.mock import Mock, patch

import test_database_copy as copies


class CopyManagementTests(unittest.TestCase):
    setUp = copies.CopyUiTests.setUp
    sql_panel = copies.CopyUiTests.sql_panel
    pg_panel = copies.CopyUiTests.pg_panel

    def test_sql_create_selects_new_target_and_preserves_source(self):
        panel = self.sql_panel()
        refreshed = ([*panel.databases, dict(name='NewTarget', recovery_model_desc='SIMPLE')], dict(machine='REMOTE'))
        with patch('database_copy_ui.ask_create_database', return_value=dict(name='NewTarget', owner='')), \
                patch.object(panel, 'work') as work:
            panel.create_db_button.invoke()
        operation, success = work.call_args.args
        with patch('database_copy_ui.create_database') as create, patch('database_copy_ui.list_databases', return_value=refreshed):
            result = operation(Mock())
        create.assert_called_once_with(panel.connection(), 'NewTarget', create.call_args.args[2])
        success(result)
        self.assertEqual(panel.selected(), 'Original')
        self.assertEqual(panel.destination.get(), 'NewTarget')
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'NewTarget')
        self.assertEqual(len(panel.tree.get_children()), 3)
        self.assertFalse(panel.running)

    def test_sql_delete_confirmation_identifies_correct_list_and_cancel_does_nothing(self):
        panel = self.sql_panel()
        for button, database, role in ((panel.delete_db_button, 'Original', 'Источник'),
                                       (panel.delete_target_button, 'Target', 'Получатель')):
            with self.subTest(database=database), patch('database_copy_ui.messagebox.askyesno', return_value=False) as confirm, \
                    patch.object(panel, 'work') as work:
                button.invoke()
            self.assertIn('База: ' + database, confirm.call_args.args[1])
            self.assertIn('Список: ' + role, confirm.call_args.args[1])
            self.assertIn(panel.server.get(), confirm.call_args.args[1])
            self.assertEqual(confirm.call_args.kwargs['default'], 'no')
            work.assert_not_called()

    def test_sql_delete_target_refresh_failure_removes_stale_target_without_retry(self):
        panel = self.sql_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.delete_target_button.invoke()
        operation, success = work.call_args.args
        with patch('database_copy_ui.delete_database') as delete, \
                patch('database_copy_ui.list_databases', side_effect=RuntimeError('read failed')):
            result = operation(Mock())
        self.assertEqual(delete.call_args.args[1], 'Target')
        self.assertEqual(delete.call_count, 1)
        success(result)
        self.assertEqual(panel.selected(), 'Original')
        self.assertEqual(panel.destination.get(), '')
        self.assertIsNone(panel.connected)
        self.assertEqual(len(panel.tree.get_children()), 1)
        self.assertEqual(len(panel.target_tree.get_children()), 1)
        self.assertIn('Повторять действие не нужно', panel.output.get('1.0', 'end-1c'))

    def test_pg_target_create_uses_target_credentials_and_keeps_source_selection(self):
        panel = self.pg_panel()
        panel.target_password.set('target-secret')
        panel.target_loaded(panel.target_key(), [dict(name='Target', owner='Owner')])
        with patch('database_copy_ui.ask_create_database', return_value=dict(name='NewTarget', owner='NewOwner')) as dialog, \
                patch.object(panel, 'work') as work:
            panel.target_create_button.invoke()
        self.assertIn('target-host', dialog.call_args.args[2])
        operation, success = work.call_args.args
        def backend(payload, event):
            if payload['mode'] == 'create':
                return payload['database']
            return [dict(name=name, owner='Owner') for name in
                    (('Target', 'NewTarget') if payload['connection']['host'] == 'target-host' else ('Original',))]
        with patch('database_copy_ui.run_job', side_effect=backend) as job:
            result = operation(Mock())
        create = job.call_args_list[0].args[0]
        self.assertEqual(create['mode'], 'create')
        self.assertEqual(create['connection']['host'], 'target-host')
        self.assertEqual(create['connection']['password'], 'target-secret')
        self.assertEqual(create['owner'], 'NewOwner')
        self.assertEqual(sum(call.args[0]['mode'] == 'create' for call in job.call_args_list), 1)
        success(result)
        self.assertEqual(panel.selected(), ['Original'])
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'NewTarget')
        self.assertEqual(panel.connected, panel.connection_key())
        self.assertEqual(panel.target_connected, panel.target_key())

    def test_pg_source_and_target_delete_confirmations_are_explicit(self):
        panel = self.pg_panel()
        for button, name, host in ((panel.delete_db_button, 'Original', 'source-host'),
                                   (panel.target_delete_button, 'Target', 'target-host')):
            with self.subTest(host=host), patch('database_copy_ui.messagebox.askyesno', return_value=False) as confirm, \
                    patch.object(panel, 'work') as work:
                button.invoke()
            self.assertIn(host + ':5432', confirm.call_args.args[1])
            self.assertIn('База: ' + name, confirm.call_args.args[1])
            self.assertEqual(confirm.call_args.kwargs['default'], 'no')
            work.assert_not_called()

    def test_pg_confirmed_delete_targets_one_server_and_refreshes_each_side_separately(self):
        panel = self.pg_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.target_delete_button.invoke()
        operation, success = work.call_args.args
        def backend(payload, event):
            if payload['mode'] == 'delete':
                return payload['database']
            return [dict(name='Original', owner='Owner')] if payload['connection']['host'] == 'source-host' else []
        with patch('database_copy_ui.run_job', side_effect=backend) as job:
            result = operation(Mock())
        deletes = [call.args[0] for call in job.call_args_list if call.args[0]['mode'] == 'delete']
        self.assertEqual(len(deletes), 1)
        self.assertEqual((deletes[0]['connection']['host'], deletes[0]['database']), ('target-host', 'Target'))
        success(result)
        self.assertEqual(panel.selected(), ['Original'])
        self.assertFalse(panel.target_tree.get_children())
        self.assertEqual(panel.target_connected, panel.target_key())

    def test_pg_failed_delete_does_not_refresh_or_report_success(self):
        panel = self.pg_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.delete_db_button.invoke()
        operation, _ = work.call_args.args
        with patch('database_copy_ui.run_job', side_effect=RuntimeError('Denied')) as job, self.assertRaises(RuntimeError):
            operation(Mock())
        self.assertEqual(job.call_count, 1)
        panel.finish(error='Denied')
        self.assertEqual(panel.selected(), ['Original'])
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'Target')

    def test_pg_lists_survive_connect_refresh_and_automatic_client_version_changes(self):
        panel = self.pg_panel()
        original = panel.tree.selection()
        with patch.object(panel, 'work') as work:
            panel.target_refresh_button.invoke()
        operation, success = work.call_args.args
        self.assertEqual(panel.tree.selection(), original)
        with patch('database_copy_ui.run_job', return_value=[dict(name='Target', owner='Owner')]) as job:
            rows = operation(Mock())
        self.assertEqual(job.call_args.args[0]['connection']['host'], 'target-host')
        success(rows)
        self.assertEqual(panel.tree.selection(), original)
        target_name = panel.target_tree.set(panel.target_tree.selection()[0], 'name')
        panel.bundled = {14: dict(psql='C:/14/psql.exe', pg_dump='C:/14/pg_dump.exe', pg_restore='C:/14/pg_restore.exe')}
        with patch.object(panel, 'work') as work:
            panel.source_refresh_button.invoke()
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', return_value=[dict(name='Original', owner='Owner', server_version=140003)]):
            rows = operation(Mock())
        success(rows)
        self.assertEqual(panel.selected(), ['Original'])
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), target_name)
        self.assertEqual(panel.target_connected, panel.target_key())
        self.assertEqual(panel.tool_values['psql'].get(), 'C:/14/psql.exe')

    def test_pg_endpoint_edits_invalidate_only_their_side_and_executor_changes_both(self):
        panel = self.pg_panel()
        panel.host.set('new-source-host')
        self.assertFalse(panel.tree.get_children())
        self.assertIsNone(panel.connected)
        self.assertTrue(panel.target_tree.get_children())
        self.assertEqual(panel.target_connected, panel.target_key())
        panel.loaded(panel.connection_key(), [dict(name='Original', owner='Owner')])
        panel.target_host.set('new-target-host')
        self.assertTrue(panel.tree.get_children())
        self.assertFalse(panel.target_tree.get_children())
        panel.target_loaded(panel.target_key(), [dict(name='Target', owner='Owner')])
        panel.execution.set('SSH · Linux')
        self.assertFalse(panel.tree.get_children())
        self.assertFalse(panel.target_tree.get_children())

    def test_actions_and_refresh_are_disabled_during_jobs(self):
        panel = self.pg_panel()
        panel.start()
        for button in (panel.create_db_button, panel.delete_db_button, panel.target_create_button,
                       panel.target_delete_button, panel.source_refresh_button, panel.target_refresh_button):
            self.assertEqual(str(button.cget('state')), 'disabled')
        panel.finish('Done')
        for button in (panel.create_db_button, panel.target_delete_button, panel.source_refresh_button):
            self.assertEqual(str(button.cget('state')), 'normal')
