"""ITS fixtures and isolated setup/Designer substitutes; no real DB or installer."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import sys
import subprocess
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from configuration_catalog import ConfigurationPortal, russian_projects, project_releases, configuration_distributions
from configuration_requirements import requirements, requirements_text
import configuration_install as backend
from install_workspace import create_configuration_workspace, cleanup_workspace
from postgres_runner import OperationCancelled
import test_service_ui
from test_cluster_infobase import CLUSTER, INFOBASE, TARGET, PARAMS
import test_cluster_infobase as cluster_fixtures

PROJECT = dict(nick='HRMCorp30', title='Зарплата и управление персоналом КОРП', version='3.1.38.123', date='07.10.2026', url='https://releases.1c.ru/project/HRMCorp30')
RELEASE = dict(version=PROJECT['version'], date=PROJECT['date'], project=PROJECT, url='https://releases.1c.ru/version_files?nick=HRMCorp30&ver=3.1.38.123')
DISTRIBUTION = dict(title='Полный дистрибутив', kind='Полная установка', filename='HRMCorp_setup1c.zip', url='https://releases.1c.ru/template?path=HRMCorp_setup1c.zip', version=PROJECT['version'], project=PROJECT)
MFT = '''Vendor=1C Company
Name=HRMCorp
Version=3.1.38.123
AppVersion=8.3
[Config1]
Catalog=1С:Зарплата/Зарплата КОРП
Destination=1C\\HRMCorp
Source=1cv8.cf
[Config2]
Catalog=1С:Зарплата/Зарплата КОРП (демо)
Destination=1C\\DemoHRMCorp
Source=1Cv8.dt
'''


def write_templates(folder, encoding='utf-8-sig'):
    folder = Path(folder) / '1C/HRMCorp/3_1_38_123'
    folder.mkdir(parents=True)
    (folder / '1cv8.mft').write_text(MFT, encoding=encoding)
    (folder / '1cv8.cf').write_bytes(b'configuration-fixture')
    (folder / '1Cv8.dt').write_bytes(b'demo-fixture')
    return folder


class CatalogTests(unittest.TestCase):
    def test_platform_minimum_comes_from_the_named_column_of_each_release(self):
        html = '''<table><tr><th>Версия</th><th>Дата</th><th>Обновление версии</th><th>Минимальная версия платформы</th></tr>
        <tr><td><a href="/version_files?nick=HRMCorp30&amp;ver=3.1.38.123">3.1.38.123</a></td>
        <td>07.10.26</td><td>8.3.99.9999</td><td>8.3.27.1688</td></tr>
        <tr><td><a href="/version_files?nick=HRMCorp30&amp;ver=3.1.34.332">3.1.34.332</a></td>
        <td>20.08.26</td><td>3.1.34.317</td><td>8.3.24.1548</td></tr></table>'''
        rows = project_releases(html, PROJECT, PROJECT['url'])
        self.assertEqual([r['platform_minimum'] for r in rows], ['8.3.27.1688', '8.3.24.1548'])
        self.assertEqual(rows[0]['platform_requirements']['minimum'], '8.3.27.1688')

    def test_official_notice_retains_minimum_recommended_and_restrictions(self):
        notice = ('Текущая версия конфигурации предназначена для использования с версией технологической платформы '
                  '1С:Предприятие 8 не ниже 8.3.27.1688 (рекомендованная 8.3.27.2130 и выше).')
        value = requirements('<div><div><p>' + notice + '</p></div></div>', RELEASE['url'])
        self.assertEqual(value['minimum'], '8.3.27.1688')
        self.assertEqual(value['recommended'], '8.3.27.2130')
        self.assertEqual(value['text'], notice)
        branches = requirements('<p>Для использования платформы 8.3.17 требуется 8.3.17.1851; '
                                '8.3.15.2100 не поддерживается.</p>', RELEASE['url'])
        self.assertEqual(branches['minimum'], '')
        self.assertIn('не поддерживается', branches['text'])
        self.assertIn('не указана', requirements_text(branches))

    def test_random_config_version_or_appversion_is_not_a_platform_requirement(self):
        for html in ('<p>Конфигурация 8.3.27.1688</p>', '<p>AppVersion=8.3</p>',
                     '<p>Версия платформы 8.3.27.1688 в примере кода</p>'):
            self.assertFalse(requirements(html, RELEASE['url'])['known'])
        row = project_releases('<table><tr><td><a href="/version_files?nick=HRMCorp30">3.1.38.123</a></td></tr></table>', PROJECT, PROJECT['url'])[0]
        self.assertEqual(row['platform_minimum'], 'Не указана')

    def test_explicit_release_notice_takes_precedence_and_retains_table_difference(self):
        value = requirements('<p>Платформа не ниже 8.3.27.2130.</p>', RELEASE['url'], '8.3.27.1688')
        self.assertEqual(value['minimum'], '8.3.27.2130')
        self.assertIn('В таблице релизов указано: 8.3.27.1688', requirements_text(value))

    def test_redirect_to_another_release_is_rejected_before_display(self):
        portal = ConfigurationPortal()
        portal.page = Mock(return_value=(RELEASE['url'].replace('3.1.38.123', '3.1.34.332'),
                                        '<div>Платформа не ниже 8.3.24.1548.</div>'))
        with self.assertRaisesRegex(RuntimeError, 'другого релиза'):
            portal.configuration_details(RELEASE)

    def test_details_fetch_only_chosen_release_and_cache_defensive_copies(self):
        portal = ConfigurationPortal()
        portal.page = Mock(return_value=(RELEASE['url'], '<div>Платформа не ниже 8.3.27.1688 (рекомендованная 8.3.27.2130).</div>'
                                        '<a href="/template?path=setup.zip">Полный дистрибутив</a>'))
        info = portal.configuration_details(RELEASE)
        self.assertEqual(info['release']['platform_requirements']['recommended'], '8.3.27.2130')
        self.assertEqual(info['distributions'][0]['platform_requirements']['minimum'], '8.3.27.1688')
        info['distributions'][0]['platform_requirements']['minimum'] = 'modified'
        self.assertEqual(portal.configuration_distributions(RELEASE)[0]['platform_requirements']['minimum'], '8.3.27.1688')
        portal.page.assert_called_once_with(RELEASE['url'])
        portal.cancel.set()
        with self.assertRaises(OperationCancelled):
            portal.configuration_details(RELEASE)

    def test_expired_login_does_not_turn_into_unknown_requirements(self):
        portal = ConfigurationPortal()
        portal.page = Mock(return_value=('https://login.1c.ru/login', '<form>Login</form>'))
        with self.assertRaisesRegex(RuntimeError, 'повторный вход'):
            portal.configuration_details(RELEASE)

    def test_only_official_russian_group_derived_from_heading(self):
        html = '''<table><tr group="new-id"><td>Типовые конфигурации фирмы "1С" для России</td></tr>
        <tr parent-group="new-id"><td><a href="/project/HRMCorp30">Зарплата</a>
        <a href="/project/HRMCorp30#plan-release">План</a></td><td class="versionColumn actualVersionColumn">3.1.38.123</td><td class="releaseDate">07.10.26</td></tr>
        <tr group="2"><td>Конфигурации для других стран</td></tr><tr parent-group="2"><td><a href="/project/Foreign">Other</a></td></tr>
        <tr parent-group="new-id"><td><a href="https://example.com/project/Fake">Fake</a></td></tr></table>'''
        result = russian_projects(html)
        self.assertEqual([r['nick'] for r in result], ['HRMCorp30'])
        self.assertEqual(result[0]['date'], '07.10.2026')
        self.assertEqual(result[0]['version'], '3.1.38.123')
        with self.assertRaises(RuntimeError):
            russian_projects(html.replace('для России', 'для Казахстана'))

    def test_configuration_versions_are_not_restricted_to_platform_8(self):
        html = '<table><tr><td><a href="/version_files?nick=HRMCorp30&amp;ver=3.1.38.123">3.1.38.123</a></td><td>07.10.26</td></tr><tr><td><a href="/version_files?nick=Foreign">1.2.3</a></td></tr></table>'
        rows = project_releases(html, PROJECT, PROJECT['url'])
        self.assertEqual([r['version'] for r in rows], ['3.1.38.123'])
        self.assertEqual(rows[0]['project'], PROJECT)

    def test_distribution_list_filters_readme_and_web_documents_and_keeps_full_first(self):
        html = ''.join('<a href="/template?path=' + file + '">' + name + '</a>' for file, name in
                       [('ReadMe.txt', 'ReadMe.txt'), ('news.htm', 'Новое в версии'), ('updsetup.zip', 'Дистрибутив обновления'),
                        ('full_setup1c.zip', 'Полный дистрибутив')])
        rows = configuration_distributions(html, RELEASE, RELEASE['url'])
        self.assertEqual([r['kind'] for r in rows], ['Полная установка', 'Обновление'])


class DesignerTests(unittest.TestCase):
    def test_exact_pe_version_wins_over_stale_list_or_misleading_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wanted = root / '8.3.27.1644/bin/1cv8.exe'
            stale = root / '8.3.27.2170/bin/1cv8.exe'
            misleading = root / 'other/bin/1cv8.exe'
            for path in (wanted, stale, misleading):
                path.parent.mkdir(parents=True)
                path.write_bytes(b'fixture')
            def version(path):
                return '8.3.27.1644' if Path(path) == wanted else '8.3.27.2170'
            with patch.object(backend, 'platform_roots', return_value=[root]), patch.object(backend, 'executable_version', side_effect=version):
                self.assertEqual(backend.designer_for_version('8.3.27.1644', str(stale)), str(wanted.resolve()))
                with self.assertRaisesRegex(ValueError, '8.3.27.9999'):
                    backend.designer_for_version('8.3.27.9999', str(misleading))

    def test_matching_custom_path_is_accepted_after_reading_its_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / '1cv8.exe'
            path.write_bytes(b'fixture')
            with patch.object(backend, 'platform_roots', return_value=[]), patch.object(backend, 'executable_version', return_value=TARGET['version']):
                self.assertEqual(backend.designer_for_version(TARGET['version'], str(path)), str(path.resolve()))

    def test_native_version_resource_is_read_without_launching_the_executable(self):
        rac = Path(__file__).resolve().parents[1] / 'vendor/1c-ras/bin/rac.exe'
        self.assertEqual(backend.executable_version(rac), '8.5.1.1529')

    def test_windows_native_process_receives_connection_string_with_spaces_and_quotes_intact(self):
        with tempfile.TemporaryDirectory(prefix='КА native quotes ') as temporary:
            directory = Path(temporary)
            script = directory / 'argv probe.py'
            script.write_text('import json,sys;print(json.dumps(sys.argv[1:],ensure_ascii=True))', encoding='utf-8')
            connection = backend._connection_file(directory / 'Чистая база')
            server = 'Srvr="cluster-host:4541";Ref="База ""Тест""";'
            args = [sys.executable, str(script), 'CREATEINFOBASE', connection, '/IBConnectionString', server,
                    '/UseTemplate', str(directory / 'Шаблон базы.cf')]
            command = backend.one_c_command(args)
            self.assertIn('"File=""', command)
            self.assertNotIn('File=\\"', command)
            output = subprocess.check_output(command, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(json.loads(output), args[2:])

    def test_live_agent_version_and_cluster_endpoint_are_read_in_one_connection(self):
        client, _ = cluster_fixtures.ClientTests.client(self)
        with patch.object(client, 'run', side_effect=[cluster_fixtures.CLUSTERS + 'host : actual-host\n', '8.3.27.1644\n']) as run:
            context = client.deployment_context(CLUSTER)
        self.assertEqual(context['port'], '4541')
        self.assertEqual(context['host'], 'actual-host')
        self.assertEqual(context['platform_version'], '8.3.27.1644')
        self.assertEqual(run.call_args.args[0][1:3], ['agent', 'version'])
        with patch.object(client, 'run', return_value='unknown'):
            with self.assertRaisesRegex(ValueError, 'версию платформы'):
                client.agent_version('rac.exe', '127.0.0.1:1545')


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_manifest_decides_variants_and_checks_selected_version(self):
        for index, encoding in enumerate(('utf-8-sig', 'cp1251', 'utf-16')):
            root = self.root / str(index)
            write_templates(root, encoding)
            rows = backend.templates_in(root, PROJECT['version'])
            self.assertEqual([r['variant'] for r in rows], ['Пустая', 'Демонстрационная'])
            self.assertEqual([r['demo'] for r in rows], [False, True])
            self.assertEqual(backend.templates_in(root, '1.0.0'), [])

    def test_manifest_rejects_source_escape_or_missing_file(self):
        folder = write_templates(self.root)
        for source in ('..\\..\\outside.cf', 'C:\\outside.cf', '\\\\host\\share\\outside.dt', '1cv8.cf:stream'):
            (folder / '1cv8.mft').write_text(MFT.replace('Source=1cv8.cf', 'Source=' + source), encoding='utf-8')
            with self.subTest(source=source), self.assertRaises(ValueError):
                backend.templates_in(self.root)

    def test_setup_destination_restores_original_bytes_on_failure_and_keeps_concurrent_edits(self):
        config = self.root / 'roaming/1C/1CEStart/1cestart.cfg'
        config.parent.mkdir(parents=True)
        original = b'\xef\xbb\xbfConfigurationTemplatesLocation=C:\\Existing\r\nDefaultVersion=8.3\r\n'
        config.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with backend.isolated_template_destination(self.root / 'private', threading.Event(), config):
                self.assertTrue(backend.text_file(config).startswith('ConfigurationTemplatesLocation=' + str(self.root / 'private')))
                raise RuntimeError('fixture')
        self.assertEqual(config.read_bytes(), original)
        with backend.isolated_template_destination(self.root / 'private', threading.Event(), config):
            config.write_text(backend.text_file(config) + 'NewSetting=keep\n', encoding='utf-16')
        text = backend.text_file(config)
        self.assertIn('NewSetting=keep', text)
        self.assertIn('C:\\Existing', text)
        self.assertNotIn(str(self.root / 'private'), text)

    def test_setup_destination_removes_only_new_empty_config_directories(self):
        config = self.root / 'new/1C/1CEStart/1cestart.cfg'
        with backend.isolated_template_destination(self.root / 'private', threading.Event(), config):
            self.assertTrue(config.is_file())
        self.assertFalse((self.root / 'new').exists())
        self.assertTrue((self.root / 'private').is_dir())

    def test_setup_cancel_before_lock_acquired_does_not_modify_config(self):
        cancel = threading.Event()
        cancel.set()
        backend._SETUP_LOCK.acquire()
        try:
            with self.assertRaises(OperationCancelled):
                with backend.isolated_template_destination(self.root / 'private', cancel, self.root / 'no.cfg'):
                    self.fail('Acquired a busy lock despite cancellation')
        finally:
            backend._SETUP_LOCK.release()
        self.assertFalse((self.root / 'no.cfg').exists())

    def test_native_failure_redacts_password_in_log_and_error(self):
        log = self.root / 'run.log'
        code = 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("secret-password", encoding="utf-8");sys.exit(7)'
        events = []
        with self.assertRaises(RuntimeError) as error:
            backend.run_native([sys.executable, '-c', code, str(log)], events.append, threading.Event(), log, ('secret-password',))
        self.assertNotIn('secret-password', str(error.exception))
        self.assertNotIn('secret-password', str(events))
        self.assertIn('код 7', str(error.exception))

    def test_native_cancellation_stops_process_promptly(self):
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        with self.assertRaises(OperationCancelled):
            backend.run_native([sys.executable, '-c', 'import time;time.sleep(60)'], Mock(), cancel)
        self.assertLess(time.monotonic() - start, 4)

    def test_setup_runs_with_private_destination_and_archive_is_removed(self):
        folder = self.root / 'downloads'
        config = self.root / 'roaming/1cestart.cfg'
        portal = Mock()
        def download(distribution, root, event):
            path = Path(root) / 'full.zip'
            path.write_bytes(b'fixture')
            return path
        def unpack(archive, event, cancel):
            path = Path(archive).parent / 'unpacked'
            path.mkdir()
            (path / 'setup.exe').write_bytes(b'fixture')
            return path
        def setup(args, event, cancel):
            self.assertEqual(args[1:], ['/s'])
            # Match the native installer's UTF-16LE config reader, not our
            # permissive text_file helper (which used to hide this defect).
            raw = config.read_bytes()
            self.assertTrue(raw.startswith(b'\xff\xfe'))
            destination = raw[2:].decode('utf-16-le').splitlines()[0].partition('=')[2]
            write_templates(destination)
        portal.download.side_effect = download
        with patch.object(backend, 'launcher_config_path', return_value=config), patch.object(backend, 'unpack_distribution', side_effect=unpack), patch.object(backend, 'run_native', side_effect=setup):
            result = backend.install_configuration(portal, DISTRIBUTION, folder, Mock(), threading.Event())
        self.assertEqual(len(result['templates']), 2)
        self.assertFalse(config.exists())
        self.assertEqual(len(list(folder.rglob('ka-install-*'))), 1)
        self.assertEqual(list(folder.rglob('*.zip')), [])
        self.assertEqual(list(folder.rglob('setup.exe')), [])
        backend.cleanup_configuration(result, Mock())
        self.assertFalse(folder.exists())


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        TemplateTests.setUp(self)
        patcher = patch.object(backend, 'designer_for_version', side_effect=lambda version, preferred, versions: preferred)
        patcher.start()
        self.addCleanup(patcher.stop)
    def package(self):
        context = create_configuration_workspace(self.root / 'downloads', PROJECT['nick'], PROJECT['version'])
        write_templates(Path(context['root']) / 'templates')
        rows = backend.templates_in(context['root'], PROJECT['version'])
        return dict(workspace=context, templates=rows, distribution=DISTRIBUTION), rows

    def plan(self, new=False):
        exe = self.root / '1cv8.exe'
        exe.write_bytes(b'fixture')
        return dict(target=TARGET, versions={}, cluster=dict(cluster=CLUSTER, host='cluster-host', port='4541'),
                    base=dict(infobase=INFOBASE, name='Existing'), new=new, parameters=dict(PARAMS) if new else None,
                    executable=str(exe), folder=str(self.root / 'downloads'), ib_user='Admin', ib_password='secret-password')

    def native(self, args, event, cancel, log=None, secrets=()):
        args = [str(a) for a in args]
        if '/DumpResult' in args:
            Path(args[args.index('/DumpResult') + 1]).write_text('0', encoding='utf-8')
        if 'CREATEINFOBASE' in args:
            directory = Path(args[2].split('"')[1])
            directory.mkdir()
            (directory / '1Cv8.1CD').write_bytes(b'fixture')
        if '/DumpIB' in args:
            Path(args[args.index('/DumpIB') + 1]).write_bytes(b'empty-dt-fixture')

    def client(self, mock):
        client = mock.return_value
        client.connect.return_value = nullcontext(('rac.exe', 'localhost:9999'))
        client.create.return_value = dict(infobase_id=INFOBASE, name=PARAMS['name'], settings_error='')
        client.deployment_context.return_value = dict(cluster=CLUSTER, host='cluster-host', port='4541', platform_version=TARGET['version'])
        return client

    def test_empty_cf_is_converted_to_clean_dt_then_restored_and_all_owned_files_removed(self):
        package, rows = self.package()
        keep = self.root / 'keep.dt'
        keep.write_bytes(b'user-file')
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native', side_effect=self.native) as native:
            client = self.client(cls)
            result = backend.deploy_configuration(package, rows[0], self.plan(), Mock(), threading.Event())
        self.assertTrue(result['loaded'])
        self.assertEqual(native.call_count, 3)
        restore = [str(a) for a in native.call_args.args[0]]
        self.assertIn('/RestoreIB', restore)
        self.assertNotIn('/LoadCfg', restore)
        self.assertIn('Srvr="cluster-host:4541";Ref="Existing";', restore)
        client.create.assert_not_called()
        client.require_base.assert_called_once()
        self.assertFalse((self.root / 'downloads').exists())
        self.assertTrue(keep.is_file())

    def test_new_demo_registers_once_then_restores_dt_without_local_filebase(self):
        package, rows = self.package()
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native', side_effect=self.native) as native:
            client = self.client(cls)
            result = backend.deploy_configuration(package, rows[1], self.plan(True), Mock(), threading.Event())
        client.create.assert_called_once_with(CLUSTER, PARAMS)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(result['name'], PARAMS['name'])

    def test_failed_restore_keeps_templates_and_registration_without_drop_or_retry(self):
        package, rows = self.package()
        events = []
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native', side_effect=RuntimeError('fixture')) as native:
            client = self.client(cls)
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                backend.deploy_configuration(package, rows[1], self.plan(True), events.append, threading.Event())
        self.assertEqual(native.call_count, 1)
        client.create.assert_called_once()
        client.drop.assert_not_called()
        self.assertTrue(Path(package['workspace']['root']).is_dir())
        self.assertIn('ИБ уже создана', str(events))
        self.assertEqual(len(list((self.root / 'downloads').rglob('ka-install-*'))), 1)

    def test_missing_result_never_reports_success_or_cleans_installed_templates(self):
        package, rows = self.package()
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native'):
            self.client(cls)
            with self.assertRaisesRegex(RuntimeError, 'не подтверждена'):
                backend.deploy_configuration(package, rows[1], self.plan(), Mock(), threading.Event())
        self.assertTrue(Path(rows[1]['path']).exists())

    def test_invalid_new_parameters_and_missing_live_port_fail_before_native_or_mutation(self):
        package, rows = self.package()
        for new in (False, True):
            plan = self.plan(new)
            if new:
                plan['parameters']['name'] = ''
            else:
                plan['cluster']['port'] = ''
            with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native') as native:
                client = self.client(cls)
                if not new:
                    client.deployment_context.return_value['port'] = ''
                with self.assertRaises(ValueError):
                    backend.deploy_configuration(package, rows[0], plan, Mock(), threading.Event())
                if new:
                    cls.assert_not_called()
                client.create.assert_not_called()
                native.assert_not_called()

    def test_cached_missing_port_is_resolved_from_cluster_uuid_before_cf_preparation(self):
        package, rows = self.package()
        plan = self.plan()
        plan['cluster']['port'] = ''
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native', side_effect=self.native):
            client = self.client(cls)
            result = backend.deploy_configuration(package, rows[0], plan, Mock(), threading.Event())
        self.assertTrue(result['loaded'])
        self.assertIn('cluster-host:4541', result['address'])
        client.deployment_context.assert_called_once_with(CLUSTER)

    def test_changed_live_cluster_endpoint_aborts_before_preparing_or_changing_database(self):
        package, rows = self.package()
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native') as native:
            client = self.client(cls)
            client.deployment_context.return_value['port'] = '5541'
            with self.assertRaisesRegex(ValueError, 'изменились'):
                backend.deploy_configuration(package, rows[0], self.plan(), Mock(), threading.Event())
        native.assert_not_called()
        client.create.assert_not_called()

    def test_cleanup_failure_preserves_success_and_allows_cleanup_without_restore(self):
        package, rows = self.package()
        original = backend.cleanup_workspace
        def cleanup(context):
            if context == package['workspace']:
                raise PermissionError('fixture locked file')
            original(context)
        with patch.object(backend, 'ClusterClient') as cls, patch.object(backend, 'run_native', side_effect=self.native) as native, patch.object(backend, 'cleanup_workspace', side_effect=cleanup):
            self.client(cls)
            result = backend.deploy_configuration(package, rows[1], self.plan(), Mock(), threading.Event())
        self.assertTrue(result['loaded'])
        self.assertIn('locked file', result['cleanup_error'])
        self.assertEqual(native.call_count, 1)
        self.assertFalse(backend.cleanup_configuration(package, Mock()))


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('configuration_install')
        return self.app.configuration_install

    def prepared(self, panel):
        panel.metadata = dict(workspace=dict(root='fixture'), distribution=DISTRIBUTION)
        panel.templates = [dict(title='Зарплата (демо)', variant='Демонстрационная', version=PROJECT['version'], path='fixture.dt', demo=True)]
        panel.render('templates')
        panel.trees['templates'].selection_set('0')
        panel.portal = Mock()
        panel.update_actions()

    def test_primary_button_on_templates_deploys_without_downloading_again(self):
        panel = self.panel()
        self.prepared(panel)
        panel.catalog_tabs.select(3)
        self.app.root.update()
        self.assertEqual(panel.execute_button.cget('text'), 'Добавить базу на сервер')
        self.assertEqual(str(panel.execute_button.cget('state')), 'normal')
        with patch.object(panel, 'open_target') as deploy, patch.object(panel, 'execute') as install:
            panel.execute_button.invoke()
            deploy.assert_called_once_with()
            install.assert_not_called()
            panel.catalog_tabs.select(2)
            self.app.root.update()
            self.assertEqual(panel.execute_button.cget('text'), 'Установить')
        panel.catalog_tabs.select(3)
        panel.metadata['loaded'] = True
        panel.update_actions()
        self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')

    def test_page_in_1c_folder_and_single_click_never_downloads(self):
        panel = self.panel()
        panel.distributions, panel.portal = [DISTRIBUTION], Mock()
        panel.render('distributions')
        panel.trees['distributions'].selection_set('0')
        self.app.root.update()
        panel.choose_distribution()
        panel.portal.download.assert_not_called()
        self.assertFalse(panel.running)
        self.assertEqual(self.app.bookmarks.layout.location('configuration_install')[0], 'folder-1c')

    def test_requirements_shown_for_release_and_preserved_after_setup(self):
        panel = self.panel()
        info = requirements('<p>Платформа не ниже 8.3.27.1688 (рекомендованная 8.3.27.2130).</p>', RELEASE['url'])
        release = dict(RELEASE, platform_minimum=info['minimum'], platform_requirements=info)
        distribution = dict(DISTRIBUTION, platform_requirements=info)
        panel.releases = [release]
        panel.render('releases')
        panel.trees['releases'].selection_set('0')
        panel.preview_release()
        self.assertIn('8.3.27.1688', panel.platform_note.cget('text'))
        self.assertIn('8.3.27.2130', panel.platform_note.cget('text'))
        panel.portal = Mock()
        with patch.object(panel, 'work') as work:
            panel.choose_release()
        operation, success = work.call_args.args
        panel.portal.configuration_details.return_value = dict(release=release, distributions=[distribution])
        success(operation(Mock()))
        panel.portal.download.assert_not_called()
        self.prepared(panel)
        panel.installed(dict(panel.metadata, distribution=distribution, templates=panel.templates))
        self.app.root.update()
        self.assertIn('8.3.27.2130', panel.platform_note.cget('text'))
        panel.releases_loaded(PROJECT, [RELEASE])
        self.assertEqual(panel.platform_requirements, {})
        self.assertNotIn('8.3.27.1688', panel.platform_note.cget('text'))

    def test_project_and_version_are_fetched_in_background_and_search_filters(self):
        panel = self.panel()
        panel.projects, panel.portal = [PROJECT, dict(PROJECT, title='Другой продукт', nick='Other')], Mock()
        panel.render('projects')
        panel.searches['projects'].set('зарплата')
        self.assertEqual(panel.trees['projects'].get_children(), ('0',))
        panel.trees['projects'].selection_set('0')
        with patch.object(panel, 'work') as work:
            panel.choose_project()
        self.assertTrue(panel.running)
        operation, success = work.call_args.args
        panel.portal.configuration_releases.return_value = [RELEASE]
        success(operation(Mock()))
        self.assertFalse(panel.running)
        self.assertEqual(panel.catalog_tabs.index(panel.catalog_tabs.select()), 1)

    def test_existing_or_new_target_dialog_uses_cluster_fields_and_is_not_modal(self):
        panel = self.panel()
        self.prepared(panel)
        self.app.all_items = [TARGET]
        panel.open_target(refresh=False)
        window = panel.target_dialog
        self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
        target = window.winfo_children()[0]
        self.assertIsNone(self.app.root.grab_current())
        self.assertFalse(target.new.get())
        self.assertEqual(str(target.name_entry.cget('state')), 'disabled')
        target.toggle_new()
        self.assertEqual(str(target.name_entry.cget('state')), 'normal')
        self.assertTrue(target.deny_jobs.get())
        target.dbms.set('PostgreSQL')
        self.assertEqual(target.date_offset.get(), '0')
        target.dbms.set('Microsoft SQL Server')
        self.assertEqual(target.date_offset.get(), '2000')
        self.assertFalse(target.delete_button.winfo_manager())

    def test_target_refresh_marks_owner_running_and_other_pages_remain_usable(self):
        panel = self.panel()
        self.prepared(panel)
        self.app.all_items = [TARGET]
        panel.open_target(refresh=False)
        window = panel.target_dialog
        self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
        target = window.winfo_children()[0]
        with patch('cluster_infobase_ui.ClusterClient') as cls, patch.object(target, 'work') as work:
            target.load_clusters()
        self.assertTrue(panel.running)
        self.assertTrue(target.running)
        self.app._switch_mode('db_copy')
        self.assertEqual(str(self.app.sql_copy.execute_button.cget('state')), 'normal')
        operation, success = work.call_args.args
        cls.return_value.overview.return_value = dict(clusters=[dict(cluster=CLUSTER, name='Main', host='host', port='4541', platform_version=TARGET['version'])], cluster=CLUSTER, bases=[])
        with patch('configuration_install_ui.designer_for_version', return_value='fixture/1cv8.exe'):
            success(operation(Mock()))
        cls.return_value.overview.assert_called_once_with('', include_version=True)
        self.assertFalse(panel.running)
        self.assertFalse(target.running)

    def test_existing_target_confirmation_defaults_to_no_then_captures_exact_selection(self):
        panel = self.panel()
        self.prepared(panel)
        self.app.all_items = [TARGET]
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / '1cv8.exe'
            executable.write_bytes(b'fixture')
            panel.executable.set(str(executable))
            panel.open_target(refresh=False)
            window = panel.target_dialog
            self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
            target = window.winfo_children()[0]
            target.render_clusters([dict(cluster=CLUSTER, name='Main', host='actual-cluster', port='4541', platform_version=TARGET['version'])], CLUSTER)
            target.render_bases([dict(infobase=INFOBASE, name='Destination')])
            target.tree.selection_set('0')
            with patch('configuration_install_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'deploy') as deploy:
                target.execute()
            deploy.assert_not_called()
            self.assertEqual(confirm.call_args.kwargs['default'], 'no')
            self.assertIn('Destination', confirm.call_args.args[1])
            self.assertIn('заменены', confirm.call_args.args[1])
            self.assertFalse(panel.running)
            with patch('configuration_install_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'deploy') as deploy:
                target.execute()
            template, plan = deploy.call_args.args
            self.assertFalse(plan['new'])
            self.assertEqual(plan['base']['infobase'], INFOBASE)
            self.assertEqual(plan['cluster']['port'], '4541')
            self.assertIsNone(panel.target_dialog)

    def test_invalid_target_shows_visible_error_without_starting_work(self):
        panel = self.panel()
        self.prepared(panel)
        panel.open_target(refresh=False)
        window = panel.target_dialog
        self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
        target = window.winfo_children()[0]
        with patch('configuration_install_ui.messagebox.showerror') as error, patch.object(panel, 'deploy') as deploy:
            target.execute()
        error.assert_called_once()
        deploy.assert_not_called()
        self.assertFalse(panel.running)

    def test_successful_deployment_cannot_be_started_again_when_only_cleanup_failed(self):
        panel = self.panel()
        self.prepared(panel)
        panel.deployed(dict(name='Destination', address='Srvr="server:1541";Ref="Destination";', loaded=True,
                            cleanup_error='locked', settings_error='locked'))
        self.assertTrue(panel.metadata['loaded'])
        self.assertEqual(str(panel.deploy_button.cget('state')), 'disabled')
        self.assertEqual(str(panel.cleanup_button.cget('state')), 'normal')
        self.assertIn('База загружена', panel.output.get('1.0', 'end'))

    def test_open_fetches_actual_version_and_port_and_reopening_preserves_metadata(self):
        panel = self.panel()
        self.prepared(panel)
        self.app.all_items = [dict(TARGET, version='8.3.27.2170')]
        with patch('cluster_infobase_ui.ClusterClient') as cls, patch('configuration_install_ui.ConfigurationTargetPanel.work') as work:
            panel.open_target()
        window = panel.target_dialog
        target = window.winfo_children()[0]
        self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
        self.assertTrue(panel.running)
        operation, success = work.call_args.args
        cls.return_value.overview.return_value = dict(clusters=[dict(cluster=CLUSTER, name='Main', host='actual-host', port='4541', platform_version='8.3.27.1644')],
                                                     cluster=CLUSTER, bases=[dict(infobase=INFOBASE, name='Existing')])
        with patch('configuration_install_ui.designer_for_version', return_value='C:/1C/8.3.27.1644/bin/1cv8.exe') as resolver:
            success(operation(Mock()))
        self.assertEqual(resolver.call_args.args[0], '8.3.27.1644')
        self.assertEqual(panel.executable.get(), 'C:/1C/8.3.27.1644/bin/1cv8.exe')
        target.tree.selection_set('0')
        target.update_context()
        self.assertIn('actual-host:4541', target.address.get())
        self.assertEqual(self.app._all_bases[0]['cluster_port'], '4541')
        target.close_dialog()
        panel.open_target(refresh=False)
        window2 = panel.target_dialog
        self.addCleanup(lambda: window2.destroy() if window2.winfo_exists() else None)
        target2 = window2.winfo_children()[0]
        self.assertEqual(target2.selected_cluster()['port'], '4541')
        self.assertEqual(target2.selected_cluster()['platform_version'], '8.3.27.1644')

    def test_missing_cached_port_automatically_refreshes_instead_of_asking_user_for_port(self):
        panel = self.panel()
        self.prepared(panel)
        self.app.all_items = [TARGET]
        panel.open_target(refresh=False)
        window = panel.target_dialog
        self.addCleanup(lambda: window.destroy() if window.winfo_exists() else None)
        target = window.winfo_children()[0]
        target.render_clusters([dict(cluster=CLUSTER, name='Cached')], CLUSTER)
        target.render_bases([dict(infobase=INFOBASE, name='Existing')])
        target.tree.selection_set('0')
        with patch.object(target, 'load_clusters') as refresh, patch('configuration_install_ui.messagebox.showerror') as error:
            target.execute()
        refresh.assert_called_once()
        error.assert_not_called()
        self.assertTrue(target._continue_after_refresh)

