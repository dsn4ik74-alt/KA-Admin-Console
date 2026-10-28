"""User-triggered table copy and confirmed deletion; clipboard is memory-only."""
import csv
import io
import stat
import unittest
from unittest.mock import Mock,patch
import test_service_ui


class ActionTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp

    def panel(self,directory=False):
        self.app._switch_mode('linux_disk');panel=self.app._pages['linux_disk']
        panel.ssh_host.set('vm');panel.ssh_password.set('secret')
        row=dict(name='folder' if directory else 'file',path='/data/folder' if directory else '/data/file',directory=directory,
                 size=512,apparent=100,files=1,identity=dict(device=1,inode=2,mtime_ns=3,size=100,mode=stat.S_IFDIR if directory else stat.S_IFREG))
        panel.loaded(dict(path='/data',children=[row],largest=[] if directory else [row],extensions=[],size=512,files=1,skipped=0,available=100))
        panel.trees['children'].selection_set('0');panel.update_table_actions()
        return panel

    def test_copy_table_contains_headings_and_displayed_values(self):
        panel=self.panel();panel.copy_table()
        rows=list(csv.reader(io.StringIO(panel.clipboard_get()),delimiter='\t'))
        self.assertEqual(rows[0],['Имя','На диске','Размер файлов','Файлов'])
        self.assertEqual(rows[1],['file','512 Б','100 Б','1'])
        self.assertEqual(panel.copy_button.cget('text'),'Копировать журнал')

    def test_copy_uses_active_table_and_does_not_touch_clipboard_without_action(self):
        panel=self.panel();panel.tabs.select(1)
        with patch.object(panel,'clipboard_clear') as clear,patch.object(panel,'clipboard_append') as append:
            panel.update_table_actions();clear.assert_not_called();append.assert_not_called()
            panel.copy_table()
        self.assertIn('Полный путь',append.call_args.args[0]);self.assertIn('/data/file',append.call_args.args[0])

    def test_declining_file_deletion_never_starts_an_ssh_worker(self):
        panel=self.panel()
        with patch('linux_disk_ui.messagebox.askyesno',return_value=False) as confirm,patch.object(panel,'work') as work:panel.delete_selected()
        self.assertIn('/data/file',confirm.call_args.args[1]);self.assertEqual(confirm.call_args.kwargs['default'],'no')
        work.assert_not_called()

    def test_directory_deletion_requires_the_exact_path(self):
        panel=self.panel(directory=True)
        with patch('linux_disk_ui.simpledialog.askstring',return_value='YES'),patch.object(panel,'work') as work:panel.delete_selected()
        work.assert_not_called()
        with patch('linux_disk_ui.simpledialog.askstring',return_value='/data/folder'),patch.object(panel,'work') as work:panel.delete_selected()
        operation,success=work.call_args.args
        with patch('linux_disk_ui.run_script',return_value=dict(deleted='/data/folder',directory=True)) as run:success(operation(Mock()))
        self.assertEqual(run.call_args.args[0],'linux_disk_delete');self.assertTrue(run.call_args.args[2]['recursive'])
        self.assertEqual(panel._views,{});self.assertEqual(panel.trees['children'].get_children(),())

    def test_changing_server_disables_deletion_of_old_servers_results(self):
        panel=self.panel();panel.ssh_host.set('another')
        self.assertEqual(str(panel.delete_button['state']),'disabled')
        with patch('linux_disk_ui.messagebox.askyesno') as confirm,patch.object(panel,'work') as work:panel.delete_selected()
        confirm.assert_not_called();work.assert_not_called()

    def test_delete_is_disabled_for_extension_summary_and_disk_map(self):
        panel=self.panel()
        for index in (2,3):
            panel.tabs.select(index);panel.update_table_actions()
            self.assertEqual(str(panel.delete_button['state']),'disabled')


if __name__=='__main__':unittest.main()
