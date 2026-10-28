"""User choices survive catalog changes, downloads and capability inspection."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from platform_install_ui import FEATURES
import linux_platform
from test_platform_install import metadata
import test_service_ui


class InstallationDraftTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def toggle_to(self, panel, key, selected):
        if panel.feature_values[key].get() != selected:
            panel.feature_widgets[key].invoke()
        self.assertEqual(panel.feature_values[key].get(), selected)

    def test_selecting_different_archives_never_overwrites_manual_choices_or_starts_download(self):
        panel = self.panel()
        panel.distributions = [dict(version='8.5.1.1529', filename=name, title=name, url='fixture/' + name, architecture='64')
                               for name in ('windows64full.rar', 'setuptc64.rar', 'windows64server.rar')]
        panel.render_distributions()
        for key in FEATURES:
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal')
            self.toggle_to(panel, key, key in ('CSServer', 'CSClient'))
        chosen = {key: value.get() for key, value in panel.feature_values.items()}
        panel.hasp_widget.invoke()
        panel.service_widget.invoke()
        panel.ras_widget.invoke()
        with patch.object(panel, 'work') as work:
            for index in (0, 1, 2, 0):
                panel.distribution_tree.selection_set(str(index))
                panel.distribution_selected()
                self.app.root.update()
                self.assertEqual({key: value.get() for key, value in panel.feature_values.items()}, chosen)
                self.assertTrue(panel.hasp.get())
                self.assertTrue(panel.service.get())
                self.assertTrue(panel.ras_service.get())
        work.assert_not_called()

    def test_manual_edits_during_download_survive_verified_metadata(self):
        panel = self.panel()
        panel.portal = Mock()
        row = dict(version='8.5.1.1529', filename='windows64full.rar', title='Full', url='fixture', architecture='64')
        panel.distributions = [row]
        panel.render_distributions()
        panel.distribution_tree.selection_set('0')
        with patch.object(panel, 'work') as work:
            panel.prepare_distribution()
        self.assertTrue(panel.running)
        self.assertEqual(str(panel.catalog_button.cget('state')), 'disabled')
        for key in FEATURES:
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal')
            self.toggle_to(panel, key, key in ('CSServer', 'CSClient'))
        panel.directory.set(r'C:\Applications\1C')
        panel.hasp_widget.invoke()
        panel.hardware_widget.invoke()
        panel.service_widget.invoke()
        panel.ras_widget.invoke()
        ready = dict(metadata(), distribution_key=panel.distribution_key(row), hasp_driver={'file': 'fixture'},
                     ras_features=['CSClient'], ras_component='fixture', directories=['INSTALLDIR'])
        ready['properties']['USEHWLICENSES'] = '0'
        work.call_args.args[1](ready)
        self.assertFalse(panel.running)
        self.assertEqual(set(panel.settings()['features']), {'CSServer', 'CSClient'})
        self.assertEqual(panel.directory.get(), r'C:\Applications\1C')
        self.assertTrue(panel.hasp.get())
        self.assertTrue(panel.hardware.get())
        self.assertTrue(panel.service.get())
        self.assertTrue(panel.ras_service.get())
        for key in ('CSServer', 'CSClient'):
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal')
        self.toggle_to(panel, 'CSServer', False)
        self.toggle_to(panel, 'CSClient', False)
        self.assertEqual(panel.settings()['features'], [])
        panel.update_features()
        self.assertEqual(panel.settings()['features'], [])

    def test_empty_manual_selection_is_not_replaced_by_a_default_after_download(self):
        panel = self.panel()
        for key in FEATURES:
            self.toggle_to(panel, key, False)
        panel.package_ready(metadata())
        self.assertEqual(panel.settings()['features'], [])
        self.assertFalse(panel.service.get())
        self.assertFalse(panel.ras_service.get())
        with patch.object(panel, 'work') as work:
            panel.execute()
        work.assert_not_called()
        self.assertIn('Выберите компоненты', self.app.form_notice.message.cget('text'))

    def test_verified_thin_client_disables_only_absent_components_and_removes_invalid_options(self):
        panel = self.panel()
        self.toggle_to(panel, 'CSServer', True)
        self.toggle_to(panel, 'MCWFileVersion', True)
        panel.service_widget.invoke()
        panel.ras_widget.invoke()
        panel.hasp_widget.invoke()
        package = metadata()
        package['features'] = [row for row in package['features'] if row['id'] == 'MCWFileVersion']
        panel.package_ready(package)
        for key in FEATURES:
            self.assertEqual(str(panel.feature_widgets[key].cget('state')), 'normal' if key == 'MCWFileVersion' else 'disabled')
            if key != 'MCWFileVersion':
                self.assertFalse(panel.feature_values[key].get())
                self.assertIn('нет в дистрибутиве', panel.feature_widgets[key].cget('text'))
        self.assertFalse(panel.service.get())
        self.assertFalse(panel.ras_service.get())
        self.assertFalse(panel.hasp.get())
        panel.feature_widgets['MCWFileVersion'].invoke()
        self.assertEqual(panel.settings()['features'], [])

    def test_run_inspection_preserves_choices_and_does_not_force_service_or_server(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        for key in linux_platform.FEATURES:
            self.toggle_to(panel, key, key == 'client_thin')
        package = dict(kind='linux', path='setup.run', format='run', properties=dict(ProductVersion='8.3.27.2342'),
                       features=[], capabilities_verified=False)
        panel.package_ready(package)
        self.assertTrue(panel.needs_inspection())
        self.assertEqual(str(panel.feature_widgets['client_thin'].cget('state')), 'normal')
        panel.start()
        panel.allow_setting_edits()
        panel.inspection_ready(dict(capabilities=dict(features=['server', 'server_admin', 'client_thin'],
            capabilities_verified=True, languages=['EN', 'RU'], directory_option='--prefix'), remote_prepared={}))
        self.assertEqual(panel.settings()['features'], ['client_thin'])
        self.assertFalse(panel.service.get())
        self.assertFalse(panel.ras_service.get())
        self.assertEqual(str(panel.feature_widgets['client_full'].cget('state')), 'disabled')
        self.assertEqual(str(panel.feature_widgets['server'].cget('state')), 'normal')
        panel.feature_widgets['server'].invoke()
        panel.update_features()
        self.assertFalse(panel.service.get())
        panel.service_widget.invoke()
        self.assertTrue(panel.service.get())

    def test_reading_catalog_does_not_lock_the_draft_and_installation_does(self):
        panel = self.panel()
        panel.username.set('fixture')
        panel.password.set('fixture')
        with patch.object(panel, 'work'):
            panel.load_catalog()
        self.assertTrue(panel.running)
        self.toggle_to(panel, 'CSServer', True)
        self.toggle_to(panel, 'FileVersion', False)
        panel.finish('Catalog ready')
        panel.package_ready(metadata())
        with patch.object(panel, 'work'):
            panel.execute()
        self.assertTrue(panel.running)
        self.assertEqual(str(panel.feature_widgets['CSServer'].cget('state')), 'disabled')
        self.assertTrue(panel.feature_values['CSServer'].get())
        panel.feature_widgets['CSServer'].invoke()
        self.assertTrue(panel.feature_values['CSServer'].get())
        panel.finish(error='fixture installation failure')
        self.assertEqual(str(panel.feature_widgets['CSServer'].cget('state')), 'normal')
        self.toggle_to(panel, 'CSServer', False)

    def test_real_background_delivery_keeps_choices_made_while_reading_installer(self):
        panel = self.panel()
        panel.portal = Mock()
        row = dict(version='8.5.1.1529', filename='windows64full.rar', title='Full', url='fixture', architecture='64')
        panel.distributions = [row]
        panel.render_distributions()
        panel.distribution_tree.selection_set('0')
        reading, proceed = threading.Event(), threading.Event()
        worker_threads = []
        def read_metadata(*args):
            worker_threads.append(threading.get_ident())
            reading.set()
            if not proceed.wait(5):
                raise RuntimeError('Test did not release metadata worker')
            return metadata()
        with (tempfile.TemporaryDirectory() as directory,
              patch('platform_install_ui.extract_distribution', return_value=r'C:\fixture\setup.msi'),
              patch('platform_install_ui.Msi') as msi):
            panel.folder.set(directory)
            panel.portal.download.return_value = str(Path(directory) / 'fixture.rar')
            msi.return_value.metadata.side_effect = read_metadata
            try:
                panel.prepare_distribution()
                self.assertTrue(reading.wait(2))
                self.assertTrue(panel.running)
                for key in FEATURES:
                    self.toggle_to(panel, key, key == 'CSServer')
            finally:
                proceed.set()
            deadline = time.monotonic() + 4
            while panel.running and time.monotonic() < deadline:
                self.app.root.update()
                time.sleep(.01)
            self.assertFalse(panel.running)
            self.assertEqual(panel.settings()['features'], ['CSServer'])
            self.assertEqual(str(panel.feature_widgets['CSServer'].cget('state')), 'normal')
            self.assertEqual(str(panel.feature_widgets['Convertor'].cget('state')), 'disabled')
            self.assertTrue(all(identifier != threading.get_ident() for identifier in worker_threads))


if __name__ == '__main__':
    unittest.main()
