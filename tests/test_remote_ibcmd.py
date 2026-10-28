"""Run the real SSH protocol against a separate test executor, never a DB."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from postgres_jobs import run_job
from postgres_runner import Runner, execute
from test_dt_download import LocalSFTPClient
import test_service_ui


REMOTE = r'''
import json,os,pathlib,platform,socket,sys,tempfile
executor=pathlib.Path(sys.argv[1])
os.chdir(executor)
(executor/'tmp').mkdir(exist_ok=True)
tempfile.tempdir=str(executor/'tmp')
source=json.loads(sys.stdin.buffer.readline())
scope={'__name__':'remote_fixture'}
exec(compile(source,'<remote-console-runner>','exec'),scope)
original=scope['Runner'].run
scope['Runner'].tool=lambda self,name:name
def run(self,args,*other,**kwargs):
    return original(self,[sys.executable,str(executor/'tools.py'),*args],*other,**kwargs)
scope['Runner'].run=run
socket.gethostname=lambda:'VM-EXECUTOR'
platform.system=lambda:'Linux'
actual_execute=scope['execute']
def execute(payload,*args):
    result=actual_execute(payload,*args)
    if payload['mode']=='ibcmd_export':
        for key in ('workspace','path'):
            result[key]='/executor/'+pathlib.Path(result[key]).relative_to(executor).as_posix()
    return result
scope['execute']=execute
scope['remote_main']()
'''

TOOLS = r'''
import json,os,pathlib,sys
tool,args=sys.argv[1],sys.argv[2:]
with open('calls.jsonl','a',encoding='utf-8') as target:
    target.write(json.dumps(dict(tool=tool,args=args,cwd=os.getcwd()))+'\n')
if tool != 'ibcmd':
    raise RuntimeError('A database tool must not run for this test')
if args == ['--version']:
    print('1C:Enterprise 8.3.27.1859 ibcmd')
elif args[:2] == ['infobase','dump']:
    pathlib.Path(args[-1]).write_bytes(b'DT_CREATED_ON_EXECUTOR')
    print('Dump completed pg-fixture-secret ib-fixture-secret')
else:
    raise RuntimeError('Unexpected ibcmd command')
'''


class RemoteProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executor, self.console = self.root / 'executor', self.root / 'console'
        self.executor.mkdir()
        self.console.mkdir()
        self.ssh = self.root / 'ssh_fixture.py'
        self.ssh.write_text(REMOTE, encoding='utf-8')
        (self.executor / 'tools.py').write_text(TOOLS, encoding='utf-8')
        self.connection = dict(execution='ssh', ssh_host='VM-EXECUTOR', ssh_user='usr1cv8',
            host='PG-THIRD-VM', port='5432', username='postgres', password='pg-fixture-secret',
            ib_password='ib-fixture-secret', ib_user='Admin', data_path=str(self.executor / 'srvinfo'), ibcmd='ibcmd')
        self.events = []

    def run_remote(self, payload):
        def start(*args):
            process = subprocess.Popen([sys.executable, str(self.ssh), str(self.executor)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            process.client = LocalSFTPClient(self.executor)
            return process
        with patch('postgres_jobs.start_remote', side_effect=start):
            return run_job(payload, self.events.append)

    def calls(self):
        return [json.loads(row) for row in (self.executor / 'calls.jsonl').read_text().splitlines()]

    def test_probe_executes_remotely_without_database_connection_or_backup_creation(self):
        connection = dict(self.connection, host='', username='', port='not configured')
        result = self.run_remote(dict(mode='ibcmd_probe', connection=connection))
        self.assertEqual(result['computer'], 'VM-EXECUTOR')
        self.assertEqual(result['system'], 'Linux')
        self.assertIn('8.3.27.1859', result['version'])
        self.assertEqual([row['args'] for row in self.calls()], [['--version']])
        self.assertFalse(list(self.executor.rglob('*.dt')))
        self.assertEqual(list(self.console.iterdir()), [])

    def test_dt_is_created_on_linux_downloaded_to_console_and_remote_temporary_copy_removed(self):
        folder = self.console / 'backups'
        result = self.run_remote(dict(mode='ibcmd_dump', connection=self.connection,
                                      databases=['One'], path=str(folder)))
        self.assertEqual(len(result), 1)
        artifact = Path(result[0])
        self.assertTrue(artifact.is_relative_to(folder))
        self.assertEqual(artifact.read_bytes(), b'DT_CREATED_ON_EXECUTOR')
        self.assertEqual(artifact.suffix, '.dt')
        self.assertEqual(list((self.executor / 'tmp').iterdir()), [])
        self.assertFalse(list(folder.rglob('*.part')))
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn('--database-server=PG-THIRD-VM port=5432', calls[0]['args'])
        self.assertIn('--data=' + self.connection['data_path'], calls[0]['args'])
        self.assertEqual(Path(calls[0]['cwd']), self.executor)
        self.assertNotIn('pg-fixture-secret', str(self.events))
        self.assertNotIn('ib-fixture-secret', str(self.events))
        self.assertTrue(any(event.get('kind') == 'artifact' and event['path'] == str(artifact) for event in self.events))

    def test_probe_timeout_stops_a_silent_process(self):
        runner = Runner(dict(host='unused', username='unused'), Mock())
        with self.assertRaisesRegex(RuntimeError, 'не завершилась'):
            runner.run([sys.executable, '-c', 'import time; time.sleep(30)'], capture=True, timeout=.2)

    def test_keep_on_server_flag_never_downloads_and_retains_server_artifact(self):
        folder = self.executor / 'backups'
        with patch('postgres_jobs.download_dt', side_effect=AssertionError('Must not download')):
            result = self.run_remote(dict(mode='ibcmd_dump', connection=dict(self.connection, dt_keep_remote=True),
                                          databases=['One'], path=str(folder)))
        self.assertEqual(Path(result[0]).read_bytes(), b'DT_CREATED_ON_EXECUTOR')
        self.assertTrue(Path(result[0]).is_relative_to(folder))
        self.assertEqual(list(self.console.iterdir()), [])
        self.assertEqual(list((self.executor / 'tmp').iterdir()), [])
        self.assertTrue(any(event.get('kind') == 'artifact' and event['path'] == result[0] for event in self.events))


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('ibcmd_dump')
        return self.app.postgres

    def test_remote_executor_is_explicit_and_probe_requires_neither_database_nor_exclusive_mode(self):
        panel = self.panel()
        self.assertEqual(panel.execution_combo.caption.cget('text'), 'Где запускать ibcmd')
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('VM-EXECUTOR')
        panel.ssh_user.set('usr1cv8')
        panel.tool_values['ibcmd'].set('/opt/1cv8/x86_64/8.3.27.1859/ibcmd')
        self.assertIn('VM-EXECUTOR', panel.ibcmd_executor_note.cget('text'))
        self.assertIsNone(panel.connected)
        self.assertFalse(panel.exclusive.get())
        with patch.object(panel, 'work') as work:
            panel.ibcmd_probe_button.invoke()
        self.assertTrue(panel.running)
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', return_value={}) as job:
            operation(Mock())
        payload = job.call_args.args[0]
        self.assertEqual(payload['mode'], 'ibcmd_probe')
        self.assertEqual(payload['connection']['execution'], 'ssh')
        self.assertEqual(payload['connection']['ssh_host'], 'VM-EXECUTOR')
        success(dict(computer='VM-EXECUTOR', system='Linux', ibcmd='/opt/1cv8/ibcmd', version='8.3.27.1859'))
        self.assertFalse(panel.running)
        self.assertIn('VM-EXECUTOR', panel.output.get('1.0', 'end-1c'))
        self.assertIsNone(panel.connected)

    def test_empty_remote_host_shows_error_without_starting_any_command(self):
        panel = self.panel()
        panel.execution.set('SSH · Linux')
        with patch.object(panel, 'work') as work:
            panel.ibcmd_probe_button.invoke()
        work.assert_not_called()
        self.assertIn('компьютера выполнения', self.app.form_notice.message.cget('text'))

    def test_dump_payload_separates_executor_postgres_and_local_download_folder(self):
        panel = self.panel()
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('VM-EXECUTOR')
        panel.ssh_user.set('usr1cv8')
        panel.host.set('PG-THIRD-VM')
        panel.tool_values['ibcmd'].set('/opt/1cv8/ibcmd')
        panel.data_path.set('/home/usr1cv8/srvinfo')
        panel.loaded(panel.connection_key(), [dict(name='One', owner='postgres')])
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.exclusive.set(True)
        panel.path.set(r'C:\Backup\DT')
        with patch.object(panel, 'work') as work:
            panel.execute()
        operation = work.call_args.args[0]
        with patch('postgres_ui.run_job', return_value=[r'C:\Backup\DT\One\One.dt']) as job:
            operation(Mock())
        payload = job.call_args.args[0]
        self.assertEqual(payload['mode'], 'ibcmd_dump')
        self.assertEqual(payload['path'], r'C:\Backup\DT')
        self.assertEqual(payload['connection']['host'], 'PG-THIRD-VM')
        self.assertEqual(payload['connection']['ssh_host'], 'VM-EXECUTOR')
        self.assertEqual(payload['connection']['data_path'], '/home/usr1cv8/srvinfo')
        self.assertEqual(payload['connection']['ibcmd'], '/opt/1cv8/ibcmd')
        panel.finish('Fixture finished')

    def test_only_local_windows_and_ssh_linux_are_available_and_dt_picker_is_local(self):
        panel = self.panel()
        self.assertEqual(tuple(panel.execution_combo.cget('values')), ('Локально · Windows', 'SSH · Linux'))
        panel.execution.set('SSH · Linux')
        self.assertIn('Локальная папка DT', panel.path_label.cget('text'))
        self.assertIn('Выбрать в проводнике', panel.picker_button.cget('text'))
        with patch('postgres_ui.filedialog.askdirectory', return_value=r'C:\Downloads\DT') as picker:
            panel.choose_path()
        picker.assert_called_once()
        self.assertEqual(panel.path.get(), r'C:\Downloads\DT')
        self.assertIn('скачан', panel.path_note.cget('text'))
        self.app._switch_mode('pg_dump')
        self.assertIn('вручную', panel.picker_button.cget('text'))
        panel.execution.set('SSH · Windows')  # A stale profile cannot resurrect the removed mode.
        self.assertEqual(panel.execution.get(), 'Локально · Windows')
        self.assertEqual(panel._execution, 'local')

    def test_keep_on_server_checkbox_changes_path_and_preserves_local_and_remote_destinations(self):
        panel = self.panel()
        panel.execution.set('SSH · Linux')
        self.assertFalse(panel.dt_keep_remote.get())
        panel.path.set(r'C:\Downloads\DT')
        panel.dt_keep_remote.set(True)
        self.assertEqual(panel.path.get(), '')  # Never send a local Windows path to Linux.
        self.assertIn('на компьютере выполнения · Linux', panel.path_label.cget('text'))
        self.assertIn('вручную', panel.picker_button.cget('text'))
        panel.path.set('/BACKUP/DT')
        with patch('postgres_ui.filedialog.askdirectory') as picker:
            panel.choose_path()
        picker.assert_not_called()
        panel.dt_keep_remote.set(False)
        self.assertEqual(panel.path.get(), r'C:\Downloads\DT')
        self.assertIn('Выбрать в проводнике', panel.picker_button.cget('text'))
        panel.dt_keep_remote.set(True)
        self.assertEqual(panel.path.get(), '/BACKUP/DT')
        self.assertTrue(panel.connection()['dt_keep_remote'])
        self.assertIn('скачивания в консоль не будет', panel.ibcmd_executor_note.cget('text'))

    def test_keep_on_execution_computer_is_available_for_local_windows_and_uses_its_folder_picker(self):
        panel = self.panel()
        self.assertEqual(panel.dt_destination_check.winfo_manager(), 'pack')
        panel.path.set(r'C:\DT')
        panel.dt_keep_remote.set(True)
        self.assertEqual(panel.path.get(), r'C:\DT')
        self.assertIn('на компьютере выполнения · Windows', panel.path_label.cget('text'))
        self.assertIn('та же ВМ', panel.path_note.cget('text'))
        with patch('postgres_ui.filedialog.askdirectory', return_value=r'D:\Backups') as picker:
            panel.choose_path()
        picker.assert_called_once()
        self.assertEqual(panel.path.get(), r'D:\Backups')
        self.assertTrue(panel.connection()['dt_keep_remote'])


if __name__ == '__main__':
    unittest.main()
