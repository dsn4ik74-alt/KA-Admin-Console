import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from postgres_runner import Runner
from ssh_connection import UnknownHostKey, probe
from test_postgres import ProcessRunner
from test_saved_servers import PG, SQL
import test_activity
import test_service_ui


class BackendTests(unittest.TestCase):
    def test_ibcmd_uses_1c_postgresql_server_syntax_for_default_and_custom_ports(self):
        with tempfile.TemporaryDirectory() as temp:
            for port in ('5432', '6432'):
                runner = ProcessRunner(dict(host='YKT', port=port, username='postgres', data_path=temp), Mock())
                for restore in (False, True):
                    args = runner.ibcmd_args('test_YKT_dt', restore)
                    self.assertIn('--database-server=YKT port=' + port, args)
                    self.assertFalse(any(arg.startswith('--database-server=YKT:') for arg in args))

    def test_real_child_ibcmd_log_keeps_oem_russian_and_utf8_messages_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool = root / 'ibcmd_fixture.py'
            tool.write_text("""
from pathlib import Path
import sys
Path(sys.argv[-1]).write_bytes(b'DT_fixture')
sys.stdout.buffer.write(('Соединение с сервером базы данных разорвано администратором\\n').encode('cp866'))
sys.stdout.buffer.write(('UTF8: Выгрузка завершена\\n').encode('utf-8'))
""", encoding='utf-8')
            events = []
            runner = ProcessRunner(dict(host='YKT', port='5432', username='postgres', data_path=temp), events.append)
            runner.script = str(tool)
            with patch('postgres_runner.ibcmd_output_encoding', return_value='cp866'):
                result = runner.backup(['Target'], str(root / 'dt'), ibcmd=True)
            self.assertTrue(Path(result[0]).is_file())
            text = '\n'.join(event.get('text', '') for event in events)
            self.assertIn('Соединение с сервером базы данных разорвано администратором', text)
            self.assertIn('UTF8: Выгрузка завершена', text)

    def test_ssh_probe_authenticates_and_reads_environment_without_database_or_python(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        output = io.BytesIO(b'x86_64\nNAME=Ubuntu\n1000\n')
        output.channel = Mock()
        output.channel.recv_exit_status.return_value = 0
        client.exec_command.return_value = io.BytesIO(), output, io.BytesIO()
        with patch('ssh_connection.connect', return_value=client) as connect:
            result = probe(dict(host='VM', user='operator', password='fixture'))
        self.assertIn('Ubuntu', result['description'])
        self.assertNotIn('python', client.exec_command.call_args.args[0])
        self.assertEqual(connect.call_args.args[0]['password'], 'fixture')


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self)
        test_activity.mock_dpapi(self)

    def test_dt_checkbox_is_in_the_folder_input_row_for_windows_and_linux(self):
        self.app._switch_mode('ibcmd_dump')
        panel = self.app.postgres
        for execution in ('Локально · Windows', 'SSH · Linux'):
            panel.execution.set(execution)
            self.assertIs(panel.dt_destination_check.master, panel.path_entry.master)
            self.assertEqual(panel.dt_destination_check.pack_info()['side'], 'right')
            self.assertEqual(panel.dt_destination_check.winfo_manager(), 'pack')
        self.app._switch_mode('pg_dump')
        self.assertEqual(panel.dt_destination_check.winfo_manager(), '')

    def context(self, panel, tree, role, name='Имя_БД'):
        row = tree.insert('', 'end', values=(name, 'Other value', 'owner'))
        event = SimpleNamespace(y=22, x_root=30, y_root=30)
        menu = self.app.hub.database_menus[(panel, role)]
        with patch.object(tree, 'identify_row', return_value=row), patch.object(menu, 'tk_popup'):
            self.app.hub.database_context(panel, role, event)
        return menu, row

    def test_context_menu_copies_only_database_name_and_calls_the_correct_delete_action(self):
        for mode in ('db_backup', 'pg_dump', 'db_copy', 'pg_copy', 'cluster_infobase'):
            self.app._switch_mode(mode)
            panel = self.app._active_panel
            for role in ('source', 'target') if hasattr(panel, 'target_tree') else ('source',):
                with self.subTest(mode=mode, role=role):
                    tree = panel.target_tree if role == 'target' else panel.tree
                    menu, row = self.context(panel, tree, role)
                    self.assertEqual(menu.entrycget(0, 'label'), 'Удалить…')
                    menu.invoke(1)
                    # run_tests.py replaces Tcl's clipboard with an in-memory fixture.
                    self.assertEqual(self.app.root.clipboard_get(), 'Имя_БД')
                    method = 'delete_selected' if hasattr(panel, 'dbms') else 'delete_target_database' if role == 'target' else 'delete_selected_database'
                    with patch.object(panel, method) as delete:
                        menu.invoke(0)
                    delete.assert_called_once_with()
                    self.assertEqual(tree.selection(), (row,))

    def test_context_delete_keeps_confirmation_and_cancel_never_starts_a_job(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        panel.connected = panel.connection_key()
        menu, row = self.context(panel, panel.tree, 'source', name='Target')
        with patch('postgres_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'work') as work:
            menu.invoke(0)
        self.assertEqual(confirm.call_args.kwargs['default'], 'no')
        self.assertIn('Target', confirm.call_args.args[1])
        work.assert_not_called()

    def test_context_delete_is_disabled_when_busy_and_rejects_a_changed_row(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        panel.running = True
        menu, row = self.context(panel, panel.tree, 'source')
        self.assertEqual(menu.entrycget(0, 'state'), 'disabled')
        panel.running = False
        menu, row = self.context(panel, panel.tree, 'source', name='Original')
        panel.tree.set(row, 'name', 'Replacement')
        with patch.object(panel, 'delete_selected_database') as delete:
            menu.invoke(0)
        delete.assert_not_called()
        self.assertIn('изменился', self.app.form_notice.message.cget('text'))

    def test_main_infobase_menu_copies_name_and_delegates_deletion_to_the_exact_cluster_uuid(self):
        cluster, infobase = '11111111-2222-4333-8444-555555555555', 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        target = dict(server='1C-AGENT', port='1540', version='8.3.27.1859')
        row = dict(target, base='Main_IB', cluster='Cluster', cluster_id=cluster, infobase_id=infobase)
        self.app.all_items = [target]
        self.app._all_bases = [row]
        self.app._switch_mode('bases')
        self.app._render()
        iid = self.app.tree.get_children()[0]
        event = SimpleNamespace(y=22, x_root=30, y_root=30)
        with patch.object(self.app.tree, 'identify_row', return_value=iid), patch.object(self.app.base_context_menu, 'tk_popup'):
            self.app._base_context(event)
        self.app.base_context_menu.invoke(1)
        self.assertEqual(self.app.root.clipboard_get(), 'Main_IB')
        panel = self.app.cluster_infobase
        with patch.object(panel, 'delete_selected') as delete:
            self.app.base_context_menu.invoke(0)
        delete.assert_called_once_with()
        self.assertEqual(panel.selected_base()['infobase'], infobase)
        self.assertEqual(panel.selected_cluster()['cluster'], cluster)
        self.assertEqual(panel.target()['server'], '1C-AGENT')

    def test_saved_server_manager_removes_only_connections_and_updates_all_server_lists(self):
        for profile in (PG, SQL):
            self.app.activity.save_connection(profile)
        self.app.activity.save_favorite('PG favorite', PG)
        self.app.activity.remember(PG, 'Main')
        self.app._switch_mode('pg_copy')
        panel = self.app.postgres_copy
        panel.host.set('Current')
        window = self.app.saved_servers.manage(panel.host_entry)
        rows = window.connection_tree.get_children()
        self.assertEqual(len(rows), 1)
        self.assertNotIn('pg-test-secret', str(window.connection_tree.item(rows[0], 'values')))
        window.connection_tree.selection_set(rows[0])
        with patch.object(panel, 'work') as work:
            window.remove_button.invoke()
        work.assert_not_called()
        self.assertEqual(panel.host.get(), 'Current')
        self.assertEqual(window.connection_tree.get_children(), ())
        self.assertEqual([row['kind'] for row in self.app.activity.snapshot('connections')], ['sql'])
        self.assertEqual(len(self.app.activity.snapshot('favorites')), 1)
        self.assertEqual(len(self.app.activity.snapshot('recent')), 1)
        for entry in (self.app.postgres.host_entry, panel.host_entry, panel.target_host_entry):
            self.assertEqual(entry.cget('values'), '')
        self.assertTrue(panel.host_entry.manage_servers_button.winfo_exists())

    def test_check_ssh_does_not_require_postgres_configuration_and_never_runs_a_db_job(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        panel.execution.set('SSH · Linux')
        panel.host.set('')
        panel.username.set('')
        panel.ssh_host.set('VM')
        panel.ssh_user.set('operator')
        panel.ssh_password.set('ssh-fixture')
        with patch.object(panel, 'work') as work:
            panel.ssh_probe_button.invoke()
        operation, success = work.call_args.args
        with patch('postgres_ui.probe_ssh', return_value=dict(description='Ubuntu fixture')) as check, \
             patch('postgres_ui.run_job', side_effect=AssertionError('No DB command')):
            result = operation(Mock())
        self.assertEqual(check.call_args.args[0]['password'], 'ssh-fixture')
        success(result)
        self.assertFalse(panel.running)
        self.assertIn('Ubuntu fixture', panel.output.get('1.0', 'end-1c'))

    def test_ssh_probe_unknown_key_is_offered_for_confirmation_and_retry_only(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        key = Mock()
        key.get_name.return_value, key.get_base64.return_value, key.asbytes.return_value = 'ssh-ed25519', 'fixture', b'fixture'
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('VM')
        with patch.object(panel, 'work') as work:
            panel.check_ssh()
        operation, success = work.call_args.args
        with patch('postgres_ui.probe_ssh', side_effect=UnknownHostKey('VM', key)):
            result = operation(Mock())
        with patch.object(panel, 'defer_until_visible', return_value=False), \
             patch('postgres_ui.messagebox.askyesno', return_value=True), \
             patch.object(self.app, '_save_settings'), patch.object(panel, 'check_ssh') as retry:
            success(result)
        retry.assert_called_once_with()
        self.assertIn('VM', self.app._settings['ssh_hosts'])
