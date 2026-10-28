import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import paramiko
import ssh_connection
from postgres_jobs import BOOTSTRAP, read_only_job, run_job, start_remote
import test_service_ui


class AuthenticationTests(unittest.TestCase):
    def test_password_default_does_not_try_keys_or_put_password_in_command(self):
        client = Mock()
        with patch('ssh_connection.paramiko.SSHClient', return_value=client):
            ssh_connection.connect(dict(host='vm', user=r'DOMAIN\Администратор ВМ', password='ssh-secret'))
        options = client.connect.call_args.kwargs
        self.assertEqual(options['password'], 'ssh-secret')
        self.assertFalse(options['allow_agent'])
        self.assertFalse(options['look_for_keys'])
        self.assertNotIn('key_filename', options)
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], ssh_connection.RequireKnownHost)

    def test_key_checkbox_uses_key_passphrase_without_password_fallback(self):
        with tempfile.NamedTemporaryFile() as key:
            client = Mock()
            with patch('ssh_connection.paramiko.SSHClient', return_value=client):
                ssh_connection.connect(dict(host='vm', user='operator', use_key=True, key=key.name, password='key-secret'))
        options = client.connect.call_args.kwargs
        self.assertEqual(options['key_filename'], key.name)
        self.assertEqual(options['passphrase'], 'key-secret')
        self.assertTrue(options['allow_agent'])
        self.assertNotIn('password', options)

    def test_empty_password_requires_explicit_key_selection(self):
        with patch('ssh_connection.paramiko.SSHClient') as client:
            with self.assertRaisesRegex(ValueError, 'По SSH-ключу'):
                ssh_connection.connect(dict(host='vm', user='operator'))
        client.assert_not_called()

    def test_first_key_trust_is_only_available_for_read_only_jobs(self):
        error = ssh_connection.UnknownHostKey('vm', paramiko.RSAKey.generate(1024))
        job = Mock(side_effect=error)
        result = read_only_job(dict(mode='ibcmd_probe'), Mock(), runner=job)
        self.assertEqual(result['ssh_host_key']['host'], 'vm')
        with self.assertRaisesRegex(ValueError, 'только для чтения'):
            read_only_job(dict(mode='ibcmd_dump'), Mock(), runner=job)
        self.assertEqual(job.call_count, 1)

    def test_shared_installer_transport_uses_the_same_authentication(self):
        import linux_platform
        with patch('linux_platform.shared_ssh_connect', return_value='client') as connect:
            self.assertEqual(linux_platform.connect(dict(host='vm', user='operator', password='ssh-secret', use_key=False)), 'client')
        self.assertFalse(connect.call_args.args[0]['use_key'])

    def test_saved_password_mode_and_key_mode_roundtrip_without_plaintext_secrets(self):
        from test_activity import mock_dpapi
        mock_dpapi(self)
        from activity_store import ActivityStore, clean_profile
        with tempfile.TemporaryDirectory() as temp:
            store = ActivityStore(Path(temp) / 'activity.json')
            profile = dict(kind='postgres', page='ibcmd_dump', values=dict(execution='SSH · Windows',
                host='PG-VM', port='5432', username='postgres', ssh_host='EXECUTOR', ssh_user='operator',
                ssh_use_key=False, ssh_password='SSH-SAVED-SECRET', password='PG-SAVED-SECRET', dt_keep_remote=True))
            store.save_favorite('Remote', profile)
            store.save_connection(profile)
            store.close(wait=True)
            saved = store.path.read_text(encoding='utf-8')
            self.assertNotIn('SSH-SAVED-SECRET', saved)
            self.assertNotIn('PG-SAVED-SECRET', saved)
            for section in ('favorites', 'connections'):
                row = ActivityStore(store.path).snapshot(section)[0]
                self.assertFalse(row['values']['ssh_use_key'])
                self.assertIs(row['values']['dt_keep_remote'], True)
                self.assertEqual(store.credentials(row)['ssh_password'], 'SSH-SAVED-SECRET')
            legacy = clean_profile(dict(kind='postgres', page='pg_dump', values=dict(execution='SSH · Linux')))
            self.assertTrue(legacy['values']['ssh_use_key'])

    def test_failed_remote_start_closes_authenticated_session(self):
        client = Mock()
        with patch('postgres_jobs.ssh_connect', return_value=client), \
             patch('postgres_jobs.SSHProcess', side_effect=RuntimeError('fixture')):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                start_remote(dict(execution='ssh'), Mock())
        client.close.assert_called_once()

    def test_protocol_does_not_forward_ssh_password_and_redacts_typed_events(self):
        settings = dict(execution='ssh', ssh_host='EXECUTOR', ssh_password='SSH-SECRET',
                        ssh_host_keys={'EXECUTOR': 'public key'}, password='PG-SECRET')
        process = Mock()
        process.stdin, process.stdout = io.BytesIO(), io.BytesIO(
            b'{"kind":"message","text":"SSH-SECRET PG-SECRET"}\n{"kind":"result","result":[]}\n')
        process.stdin.close = Mock()
        process.poll.return_value, process.wait.return_value = 0, 0
        events = []
        with patch('postgres_jobs.start_remote', return_value=process):
            run_job(dict(mode='list', connection=settings), events.append)
        payload = json.loads(process.stdin.getvalue().splitlines()[1])
        self.assertNotIn('ssh_password', payload['connection'])
        self.assertNotIn('ssh_host_keys', payload['connection'])
        self.assertEqual(payload['connection']['password'], 'PG-SECRET')
        self.assertNotIn('SECRET', str(events))
        process.close.assert_called_once()


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_all_ssh_forms_default_to_password_and_show_key_only_when_selected(self):
        for mode, name in (('pg_dump', 'postgres'), ('pg_copy', 'postgres_copy'), ('platform_install', 'platform_install')):
            self.app._switch_mode(mode)
            panel = self.app._active_panel
            self.assertFalse(panel.ssh_use_key.get())
            self.assertEqual(panel.ssh_auth_group.winfo_manager(), '')
            panel.ssh_use_key.set(True)
            self.assertEqual(panel.ssh_auth_group.winfo_manager(), 'pack')
            panel.ssh_use_key.set(False)
            self.assertEqual(panel.ssh_auth_group.winfo_manager(), '')

    def test_linux_ibcmd_download_does_not_require_psql_or_a_loaded_database_list(self):
        self.app._switch_mode('ibcmd_dump')
        panel = self.app.postgres
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('VM-LINUX')
        panel.ssh_user.set('operator')
        panel.ssh_password.set('ssh-secret')
        panel.host.set('PG-VM')
        panel.manual_database.set('One')
        panel.path.set(r'D:\Backup')
        panel.data_path.set('/home/operator/ibcmd/One')
        panel.exclusive.set(True)
        with patch.object(panel, 'work') as work:
            panel.execute()
        self.assertTrue(panel.running)
        with patch('postgres_ui.run_job', return_value=[]) as job:
            work.call_args.args[0](Mock())
        payload = job.call_args.args[0]
        self.assertEqual(payload['connection']['execution'], 'ssh')
        self.assertEqual(payload['databases'], ['One'])
        self.assertEqual(payload['path'], r'D:\Backup')
        self.assertIsNone(panel.connected)
        panel.finish()


if __name__ == '__main__':
    unittest.main()
