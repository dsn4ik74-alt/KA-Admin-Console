import contextlib
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from add_cluster_infobase import (ClusterClient, PARAMETERS, create_command, main, redact,
                                 run_rac, validate_parameters)
from bookmark_navigation import NavigationLayout
from postgres_runner import OperationCancelled
import test_service_ui


CLUSTER = '11111111-2222-4333-8444-555555555555'
INFOBASE = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
TARGET = dict(server='agent.internal', port='4540', version='8.3.27.1859')
PARAMS = {'name': 'База "Тест"', 'descr': 'Описание', 'security-level': '0',
          'dbms': 'MSSQLServer', 'db-server': 'SQL\\INSTANCE,1433', 'db-name': 'ExistingDB',
          'db-user': 'db-user', 'db-pwd': 'db-secret', 'license-distribution': 'allow',
          'locale': 'ru_RU', 'date-offset': '0', 'create-database': False, 'scheduled-jobs-deny': 'off'}
HELP = '\n'.join('--' + key for key in PARAMETERS) + '\n--create-database'
CLUSTERS = 'cluster : ' + CLUSTER + '\nname : "Основной"\nport : 4541\n'
CREATED = 'infobase : ' + INFOBASE + '\n'
INFO_OFF = CREATED + 'scheduled-jobs-deny : off\n'


