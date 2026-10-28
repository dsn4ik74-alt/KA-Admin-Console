"""Linux unit generation and SSH/UI boundaries; never change system services."""
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from linux_platform_runner import (Runner, LINUX_DATA_DEFAULT, environment_lines, linux_service_options,
                                   render_service_unit, service_destination)
from platform_install_ui import windows_distribution_features
from test_platform_install import metadata
import linux_platform
import test_service_ui

UNIT = '''[Unit]
Description=1C:Enterprise Server 8.3 (8.3.27.2342) (%I)
[Service]
User=usr1cv8
Group=grp1cv8
Environment=SRV1CV8_PORT=1540
Environment=SRV1CV8_REGPORT=1541
Environment=SRV1CV8_RANGE=1560:1591
Environment=SRV1CV8_DEBUG=
Environment=SRV1CV8_DATA=/home/usr1cv8/.1cv8/1C/1cv8
Environment=OTHER=value
ExecStart=/opt/1cv8/x86_64/8.3.27.2342/ragent -d ${SRV1CV8_DATA} -port ${SRV1CV8_PORT} -regport ${SRV1CV8_REGPORT} -range ${SRV1CV8_RANGE} $SRV1CV8_DEBUG
Restart=always
[Install]
DefaultInstance=default
WantedBy=multi-user.target
'''


