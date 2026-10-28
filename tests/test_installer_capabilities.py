"""Actual package metadata and inspection boundaries; no installer is executed."""
import base64
import io
import json
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from installer_fixtures import deb_bytes, run_help
import linux_platform
from linux_packages import inspect_debs
from linux_platform_runner import Runner, run_capabilities, run_arguments
from platform_install import install_properties
from test_platform_install import metadata
from test_linux_platform import Channel, Sftp
import test_service_ui


class CapabilitiesTests(unittest.TestCase):
    def test_run_requires_explicit_allowed_list_and_ignores_other_help_mentions(self):
        for help_text in ('client_full server server_admin ru', '--enable-components Components\nDefault: server,ru\n--mode unattended'):
            with self.subTest(help_text=help_text), self.assertRaisesRegex(ValueError, 'состав не подтверждён'):
                run_capabilities(help_text)
        result = run_capabilities(run_help('client_thin ru', directory='') + '\nserver and server_admin are not in this installer')
        self.assertEqual(result['features'], ['client_thin'])
        self.assertEqual(result['languages'], ['EN', 'RU'])
        self.assertIsNone(result['directory_option'])
        with self.assertRaises(ValueError):
            run_arguments('/setup', dict(features=['server']), run_help('client_thin ru'))

    def test_deb_reads_binary_owners_and_ras_without_inventing_missing_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'arbitrary-name.deb'
            path.write_bytes(deb_bytes('1c-enterprise83-server', ['opt/1cv8/ragent', 'opt/1cv8/rac', 'opt/1cv8/ras']))
            result = inspect_debs([path], '8.3.27.2342', threading.Event())
            self.assertEqual(result['features'], ['server', 'server_admin'])
            self.assertEqual(result['ras_features'], ['server'])
            self.assertEqual(result['bundled_features']['server_admin']['with_feature'], 'server')
            self.assertEqual(result['packages'][0]['package'], '1c-enterprise83-server')
            self.assertNotIn('client_thin', result['features'])

    def test_msi_level_zero_is_unavailable_even_when_addlocal_is_explicit(self):
        package = metadata()
        package['features'][0]['level'] = '0'
        with self.assertRaises(ValueError):
            install_properties(package, dict(features=['FileVersion']))
        self.assertIn('CSServer', install_properties(package, dict(features=['CSServer']))['ADDLOCAL'])

    def test_run_inspection_executes_only_help_and_keeps_prepared_distribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / 'setup.run'
            file.write_bytes(b'not executed')
            runner = Runner(dict(format='run', installer=file.name), Mock())
            with patch.object(runner, 'validate_files', return_value=(root, 'amd64')), \
                    patch.object(runner, 'run', return_value=run_help()) as run, \
                    patch.object(runner, 'run_dependencies') as dependencies:
                result = runner.inspect()
            self.assertTrue(result['inspected'])
            self.assertIn('server', result['capabilities']['features'])
            run.assert_called_once_with([str(file), '--help'], capture=True)
            dependencies.assert_not_called()
            self.assertTrue(file.exists())

    def test_inspected_files_are_reused_on_same_vm_without_second_sftp_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'setup.run'
            file.write_bytes(b'not executed')
            package = dict(root=directory, path=str(file), files=[str(file)], format='run', architecture='64',
                           properties=dict(ProductVersion='8.3.27.2342'), features=[dict(id='server')], capabilities_verified=True)
            package['remote_prepared'] = dict(token='a' * 32, connection=('vm', 22, 'root'),
                 source_key=[(str(file), file.stat().st_size, file.stat().st_mtime_ns)], files=[dict(name=file.name, sha256='fixture')])
            client, sftp = Mock(), Sftp()
            sftp.put = Mock()
            client.__enter__, client.__exit__ = Mock(return_value=client), Mock(return_value=False)
            client.open_sftp.return_value = sftp
            channel = Channel((json.dumps(dict(kind='result', result=dict(installed=True))) + '\n').encode())
            client.exec_command.return_value = io.StringIO(), Mock(channel=channel), io.StringIO()
            def preflight(ssh, args, *other, **kwargs):
                return 'ID=ubuntu' if args[0] == 'cat' else '0' if args[0] == 'id' else 'x86_64' if args[0] == 'uname' else 'Python 3.12.15' if args[-1] == '--version' else ''
            with patch('linux_platform.connect', return_value=client), patch('linux_platform.command', side_effect=preflight):
                result = linux_platform.install(package, dict(features=['server']), dict(host='vm', user='root'), Mock(), threading.Event())
            self.assertTrue(result['installed'])
            sftp.put.assert_not_called()
            payload = json.loads(base64.b64decode(shlex.split(client.exec_command.call_args.args[0])[-1]))
            self.assertEqual(payload['token'], 'a' * 32)
            self.assertEqual(payload['action'], 'install')


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_unknown_msi_and_level_zero_have_different_reasons(self):
        panel = self.panel()
        self.assertEqual(str(panel.feature_widgets['FileVersion'].cget('state')), 'normal')
        self.assertIn('После проверки', panel.capabilities_note.cget('text'))
        package = metadata()
        package['features'][0]['level'] = '0'
        panel.package_ready(package)
        self.assertIn('Level=0', panel.feature_widgets['FileVersion'].cget('text'))
        self.assertFalse(panel.feature_values['FileVersion'].get())
        self.assertEqual(str(panel.feature_widgets['CSServer'].cget('state')), 'normal')
        self.assertEqual(str(panel.hasp_widget.cget('state')), 'disabled')

    def test_linux_deb_groups_inseparable_components_and_service_is_only_a_user_choice(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        package = dict(kind='linux', path='fixture.deb', format='deb', properties=dict(ProductVersion='8.3.27.2342'),
                       features=[dict(id='server'), dict(id='server_admin')], capabilities_verified=True,
                       bundled_features=dict(server_admin=dict(package='server-package', with_feature='server')), ras_features=['server'])
        panel.package_ready(package)
        self.assertFalse(panel.service.get())
        self.assertTrue(panel.feature_values['server_admin'].get())
        self.assertEqual(panel.feature_widgets['server_admin'].winfo_manager(), '')
        self.assertEqual(str(panel.feature_widgets['server'].cget('state')), 'normal')
        self.assertIn('включает: Администрирование', panel.feature_widgets['server'].cget('text'))
        self.assertEqual(panel.settings()['features'], ['server'])
        self.assertEqual(str(panel.directory_entry.cget('state')), 'disabled')
        self.assertEqual(str(panel.language_combo.cget('state')), 'disabled')
        panel.service_widget.invoke()
        self.assertTrue(panel.service.get())
        panel.service_widget.invoke()
        panel.update_features()
        self.assertFalse(panel.service.get())
        panel.service_port.set('4540')
        self.assertEqual(panel.linux_service_data.get(), '/home/usr1cv8/.1cv8/1C/1cv8/srvinfo_4541')
        panel.linux_service_data.set('/custom/data')
        panel.service_port.set('1640')
        self.assertEqual(panel.linux_service_data.get(), '/custom/data')
        panel.feature_values['server'].set(False)
        panel.update_features()
        self.assertFalse(panel.feature_values['server_admin'].get())

    def test_run_uses_only_verified_languages_directory_and_features(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        package = dict(kind='linux', path='setup.run', format='run', properties=dict(ProductVersion='8.3.27.2342'),
                       features=[dict(id='client_thin')], languages=['EN', 'DE'], directory_option=None, capabilities_verified=True)
        panel.package_ready(package)
        self.assertEqual(str(panel.feature_widgets['client_thin'].cget('state')), 'normal')
        self.assertEqual(str(panel.feature_widgets['server'].cget('state')), 'disabled')
        self.assertEqual(tuple(panel.language_combo.cget('values')), ('English', 'Deutsch'))
        self.assertEqual(str(panel.directory_entry.cget('state')), 'disabled')
        self.assertFalse(panel.service.get())
