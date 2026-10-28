"""Open selected 1C databases; no real application or server is started."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import configuration_install as clients
import infobase_launcher as launcher
from postgres_runner import OperationCancelled
import test_cluster_infobase as fixtures
import test_service_ui
from test_console import app_module

VERSION = '8.3.27.1644'
ROW = dict(server='ONEC', port='1540', base='Бухгалтерия "Тест"', cluster_id=fixtures.CLUSTER,
           infobase_id=fixtures.INFOBASE, cluster='Main', cluster_port='1540', cluster_version='8.3.27.2170')
TARGET = dict(server='ONEC', port='1540', version='8.3.27.2170')
CONTEXT = dict(cluster=dict(cluster=fixtures.CLUSTER, host='actual-cluster', port='4541', platform_version=VERSION),
               base=dict(infobase=fixtures.INFOBASE, name=ROW['base']))


class SelectionTests(unittest.TestCase):
    def test_thick_in_second_install_root_has_priority_over_thin_in_first_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / str(i) for i in range(2)]
            thin = roots[0] / VERSION / 'bin/1cv8c.exe'
            thick = roots[1] / VERSION / 'bin/1cv8.exe'
            for file in (thin, thick):
                file.parent.mkdir(parents=True)
                file.write_bytes(b'fixture')
            with patch.object(clients, 'platform_roots', return_value=roots), patch.object(clients, 'executable_version', return_value=VERSION):
                result = clients.platform_client(VERSION)
            self.assertEqual(result['path'], str(thick.resolve()))
            self.assertEqual(result['kind'], 'толстый')

    def test_matching_thin_wins_over_newer_or_mislabelled_thick(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thin = root / VERSION / 'bin/1cv8c.exe'
            thick = root / VERSION / 'bin/1cv8.exe'
            thin.parent.mkdir(parents=True)
            for file in (thin, thick):
                file.write_bytes(b'fixture')
            def version(path):
                return VERSION if Path(path) == thin else '8.3.27.2170'
            with patch.object(clients, 'platform_roots', return_value=[root]), patch.object(clients, 'executable_version', side_effect=version):
                result = clients.platform_client(VERSION)
            self.assertEqual(result['path'], str(thin.resolve()))
            self.assertEqual(result['kind'], 'тонкий')

    def test_missing_clients_reports_required_version_without_choosing_any_newer_version(self):
        with patch.object(clients, 'platform_roots', return_value=[]):
            with self.assertRaisesRegex(ValueError, VERSION):
                clients.platform_client(VERSION)

    def test_rac_sibling_in_custom_installation_is_found_and_version_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            thick = root / '1cv8.exe'
            thick.write_bytes(b'fixture')
            with patch.object(clients, 'platform_roots', return_value=[]), patch.object(clients, 'executable_version', return_value=VERSION) as version:
                result = clients.platform_client(VERSION, {VERSION: str(root / 'rac.exe')})
            self.assertEqual(result['path'], str(thick.resolve()))
            version.assert_called_once_with(thick)


class BackendTests(unittest.TestCase):
    def test_live_cluster_address_version_and_uuid_override_agent_port_and_stale_cached_values(self):
        with patch.object(launcher, 'ClusterClient') as cls, patch.object(launcher, 'platform_client', return_value=dict(path='C:/1C/1cv8.exe', kind='толстый', version=VERSION)) as select:
            cls.return_value.launch_context.return_value = CONTEXT
            plan = launcher.prepare_client(ROW, TARGET, {}, 'ClusterAdmin', 'cluster-secret')
        cls.return_value.launch_context.assert_called_once_with(fixtures.CLUSTER, fixtures.INFOBASE, ROW['base'])
        select.assert_called_once_with(VERSION, {})
        self.assertIn('Srvr="actual-cluster:4541";', plan['address'])
        self.assertNotIn(':1540', plan['address'])
        self.assertEqual(plan['args'][1:3], ['ENTERPRISE', '/IBConnectionString'])
        self.assertNotIn('ClusterAdmin', str(plan['args']))
        self.assertNotIn('cluster-secret', str(plan['args']))
        self.assertIn('Ref="Бухгалтерия ""Тест""";', plan['address'])

    def test_missing_cluster_port_never_falls_back_to_agent_port(self):
        with patch.object(launcher, 'ClusterClient') as cls, patch.object(launcher, 'platform_client') as select:
            cls.return_value.launch_context.return_value = dict(CONTEXT, cluster=dict(CONTEXT['cluster'], port=''))
            with self.assertRaisesRegex(ValueError, 'порт выбранного кластера'):
                launcher.prepare_client(ROW, TARGET, {})
        select.assert_not_called()

    def test_cancelled_or_incomplete_row_never_queries_server_or_launches_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(launcher, 'ClusterClient') as cls:
            with self.assertRaises(OperationCancelled):
                launcher.prepare_client(ROW, TARGET, {}, cancel=cancel)
            with self.assertRaises(ValueError):
                launcher.prepare_client(dict(ROW, infobase_id=''), TARGET, {})
            with self.assertRaises(ValueError):
                launcher.prepare_client(ROW, dict(TARGET, server='OTHER'), {})
            cls.assert_not_called()

    def test_uuid_and_name_are_checked_before_agent_version_or_client_lookup(self):
        client, _ = fixtures.ClientTests.client(self)
        summary = fixtures.CREATED + 'name : "Original"\n'
        with patch.object(client, 'run', side_effect=[fixtures.CLUSTERS, summary]) as run:
            with self.assertRaisesRegex(ValueError, 'переименована'):
                client.launch_context(fixtures.CLUSTER, fixtures.INFOBASE, 'Renamed')
        self.assertEqual(run.call_count, 2)
        with patch.object(client, 'run', side_effect=[fixtures.CLUSTERS, summary, VERSION]):
            result = client.launch_context(fixtures.CLUSTER, fixtures.INFOBASE, 'Original')
        self.assertEqual(result['cluster']['port'], '4541')
        self.assertEqual(result['cluster']['platform_version'], VERSION)

    def test_start_quotes_connection_and_does_not_wait_for_client_window_or_pass_shell_commands(self):
        with tempfile.TemporaryDirectory(prefix='КА launch ') as temporary:
            exe = Path(temporary) / '1cv8c.exe'
            exe.write_bytes(b'fixture')
            address = 'Srvr="host:4541";Ref="База ""Тест""";'
            plan = dict(path=str(exe), args=[str(exe), 'ENTERPRISE', '/IBConnectionString', address])
            with patch.object(launcher.subprocess, 'Popen') as popen:
                popen.return_value.pid = 321
                result = launcher.start_client(plan, threading.Event())
            self.assertEqual(result['pid'], 321)
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertIn('"Srvr=""host:4541"";', popen.call_args.args[0])
            popen.return_value.wait.assert_not_called()
            popen.return_value.terminate.assert_not_called()
            cancel = threading.Event()
            cancel.set()
            with patch.object(launcher.subprocess, 'Popen') as blocked, self.assertRaises(OperationCancelled):
                launcher.start_client(plan, cancel)
            blocked.assert_not_called()


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def listing(self):
        self.app.all_items = [dict(TARGET)]
        self.app._all_bases = [dict(ROW), dict(ROW, base='Other', infobase_id='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee')]
        self.app._switch_mode('bases')
        self.app.tree.selection_set('0')
        self.app.root.update()
        return self.app

    def test_button_is_only_in_infobases_and_needs_one_selected_row(self):
        app = self.listing()
        self.assertEqual(app.open_base_button.winfo_manager(), 'pack')
        self.assertEqual(str(app.open_base_button.cget('state')), 'normal')
        app.tree.selection_set(('0', '1'))
        app.root.update()
        self.assertEqual(str(app.open_base_button.cget('state')), 'disabled')
        app._switch_mode('servers')
        self.assertFalse(app.open_base_button.winfo_manager())

    def test_open_and_cancel_controls_remain_visible_in_small_window(self):
        app = self.listing()
        app.root.geometry('960x540')
        app.root.deiconify()
        key = app._base_identity(ROW)
        app._opening_bases[key] = dict(row=ROW, cancel=threading.Event())
        app._set_busy()
        app.root.update()
        for button in (app.open_base_button, app.cancel_open_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
            right = button.winfo_rootx() + button.winfo_width()
            self.assertLessEqual(right, app.root.winfo_rootx() + app.root.winfo_width())

    def test_double_click_opens_clicked_row_without_pinning_or_launching_mmc(self):
        app = self.listing()
        event = SimpleNamespace(x=15, y=35)
        with patch.object(app.tree, 'identify_region', return_value='cell'), patch.object(app.tree, 'identify_row', return_value='1'), patch.object(app, '_open_base') as launch, patch.object(app, '_do_connect') as mmc:
            app._on_dbl(event)
        self.assertEqual(app.tree.selection(), ('1',))
        launch.assert_called_once_with()
        mmc.assert_not_called()
        self.assertEqual(len(app.tree.get_children()), 2)
        self.assertIsNone(app._base_pin)

    def test_worker_captures_selection_and_allows_other_tabs_until_client_starts(self):
        app = self.listing()
        app.user_var.set('ClusterAdmin')
        app.pwd_var.set('cluster-secret')
        with patch.object(app_module.threading, 'Thread') as thread:
            app.open_base_button.invoke()
        key = app._base_identity(ROW)
        self.assertIn(key, app._opening_bases)
        self.assertIn(('open-base', key), app.running_tasks.entries)
        self.assertEqual(str(app.open_base_button.cget('state')), 'disabled')
        app.tree.selection_set('1')
        app.root.update()
        self.assertEqual(str(app.open_base_button.cget('state')), 'normal')
        app._switch_mode('pg_dump')
        self.assertEqual(str(app.postgres.execute_button.cget('state')), 'normal')
        process = Mock(pid=123)
        process.poll.return_value = None
        result = dict(kind='тонкий', version=VERSION, address='Srvr="actual-cluster:4541";Ref="База";', cluster=CONTEXT['cluster'], process=process)
        with patch.object(app_module, 'prepare_client', return_value=dict(path='fixture')) as prepare, patch.object(app_module, 'start_client', return_value=result) as start:
            thread.call_args.kwargs['target']()
        self.assertEqual(prepare.call_args.args[0]['infobase_id'], ROW['infobase_id'])
        self.assertEqual(prepare.call_args.args[3:5], ('ClusterAdmin', 'cluster-secret'))
        app._drain_events()
        self.assertEqual(app.mode, 'pg_dump')
        self.assertFalse(app._opening_bases)
        self.assertEqual(app._all_bases[0]['cluster_port'], '4541')
        self.assertEqual(app.activity.snapshot('history')[0]['status'], 'Клиент запущен')
        process.terminate.assert_not_called()

    def test_duplicate_requests_are_ignored_and_cancel_stops_preparation(self):
        app = self.listing()
        with patch.object(app_module.threading, 'Thread') as thread:
            app._open_base()
            app._open_base()
        self.assertEqual(thread.call_count, 1)
        key, job = next(iter(app._opening_bases.items()))
        app.cancel_open_button.invoke()
        self.assertTrue(job['cancel'].is_set())
        with patch.object(app_module, 'prepare_client', side_effect=OperationCancelled('Cancelled')), patch.object(app_module, 'start_client') as start:
            thread.call_args.kwargs['target']()
        app._drain_events()
        start.assert_not_called()
        self.assertFalse(app._opening_bases)
        self.assertEqual(app.activity.snapshot('history')[0]['status'], 'Прервано')

    def test_lookup_failure_is_visible_without_attempting_to_start_client(self):
        app = self.listing()
        with patch.object(app_module.threading, 'Thread') as thread:
            app._open_base()
        with patch.object(app_module, 'prepare_client', side_effect=ValueError('Нужен клиент 8.3.27.1644')), patch.object(app_module, 'start_client') as start:
            thread.call_args.kwargs['target']()
        app._drain_events()
        self.assertIn('8.3.27.1644', app._notice[1])
        self.assertTrue(app.form_notice.winfo_manager())
        self.assertFalse(app._opening_bases)
        start.assert_not_called()

    def test_root_close_waits_only_for_preparation_not_for_launched_client(self):
        app = self.listing()
        with patch.object(app_module.threading, 'Thread'):
            app._open_base()
        with patch.object(app.root, 'destroy') as destroy:
            app._on_close()
        destroy.assert_not_called()
        app._opening_bases.clear()
        process = Mock()
        process.poll.return_value = None
        app._opened_clients.append(process)
        with patch.object(app, '_save_settings'), patch.object(app.bookmarks, 'close'), patch.object(app.root, 'destroy') as destroy:
            app._on_close()
        destroy.assert_called_once()
        process.terminate.assert_not_called()