class UnitTests(unittest.TestCase):
    def test_defaults_and_family_ports_use_cluster_port_in_filename(self):
        defaults = linux_service_options({})
        self.assertEqual(defaults['name'], 'srv1cv8_1541.service')
        self.assertEqual(defaults['data'], LINUX_DATA_DEFAULT)
        self.assertIn('Environment=SRV1CV8_DEBUG=', environment_lines(defaults))
        for port, registration, first, last in (('4540', 4541, 4560, 4591), ('1640', 1641, 1660, 1691)):
            result = linux_service_options(dict(service_port=port))
            self.assertEqual((result['registration'], result['first'], result['last']), (registration, first, last))
            self.assertEqual(result['name'], 'srv1cv8_' + str(registration) + '.service')
        self.assertEqual(linux_service_options(dict(linux_service_name='my-1c.service'))['name'], 'my-1c.service')

    def test_reject_invalid_ports_names_and_paths(self):
        for values in (dict(service_port='1541'), dict(service_port='1560'), dict(service_port='65535'), dict(service_port='abc'),
                       dict(linux_service_name='../other.service'), dict(linux_service_name='/etc/other.service'),
                       dict(linux_service_name='test@.service'), dict(linux_service_name='test'), dict(linux_service_name=''),
                       dict(linux_service_data='relative'), dict(linux_service_data='/'),
                       dict(linux_service_data='/root/../'), dict(linux_service_data='/data\nExecStart=bad')):
            with self.subTest(values=values), self.assertRaises(ValueError):
                linux_service_options(values)

    def test_render_replaces_five_values_and_preserves_vendor_start_account_and_other_settings(self):
        options = linux_service_options(dict(service_port='4540', service_debug=True, linux_service_data='/data/1C'))
        result = render_service_unit(UNIT, options)
        for line in environment_lines(options):
            self.assertEqual(result.count(line + '\n'), 1)
        self.assertNotIn('Environment=SRV1CV8_PORT=1540\n', result)
        self.assertNotIn('DefaultInstance=', result)
        self.assertNotIn('%I', result)
        self.assertIn('(4541)', result)
        self.assertIn('User=usr1cv8\nGroup=grp1cv8', result)
        self.assertIn('Environment=OTHER=value', result)
        self.assertIn('Restart=always', result)
        self.assertEqual(next(line for line in result.splitlines() if line.startswith('ExecStart=')),
                         next(line for line in UNIT.splitlines() if line.startswith('ExecStart=')))
        self.assertLess(result.index('Environment=SRV1CV8_PORT'), result.index('[Install]'))

    def test_debug_false_removes_existing_debug_and_data_is_quoted_and_percent_escaped(self):
        source = UNIT.replace('Environment=SRV1CV8_DEBUG=', 'Environment=SRV1CV8_DEBUG=-debug')
        options = linux_service_options(dict(service_debug=False, linux_service_data='/Data 1C/100%'))
        result = render_service_unit(source, options)
        self.assertIn('Environment=SRV1CV8_DEBUG=\n', result)
        self.assertNotIn('Environment=SRV1CV8_DEBUG=-debug', result)
        self.assertIn('Environment="SRV1CV8_DATA=/Data 1C/100%%"', result)

    def test_overrides_are_after_resets_and_duplicate_original_values(self):
        source = UNIT.replace('Restart=always', 'Environment=\nEnvironment="SRV1CV8_DATA=/old data"\nRestart=always')
        result = render_service_unit(source, linux_service_options({}))
        self.assertGreater(result.index('Environment=SRV1CV8_DATA='), result.index('Environment=\n'))
        self.assertNotIn('/old data', result)

    def test_unrecognized_template_is_rejected(self):
        for source in ('[Unit]\nDescription=bad', UNIT.replace('$SRV1CV8_DEBUG', '')):
            with self.assertRaises(ValueError):
                render_service_unit(source, linux_service_options({}))

    def test_existing_unit_is_rejected_before_installation_changes(self):
        runner = Runner(dict(settings=dict(service=True, features=['server']), root='fixture', token='a' * 32), Mock())
        with patch('linux_platform_runner.validate_stage'), \
                patch('linux_platform_runner.service_destination', side_effect=ValueError('уже существует')), \
                patch.object(runner, 'run') as run, self.assertRaisesRegex(ValueError, 'уже существует'):
            runner.install()
        run.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'srv1cv8_1541.service'
            target.write_text('existing service')
            with self.assertRaisesRegex(ValueError, 'другое имя'):
                service_destination(linux_service_options({}), directory)
            self.assertEqual(target.read_text(), 'existing service')

    def test_copies_regular_unit_and_uses_chosen_name_without_systemctl_link(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'platform'
            base.mkdir()
            target_dir = Path(directory) / 'systemd'
            target_dir.mkdir()
            source = base / 'srv1cv8-8.3.27.2342@.service'
            source.write_text(UNIT, encoding='utf-8')
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            settings = dict(directory=str(base), service_port='4540', service_debug=True, linux_service_data='/Data/1C')
            target = target_dir / 'srv1cv8_4541.service'
            runner, commands, events = Runner({}, Mock()), [], []
            runner.event = events.append
            def run(args, **kwargs):
                commands.append(args)
                return str(target) if args[:2] == ['systemctl', 'show'] else ''
            with patch.object(runner, 'run', side_effect=run), patch.object(runner, 'prepare_service_data') as data:
                self.assertEqual(runner.install_service(settings, '8.3.27.2342', str(target_dir)), str(target))
            self.assertTrue(target.is_file())
            self.assertFalse(target.is_symlink())
            self.assertIn('Environment=SRV1CV8_DEBUG=-debug', target.read_text())
            self.assertIn(['systemctl', 'enable', '--now', 'srv1cv8_4541.service'], commands)
            self.assertIn(['systemctl', 'is-active', '--quiet', 'srv1cv8_4541.service'], commands)
            self.assertFalse(any(args[:2] == ['systemctl', 'link'] for args in commands))
            data.assert_called_once_with('/Data/1C', 'usr1cv8', 'grp1cv8')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
            self.assertIn('srv1cv8_4541.service', str(events))

    def test_wrong_loaded_fragment_never_starts_the_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'srv1cv8-8.3.27.2342@.service').write_text(UNIT, encoding='utf-8')
            target = root / 'systemd'
            target.mkdir()
            runner = Runner({}, Mock())
            with patch.object(runner, 'run', return_value='/another/unit') as run, \
                    patch.object(runner, 'prepare_service_data'), self.assertRaisesRegex(RuntimeError, 'не подтвердил'):
                runner.install_service(dict(directory=str(root)), '8.3.27.2342', str(target))
            self.assertFalse(any(call.args[0][:2] == ['systemctl', 'enable'] for call in run.call_args_list))

    def test_transport_keeps_linux_service_values_but_drops_windows_passwords(self):
        settings = linux_platform.installation_settings(dict(features=['server'], service=True, service_port='4540',
             linux_service_name='my1c.service', linux_service_data='/data/1C', service_debug=True,
             service_password='windows-secret', password='ssh-secret', service_display_name='Windows name'))
        self.assertEqual(settings['linux_service_name'], 'my1c.service')
        self.assertEqual(settings['linux_service_data'], '/data/1C')
        self.assertTrue(settings['service_debug'])
        self.assertNotIn('secret', str(settings))
        self.assertNotIn('service_display_name', settings)

    def test_run_needs_help_inspection_even_when_installer_is_named_full(self):
        import zipfile
        import threading
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'server64_8_3_27_2342.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('setup-full-8.3.27.2342-x86_64.run', b'fixture, never executed')
            package = linux_platform.linux_metadata(archive, dict(filename=archive.name, version='8.3.27.2342', architecture='64'), Mock(), threading.Event())
        self.assertFalse(package['capabilities_verified'])
        self.assertEqual(package['features'], [])


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_linux_fields_defaults_port_name_preview_and_manual_name(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        self.assertEqual(panel.linux_service_frame.winfo_manager(), 'pack')
        panel.service.set(True)
        self.assertEqual(panel.linux_service_name.get(), 'srv1cv8_1541.service')
        self.assertEqual(panel.linux_service_data.get(), LINUX_DATA_DEFAULT)
        panel.service_port.set('4540')
        panel.service_debug.set(True)
        self.assertEqual(panel.linux_service_name.get(), 'srv1cv8_4541.service')
        self.assertIn('Environment=SRV1CV8_RANGE=4560:4591', panel.linux_service_preview.get())
        self.assertIn('Environment=SRV1CV8_DEBUG=-debug', panel.linux_service_preview.get())
        panel.linux_service_name.set('custom1c.service')
        panel.service_port.set('1640')
        self.assertEqual(panel.linux_service_name.get(), 'custom1c.service')
        panel.reset_linux_service_name()
        self.assertEqual(panel.linux_service_name.get(), 'srv1cv8_1641.service')
        panel.service.set(False)
        self.assertTrue(all(str(widget.cget('state')) == 'disabled' for widget in panel.linux_service_controls))
        panel.system.set('Windows · этот компьютер')
        panel.system_changed()
        self.assertEqual(panel.linux_service_frame.winfo_manager(), '')

    def test_windows_distribution_keeps_draft_editable_until_actual_metadata_is_read(self):
        panel = self.panel()
        panel.distributions = [dict(version='8.5.1.1529', filename='setuptc64_8_5_1_1529.rar', title='Thin client', architecture='64')]
        panel.render_distributions()
        panel.distribution_tree.selection_set('0')
        panel.distribution_selected()
        for key in ('FileVersion', 'CSServer', 'CSClient', 'WebServices'):
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal')
        self.assertIn('После проверки', panel.capabilities_note.cget('text'))
        self.assertEqual(str(panel.feature_widgets['MCWFileVersion'].cget('state')), 'normal')
        self.assertEqual(str(panel.service_widget.cget('state')), 'normal')

    def test_msi_is_authoritative_and_full_linux_clients_are_available(self):
        panel = self.panel()
        panel.distributions = [dict(version='8.5.1.1529', filename='windows64_8_5_1_1529.rar', title='Server', architecture='64')]
        panel.render_distributions()
        panel.distribution_tree.selection_set('0')
        panel.distribution_selected()
        self.assertEqual(str(panel.feature_widgets['FileVersion'].cget('state')), 'normal')
        package = dict(metadata(), distribution_key=panel.distribution_key(panel.distributions[0]))
        panel.package_ready(package)
        self.assertEqual(str(panel.feature_widgets['FileVersion'].cget('state')), 'normal')
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        package = dict(kind='linux', path='fixture.run', properties=dict(ProductVersion='8.3.27.2342'),
                       features=[dict(id=key) for key in ('client_full', 'client_thin', 'server', 'server_admin')])
        panel.package_ready(package)
        for key in ('client_full', 'client_thin'):
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal')
