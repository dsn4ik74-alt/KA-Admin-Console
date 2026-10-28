"""Remembered SSH hosts are shared, protected and never initiate a job on choice."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from activity_store import ActivityStore
import test_activity
import test_service_ui


SSH=dict(kind='ssh',page='linux_disk',values=dict(host='LINUX01',port='2222',user='operator',
    use_key=False,key='',password='SSH-TEST-SECRET',sudo_password='SUDO-TEST-SECRET'))


class StoreTests(unittest.TestCase):
    def setUp(self):test_activity.mock_dpapi(self)

    def test_ssh_password_and_sudo_roundtrip_without_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'activity.json';store=ActivityStore(path)
            key=store.save_connection(SSH);store.close(wait=True)
            content=path.read_text(encoding='utf-8')
            for secret in ('SSH-TEST-SECRET','SUDO-TEST-SECRET'):self.assertNotIn(secret,content)
            loaded=ActivityStore(path);row=loaded.snapshot('connections')[0]
            self.assertEqual(row['id'],key);self.assertEqual(row['kind'],'ssh')
            self.assertEqual(loaded.credentials(row),dict(password='SSH-TEST-SECRET',sudo_password='SUDO-TEST-SECRET'))
            self.assertEqual(row['values']['port'],'2222');loaded.close(wait=True)

    def test_postgres_form_update_preserves_sudo_and_password_and_key_modes_are_separate(self):
        store=ActivityStore();original=store.save_connection(SSH)
        values=dict(SSH['values'],password='NEW-SSH-SECRET');values.pop('sudo_password')
        self.assertEqual(store.save_connection(dict(SSH,values=values)),original)
        row=store.snapshot('connections')[0]
        self.assertEqual(store.credentials(row),dict(password='NEW-SSH-SECRET',sudo_password='SUDO-TEST-SECRET'))
        store.save_connection(dict(SSH,values=dict(values,use_key=True,key='C:/keys/id_ed25519',password='KEY-TEST-SECRET')))
        self.assertEqual(len(store.snapshot('connections')),2)
        self.assertTrue(store.snapshot('connections')[0]['values']['use_key'])

    def test_encryption_failure_does_not_replace_saved_credentials(self):
        store=ActivityStore();store.save_connection(SSH);before=store.snapshot('connections')
        with patch('activity_store.protect',side_effect=ValueError('DPAPI unavailable')),self.assertRaises(ValueError):
            store.save_connection(dict(SSH,values=dict(SSH['values'],password='replacement')))
        self.assertEqual(store.snapshot('connections'),before)


class UiTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self);test_activity.mock_dpapi(self)

    def panel(self,mode):
        self.app._switch_mode(mode);return self.app._current_panel()

    def choose(self,panel):
        widget=panel.ssh_host_entry;self.app.saved_servers.refresh(widget)
        label=next(iter(widget._saved_choices));widget.set(label);widget.event_generate('<<ComboboxSelected>>')
        return label

    def test_one_ssh_record_is_available_in_all_linux_forms_without_running_commands(self):
        self.app.activity.save_connection(SSH)
        for mode in ('linux_disk','postgres_install','platform_install','pg_dump','ibcmd_dump','pg_copy'):
            with self.subTest(mode=mode):
                panel=self.panel(mode)
                with patch.object(panel,'work') as work:label=self.choose(panel)
                work.assert_not_called();self.assertFalse(panel.running)
                self.assertEqual((panel.ssh_host.get(),panel.ssh_user.get(),panel.ssh_port.get()),('LINUX01','operator','2222'))
                self.assertEqual(panel.ssh_password.get(),'SSH-TEST-SECRET');self.assertFalse(panel.ssh_use_key.get())
                if hasattr(panel,'sudo_password'):self.assertEqual(panel.sudo_password.get(),'SUDO-TEST-SECRET')
                self.assertNotIn('SECRET',label);self.assertEqual(str(panel.ssh_host_entry['state']),'normal')

    def test_choosing_ssh_keeps_postgres_source_target_and_database_passwords(self):
        self.app.activity.save_connection(SSH);panel=self.panel('pg_copy')
        panel.host.set('SOURCE');panel.password.set('SOURCE-DB-SECRET');panel.target_host.set('TARGET');panel.target_password.set('TARGET-DB-SECRET')
        self.choose(panel)
        self.assertEqual((panel.host.get(),panel.target_host.get()),('SOURCE','TARGET'))
        self.assertEqual((panel.password.get(),panel.target_password.get()),('SOURCE-DB-SECRET','TARGET-DB-SECRET'))

    def test_key_choice_restores_passphrase_and_shows_key_fields(self):
        profile=dict(SSH,values=dict(SSH['values'],use_key=True,key='C:/keys/id_ed25519',password='KEY-TEST-SECRET'))
        self.app.activity.save_connection(profile);panel=self.panel('linux_disk');self.choose(panel)
        self.assertTrue(panel.ssh_use_key.get());self.assertEqual(panel.ssh_key.get(),'C:/keys/id_ed25519')
        self.assertEqual(panel.ssh_auth_group.winfo_manager(),'pack');self.assertEqual(panel.ssh_password.get(),'KEY-TEST-SECRET')

    def test_successful_analysis_remembers_host_even_without_separate_check(self):
        panel=self.panel('linux_disk');panel.ssh_host.set('LINUX01');panel.ssh_user.set('operator');panel.ssh_password.set('SSH-TEST-SECRET')
        with patch.object(panel,'work') as work:panel.execute()
        self.assertEqual(self.app.activity.snapshot('connections'),[])
        operation,loaded=work.call_args.args
        result=dict(path='/',children=[],largest=[],extensions=[],size=0,files=0,available=100,skipped=0)
        with patch('linux_disk_ui.run_script',return_value=result):loaded(operation(Mock()))
        self.assertEqual([r['kind'] for r in self.app.activity.snapshot('connections')],['ssh'])
        self.assertFalse(panel.running)

    def test_only_successful_ssh_check_is_saved_and_refreshes_other_tabs(self):
        panel=self.panel('linux_disk');panel.ssh_host.set('LINUX01');panel.ssh_user.set('operator');panel.ssh_password.set('SSH-TEST-SECRET')
        other=self.panel('postgres_install');self.app._switch_mode('linux_disk')
        with patch.object(panel,'work') as work:panel.check_ssh()
        self.assertEqual(self.app.activity.snapshot('connections'),[])
        operation,ready=work.call_args.args
        with patch('ssh_forms.probe',return_value=dict(description='Ubuntu 20.04')):ready(operation(Mock()))
        rows=self.app.activity.snapshot('connections');self.assertEqual([r['kind'] for r in rows],['ssh'])
        self.assertTrue(any(v.startswith('LINUX01:') for v in other.ssh_host_entry['values']))
        self.assertNotIn('SECRET',json.dumps(rows))

    def test_unknown_host_key_is_not_saved_without_successful_authentication(self):
        panel=self.panel('linux_disk');panel.ssh_host.set('NEW');panel.ssh_user.set('operator')
        with patch.object(panel,'work') as work:panel.check_ssh()
        ready=work.call_args.args[1]
        with patch('ssh_forms.messagebox.askyesno',return_value=False):ready(dict(key=dict(host='NEW',fingerprint='SHA256:fixture')))
        self.assertEqual(self.app.activity.snapshot('connections'),[])

    def test_management_removes_only_ssh_entry_and_refreshes_all_suggestions(self):
        self.app.activity.save_connection(SSH)
        sql=dict(kind='sql',page='db_backup',values=dict(server='SQL01',username='',windows_auth=True,encrypt=False,trust_certificate=True,password=''))
        self.app.activity.save_connection(sql);panel=self.panel('linux_disk');other=self.panel('postgres_install')
        window=self.app.saved_servers.manage(panel.ssh_host_entry);self.addCleanup(window.destroy)
        self.assertIn('SSH',window.title());ids=window.connection_tree.get_children();self.assertEqual(len(ids),1)
        window.connection_tree.selection_set(ids);window.remove_button.invoke()
        self.assertEqual([r['kind'] for r in self.app.activity.snapshot('connections')],['sql'])
        self.assertEqual(other.ssh_host_entry['values'],'')

    def test_unreadable_password_keeps_previous_fields_and_running_panel_is_not_changed(self):
        self.app.activity.save_connection(SSH);panel=self.panel('linux_disk')
        panel.ssh_host.set('previous');panel.ssh_password.set('previous-secret')
        self.app.saved_servers.refresh(panel.ssh_host_entry)
        panel.ssh_host_entry._before_choice='previous';panel.ssh_host_entry.set(next(iter(panel.ssh_host_entry._saved_choices)))
        with patch.object(self.app.activity,'credentials',side_effect=ValueError('Cannot decrypt')):
            self.app.saved_servers.choose(panel.ssh_host_entry)
        self.assertEqual(panel.ssh_host.get(),'previous');self.assertEqual(panel.ssh_password.get(),'previous-secret')
        panel.running=True
        try:self.choose(panel);self.assertEqual(panel.ssh_password.get(),'previous-secret')
        finally:panel.running=False

    def test_platform_and_postgres_ssh_checks_remember_captured_credentials(self):
        panel=self.panel('platform_install')
        panel.ssh_checked(dict(description='Ubuntu'),SSH['values'],False)
        self.assertEqual(self.app.activity.credentials(self.app.activity.snapshot('connections')[0])['sudo_password'],'SUDO-TEST-SECRET')
        pg=self.panel('pg_dump');pg.execution.set('SSH · Linux')
        pg.ssh_checked(dict(description='Ubuntu'),dict(SSH['values'],password='UPDATED-SSH-SECRET'))
        rows=self.app.activity.snapshot('connections');self.assertEqual(len(rows),1)
        self.assertEqual(self.app.activity.credentials(rows[0])['password'],'UPDATED-SSH-SECRET')


if __name__=='__main__':unittest.main()
