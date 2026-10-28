"""ITS and native installer boundaries; never install a product or contact ITS."""
import ctypes
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import zipfile

from infobase_address import infobase_address
from its_portal import Portal, trusted_url
from platform_install import InstallProgress, extract_distribution, install_hasp, install_msi, install_properties
from postgres_runner import OperationCancelled
import test_service_ui
import test_cluster_infobase
from test_activity import mock_dpapi


def metadata():
    features = [dict(id=key, parent='', title=key, display='1', level='200')
                for key in ('FileVersion', 'MCWFileVersion', 'CSServer', 'CSClient', 'Languages')]
    features += [dict(id='RU', parent='Languages', display='1', level='200'),
                 dict(id='FileVersion_RU', parent='RU', display='0', level='200'),
                 dict(id='ForCommon', parent='', display='0', level='1'),
                 dict(id='AdmUpdate', parent='', display='0', level='1')]
    return dict(path=r'C:\fixture\1CEnterprise.msi', properties=dict(ProductName='1C:Enterprise 8 (x86-64)',
                ProductVersion='8.5.1.1529'), features=features)


class Response(io.BytesIO):
    def __init__(self, data=b'fixture', headers=None, url='https://dl03.1c.ru/package.rar'):
        super().__init__(data)
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.url = url

    def geturl(self):
        return self.url


