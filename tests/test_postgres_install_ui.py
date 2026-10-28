"""New pages stay responsive; selection never starts an installation."""
from unittest.mock import Mock, patch
from pathlib import Path
import unittest
import test_service_ui
from bookmark_navigation import NavigationLayout, default_layout
from postgres_install_catalog import distributions


class InstallUiTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp

    def legacy_catalog(self):
        release=dict(version='14.3-1.1C',date='10.05.2022',url='https://releases.1c.ru/version_files?nick=AddCompPostgre&ver=14.3-1.1C')
        source=(Path(__file__).parent/'fixtures'/'postgresql_14_3_distribution_links.html').read_text(encoding='utf-8')
        rows=distributions(source,release,release['url'])
        self.app._switch_mode('postgres_install');panel=self.app._pages['postgres_install']
        panel.portal=Mock();panel.portal.postgres_distributions.return_value=rows;panel.releases=[release]
        panel.render('releases');panel.trees['releases'].selection_set('0')
        return panel,release,rows

    def test_selecting_legacy_release_shows_generic_linux_deb_without_downloading(self):
        panel,release,rows=self.legacy_catalog();panel.execution.set('SSH · Ubuntu / Debian')
        panel.searches['distributions'].set('stale query')
        with patch.object(panel,'work') as work:
            panel.choose_release();operation,loaded=work.call_args.args;loaded(operation(Mock()))
        self.assertEqual(panel.trees['distributions'].get_children(),('1',));self.assertEqual(panel.searches['distributions'].get(),'')
        self.assertEqual(panel.trees['distributions'].set('1','format'),'DEB')
        self.assertIn('14.3-1.1C',panel.note_label.cget('text'));self.assertEqual(str(panel.execute_button['state']),'disabled')
        panel.trees['distributions'].selection_set('1');panel.choose_distribution()
        self.assertEqual(str(panel.execute_button['state']),'normal');panel.portal.download.assert_not_called()
        panel.execution.set('Локально · Windows');self.assertEqual(panel.trees['distributions'].get_children(),('0',))

    def test_empty_mode_has_visible_reason_and_cannot_start_installation(self):
        panel,release,rows=self.legacy_catalog();panel.distributions=rows[:1];panel.loaded_release=release
        panel.execution.set('SSH · Ubuntu / Debian');panel.render('distributions')
        self.assertEqual(panel.empty_labels['distributions'].winfo_manager(),'place')
        self.assertIn('Ubuntu / Debian',panel.empty_labels['distributions'].cget('text'))
        self.assertIn('14.3-1.1C',panel.empty_labels['distributions'].cget('text'))
        with patch.object(panel,'execute') as install:panel.execute_button.invoke()
        install.assert_not_called();self.assertEqual(str(panel.execute_button['state']),'disabled')

    def test_refresh_uses_loaded_release_and_empty_search_explains_how_to_restore_list(self):
        panel,release,rows=self.legacy_catalog();panel.distributions=rows;panel.loaded_release=release
        panel.execution.set('SSH · Ubuntu / Debian');panel.searches['distributions'].set('Ubuntu')
        self.assertEqual(panel.trees['distributions'].get_children(),('1',))
        panel.searches['distributions'].set('missing')
        self.assertIn('Очистите',panel.empty_labels['distributions'].cget('text'))
        with patch.object(panel,'work') as work:
            panel.refresh_distributions();operation,loaded=work.call_args.args;loaded(operation(Mock()))
        panel.portal.postgres_distributions.assert_called_once_with(release)
        self.assertEqual(panel.trees['distributions'].get_children(),('1',));self.assertFalse(panel.running)
    def test_postgres_page_lazy_and_instance_drafts_survive_switching_modes(self):
        self.assertNotIn('postgres_install',self.app._pages)
        self.app._switch_mode('postgres_install');panel=self.app._pages['postgres_install']
        self.assertEqual(str(panel.mode_choice['state']),'readonly')
        panel.data.set(r'E:\Postgres\MyData');panel.execution.set('SSH · Ubuntu / Debian')
        self.assertEqual(panel.ssh_form.winfo_manager(),'pack');self.assertTrue(panel.data.get().startswith('/'))
        panel.data.set('/data/custom');panel.execution.set('Локально · Windows')
        self.assertEqual(panel.data.get(),r'E:\Postgres\MyData');panel.execution.set('SSH · Ubuntu / Debian')
        self.assertEqual(panel.data.get(),'/data/custom')
        panel.finish('Готово');self.assertEqual(str(panel.mode_choice['state']),'readonly')
        self.assertEqual(str(panel.listen_choice['state']),'readonly')

    def test_distribution_click_and_version_filter_do_not_download(self):
        self.app._switch_mode('postgres_install');panel=self.app._pages['postgres_install'];panel.portal=Mock()
        panel.distributions=[dict(title='Windows',version='18.4-1.1C',os='windows',architecture='x86_64',format='package')]
        panel.render('distributions');panel.trees['distributions'].selection_set('0');panel.choose_distribution()
        panel.portal.download.assert_not_called();self.assertFalse(panel.running)
        panel.releases=[dict(version='18.4-1.1C',date='2026-10-15'),dict(version='17.10-2.1C',date='2026-10-15')]
        panel.searches['releases'].set('17.10');self.assertEqual(panel.trees['releases'].get_children(),('1',))

    def test_linux_confirmation_shows_exact_service_name_and_real_file_path(self):
        panel,release,rows=self.legacy_catalog();panel.execution.set('SSH · Ubuntu / Debian')
        panel.distributions=rows;panel.render('distributions');panel.trees['distributions'].selection_set('1')
        panel.instance.set('MyPG_5555');panel.port.set('5433');panel.pg_password.set('test-password')
        with patch('postgres_install_ui.messagebox.askyesno',return_value=False) as confirm,patch.object(panel,'work') as work:
            panel.execute()
        text=confirm.call_args.args[1]
        self.assertIn('Служба: MyPG_5555.service',text)
        self.assertIn('/etc/systemd/system/MyPG_5555.service',text)
        self.assertNotIn('postgresql@',text);work.assert_not_called()

    def test_disk_page_is_independent_and_invalid_path_reveals_real_entry(self):
        self.app._switch_mode('linux_disk');panel=self.app._pages['linux_disk'];panel.path.set('relative')
        with patch.object(self.app,'_panel_error') as notice:panel.execute()
        self.assertIs(notice.call_args.args[2],panel.path_entry)
        self.app._switch_mode('postgres_install');other=self.app._pages['postgres_install']
        self.assertIsNot(other,panel);self.assertFalse(other.running)

    def test_existing_layout_gets_new_pages_in_postgres_and_linux_folders(self):
        old=default_layout()
        old=[node for node in old if node['id']!='folder-linux']
        for node in old:
            if node['id']=='folder-sql':
                for child in node['children']:
                    if child['id']=='folder-postgres':child['children']=[p for p in child['children'] if p['id']!='postgres_install']
        layout=NavigationLayout(old)
        self.assertTrue(layout.contains('folder-postgres','postgres_install'));self.assertTrue(layout.contains('folder-linux','linux_disk'))

    def test_disk_size_headings_sort_numeric_values_across_units(self):
        self.app._switch_mode('linux_disk');panel=self.app._pages['linux_disk']
        rows=[dict(name='big',path='/big',directory=True,size=1024**3,apparent=1024**3,files=2),
              dict(name='small',path='/small',directory=True,size=9*1024**2,apparent=9*1024**2,files=20)]
        panel.loaded(dict(path='/',children=rows,largest=[],extensions=[],size=1024**3,files=22,available=1024,skipped=0))
        panel.sorters['children'].toggle('size')
        self.assertEqual(panel.trees['children'].get_children(),('1','0'))
        panel.sorters['children'].toggle('size')
        self.assertEqual(panel.trees['children'].get_children(),('0','1'))


