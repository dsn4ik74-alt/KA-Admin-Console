"""Cached navigation never hides an explicit refresh or crosses connections."""
from unittest.mock import Mock,patch
import unittest
import test_service_ui


def result(path):
    return dict(path=path,children=[],largest=[],extensions=[],size=0,files=0,available=100,skipped=0)


class NavigationTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('linux_disk');panel=self.app._pages['linux_disk']
        panel.ssh_host.set('vm');panel.ssh_user.set('operator');panel.ssh_password.set('secret')
        return panel

    def test_back_navigation_uses_snapshot_without_a_second_ssh_scan(self):
        panel=self.panel();panel.loaded(result('/data'));panel.loaded(result('/data/child'))
        with patch.object(panel,'execute') as execute:panel.up()
        execute.assert_not_called();self.assertEqual(panel.path.get(),'/data');self.assertFalse(panel.running)
        self.assertIn('Из памяти',panel.summary.cget('text'))

    def test_analyze_button_always_refreshes_even_if_snapshot_exists(self):
        panel=self.panel();panel.loaded(result('/data'))
        with patch.object(panel,'work') as work:panel.execute_button.invoke()
        work.assert_called_once()
        operation,loaded=work.call_args.args
        with patch('linux_disk_ui.run_script',return_value=result('/data')) as scan:
            loaded(operation(Mock()))
        scan.assert_called_once();self.assertNotIn('Из памяти',panel.summary.cget('text'))

    def test_host_mode_privileges_and_mount_choices_have_independent_views(self):
        panel=self.panel();panel.loaded(result('/data'))
        for variable,value in ((panel.ssh_host,'other'),(panel.scan_mode,'Быстрый обзор (du)'),(panel.elevate,True),(panel.cross,True)):
            original=variable.get();variable.set(value)
            with patch.object(panel,'execute') as execute:panel.navigate('/data')
            execute.assert_called_once();variable.set(original)

    def test_quick_mode_is_sent_to_server_and_unknown_counts_are_dashes(self):
        panel=self.panel();panel.scan_mode.set('Быстрый обзор (du)')
        with patch.object(panel,'work') as work:panel.execute()
        operation,loaded=work.call_args.args
        fast=dict(result('/data'),mode='quick',files=None,skipped=None,children=[dict(name='file',path='/data/file',directory=False,size=512,apparent=None,files=None)])
        with patch('linux_disk_ui.run_script',return_value=fast) as scan:loaded(operation(Mock()))
        self.assertEqual(scan.call_args.args[2]['mode'],'quick')
        self.assertEqual(panel.trees['children'].set('0','apparent'),'—');self.assertEqual(panel.trees['children'].set('0','files'),'—')
        self.assertEqual(str(panel.mode_choice['state']),'readonly')

    def test_memory_retains_at_most_four_views_and_never_stores_passwords_in_keys(self):
        panel=self.panel()
        for i in range(6):panel.loaded(result('/folder'+str(i)))
        self.assertEqual(len(panel._views),4)
        self.assertNotIn('secret',repr(list(panel._views)))
        with patch.object(panel,'execute') as execute:panel.navigate('/folder0')
        execute.assert_called_once()


if __name__=='__main__':unittest.main()
