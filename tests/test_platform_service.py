"""Service installation boundaries. No Windows service is created or changed."""
import ctypes
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from platform_install import install_msi, prepare_service
from platform_service import (WindowsService, configure_installed_service, display_template,
                              service_options, service_image_path, prepare_srvinfo)
from service_management import windows_arguments
from test_platform_install import metadata
import test_service_ui


class PlanTests(unittest.TestCase):
    def settings(self, **changes):
        return dict(features=['CSServer', 'CSClient'], service=True, service_user='USR1CV8',
                    service_password='fixture-password', service_password_confirm='fixture-password', **changes)

    def test_templates_use_selected_version_and_debug_is_last_with_quoted_paths(self):
        package = metadata()
        for role, prefix in (('Сервер 1С', '1C Server'), ('Сервер лицензирования', '1C License Server')):
            options = service_options(package, dict(service_role=role, service_port='4540', srvinfo=r'G:\Data 1C', service_debug=True))
            self.assertEqual(options['display'], prefix + ' | 4540 | 8.5.1.1529')
            image = service_image_path(r'C:\Program Files\1cv8\8.5.1.1529\bin\ragent.exe', options)
            args = windows_arguments(image)
            self.assertEqual(args[-1], '-debug')
            self.assertEqual(args[args.index('-d') + 1], r'G:\Data 1C')
            self.assertEqual(args[args.index('-regport') + 1], '4541')
            self.assertEqual(args[args.index('-range') + 1], '4560:4591')
        self.assertEqual(service_options(package, dict(service_display_name='Моя служба'))['display'], 'Моя служба')
        self.assertNotIn('-debug', service_image_path(r'C:\1cv8\ragent.exe', service_options(package, {})))

    def test_invalid_fields_are_rejected_before_msi(self):
        for changes in (dict(srvinfo='relative'), dict(srvinfo='C:\\'), dict(srvinfo='C:\\bad\nfolder'),
                        dict(service_port='1541'), dict(service_display_name=''), dict(service_role='unknown')):
            with self.subTest(changes=changes), patch('platform_install.Msi') as native, self.assertRaises(ValueError):
                install_msi(metadata(), self.settings(**changes), Mock())
            native.assert_not_called()

    def test_transform_changes_only_target_start_and_keeps_stop_uninstall_events(self):
        native = Mock()
        native.query.side_effect = [
            [dict(id='agent', name='1C agent', component='service')],
            [dict(id='service', guid='{11111111-1111-1111-1111-111111111111}', file='agent-file')],
            [dict(id='agent-file', name='ragent.exe')],
            [dict(id='target', name='1C agent', event='163', component='service'),
             dict(id='other', name='Another service', event='163', component='other')],
        ]
        native.create_transform.return_value = 'no-start.mst'
        package = metadata()
        package['properties']['ProductCode'] = '{22222222-2222-2222-2222-222222222222}'
        definition = prepare_service(native, package)
        statements = native.create_transform.call_args.args[1]
        self.assertEqual(statements, ["UPDATE `ServiceControl` SET `Event`=162 WHERE `ServiceControl`='target'"])
        self.assertEqual(definition['name'], '1C agent')
        self.assertEqual(definition['transform'], 'no-start.mst')

    def test_msi_must_suppress_start_before_install_and_configure_only_after_success(self):
        for code in (0, 1603):
            order, native = [], Mock()
            native.dll.MsiSetInternalUI.return_value = 2
            native.dll.MsiSetExternalUIRecord.return_value = 0
            def install(path, command):
                self.assertIn('TRANSFORMS="no-start.mst"', command)
                self.assertIn('TRANSFORMSSECURE="1"', command)
                order.append('install')
                return code
            native.dll.MsiInstallProductW.side_effect = install
            def prepare(*args):
                order.append('prepare')
                return dict(transform='no-start.mst')
            def configure(*args):
                order.append('configure')
                return dict(stopped=True)
            with patch('platform_install.Msi', return_value=native), \
                    patch('platform_install.prepare_service', side_effect=prepare), \
                    patch('platform_install.configure_installed_service', side_effect=configure):
                if code:
                    with self.assertRaisesRegex(RuntimeError, '1603'):
                        install_msi(metadata(), self.settings(), Mock())
                    self.assertEqual(order, ['prepare', 'install'])
                else:
                    result = install_msi(metadata(), self.settings(), Mock())
                    self.assertTrue(result['service']['stopped'])
                    self.assertEqual(order, ['prepare', 'install', 'configure'])

    def test_prepare_failure_never_starts_msi(self):
        native = Mock()
        with patch('platform_install.Msi', return_value=native), \
                patch('platform_install.prepare_service', side_effect=RuntimeError('cannot suppress start')), \
                self.assertRaisesRegex(RuntimeError, 'suppress'):
            install_msi(metadata(), self.settings(), Mock())
        native.dll.MsiInstallProductW.assert_not_called()

    def test_configure_exact_installed_service_with_sc_and_confirm_stopped(self):
        native, service, events = Mock(), Mock(), []
        executable = r'C:\Program Files\1cv8\8.5.1.1529\bin\ragent.exe'
        native.component_path.return_value = executable
        settings = self.settings(srvinfo=r'D:\Data 1C', service_debug=True, service_role='Сервер лицензирования')
        options = service_options(metadata(), settings)
        image = service_image_path(executable, options)
        service.config.side_effect = [dict(binary='"' + executable + '" -srvc', account='.\\USR1CV8'),
                                      dict(binary=image, display=options['display'])]
        manager = Mock()
        manager.__enter__ = Mock(return_value=service)
        manager.__exit__ = Mock(return_value=False)
        with patch('platform_service.open_service', return_value=manager) as opened, \
                patch('platform_service.prepare_srvinfo') as data, \
                patch('platform_service.run_service_command', return_value='SC success') as run:
            result = configure_installed_service(native, dict(product='product', component_guid='component', name='exact agent'), metadata(), settings, events.append)
        opened.assert_called_once_with('exact agent')
        native.component_path.assert_called_once_with('product', 'component')
        self.assertEqual(run.call_args.args[0].arguments, ('config', 'exact agent', 'binPath=', image, 'DisplayName=', options['display']))
        data.assert_called_once_with(r'D:\Data 1C', '.\\USR1CV8')
        self.assertEqual(service.ensure_stopped.call_count, 2)
        self.assertTrue(result['stopped'])
        self.assertNotIn('fixture-password', str(events))
        self.assertNotIn('start=', str(run.call_args))

    def test_other_version_service_is_never_stopped_or_configured(self):
        native, service = Mock(), Mock()
        native.component_path.return_value = r'C:\1cv8\8.5.1.1529\bin\ragent.exe'
        service.config.return_value = dict(binary=r'C:\1cv8\8.3.27.1859\bin\ragent.exe -srvc')
        manager = Mock()
        manager.__enter__ = Mock(return_value=service)
        manager.__exit__ = Mock(return_value=False)
        with patch('platform_service.open_service', return_value=manager), patch('platform_service.run_service_command') as run, \
                patch('platform_service.prepare_srvinfo') as data, self.assertRaisesRegex(RuntimeError, 'Чужая служба'):
            configure_installed_service(native, dict(product='product', component_guid='component', name='agent'), metadata(), self.settings(), Mock())
        service.ensure_stopped.assert_not_called()
        run.assert_not_called()
        data.assert_not_called()

    def test_stopped_service_does_not_receive_control_or_start(self):
        native = Mock()
        def query(handle, pointer):
            pointer._obj.state = 1
            return 1
        native.QueryServiceStatus.side_effect = query
        WindowsService(123, native).ensure_stopped()
        native.ControlService.assert_not_called()
        self.assertNotIn('StartService', str(native.mock_calls))

    def test_srvinfo_grants_only_selected_folder_without_recursive_reset(self):
        with tempfile.TemporaryDirectory() as directory, patch('platform_service.subprocess.run', return_value=Mock(returncode=0)) as run:
            prepare_srvinfo(directory, '.\\USR1CV8')
        args = run.call_args.args[0]
        self.assertEqual(args[1], directory)
        self.assertIn('/grant', args)
        self.assertNotIn('/t', args)
        self.assertIn(':(OI)(CI)M', args[-2])


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_role_template_tracks_version_and_port_but_keeps_custom_name(self):
        panel = self.panel()
        panel.package_ready(metadata())
        panel.service.set(True)
        self.assertEqual(panel.service_display_name.get(), '1C Server | 1540 | 8.5.1.1529')
        panel.service_role.set('Сервер лицензирования')
        panel.service_port.set('4540')
        panel.service_debug.set(True)
        self.assertEqual(panel.service_display_name.get(), '1C License Server | 4540 | 8.5.1.1529')
        self.assertTrue(panel.service_preview.get().endswith(' -debug'))
        panel.service_display_name.set('Мой сервер')
        panel.service_role.set('Сервер 1С')
        self.assertEqual(panel.service_display_name.get(), 'Мой сервер')
        panel.reset_service_display()
        self.assertEqual(panel.service_display_name.get(), '1C Server | 4540 | 8.5.1.1529')
        with patch('platform_install_ui.filedialog.askdirectory', return_value=r'F:\srvinfo') as browse:
            panel.choose_srvinfo()
        browse.assert_called_once()
        self.assertEqual(panel.srvinfo.get(), r'F:\srvinfo')
        panel.service.set(False)
        self.assertTrue(all(str(widget.cget('state')) == 'disabled' for widget in panel.service_controls))

    def test_linux_missing_clients_stay_visible_disabled_and_cannot_be_selected(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        package = dict(kind='linux', path='/fixture/server', format='run',
                       properties=dict(ProductVersion='8.3.27.2342'), features=[dict(id='server'), dict(id='server_admin')])
        panel.package_ready(package)
        for key in ('client_full', 'client_thin', 'client_thin_fib'):
            widget = panel.feature_widgets[key]
            self.assertEqual(widget.winfo_manager(), 'pack')
            self.assertEqual(str(widget.cget('state')), 'disabled')
            widget.invoke()
            self.assertFalse(panel.feature_values[key].get())
        self.assertEqual(str(panel.feature_widgets['server'].cget('state')), 'normal')
        self.assertEqual(panel.windows_frame.winfo_manager(), '')
        self.assertNotIn('client_full', panel.settings()['features'])
