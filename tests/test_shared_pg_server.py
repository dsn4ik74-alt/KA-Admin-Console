"""One PostgreSQL connection supplies two independently selectable copy views."""
import unittest
from unittest.mock import Mock, patch

from activity_store import ActivityStore, clean_profile, context_key
from activity_ui import endpoint
import test_activity
import test_database_copy
import test_service_ui


ROWS = [dict(name=name, owner='postgres', server_version=180003, size_bytes=index * 1024)
        for index, name in enumerate(('Original', 'Target', 'Third'), 1)]


class SharedPostgresServerTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self)
        self.app.activity.close(wait=True)
        self.app.activity = self.app.hub.store = ActivityStore()
        self.app._switch_mode('pg_copy')
        self.panel = self.app.postgres_copy

    def loaded_panel(self):
        panel = self.panel
        panel.host.set('one-server')
        panel.password.set('source-test-password')
        panel.target_host.set('other-server')
        panel.target_password.set('separate-test-password')
        panel.loaded(panel.connection_key(), ROWS)
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.path.set('C:/Backup')
        return panel

    @staticmethod
    def names(tree):
        return [tree.set(row, 'name') for row in tree.get_children()]

    @staticmethod
    def select(tree, name):
        tree.selection_set(next(row for row in tree.get_children() if tree.set(row, 'name') == name))

    def test_loaded_source_is_shown_immediately_in_both_lists_without_a_query(self):
        panel = self.loaded_panel()
        with patch.object(panel, 'work') as work:
            panel.same_server_check.invoke()
        work.assert_not_called()
        self.assertEqual(self.names(panel.tree), ['Original', 'Target', 'Third'])
        self.assertEqual(self.names(panel.target_tree), self.names(panel.tree))
        self.assertEqual(panel.target_connected, panel.target_key())
        self.assertEqual(panel.target_connection()['host'], 'one-server')
        self.assertEqual(panel.target_connection()['password'], 'source-test-password')
        self.assertEqual(panel.target_host.get(), 'other-server')
        self.assertIsNot(panel.databases, panel.target_databases)
        self.assertIsNot(panel.databases[0], panel.target_databases[0])
        self.assertEqual(panel.target_connection_form.winfo_manager(), '')

    def test_one_connection_loads_both_views_when_checked_before_connecting(self):
        panel = self.panel
        panel.host.set('one-server')
        panel.same_server.set(True)
        self.assertIsNone(panel.target_connected)
        with patch.object(panel, 'work') as work:
            panel.connect_button.invoke()
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', return_value=ROWS) as query:
            rows = operation(Mock())
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[0]['connection']['host'], 'one-server')
        success(rows)
        self.assertEqual(self.names(panel.target_tree), ['Original', 'Target', 'Third'])
        self.assertEqual(panel.connected, panel.connection_key())
        self.assertEqual(panel.target_connected, panel.target_key())

    def test_refresh_from_either_side_reads_once_and_preserves_both_selections(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        self.select(panel.target_tree, 'Target')
        for role in ('source', 'target'):
            with self.subTest(role=role), patch.object(panel, 'work') as work:
                self.app.hub.refresh_buttons[(panel, role)].invoke()
            operation, success = work.call_args.args
            with patch('postgres_ui.run_job', return_value=ROWS) as query:
                result = operation(Mock())
            success(result)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(panel.selected(), ['Original'])
            self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'Target')

    def test_filtering_and_pinning_the_source_does_not_restrict_the_target(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        self.app.hub.choose_database(panel, 'source', 'Original')
        self.assertEqual(self.names(panel.tree), ['Original'])
        self.assertEqual(self.names(panel.target_tree), ['Original', 'Target', 'Third'])
        self.app.hub.choose_database(panel, 'target', 'Target')
        self.assertEqual(self.names(panel.target_tree), ['Target'])
        self.assertEqual(panel.selected(), ['Original'])

    def test_source_edits_invalidate_both_lists_and_use_the_new_credentials(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        panel.port.set('5433')
        panel.sslmode.set('require')
        panel.password.set('updated-secret')
        self.assertEqual(self.names(panel.tree), [])
        self.assertEqual(self.names(panel.target_tree), [])
        self.assertIsNone(panel.connected)
        self.assertIsNone(panel.target_connected)
        self.assertEqual(panel.target_connection(), dict(host='one-server', port='5433',
                         username='postgres', password='updated-secret', sslmode='require'))
        self.assertNotIn('updated-secret', panel.output.get('1.0', 'end-1c'))

    def test_unchecking_restores_separate_fields_and_never_overwrites_their_values(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        panel.start()
        self.assertEqual(str(panel.same_server_check.cget('state')), 'disabled')
        panel.finish('Finished')
        self.assertEqual(str(panel.same_server_check.cget('state')), 'normal')
        self.assertEqual(panel.target_connection_form.winfo_manager(), '')
        panel.same_server_check.invoke()
        self.assertEqual(panel.target_connection_form.winfo_manager(), 'pack')
        self.assertEqual(panel.target_connection()['host'], 'other-server')
        self.assertEqual(panel.target_connection()['password'], 'separate-test-password')
        self.assertEqual(self.names(panel.tree), ['Original', 'Target', 'Third'])
        self.assertIsNone(panel.target_connected)
        self.assertEqual(self.names(panel.target_tree), [])

    def test_copy_uses_source_credentials_for_both_sides_but_two_different_databases(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        self.select(panel.target_tree, 'Target')
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.execute()
        operation, success = work.call_args.args
        with patch('database_copy_ui.run_job', return_value=dict(backup_path='test.dump')) as job:
            result = operation(Mock())
        request = job.call_args.args[0]
        self.assertEqual(request['source_database'], 'Original')
        self.assertEqual(request['target_database'], 'Target')
        target = request['connection']['target_connection']
        for key in ('host', 'port', 'username', 'password', 'sslmode'):
            self.assertEqual(target[key], request['connection'][key])
        success(result)
        self.assertNotIn('source-test-password', panel.output.get('1.0', 'end-1c'))

    def test_selecting_the_source_as_target_is_stopped_before_confirmation(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        self.select(panel.target_tree, 'Original')
        with patch('database_copy_ui.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
            panel.execute()
        confirm.assert_not_called()
        work.assert_not_called()
        self.assertIn('разными базами', self.app.form_notice.message.cget('text'))

    def test_create_on_shared_target_refreshes_both_views_with_one_read(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        with patch('database_copy_ui.ask_create_database', return_value=dict(name='New', owner='postgres')), \
                patch.object(panel, 'work') as work:
            panel.target_create_button.invoke()
        operation, success = work.call_args.args
        updated = [*ROWS, dict(name='New', owner='postgres', server_version=180003)]
        with patch('database_copy_ui.run_job', side_effect=['New', updated]) as job:
            result = operation(Mock())
        self.assertEqual([call.args[0]['mode'] for call in job.call_args_list], ['create', 'list'])
        self.assertEqual(job.call_args_list[0].args[0]['connection']['host'], 'one-server')
        success(result)
        self.assertEqual(self.names(panel.tree), self.names(panel.target_tree))
        self.assertEqual(panel.selected(), ['Original'])
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'New')

    def test_delete_and_failed_refresh_remove_the_deleted_database_from_both_views(self):
        panel = self.loaded_panel()
        panel.same_server.set(True)
        self.select(panel.target_tree, 'Target')
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.target_delete_button.invoke()
        operation, success = work.call_args.args
        with patch('database_copy_ui.run_job', side_effect=['Target', RuntimeError('Refresh failed')]) as job:
            result = operation(Mock())
        self.assertEqual([call.args[0]['mode'] for call in job.call_args_list], ['delete', 'list'])
        success(result)
        self.assertEqual(self.names(panel.tree), ['Original', 'Third'])
        self.assertEqual(self.names(panel.target_tree), ['Original', 'Third'])
        self.assertIsNone(panel.connected)
        self.assertIsNone(panel.target_connected)

    def test_favorite_preserves_the_flag_and_old_favorites_restore_separate_mode(self):
        test_activity.mock_dpapi(self)
        panel = self.loaded_panel()
        panel.same_server.set(True)
        profile = self.app.hub.profile(panel, include_passwords=True)
        self.app.activity.save_favorite('One server', profile)
        favorite = self.app.activity.snapshot('favorites')[0]
        self.assertIs(favorite['values']['same_server'], True)
        self.assertEqual(endpoint(favorite, 'target'), 'one-server:5432')
        self.assertEqual(context_key(favorite), context_key(favorite, 'target'))
        panel.same_server.set(False)
        panel.password.set('changed')
        with patch.object(panel, 'work') as work:
            self.assertTrue(self.app.hub.apply(favorite))
            work.assert_not_called()
        self.assertTrue(panel.same_server.get())
        self.assertEqual(panel.target_connection()['password'], 'source-test-password')
        old = clean_profile(dict(test_activity.PROFILE, page='pg_copy'))
        self.assertTrue(self.app.hub.apply(old))
        self.assertFalse(panel.same_server.get())
        self.assertEqual(panel.target_connection()['host'], 'DB02')

    def test_shared_mode_also_uses_one_executor_and_ssh_endpoint(self):
        panel = self.loaded_panel()
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('execution-host')
        panel.ssh_user.set('backup')
        panel.same_server.set(True)
        with patch.object(panel, 'work') as work:
            panel.connect_target()
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', return_value=ROWS) as job:
            rows = operation(Mock())
        request = job.call_args.args[0]['connection']
        self.assertEqual((request['execution'], request['ssh_host']), ('ssh', 'execution-host'))
        success(rows)
        self.assertEqual(self.names(panel.tree), self.names(panel.target_tree))
        self.assertEqual(panel.target_connected, panel.target_key())
