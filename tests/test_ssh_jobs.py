"""SSH transport with a fake channel: credentials stdin, cleanup, cancellation."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import ssh_jobs
from postgres_runner import OperationCancelled
from ssh_job_runner import read_request


class Channel:
    def __init__(self,events):self.data=(('\n'.join(json.dumps(e) for e in events))+'\n').encode();self.closed=False
    def exit_status_ready(self):return not self.data
    def recv_ready(self):return bool(self.data)
    def recv_stderr_ready(self):return False
    def recv(self,size):data=self.data[:size];self.data=self.data[size:];return data
    def recv_exit_status(self):return 0
    def close(self):self.closed=True


class Input(io.StringIO):
    def close(self):self.saved=self.getvalue();super().close()


class SshTests(unittest.TestCase):
    def execute(self,events,cancel=None):
        channel=Channel(events);stdin=Input();client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        sftp=Mock();sftp.__enter__=Mock(return_value=sftp);sftp.__exit__=Mock(return_value=False)
        sftp.open.return_value=io.StringIO()
        client.open_sftp.return_value=sftp;client.exec_command.return_value=(stdin,Mock(channel=channel),Mock())
        def command(client,args,*a,**kw):return '1000' if args==['id','-u'] else 'x86_64' if args==['uname','-m'] else ''
        events_out=[]
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)/'python.tar.gz';runtime.touch()
            with patch.object(ssh_jobs,'connect',return_value=client),patch.object(ssh_jobs,'command',side_effect=command),\
                 patch.object(ssh_jobs,'runtime_for',return_value=dict(path=str(runtime),sha256='0'*64)):
                try:
                    result=ssh_jobs.run_script('linux_disk_scan',dict(password='ssh-secret',use_key=False),
                        dict(settings=dict(password='pg-secret')),events_out.append,cancel or threading.Event(),elevate=True)
                    return result,client,stdin,channel,events_out
                except OperationCancelled:
                    self.assertTrue(channel.closed);self.assertTrue(stdin.closed);raise

    def test_secrets_only_go_to_stdin_and_channel_is_closed_after_result(self):
        result,client,stdin,channel,events=self.execute([dict(kind='stage',text='ready'),dict(kind='result',result=dict(size=10))])
        self.assertEqual(result,dict(size=10));self.assertTrue(channel.closed)
        self.assertNotIn('secret',client.exec_command.call_args.args[0]);self.assertIn('ssh-secret',stdin.saved)
        request=json.loads(stdin.saved.splitlines()[1].split(':request:',1)[1]);self.assertIn('pg-secret',request['_secrets'])
        self.assertFalse(any(e['kind']=='warning' for e in events))

    def test_remote_cancellation_waits_for_worker_exit_and_cleanup(self):
        with self.assertRaisesRegex(OperationCancelled,'cancelled'):
            self.execute([dict(kind='cancelled',text='cancelled'),dict(kind='warning',text='cleanup finished')])

    def test_unknown_worker_rejected_before_ssh(self):
        with patch.object(ssh_jobs,'connect') as connect,self.assertRaises(ValueError):
            ssh_jobs.run_script('../other',{}, {},Mock(),threading.Event())
        connect.assert_not_called()

    def test_reported_remote_error_is_distinguished_from_transport_failure_and_redacted(self):
        with self.assertRaisesRegex(RuntimeError,'Операция на Linux') as error:
            self.execute([dict(kind='error',text='DNS на ВМ: ssh-secret / pg-secret')])
        self.assertIn('DNS на ВМ',str(error.exception))
        self.assertNotIn('SSH: выполнение не подтверждено',str(error.exception))
        self.assertNotIn('secret',str(error.exception))

    def test_missing_result_still_reports_unconfirmed_transport(self):
        with self.assertRaisesRegex(RuntimeError,'SSH: выполнение не подтверждено'):
            self.execute([dict(kind='stage',text='ready')])

    def test_request_frame_handles_sudo_cached_password_and_nopasswd(self):
        token='a'*32;packet=token+':request:'+json.dumps(dict(path='/var'))+'\n'
        for prefix in ('','password not consumed\n','\n'):
            self.assertEqual(read_request(io.StringIO(prefix+packet),token),dict(path='/var'))
        with self.assertRaises(ValueError):read_request(io.StringIO('invalid\n'),token)


if __name__=='__main__':unittest.main()