class ClientTests(unittest.TestCase):
    def client(self, user='', password=''):
        events = []
        client = ClusterClient(TARGET, {}, user, password, event=events.append)
        @contextlib.contextmanager
        def connect():
            yield 'rac.exe', '127.0.0.1:23456'
        client.connect = connect
        return client, events

    def test_command_covers_all_screenshot_fields_with_literal_arguments(self):
        params = dict(PARAMS, **{'create-database': True, 'scheduled-jobs-deny': 'on', 'security-level': '2',
                              'license-distribution': 'deny', 'date-offset': '2000'})
        command = create_command('C:/path with spaces/rac.exe', '127.0.0.1:23456', CLUSTER, params)
        self.assertEqual(command[:3], ['C:/path with spaces/rac.exe', 'infobase', 'create'])
        for key in PARAMETERS:
            self.assertIn('--' + key + '=' + params[key], command)
        self.assertIn('--create-database', command)
        self.assertFalse(any(arg.startswith('--cluster-user') or arg.startswith('--cluster-pwd') for arg in command))
        self.assertEqual(command[-1], '127.0.0.1:23456')

    def test_existing_database_registration_does_not_request_db_creation(self):
        client, events = self.client('cluster-user', 'cluster-secret')
        with patch.object(client, 'run', side_effect=[HELP, CLUSTERS, '', CREATED, INFO_OFF]) as run:
            result = client.create(CLUSTER, PARAMS)
        self.assertEqual(result['infobase_id'], INFOBASE)
        self.assertEqual(run.call_count, 5)
        args = run.call_args_list[3].args[0]
        self.assertNotIn('--create-database', args)
        self.assertIn('--cluster-user=cluster-user', args)
        self.assertIn('--cluster-pwd=cluster-secret', args)
        self.assertIsNone(run.call_args_list[3].kwargs['timeout'])
        self.assertFalse(result['scheduled_jobs_deny'])
        self.assertEqual(result['settings_error'], '')
        self.assertNotIn('db-secret', str(events))
        self.assertNotIn('cluster-secret', str(events))

    def test_duplicate_casefold_name_aborts_before_create(self):
        client, _ = self.client()
        summary = 'infobase : ' + INFOBASE + '\nname : "' + PARAMS['name'].upper().replace('"', '""') + '"\n'
        with patch.object(client, 'run', side_effect=[HELP, CLUSTERS, summary]) as run:
            with self.assertRaisesRegex(ValueError, 'уже есть'):
                client.create(CLUSTER, PARAMS)
        self.assertEqual(run.call_count, 3)
        self.assertFalse(any(call.args[0][1:3] == ['infobase', 'create'] for call in run.call_args_list))

    def test_changed_cluster_or_missing_option_aborts_before_create(self):
        for replies in ([HELP.replace('--security-level', 'old-option'), CLUSTERS], [HELP, '']):
            client, _ = self.client()
            with patch.object(client, 'run', side_effect=replies) as run, self.assertRaises(ValueError):
                client.create(CLUSTER, PARAMS)
            self.assertFalse(any(call.args[0][1:3] == ['infobase', 'create'] for call in run.call_args_list))

    def test_success_without_uuid_reads_summary_once_without_repeating_create(self):
        client, _ = self.client()
        summary = CREATED + 'name : "' + PARAMS['name'].replace('"', '""') + '"\n'
        with patch.object(client, 'run', side_effect=[HELP, CLUSTERS, '', '', summary, INFO_OFF]) as run:
            self.assertEqual(client.create(CLUSTER, PARAMS)['infobase_id'], INFOBASE)
        self.assertEqual(sum(call.args[0][1:3] == ['infobase', 'create'] for call in run.call_args_list), 1)

    def test_success_but_refresh_error_is_reported_without_retry(self):
        client, events = self.client()
        with patch.object(client, 'run', side_effect=[HELP, CLUSTERS, '', '', RuntimeError('Read failed')]) as run:
            self.assertEqual(client.create(CLUSTER, PARAMS)['infobase_id'], '')
        self.assertEqual(run.call_count, 5)
        self.assertIn('Read failed', str(events))

    def test_cancel_during_creation_reports_possible_server_side_completion(self):
        client, _ = self.client()
        with patch.object(client, 'run', side_effect=[HELP, CLUSTERS, '', OperationCancelled('Cancelled')]) as run:
            with self.assertRaisesRegex(OperationCancelled, 'Сервер мог успеть'):
                client.create(CLUSTER, PARAMS)
        self.assertEqual(run.call_count, 4)

    def test_invalid_parameters_are_rejected_before_connection(self):
        client, _ = self.client()
        for key, value in (('name', ''), ('db-server', ''), ('dbms', 'SQLite'), ('security-level', '3'),
                           ('date-offset', '100'), ('locale', 'ru\nru'), ('create-database', 'yes'), ('db-pwd', 'secret\x00')):
            with patch.object(client, 'connect') as connect, self.assertRaises(ValueError):
                client.create(CLUSTER, dict(PARAMS, **{key: value}))
            connect.assert_not_called()
        with patch.object(client, 'connect') as connect, self.assertRaises(ValueError):
            client.create('not-a-uuid', PARAMS)
        connect.assert_not_called()

    def test_local_ras_uses_agent_port_and_closes_only_its_manager(self):
        client = ClusterClient(TARGET, {})
        manager = Mock()
        manager.stop = threading.Event()
        manager.connect.return_value.__enter__ = Mock(return_value='127.0.0.1:23456')
        manager.connect.return_value.__exit__ = Mock(return_value=False)
        with (patch('add_cluster_infobase.local_tool_candidates', return_value=[('rac.exe', 'ras.exe', TARGET['version'])]),
              patch('add_cluster_infobase.LocalRasManager', return_value=manager), patch.object(client, 'run', return_value=CLUSTERS)):
            with client.connect() as tools:
                self.assertEqual(tools, ('rac.exe', '127.0.0.1:23456'))
        manager.connect.assert_called_once_with('ras.exe', 'agent.internal:4540')
        manager.close.assert_called_once()

    def test_cli_flattens_current_user_lst_and_prompts_without_storing_passwords(self):
        report = Mock(current_paths=['current.lst'])
        fields = [PARAMS['name'], '', '', 'SQL', '', 'ExistingDB', 'db-user', '', '', '', '', '']
        with (patch('sys.argv', ['add_cluster_infobase.py']), patch('add_cluster_infobase.discover_local_lst', return_value=report),
              patch('add_cluster_infobase.read_lst', return_value={'agent.internal': [dict(port='4540', version=TARGET['version'])]}),
              patch('add_cluster_infobase.find_all_rac', return_value={}), patch('add_cluster_infobase.ClusterClient') as cls,
              patch('builtins.input', side_effect=fields), patch('add_cluster_infobase.getpass.getpass', return_value='db-secret'),
              patch('builtins.print')):
            cls.return_value.clusters.return_value = [dict(cluster=CLUSTER, name='Main')]
            main()
        self.assertEqual(cls.call_args.args[0]['port'], '4540')
        cls.return_value.create.assert_called_once()
        self.assertEqual(cls.return_value.create.call_args.args[1]['db-pwd'], 'db-secret')

    def test_runner_keeps_short_password_substrings_in_parseable_stdout(self):
        code = 'import sys; print(' + repr(CREATED) + ');sys.stderr.write("a")'
        result = run_rac([sys.executable, '-c', code], threading.Event(), secrets=('a',))
        self.assertIn(INFOBASE, result)
        code = 'import sys; sys.stderr.write("db-secret cluster-secret");sys.exit(7)'
        with self.assertRaises(RuntimeError) as error:
            run_rac([sys.executable, '-c', code], threading.Event(), ('db-secret', 'cluster-secret'))
        self.assertNotIn('db-secret', str(error.exception))
        self.assertNotIn('cluster-secret', str(error.exception))

    def test_runner_cancel_and_timeout_stop_silent_child(self):
        for cancel in (False, True):
            event = threading.Event()
            timer = threading.Timer(0.3, event.set) if cancel else None
            if timer:
                timer.start()
                self.addCleanup(timer.cancel)
            before = time.monotonic()
            with self.assertRaises(OperationCancelled if cancel else RuntimeError):
                run_rac([sys.executable, '-c', 'import time;time.sleep(60)'], event, timeout=None if cancel else 0.3)
            self.assertLess(time.monotonic() - before, 4)


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app.all_items = [TARGET, dict(server='other', port='1540', version=TARGET['version'])]
        self.app._switch_mode('servers')
        self.app._render()
        self.app.tree.selection_set('0')
        self.app._all_bases = [dict(server=TARGET['server'], port=TARGET['port'], base='Existing',
                                   cluster='Основной (' + CLUSTER[:8] + '...)', cluster_id=CLUSTER,
                                   infobase_id='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee')]
        self.app._switch_mode('cluster_infobase')
        panel = self.app.cluster_infobase
        panel.name.set(PARAMS['name'])
        panel.db_server.set(PARAMS['db-server'])
        panel.db_name.set(PARAMS['db-name'])
        panel.db_user.set(PARAMS['db-user'])
        panel.db_password.set(PARAMS['db-pwd'])
        return panel

    def test_fields_defaults_context_and_navigation_never_run_creation(self):
        with patch('cluster_infobase_ui.ClusterClient') as client:
            panel = self.panel()
        client.assert_not_called()
        self.assertEqual(panel.parameters(), dict(PARAMS, descr='', **{'date-offset': '2000'}))
        self.assertEqual(panel.target()['port'], '4540')
        self.assertEqual(panel.selected_cluster()['cluster'], CLUSTER)
        self.assertEqual(panel.winfo_manager(), 'pack')
        self.assertEqual(self.app.bookmarks.layout.location('cluster_infobase')[0], 'folder-1c')

    def test_create_is_background_only_on_button_with_snapshot_and_success_row(self):
        panel = self.panel()
        self.app.rac_map = {TARGET['version']: 'rac.exe'}
        self.app.user_var.set('cluster-user')
        self.app.pwd_var.set('cluster-secret')
        with patch.object(panel, 'work') as work, patch('cluster_infobase_ui.ClusterClient') as client:
            panel.execute_button.invoke()
        self.assertTrue(panel.running)
        self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')
        self.assertEqual(client.call_args.args[0]['server'], TARGET['server'])
        self.assertEqual(client.call_args.args[2:4], ('cluster-user', 'cluster-secret'))
        operation, success = work.call_args.args
        result = dict(name=PARAMS['name'], infobase_id=INFOBASE, cluster_id=CLUSTER)
        client.return_value.create.return_value = result
        client.return_value.bases.return_value = [dict(infobase=INFOBASE, name=PARAMS['name'])]
        result = operation(Mock())
        client.return_value.create.assert_called_once_with(CLUSTER, dict(PARAMS, descr='', **{'date-offset': '2000'}))
        success(result)
        self.assertFalse(panel.running)
        self.assertEqual(self.app._all_bases[-1]['infobase_id'], INFOBASE)
        self.assertNotIn('db-secret', panel.output.get('1.0', 'end-1c'))
        self.assertEqual(str(panel.server_combo.cget('state')), 'readonly')

    def test_missing_fields_or_cluster_never_starts_worker(self):
        panel = self.panel()
        with patch.object(panel, 'work') as work:
            panel.name.set('')
            panel.execute()
            panel.name.set('New')
            panel.cluster.set('')
            panel.execute()
        work.assert_not_called()
        self.assertFalse(panel.running)

    def test_refresh_selects_single_cluster_without_creating_anything(self):
        panel = self.panel()
        with patch.object(panel, 'work') as work, patch('cluster_infobase_ui.ClusterClient') as client:
            panel.load_clusters()
        operation, success = work.call_args.args
        rows = [dict(cluster=CLUSTER, name='Main', port='4541')]
        client.return_value.overview.return_value = dict(clusters=rows, cluster=CLUSTER, bases=[])
        success(operation(Mock()))
        self.assertEqual(panel.selected_cluster()['cluster'], CLUSTER)
        client.return_value.create.assert_not_called()

    def test_server_change_clears_previous_cluster_but_keeps_parameters(self):
        panel = self.panel()
        panel.server_combo.current(1)
        panel.server_changed()
        self.assertIsNone(panel.selected_cluster())
        self.assertEqual(panel.name.get(), PARAMS['name'])
        self.assertEqual(panel.db_password.get(), 'db-secret')

    def test_screenshot_dropdowns_wheel_does_not_change_values_and_footer_visible(self):
        panel = self.panel()
        self.app.root.deiconify()
        self.app.root.geometry('960x540+20+20')
        self.app.root.update()
        for widget in panel.inputs:
            if hasattr(widget, 'editable'):
                previous = widget.get()
                widget.event_generate('<MouseWheel>', delta=-120)
                self.app.root.update()
                self.assertEqual(widget.get(), previous)
        self.assertTrue(panel.execute_button.winfo_viewable())
        self.assertLess(panel.execute_button.winfo_rooty() + panel.execute_button.winfo_height(),
                        self.app.root.winfo_rooty() + self.app.root.winfo_height())

    def test_cancel_blocks_close_until_finished_and_keeps_credentials_out_of_settings(self):
        panel = self.panel()
        panel.start()
        panel.cancel_button.invoke()
        self.assertTrue(panel.cancel_event.is_set())
        with patch.object(self.app.root, 'destroy') as destroy:
            self.app._on_close()
        destroy.assert_not_called()
        panel.finish_cancelled('Stopped')
        self.assertFalse(panel.running)
        self.assertNotIn('db-secret', str(self.app._settings))

    def test_upgrade_adds_tab_to_1c_and_keeps_custom_layout_moves(self):
        layout = NavigationLayout()
        layout.move('bases', 'folder-win')
        snapshot = layout.snapshot()
        for node in snapshot:
            if node['id'] == 'folder-1c':
                node['children'] = [row for row in node['children'] if row['id'] != 'cluster_infobase']
        upgraded = NavigationLayout(snapshot)
        self.assertEqual(upgraded.location('cluster_infobase')[0], 'folder-1c')
        self.assertEqual(upgraded.location('bases')[0], 'folder-win')
        self.assertEqual(NavigationLayout(upgraded.snapshot()).snapshot(), upgraded.snapshot())


if __name__ == '__main__':
    unittest.main()
