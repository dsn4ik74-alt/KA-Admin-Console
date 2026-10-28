"""Deletion requires a reviewed selection; native/database jobs are mocked."""
import unittest
from unittest.mock import Mock, patch

import test_service_ui
from postgres_runner import OperationCancelled, Runner, execute, identifier, literal
import sql_server
from test_postgres import CONNECTION, TARGET


class DeleteUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def select(self, engine, name='Chosen'):
        self.app._switch_mode('db_backup' if engine == 'sql' else 'pg_dump')
        panel = self.app.sql if engine == 'sql' else self.app.postgres
        panel.databases = [dict(name=name, recovery_model_desc='FULL', owner='Owner'),
                           dict(name='Other', recovery_model_desc='FULL', owner='Owner')]
        panel.render()
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.connected = panel.connection() if engine == 'sql' else panel.connection_key()
        return panel

    def test_cancel_confirmation_never_starts_deletion_or_any_job(self):
        for engine, module in (('sql', 'administration_ui'), ('pg', 'postgres_ui')):
            panel = self.select(engine)
            with patch(module + '.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'work') as work:
                panel.delete_db_button.invoke()
            confirm.assert_called_once()
            self.assertIn('Chosen', confirm.call_args.args[1])
            self.assertIn('localhost', confirm.call_args.args[1])
            self.assertEqual(confirm.call_args.kwargs['default'], 'no')
            work.assert_not_called()
            self.assertFalse(panel.running)

    def test_no_selection_or_changed_connection_never_opens_confirmation(self):
        for engine, module in (('sql', 'administration_ui'), ('pg', 'postgres_ui')):
            panel = self.select(engine)
            panel.tree.selection_remove(panel.tree.selection())
            with patch(module + '.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
                panel.delete_selected_database()
                panel.tree.selection_set(panel.tree.get_children()[0])
                panel.connected = None
                panel.delete_selected_database()
            confirm.assert_not_called()
            work.assert_not_called()

    def test_postgres_multiple_selection_never_opens_confirmation(self):
        panel = self.select('pg')
        panel.tree.selection_set(panel.tree.get_children())
        with patch('postgres_ui.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
            panel.delete_selected_database()
        confirm.assert_not_called()
        work.assert_not_called()

    def test_system_database_selection_is_protected_before_dialog(self):
        for engine, module, name in (('sql', 'administration_ui', 'MASTER'), ('pg', 'postgres_ui', 'postgres')):
            panel = self.select(engine, name)
            with patch(module + '.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
                panel.delete_selected_database()
            confirm.assert_not_called()
            work.assert_not_called()

    def test_confirmed_sql_deletes_only_selected_name_and_refreshes(self):
        panel = self.select('sql')
        panel.server_path.set('unused')
        remaining = [dict(name='Other', recovery_model_desc='FULL')]
        with patch('administration_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.delete_selected_database()
        self.assertTrue(panel.running)
        self.assertEqual(str(panel.delete_db_button.cget('state')), 'disabled')
        operation, success = work.call_args.args
        with patch('administration_ui.delete_database', return_value='Chosen') as delete, patch('administration_ui.list_databases', return_value=(remaining, dict(server='SQL', machine='SQL'))):
            result = operation(Mock())
        self.assertEqual(delete.call_args.args[1], 'Chosen')
        success(result)
        self.assertEqual(panel.databases, remaining)
        self.assertEqual(panel.selected(), '')
        self.assertEqual(panel.server_path.get(), 'unused')
        self.assertIn('База удалена: Chosen', panel.output.get('1.0', 'end-1c'))
        self.assertFalse(panel.running)
        self.assertEqual(str(panel.delete_db_button.cget('state')), 'normal')

    def test_confirmed_postgres_ssh_and_failed_refresh_do_not_repeat_delete(self):
        panel = self.select('pg')
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('linux.example')
        panel.ssh_user.set('sshuser')
        panel.host.set('db.internal')
        panel.databases = [dict(name='Chosen', owner='Owner'), dict(name='Other', owner='Owner')]
        panel.render()
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.connected = panel.connection_key()
        with patch('postgres_ui.messagebox.askyesno', return_value=True) as confirm, patch.object(panel, 'work') as work:
            panel.delete_selected_database()
        self.assertIn('db.internal:5432', confirm.call_args.args[1])
        self.assertIn('sshuser@linux.example', confirm.call_args.args[1])
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', side_effect=['Chosen', RuntimeError('Refresh failed')]) as job:
            result = operation(Mock())
        payload = job.call_args_list[0].args[0]
        self.assertEqual(payload['mode'], 'delete')
        self.assertEqual(payload['database'], 'Chosen')
        self.assertEqual(payload['connection']['execution'], 'ssh')
        self.assertEqual(job.call_count, 2)
        success(result)
        self.assertEqual([row['name'] for row in panel.databases], ['Other'])
        self.assertIn('База удалена: Chosen', panel.output.get('1.0', 'end-1c'))
        self.assertIn('Refresh failed', panel.output.get('1.0', 'end-1c'))
        self.assertFalse(panel.running)

    def test_confirmed_delete_failure_does_not_refresh_or_report_success(self):
        panel = self.select('sql')
        with patch('administration_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.delete_selected_database()
        operation, _ = work.call_args.args
        with patch('administration_ui.delete_database', side_effect=RuntimeError('Denied')), patch('administration_ui.list_databases') as refresh:
            with self.assertRaisesRegex(RuntimeError, 'Denied'):
                operation(Mock())
        refresh.assert_not_called()
        self.assertNotIn('База удалена', panel.output.get('1.0', 'end-1c'))
        panel.finish('', 'Denied')

    def test_busy_operation_blocks_delete_without_confirmation(self):
        panel = self.select('pg')
        panel.start()
        with patch('postgres_ui.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
            panel.delete_selected_database()
        confirm.assert_not_called()
        work.assert_not_called()
        panel.finish_cancelled('Cancelled')


class DeleteBackendTests(unittest.TestCase):
    def test_sql_escapes_name_and_checks_database_id_before_single_batch_drop(self):
        database = 'O\'Brien]"quoted'
        with patch('sql_server.query', side_effect=[[[dict(database_id=7, user_access_desc='MULTI_USER')]], [[]]]) as query:
            self.assertEqual(sql_server.delete_database({}, database), database)
        metadata, mutation = [call.args[1][0]['sql'] for call in query.call_args_list]
        self.assertIn(sql_server.literal(database), metadata)
        self.assertIn('USE [master]', mutation)
        self.assertIn('<> 7', mutation)
        self.assertIn('<= 4', mutation)
        self.assertIn('ALTER DATABASE ' + sql_server.identifier(database) + ' SET SINGLE_USER', mutation)
        self.assertIn('DROP DATABASE ' + sql_server.identifier(database) + ';', mutation)
        self.assertNotIn('BEGIN TRANSACTION', mutation)

    def test_sql_system_or_missing_db_never_executes_mutation(self):
        for name in ('master', 'MODEL', 'msdb', 'tempdb', '', 'bad\nname'):
            with patch('sql_server.query') as query, self.assertRaises(ValueError):
                sql_server.delete_database({}, name)
            query.assert_not_called()
        for rows in ([], [dict(database_id=2, user_access_desc='MULTI_USER')]):
            with patch('sql_server.query', return_value=[rows]) as query, self.assertRaises(ValueError):
                sql_server.delete_database({}, 'Chosen')
            self.assertEqual(query.call_count, 1)

    def test_sql_cancel_uses_independent_conditional_cleanup_without_retrying_drop(self):
        event = Mock()
        event.cancel_event = Mock()
        with patch('sql_server.query', side_effect=[[[dict(database_id=9, user_access_desc='RESTRICTED_USER')]], OperationCancelled('Cancelled'), [[]]]) as query:
            with self.assertRaises(OperationCancelled):
                sql_server.delete_database({}, 'Chosen', event)
        cleanup = query.call_args_list[2].args
        self.assertIn('database_id=9', cleanup[1][0]['sql'])
        self.assertIn("user_access_desc<>N'RESTRICTED_USER'", cleanup[1][0]['sql'])
        self.assertIn('SET RESTRICTED_USER', cleanup[1][0]['sql'])
        self.assertNotIn('DROP DATABASE', cleanup[1][0]['sql'])
        self.assertNotIn('cancel_event', getattr(cleanup[2], '__dict__', {}))

    def test_postgres_drop_escapes_identifiers_and_terminates_only_selected_db(self):
        runner = Runner(CONNECTION, Mock())
        name = 'O\'Brien"quoted'
        with patch.object(runner, 'target', return_value=TARGET) as target, patch.object(runner, 'sql') as sql:
            self.assertEqual(runner.delete_database(name), name)
        target.assert_called_once_with(name)
        statements = [call.args[0] for call in sql.call_args_list]
        self.assertEqual(statements[0], 'ALTER DATABASE ' + identifier(name) + ' ALLOW_CONNECTIONS false;')
        self.assertIn('datname=' + literal(name), statements[1])
        self.assertEqual(statements[2], 'DROP DATABASE ' + identifier(name) + ';')

    def test_postgres_failure_or_cancel_restores_connections_without_retrying_drop(self):
        for error in (RuntimeError('Denied'), OperationCancelled('Cancelled')):
            runner = Runner(CONNECTION, Mock())
            with patch.object(runner, 'target', return_value=TARGET), patch.object(runner, 'sql', side_effect=['', '', error, '1', '']) as sql:
                with self.assertRaises(type(error)):
                    runner.delete_database('Chosen')
            self.assertEqual(sum(call.args[0].startswith('DROP DATABASE') for call in sql.call_args_list), 1)
            cleanup = sql.call_args_list[-1]
            self.assertIn('ALLOW_CONNECTIONS true', cleanup.args[0])
            self.assertTrue(cleanup.kwargs['ignore_cancel'])

    def test_postgres_system_template_or_missing_target_cannot_be_dropped(self):
        runner = Runner(CONNECTION, Mock())
        for name in ('postgres', 'template0', 'template1', '', 'bad\x00name'):
            with patch.object(runner, 'sql') as sql, self.assertRaises(ValueError):
                runner.delete_database(name)
            sql.assert_not_called()
        with patch.object(runner, 'sql', return_value='[]') as sql, self.assertRaises(ValueError):
            runner.delete_database('CustomTemplate')
        self.assertEqual(sql.call_count, 1)
        self.assertIn('NOT datistemplate', sql.call_args.args[0])

    def test_delete_dispatch_works_without_backup_path_or_file_utilities(self):
        payload = dict(mode='delete', connection=CONNECTION, database='Chosen')
        with patch.object(Runner, 'delete_database', return_value='Chosen') as delete:
            self.assertEqual(execute(payload, Mock()), 'Chosen')
        delete.assert_called_once_with('Chosen')


if __name__ == '__main__':
    unittest.main()
