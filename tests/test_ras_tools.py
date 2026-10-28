"""Portable tools, safe fallback, and update cleanup; no real 1C server."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import console_core
from add_cluster_infobase import ClusterClient
from local_ras import SearchCancelled, connect_with_fallback
from postgres_runner import OperationCancelled
import ras_tools


def package(root, version='8.5.1.1529'):
    root = Path(root)
    (root / 'bin').mkdir(parents=True)
    files = {}
    for name in ('rac.exe', 'ras.exe'):
        data = (version + name).encode()
        (root / 'bin' / name).write_bytes(data)
        files['bin/' + name] = hashlib.sha256(data).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(dict(version=version, files=files)), encoding='utf-8')
    return ras_tools.package_tools(root)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.embedded = package(self.root / 'embedded')
        self.local = package(self.root / 'local', '8.3.27.1859')
        self.manager = Mock(stop=threading.Event())
        self.opened, self.closed, self.messages = [], [], []
        @contextlib.contextmanager
        def connect(ras, agent):
            self.opened.append(ras)
            try:
                yield '127.0.0.1:23456'
            finally:
                self.closed.append(ras)
        self.manager.connect.side_effect = connect

    def test_embedded_is_first_and_local_discovery_is_lazy(self):
        with (patch.object(console_core, 'managed_tools', return_value=[self.embedded]),
              patch.object(console_core, 'find_all_rac') as scan):
            tools = console_core.local_tool_candidates(dict(version=self.local[2]), {self.local[2]: self.local[0]})
            self.assertEqual(next(tools), self.embedded)
            scan.assert_not_called()
            scan.return_value = {}
            self.assertEqual(next(tools), self.local)
            scan.assert_called_once()

    def test_probe_failure_closes_embedded_and_keeps_local_open_until_work_finishes(self):
        def probe(rac, endpoint):
            if rac == self.embedded[0]:
                raise RuntimeError('Несовместимая версия сервера')
            return 'cluster : id'
        with connect_with_fallback(self.manager, [self.embedded, self.local], 'agent:1540', probe, self.messages.append) as result:
            self.assertEqual(result, (self.local[0], '127.0.0.1:23456', self.local[2], 'cluster : id'))
            self.assertEqual(self.closed, [self.embedded[1]])
        self.assertEqual(self.closed, [self.embedded[1], self.local[1]])
        self.assertIn('Несовместимая версия', '\n'.join(self.messages))

    def test_recoverable_probe_diagnostics_use_warning_callback(self):
        warnings = []
        probe = Mock(side_effect=[RuntimeError('Ошибка установки соединения\nРазличаются версии клиента и сервера'), 'cluster : id'])
        with connect_with_fallback(self.manager, [self.embedded, self.local], 'agent:1540', probe,
                                   self.messages.append, warning=warnings.append):
            pass
        self.assertEqual(len(warnings), 1)
        self.assertIn('Ошибка установки соединения', warnings[0])
        self.assertNotIn('Ошибка установки соединения', '\n'.join(self.messages))

    def test_failed_start_tries_local_then_mutation_error_never_retries(self):
        original = self.manager.connect.side_effect
        def connect(ras, agent):
            if ras == self.embedded[1]:
                raise OSError('RAS не запущен')
            return original(ras, agent)
        self.manager.connect.side_effect = connect
        with self.assertRaisesRegex(RuntimeError, 'Сервер мог выполнить создание'):
            with connect_with_fallback(self.manager, [self.embedded, self.local, self.embedded], 'agent:1540',
                                       lambda *args: '', self.messages.append):
                raise RuntimeError('Сервер мог выполнить создание')
        self.assertEqual(self.manager.connect.call_count, 2)
        self.assertEqual(self.closed, [self.local[1]])

    def test_cancelled_probe_never_launches_another_version(self):
        for error in (SearchCancelled('stop'), OperationCancelled('stop')):
            self.manager.connect.reset_mock()
            with self.assertRaises(type(error)):
                with connect_with_fallback(self.manager, [self.embedded, self.local], 'agent:1540', Mock(side_effect=error)):
                    self.fail('Must not enter operation')
            self.assertEqual(self.manager.connect.call_count, 1)

    def test_all_failures_include_attempted_versions_and_reasons(self):
        with self.assertRaises(RuntimeError) as caught:
            with connect_with_fallback(self.manager, [self.embedded, self.local], 'agent:1540',
                                       Mock(side_effect=RuntimeError('agent refused'))):
                self.fail('Must not enter operation')
        text = str(caught.exception)
        self.assertIn(self.embedded[2], text)
        self.assertIn(self.local[2], text)
        self.assertIn('agent refused', text)

    def test_cluster_client_probes_before_yield_and_reuses_cluster_list_without_replaying_creation(self):
        client = ClusterClient(dict(server='agent', port='1540'), {}, event=self.messages.append)
        probe = Mock(side_effect=[RuntimeError('wrong version'), 'cluster : id'])
        with (patch('add_cluster_infobase.local_tool_candidates', return_value=[self.embedded, self.local]),
              patch('add_cluster_infobase.LocalRasManager', return_value=self.manager), patch.object(client, 'run', probe)):
            with self.assertRaisesRegex(ValueError, 'mutation failed'):
                with client.connect() as (rac, endpoint):
                    self.assertEqual(client.cluster_rows(rac, endpoint), [dict(cluster='id')])
                    raise ValueError('mutation failed')
        self.assertEqual(probe.call_count, 2)
        self.manager.close.assert_called_once()

    def test_cluster_client_emits_multiline_warning_with_redacted_credentials(self):
        events = []
        client = ClusterClient(dict(server='agent', port='1540'), {}, password='private-fixture', event=events.append)
        probe = Mock(side_effect=[RuntimeError('Ошибка установки соединения private-fixture\nРазличаются версии'), 'cluster : id'])
        with (patch('add_cluster_infobase.local_tool_candidates', return_value=[self.embedded, self.local]),
              patch('add_cluster_infobase.LocalRasManager', return_value=self.manager), patch.object(client, 'run', probe)):
            with client.connect():pass
        warnings = [event for event in events if event['kind'] == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn('Различаются версии', warnings[0]['text'])
        self.assertNotIn('private-fixture', str(events))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'cache'
        self.old = self.root / '8.3.27.1859-aaaaaaaaaaaa'
        package(self.old, '8.3.27.1859')
        self.bundle = Path(self.temp.name) / 'bundle'
        self.portal = Mock()
        self.release = dict(version='8.5.1.1529', date='07.09.2026', url='https://releases.1c.ru/fixture')
        self.portal.releases.return_value = [self.release]
        self.portal.distributions.return_value = [dict(architecture='64', filename='windows64full_fixture.rar')]
        def download(distribution, folder, event):
            path = Path(folder) / 'platform.rar'
            path.write_bytes(b'archive')
            return str(path)
        self.portal.download.side_effect = download
        for target, value in (('ras_tools.cache_root', self.root), ('ras_tools.bundled_root', self.bundle)):
            guard = patch(target, return_value=value)
            guard.start()
            self.addCleanup(guard.stop)
        self.events = []

    def update(self, extract=None, cancel=None):
        def unpack(msi, target, release, event, stop):
            package(target, release['version'])
        with (patch('platform_install.extract_distribution', side_effect=lambda archive, *args: str(Path(archive).with_suffix('.msi'))),
              patch.object(ras_tools, 'extract_tools', side_effect=extract or unpack),
              patch.object(ras_tools, 'probe_package', return_value=self.release['version'])):
            return ras_tools.update_tools(self.portal, self.events.append, cancel or threading.Event())

    def test_success_publishes_complete_package_deletes_old_and_cleans_download_tree(self):
        result = self.update()
        self.assertTrue(result['updated'])
        self.assertEqual(ras_tools.package_tools(result['path'], verify=True)[2], self.release['version'])
        self.assertFalse(self.old.exists())
        self.assertFalse((self.root / '.downloads').exists())
        self.assertEqual(ras_tools.managed_tools()[0][2], self.release['version'])
        self.assertEqual([path for path in self.root.iterdir() if path.is_dir()], [Path(result['path'])])

    def test_running_old_package_is_deleted_after_its_last_task_finishes(self):
        with ras_tools.use_tools(self.old / 'bin/ras.exe'):
            with ras_tools.use_tools(self.old / 'bin/ras.exe'):
                result = self.update()
                self.assertTrue(self.old.exists())
                self.assertTrue((self.old / '.retired').is_file())
                self.assertEqual(len(ras_tools.managed_tools()), 1)
                self.assertEqual(ras_tools.managed_tools()[0][2], result['version'])
            self.assertTrue(self.old.exists())
        self.assertFalse(self.old.exists())

    def test_failure_and_cancellation_keep_previous_package_and_cleanup_partial_downloads(self):
        for error in (RuntimeError('Invalid MSI'), OperationCancelled('stop')):
            with self.assertRaises(type(error)):
                self.update(extract=Mock(side_effect=error))
            self.assertEqual(ras_tools.package_tools(self.old, verify=True)[2], '8.3.27.1859')
            self.assertFalse((self.old / '.retired').exists())
            self.assertFalse((self.root / '.downloads').exists())
            self.assertEqual(list(self.root.iterdir()), [self.old])

    def test_up_to_date_does_not_download_and_still_removes_obsolete_package(self):
        latest = self.root / '8.5.1.1529-bbbbbbbbbbbb'
        package(latest)
        result = self.update()
        self.assertFalse(result['updated'])
        self.portal.download.assert_not_called()
        self.assertFalse(self.old.exists())
        self.assertTrue(latest.exists())

    def test_hash_failure_and_foreign_directory_are_not_accepted_or_deleted(self):
        (self.old / 'bin/ras.exe').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Повреждён'):
            ras_tools.package_tools(self.old, verify=True)
        foreign = self.root / 'existing-platform'
        package(foreign)
        ras_tools.cleanup_old_tools(self.old, self.events.append)
        self.assertTrue(foreign.exists())

    def test_interrupted_old_package_cleanup_retries_without_requiring_deleted_payload(self):
        self.update()
        partial = self.root / '8.3.25.1-cccccccccccc'
        package(partial, '8.3.25.1')
        (partial / '.retired').write_text(json.dumps(dict(package=partial.name)), encoding='utf-8')
        (partial / 'bin/ras.exe').unlink()
        self.assertTrue(ras_tools.remove_retired(partial, self.root))
        self.assertFalse(partial.exists())


class NativeBundleTests(unittest.TestCase):
    def test_shipped_ras_and_rac_versions_and_dependency_hashes(self):
        directory = Path(__file__).resolve().parents[1] / 'vendor/1c-ras'
        self.assertEqual(ras_tools.probe_package(directory, threading.Event()), '8.5.1.1529')


if __name__ == '__main__':
    unittest.main()
