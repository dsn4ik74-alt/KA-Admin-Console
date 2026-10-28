"""RAS installation plans and UI. Service APIs and installers are always mocked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from linux_platform_runner import Runner, ras_service_options, render_service_unit
from platform_install import install_msi, install_properties
from platform_service import create_ras_service
from service_management import windows_arguments
from test_platform_install import metadata
import linux_platform
import test_service_ui

RAS_UNIT = '''[Unit]
Description=1C:Enterprise Remote Administration Server
[Service]
Type=simple
User=usr1cv8
Group=grp1cv8
Environment=RAS_PORT=1545
Environment=RAS_CLUSTER_ADDRESS=
ExecStart=/opt/1cv8/x86_64/8.3.27.2342/ras cluster --port=${RAS_PORT} $RAS_CLUSTER_ADDRESS
Restart=always
[Install]
WantedBy=multi-user.target
'''


def ras_metadata():
    package = metadata()
    package.update(ras_component='{3280e8de-e66e-4ad0-83a0-0d536f5f70b4}', ras_features=['CSServer', 'CSClient'])
    package['properties']['ProductCode'] = '{26B80782-F98F-DA6A-D9CE-2373E3603ABD}'
    return package


class PlanTests(unittest.TestCase):
    def test_names_ports_version_and_target(self):
        self.assertEqual(ras_service_options({})['name'], '1c_ras_1545.service')
        result = ras_service_options(dict(service_port='4540'), '8.5.1.1529', windows=True)
        self.assertEqual(result['name'], '1C:RAS | 4545')
        self.assertEqual(result['display'], '1C:RAS | 4545 | 8.5.1.1529')
        self.assertEqual(result['endpoint'], 'localhost:4540')
        self.assertEqual(ras_service_options(dict(ras_port='2645'))['name'], '1c_ras_2645.service')
        for port in ('', 'abc', '0', '65536', '1540', '1541', '1560', '1591', '1545\nExecStart=bad'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                ras_service_options(dict(ras_port=port))

    def test_ras_requires_an_actual_installed_component_before_msi(self):
        for package, settings in ((metadata(), dict(features=['CSClient'], ras_service=True)),
                                  (ras_metadata(), dict(features=['FileVersion'], ras_service=True)),
                                  (ras_metadata(), dict(features=['CSServer'], ras_service=True, ras_port='1541'))):
            with self.subTest(settings=settings), patch('platform_install.Msi') as native, self.assertRaises(ValueError):
                install_msi(package, settings, Mock())
            native.assert_not_called()
        properties = install_properties(ras_metadata(), dict(features=['CSClient'], ras_service=True))
        self.assertEqual(properties['INSTALLSRVRASSRVC'], '0')

    def test_windows_same_port_has_one_service_name_across_platform_versions(self):
        old = ras_service_options(dict(ras_port='1543'), '8.3.27.1859', windows=True)
        new = ras_service_options(dict(ras_port='1543'), '8.5.1.1529', windows=True)
        self.assertEqual(old['name'], '1C:RAS | 1543')
        self.assertEqual(old['name'], new['name'])
        self.assertEqual(old['display'], '1C:RAS | 1543 | 8.3.27.1859')
        self.assertNotEqual(old['display'], new['display'])

    def test_windows_ras_created_and_started_only_after_msi_success(self):
        for code in (0, 1603):
            native = Mock()
            native.dll.MsiSetExternalUIRecord.return_value = 0
            native.dll.MsiSetInternalUI.return_value = 2
            native.dll.MsiInstallProductW.return_value = code
            order = []
            native.dll.MsiInstallProductW.side_effect = lambda *args: order.append('msi') or code
            with patch('platform_install.Msi', return_value=native), patch('platform_install.check_ras_destination'), \
                    patch('platform_install.create_ras_service', side_effect=lambda *args: order.append('ras') or dict(running=True)):
                if code:
                    with self.assertRaisesRegex(RuntimeError, '1603'):
                        install_msi(ras_metadata(), dict(features=['CSClient'], ras_service=True), Mock())
                    self.assertEqual(order, ['msi'])
                else:
                    result = install_msi(ras_metadata(), dict(features=['CSClient'], ras_service=True), Mock())
                    self.assertTrue(result['ras']['running'])
                    self.assertIsNone(result['service'])
                    self.assertEqual(order, ['msi', 'ras'])

    def test_existing_windows_service_blocks_msi_without_mutation(self):
        with patch('platform_install.check_ras_destination', side_effect=ValueError('уже существует')) as destination, \
                patch('platform_install.Msi') as native, self.assertRaisesRegex(ValueError, 'уже существует'):
            install_msi(ras_metadata(), dict(features=['CSClient'], ras_service=True), Mock())
        native.assert_not_called()
        destination.assert_called_once_with('1C:RAS | 1545')

    def test_windows_uses_exact_component_quotes_and_confirms_running(self):
        native, service, commands, events = Mock(), Mock(), [], []
        executable = r'C:\Program Files\1cv8\8.5.1.1529\bin\ras.exe'
        native.component_path.return_value = executable
        service.__enter__, service.__exit__ = Mock(return_value=service), Mock(return_value=False)
        service.state.return_value = 4
        def sc(command):
            commands.append(command)
            if command.action == 'create':
                service.config.return_value = dict(binary=command.arguments[3], display=command.arguments[-1])
            return 'SUCCESS'
        with patch('platform_service.check_ras_destination') as destination, patch('platform_service.open_service', return_value=service) as opened, \
                patch('platform_service.run_service_command', side_effect=sc):
            result = create_ras_service(native, ras_metadata(), dict(service_port='4540', ras_port='4545'), events.append)
        self.assertEqual(result['name'], '1C:RAS | 4545')
        self.assertEqual(result['display'], '1C:RAS | 4545 | 8.5.1.1529')
        self.assertEqual(commands[0].arguments[-2:], ('DisplayName=', result['display']))
        self.assertTrue(all(row.name == result['name'] for row in commands))
        self.assertEqual(commands[1].arguments, ('start', result['name']))
        destination.assert_called_once_with(result['name'])
        opened.assert_called_once_with(result['name'])
        self.assertEqual([row.action for row in commands], ['create', 'start'])
        self.assertEqual(windows_arguments(result['image']), [executable, 'cluster', '--service', '--port=4545', 'localhost:4540'])
        self.assertEqual(commands[0].arguments[4:6], ('start=', 'auto'))
        service.state.assert_called()
        service.ensure_stopped.assert_not_called()
        native.component_path.assert_called_once_with(ras_metadata()['properties']['ProductCode'], ras_metadata()['ras_component'])

    def test_windows_failed_start_retains_service_and_reports_partial_install(self):
        native, service = Mock(), Mock()
        native.component_path.return_value = r'C:\1cv8\ras.exe'
        service.__enter__, service.__exit__ = Mock(return_value=service), Mock(return_value=False)
        image = '"C:\\1cv8\\ras.exe" cluster --service --port=1545 localhost:1540'
        service.config.return_value = dict(binary=image, display='1C:RAS | 1545 | 8.5.1.1529')
        with patch('platform_service.check_ras_destination'), patch('platform_service.open_service', return_value=service), \
                patch('platform_service.run_service_command', side_effect=['created', RuntimeError('port busy')]) as sc, \
                self.assertRaisesRegex(RuntimeError, 'Платформа установлена.*port busy'):
            create_ras_service(native, ras_metadata(), {}, Mock())
        self.assertFalse(any(call.args[0].action == 'delete' for call in sc.call_args_list))

    def test_linux_vendor_ras_variables_are_replaced_once(self):
        result = render_service_unit(RAS_UNIT, ras_service_options(dict(service_port='4540')), ras=True)
        self.assertEqual(result.count('Environment=RAS_PORT=4545\n'), 1)
        self.assertIn('Environment=RAS_CLUSTER_ADDRESS=localhost:4540\n', result)
        self.assertIn('User=usr1cv8\nGroup=grp1cv8', result)
        self.assertNotIn('Environment=RAS_PORT=1545', result)

    def test_linux_writes_regular_named_unit_and_enables_autostart(self):
        for vendor in (False, True):
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                binary = base / 'x86_64' / '8.3.27.2342' / 'ras'
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b'never executed')
                source = binary.with_name('ras-8.3.27.2342.service')
                if vendor:
                    source.write_text(RAS_UNIT.replace('/opt/1cv8/x86_64/8.3.27.2342/ras', str(binary)), encoding='utf-8')
                units = base / 'systemd'
                units.mkdir()
                destination = units / '1c_ras_4545.service'
                runner = Runner({}, Mock())
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    return str(destination) if args[:2] == ['systemctl', 'show'] else ''
                with patch.object(runner, 'run', side_effect=run):
                    result = runner.install_ras_service(dict(directory=str(base), service_port='4540'), '8.3.27.2342', str(units))
                self.assertEqual(result, str(destination))
                self.assertTrue(destination.is_file())
                self.assertFalse(destination.is_symlink())
                self.assertIn('Environment=RAS_PORT=4545', destination.read_text())
                self.assertIn(['systemctl', 'enable', '--now', '1c_ras_4545.service'], calls)
                self.assertIn(['systemctl', 'is-active', '--quiet', '1c_ras_4545.service'], calls)
                self.assertFalse(any('link' in call for call in calls))
                if vendor:
                    self.assertIn('User=usr1cv8', destination.read_text())
                else:
                    self.assertIn('DynamicUser=yes', destination.read_text())

    def test_linux_ras_collision_is_checked_before_packages(self):
        runner = Runner(dict(settings=dict(features=['server_admin'], ras_service=True)), Mock())
        with patch('linux_platform_runner.service_destination', side_effect=ValueError('уже существует')), \
                patch.object(runner, 'validate_files') as validate, self.assertRaisesRegex(ValueError, 'уже существует'):
            runner.install()
        validate.assert_not_called()

    def test_linux_transport_keeps_ras_and_derives_matching_default_ports(self):
        settings = linux_platform.installation_settings(dict(features=['server'], service_port='4540', ras_service=True, password='secret'))
        self.assertTrue(settings['ras_service'])
        self.assertEqual(settings['ras_port'], '4545')
        self.assertNotIn('password', settings)
        package = dict(format='deb', features=[dict(id='server')], ras_features=['server'])
        linux_platform.validate_settings(package, settings)
        with self.assertRaises(ValueError):
            linux_platform.validate_settings(dict(package, ras_features=[]), settings)


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_ras_independent_checkbox_names_and_manual_port_are_preserved(self):
        self.app._switch_mode('platform_install')
        panel = self.app.platform_install
        self.assertEqual(str(panel.ras_widget.cget('state')), 'normal')
        panel.package_ready(ras_metadata())
        self.assertEqual(str(panel.ras_widget.cget('state')), 'normal')
        panel.ras_service.set(True)
        panel.update_ras_fields()
        self.assertFalse(panel.service.get())
        self.assertEqual(str(panel.ras_port_entry.cget('state')), 'normal')
        panel.service_port.set('4540')
        self.assertEqual(panel.ras_port.get(), '4545')
        self.assertIn('1C:RAS | 4545 | 8.5.1.1529', panel.ras_preview.get())
        self.assertIn('Имя службы: 1C:RAS | 4545\n', panel.ras_preview.get())
        panel.ras_port.set('2645')
        panel.service_port.set('1640')
        self.assertEqual(panel.ras_port.get(), '2645')
        self.assertEqual(panel.settings()['ras_port'], '2645')
        panel.feature_values['CSClient'].set(False)
        panel.feature_values['CSServer'].set(False)
        panel.update_features()
        self.assertTrue(panel.ras_service.get())
        self.assertEqual(str(panel.ras_widget.cget('state')), 'normal')
        with patch.object(panel, 'work') as work:
            panel.execute()
        work.assert_not_called()
        self.assertIn('Для RAS выберите компонент', self.app.form_notice.message.cget('text'))

    def test_linux_ras_in_server_bundle_is_available_without_separate_admin_package(self):
        self.app._switch_mode('platform_install')
        panel = self.app.platform_install
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        package = dict(kind='linux', path='fixture.deb', format='deb', properties=dict(ProductVersion='8.3.27.2342'),
                       features=[dict(id='server')], ras_features=['server'], capabilities_verified=True)
        panel.package_ready(package)
        self.assertEqual(str(panel.ras_widget.cget('state')), 'normal')
        panel.ras_service.set(True)
        panel.update_ras_fields()
        self.assertIn('/etc/systemd/system/1c_ras_1545.service', panel.ras_preview.get())
        self.assertTrue(panel.settings()['ras_service'])