class PortalTests(unittest.TestCase):
    def test_trusted_redirects_reject_lookalike_domains_cleartext_and_credentials(self):
        for url in ('http://releases.1c.ru/file', 'https://1c.ru.evil.test/',
                    'https://x:secret@releases.1c.ru/', 'file:///C:/fixture'):
            with self.assertRaises(ValueError):
                trusted_url(url)
        self.assertEqual(trusted_url('https://dl03.1c.ru/public/file/get/id'), 'https://dl03.1c.ru/public/file/get/id')

    def test_login_posts_actual_successful_form_controls_without_unchecked_checkboxes(self):
        source = '''<form action="/login?service=releases"><input name="execution" value="token">
          <input name="username"><input name="password" type="password">
          <input name="rememberMe" type="checkbox" checked><input name="no" type="checkbox">
          <input name="disabled" disabled value="bad"><input name="_eventId" value="submit"></form>'''
        portal = Portal()
        with patch.object(portal, 'page', side_effect=[('https://login.1c.ru/login', source),
                     ('https://releases.1c.ru/project/Platform85', 'Catalog')]) as page:
            self.assertTrue(portal.login('fixture-user', 'fixture-password'))
        url, body = page.call_args.args
        self.assertEqual(url, 'https://login.1c.ru/login?service=releases')
        posted = urllib.parse.parse_qs(body.decode())
        self.assertEqual(posted, dict(username=['fixture-user'], password=['fixture-password'],
                         rememberMe=['on'], execution=['token'], _eventId=['submit']))

    def test_failed_login_redacts_credentials_and_http_errors_hide_response_urls(self):
        portal = Portal()
        with patch.object(portal, 'page', return_value=('https://login.1c.ru/login',
                        '<div id="error">fixture-password fixture-user rejected</div>')):
            with self.assertRaises(RuntimeError) as error:
                portal.login('fixture-user', 'fixture-password')
        self.assertNotIn('fixture-password', str(error.exception))
        self.assertNotIn('fixture-user', str(error.exception))
        portal.opener.open = Mock(side_effect=urllib.error.HTTPError('https://login.1c.ru/?secret=bad', 403, 'x', {}, None))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as error:
            portal.open('https://releases.1c.ru/')
        self.assertNotIn('secret', str(error.exception))

    def test_real_catalog_url_shapes_nested_rows_dates_and_windows_only(self):
        portal = Portal()
        html = '''<table><tr><td><table><tr><td><a href="/version_files?nick=Platform85&amp;ver=8.5.1.1529">8.5.1.1529</a></td><td>12.10.26</td></tr>
          <tr><td><a href="/version_files?nick=Platform85&amp;ver=8.5.1.1522">8.5.1.1522</a></td><td>02.09.2026</td></tr></table></td></tr></table>'''
        with patch.object(portal, 'page', return_value=('https://releases.1c.ru/project/Platform85', html)):
            rows = portal.releases('8.5')
        self.assertEqual([row['date'] for row in rows], ['12.10.2026', '02.09.2026'])
        html = ''.join('<a href="/version_file?nick=Platform85&amp;path=Platform%5C' + file + '">' + title + '</a>'
                       for file, title in [('windows64full_8_5_1_1529.rar', 'Full Windows 64'),
                             ('windows_8_5_1_1529.rar', 'Windows 32'),
                             ('deb64_8_5_1_1529.tar.gz', 'Linux server + Windows thin client')])
        with patch.object(portal, 'page', return_value=(rows[0]['url'], html)):
            variants = portal.distributions(rows[0], 'windows')
        self.assertEqual([row['architecture'] for row in variants], ['64', '32'])
        self.assertTrue(all(row['version'] == '8.5.1.1529' for row in variants))

    def test_download_atomic_publish_and_progress_and_no_shared_part_clobber(self):
        portal, events = Portal(), []
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'platform.rar.part').write_bytes(b'someone else')
            response = Response(b'archive', {'Content-Type': 'application/octet-stream',
                 'Content-Disposition': 'attachment; filename="platform.rar"', 'Content-Length': '7'})
            with patch.object(portal, 'download_link', return_value='https://dl03.1c.ru/file'), \
                    patch.object(portal, 'open', return_value=response):
                path = portal.download({}, directory, events.append)
            self.assertEqual(Path(path).read_bytes(), b'archive')
            self.assertEqual((directory / 'platform.rar.part').read_bytes(), b'someone else')
            self.assertEqual(events[-1]['size'], 7)
            self.assertEqual(events[-2]['percent'], 100)

    def test_incomplete_or_cancelled_download_does_not_replace_existing_archive(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as directory:
                portal = Portal()
                path = Path(directory) / 'platform.rar'
                path.write_bytes(b'existing complete file')
                response = Response(b'partial', {'Content-Length': '900', 'Content-Disposition': 'attachment; filename=platform.rar'})
                def event(value):
                    if cancelled:
                        portal.cancel.set()
                with patch.object(portal, 'download_link', return_value='https://dl03.1c.ru/file'), \
                        patch.object(portal, 'open', return_value=response):
                    with self.assertRaises(OperationCancelled if cancelled else RuntimeError):
                        portal.download({}, directory, event)
                self.assertEqual(path.read_bytes(), b'existing complete file')
                self.assertEqual(list(Path(directory).glob('*.part')), [])

    def test_html_response_and_unexpected_filename_are_not_saved(self):
        for headers in ({'Content-Type': 'text/html'}, {'Content-Disposition': 'attachment; filename=bad:evil.rar'}):
            portal = Portal()
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(portal, 'download_link', return_value='https://dl03.1c.ru/file'), \
                    patch.object(portal, 'open', return_value=Response(headers=headers)):
                with self.assertRaises(RuntimeError):
                    portal.download({}, directory, Mock())
                self.assertFalse(list(Path(directory).iterdir()))


class InstallerTests(unittest.TestCase):
    def test_real_feature_ids_include_language_children_and_mandatory_files(self):
        properties = install_properties(metadata(), dict(features=['FileVersion', 'CSClient'], language='RU'))
        self.assertEqual(set(properties['ADDLOCAL'].split(',')), {'FileVersion', 'CSClient', 'ForCommon', 'Languages', 'RU', 'FileVersion_RU'})
        self.assertEqual(properties['DESIGNERALLCLIENTS'], '1')
        self.assertEqual(properties['SERVER'], '0')
        self.assertNotIn('AdmUpdate', properties['ADDLOCAL'])
        self.assertEqual(properties['REBOOT'], 'ReallySuppress')
        self.assertEqual(properties['HASPInstall'], 'no')

    def test_service_credentials_selection_and_required_fields_are_validated(self):
        settings = dict(features=['CSServer'], service=True, service_user='USR1CV8', new_user=True,
                        service_password='fixture-secret', service_password_confirm='fixture-secret')
        properties = install_properties(metadata(), settings)
        self.assertEqual(properties['SRVCUSERSELECTMODE'], 'new')
        self.assertEqual(properties['USER1CV82SERVER'], 'USR1CV8')
        self.assertIn('PASSWORD1CV82SERVER', properties['MsiHiddenProperties'])
        for changes in (dict(service_password_confirm='wrong'), dict(features=['FileVersion']), dict(service_user='Other'),
                        dict(service_password=''), dict(features=['Unknown']), dict(language='DE'), dict(directory='relative')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                install_properties(metadata(), dict(settings, **changes))

    def test_progress_handles_reset_increment_action_ticks_rollback_and_total_change(self):
        state = InstallProgress()
        self.assertEqual(state.update((0, 100, 0, 0)), 0)
        self.assertEqual(state.update((2, 20, 0, 0)), 20)
        state.update((1, 10, 1, 0))
        self.assertEqual(state.action_data(), 30)
        self.assertEqual(state.update((3, 100, 0, 0)), 15)
        self.assertEqual(state.update((0, 100, 1, 0)), 100)
        self.assertEqual(state.update((2, 10, 0, 0)), 90)

    def test_native_callback_receives_progress_redacts_secrets_and_restores_handlers(self):
        fake = Mock()
        fake.dll.MsiSetInternalUI.return_value = 5
        callback, messages, commands = [], [], []
        records = {1: (0, 100, 0, 0), 2: (2, 50, 0, 0)}
        fake.dll.MsiRecordGetInteger.side_effect = lambda record, column: records[record][column - 1]
        def set_handler(pointer, filter, context, previous):
            if filter:
                callback.append(ctypes.cast(pointer, ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)))
            return 0
        fake.dll.MsiSetExternalUIRecord.side_effect = set_handler
        fake.message.return_value = 'fixture-secret was rejected'
        def native_install(path, properties):
            commands.append(properties)
            for kind, record in ((0x0a000000, 1), (0x0a000000, 2), (0x02000000, 3)):
                self.assertEqual(callback[0](None, kind, record), 1)
            return 3010
        fake.dll.MsiInstallProductW.side_effect = native_install
        with patch('platform_install.Msi', return_value=fake), \
                patch('platform_install.prepare_service', return_value=dict(transform='fixture.mst')), \
                patch('platform_install.configure_installed_service', return_value=dict(stopped=True)):
            result = install_msi(metadata(), dict(features=['CSServer'], service=True, service_user='USR1CV8',
                             service_password='fixture-secret', service_password_confirm='fixture-secret'), messages.append)
        self.assertTrue(result['reboot'])
        self.assertTrue(any(value.get('percent') == 50 for value in messages))
        self.assertNotIn('fixture-secret', str(messages))
        self.assertIn('REBOOT="ReallySuppress"', commands[0])
        self.assertEqual(fake.dll.MsiSetExternalUIRecord.call_count, 2)
        self.assertEqual(fake.dll.MsiSetInternalUI.call_args.args, (5, None))

    def test_cancel_native_installer_requests_rollback_and_never_reports_success(self):
        fake, cancel = Mock(), threading.Event()
        callback = []
        def set_handler(pointer, filter, context, previous):
            if filter:
                callback.append(ctypes.cast(pointer, ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)))
            return 0
        fake.dll.MsiSetExternalUIRecord.side_effect = set_handler
        fake.dll.MsiSetInternalUI.return_value = 2
        def native_install(*args):
            cancel.set()
            self.assertEqual(callback[0](None, 0x0a000000, 1), 2)
            return 1602
        fake.dll.MsiInstallProductW.side_effect = native_install
        with patch('platform_install.Msi', return_value=fake), self.assertRaisesRegex(OperationCancelled, 'откат'):
            install_msi(metadata(), dict(features=['FileVersion']), Mock(), cancel)
        self.assertEqual(fake.dll.MsiSetExternalUIRecord.call_count, 2)

    def test_hasp_is_never_forced_or_killed_and_reports_partial_install_failure(self):
        events, cancel = [], threading.Event()
        with patch('platform_install.subprocess.run', return_value=Mock(returncode=0, stdout=b'')) as run:
            self.assertFalse(install_hasp('fixture-hasp.exe', events.append, cancel))
        self.assertEqual(run.call_args.args[0], ['fixture-hasp.exe', '-i', '-nomsg'])
        self.assertFalse(events[0]['cancellable'])
        with patch('platform_install.subprocess.run', return_value=Mock(returncode=1, stdout=b'fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'Платформа уже установлена'):
                install_hasp('fixture-hasp.exe', events.append, cancel)
        cancel.set()
        with patch('platform_install.subprocess.run') as run, self.assertRaises(OperationCancelled):
            install_hasp('fixture-hasp.exe', events.append, cancel)
        run.assert_not_called()

    def test_selected_hasp_runs_only_after_successful_msi_and_is_prepared_before_installation(self):
        for code in (0, 1603):
            fake = Mock()
            fake.dll.MsiSetInternalUI.return_value = 2
            fake.dll.MsiSetExternalUIRecord.return_value = 0
            fake.dll.MsiInstallProductW.return_value = code
            package = dict(metadata(), hasp_driver=dict(file='haspdinst.exe', cabinet='fixture.cab'))
            with patch('platform_install.Msi', return_value=fake), \
                    patch('platform_install.prepare_hasp', return_value='fixture-driver.exe') as prepare, \
                    patch('platform_install.install_hasp', return_value=False) as driver:
                if code:
                    with self.assertRaisesRegex(RuntimeError, '1603'):
                        install_msi(package, dict(features=['FileVersion'], hasp=True), Mock())
                    driver.assert_not_called()
                else:
                    self.assertFalse(install_msi(package, dict(features=['FileVersion'], hasp=True), Mock())['reboot'])
                    driver.assert_called_once()
                prepare.assert_called_once()

    def test_bundled_7zip_extracts_real_zip_and_picks_main_msi_over_nested_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'platform.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('windows/1CEnterprise 8.msi', b'fixture-main')
                output.writestr('windows/clients/1CEnterprise thin.msi', b'fixture-client')
            selected = extract_distribution(archive, Mock(), threading.Event())
            self.assertEqual(Path(selected).read_bytes(), b'fixture-main')

    def test_archive_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'platform.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('../outside.msi', b'fixture')
            with self.assertRaisesRegex(ValueError, 'вне каталога'):
                extract_distribution(archive, Mock(), threading.Event())
            self.assertFalse((Path(directory) / 'outside.msi').exists())


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_catalog_only_runs_on_login_and_remembers_encrypted_credentials(self):
        mock_dpapi(self)
        panel = self.panel()
        panel.username.set('fixture-user')
        panel.password.set('fixture-password')
        with patch('platform_install_ui.Portal') as portal, patch.object(panel, 'work') as work, \
                patch.object(self.app, '_save_settings') as save:
            portal.return_value.releases.return_value = [dict(version='8.5.1.1529', date='12.10.2026', url='https://releases.1c.ru/fixture')]
            panel.load_catalog()
            operation, success = work.call_args.args
            result = operation(Mock())
            self.assertTrue(panel.running)
            success(result)
        save.assert_called_once()
        self.assertNotIn('fixture-password', json.dumps(self.app._settings))
        self.assertNotIn('fixture-user', json.dumps(self.app._settings))
        self.assertFalse(panel.running)
        self.assertEqual(panel.release_tree.set('0', 'date'), '12.10.2026')
        self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')

    def test_selecting_distribution_downloads_in_background_while_settings_stay_editable(self):
        panel = self.panel()
        panel.portal = Mock()
        panel.distributions = [dict(version='8.5.1.1529', title='Windows full', architecture='64')]
        panel.distribution_tree.insert('', 'end', iid='0')
        panel.distribution_tree.selection_set('0')
        with patch.object(panel, 'work') as work, patch('platform_install_ui.install_msi') as install:
            panel.distribution_selected()
            self.assertFalse(panel.running)
            work.assert_not_called()
            panel.prepare_distribution()
            self.assertTrue(panel.running)
            self.assertEqual(str(panel.catalog_button.cget('state')), 'disabled')
            self.assertEqual(str(panel.feature_widgets['CSServer'].cget('state')), 'normal')
            language_combo = next(widget for widget in panel.combos if 'Русский' in widget.cget('values'))
            self.assertEqual(str(language_combo.cget('state')), 'readonly')
            self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')
            install.assert_not_called()
            panel.package_ready(metadata())
        self.assertFalse(panel.running)
        self.assertEqual(str(panel.execute_button.cget('state')), 'normal')
        self.assertEqual(str(panel.feature_widgets['Convertor'].cget('state')), 'disabled')

    def test_install_is_only_started_by_button_and_uses_captured_settings(self):
        panel = self.panel()
        panel.package_ready(metadata())
        with patch('platform_install_ui.install_msi', return_value=dict(reboot=False)) as install, \
                patch('platform_install_ui.find_all_rac', return_value={'8.5.1.1529': 'rac.exe'}), \
                patch('platform_install_ui.installed_agents', return_value={}), patch.object(panel, 'work') as work:
            install.assert_not_called()
            panel.execute_button.invoke()
            operation, success = work.call_args.args
            panel.hasp.set(True)
            result = operation(Mock())
            success(result)
        self.assertFalse(install.call_args.args[1]['hasp'])
        self.assertEqual(self.app.rac_map, {'8.5.1.1529': 'rac.exe'})
        self.assertFalse(panel.running)

    def test_service_account_fields_follow_selection_and_combobox_wheel_does_not_change_values(self):
        panel = self.panel()
        panel.package_ready(metadata())
        panel.feature_values['CSServer'].set(True)
        panel.update_features()
        panel.service.set(True)
        panel.new_user.set(True)
        self.assertEqual(panel.service_user.get(), 'USR1CV8')
        self.assertEqual(str(panel.service_user_entry.cget('state')), 'readonly')
        for combo in panel.combos:
            old = combo.get()
            combo.event_generate('<MouseWheel>', delta=-120)
            self.assertEqual(combo.get(), old)

    def test_auth_failure_and_cancellation_unlock_form_and_leave_install_disabled(self):
        panel = self.panel()
        panel.username.set('fixture')
        panel.password.set('fixture-secret')
        with patch('administration_ui.threading.Thread') as thread, \
                patch('platform_install_ui.Portal') as portal:
            portal.return_value.login.side_effect = RuntimeError('Wrong password')
            panel.load_catalog()
            self.assertEqual(panel.catalog_input(None), 'break')
            thread.call_args.kwargs['target']()
            self.app._drain_events()
        self.assertFalse(panel.running)
        self.assertEqual(str(panel.catalog_button.cget('state')), 'normal')
        self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')
        self.assertNotIn('its_credentials', self.app._settings)


class AddressTests(unittest.TestCase):
    def test_exact_cluster_endpoint_infobase_name_and_quoted_values(self):
        self.assertEqual(infobase_address(dict(host='SRV-1C01', port='1541'), 'erp_ifrs_flexity_2026_09'),
                         'Srvr="SRV-1C01:1541";Ref="erp_ifrs_flexity_2026_09";')
        self.assertEqual(infobase_address(dict(port='4541'), 'Base "A"', 'remote'), 'Srvr="remote:4541";Ref="Base ""A""";')
        for cluster in ({}, dict(port='bad'), dict(port=0), dict(port=65536)):
            self.assertEqual(infobase_address(cluster, 'Base', 'agent'), '')


class AddressUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp
    panel = test_cluster_infobase.UiTests.panel

    def test_select_copy_and_clear_use_current_cluster_port_not_agent_port(self):
        panel = self.panel()
        panel.render_clusters([dict(cluster=test_cluster_infobase.CLUSTER, host='SRV-1C01', port='1541')])
        panel.render_bases([dict(infobase='id1', name='erp_ifrs_flexity_2026_09'), dict(infobase='id2', name='Other')])
        panel.tree.selection_set('0')
        self.app.root.update()
        self.assertEqual(panel.address.get(), 'Srvr="SRV-1C01:1541";Ref="erp_ifrs_flexity_2026_09";')
        panel.copy_address()
        self.assertEqual(self.app.root.clipboard_get(), panel.address.get())
        panel.tree.selection_set('1')
        self.app.root.update()
        self.assertIn('Ref="Other";', panel.address.get())
        panel.render_bases([])
        self.assertEqual(panel.address.get(), '')
        self.assertEqual(str(panel.address_copy_button.cget('state')), 'disabled')
