from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from unittest.mock import Mock, patch

from postgres_runner import OperationCancelled
from scheduler_actions import bundle_paths, capture, execute_step, secret_values
from scheduler_store import SchedulerStore, next_time
from scheduler_worker import executor_lock, run_chain, Journal
from scheduler_windows import control, worker_command
import test_activity
import test_service_ui
import test_cluster_infobase as cluster_fixtures


def command(code='Write-Output fixture', title='Fixture'):
    return dict(action='command', title=title, summary='Windows', parameters={'code': code})


class ScheduleTests(unittest.TestCase):
    def test_once_daily_weekly_and_interval_use_future_local_time(self):
        now=datetime(2026,10,19,14,30).timestamp()
        rule=dict(kind='once',start='2026-10-19 15:00')
        self.assertEqual(next_time(rule,now),datetime(2026,10,19,15).timestamp())
        self.assertIsNone(next_time(rule,now+3600))
        rule['kind']='daily'
        self.assertEqual(next_time(rule,now+3600),datetime(2026,10,20,15).timestamp())
        rule.update(kind='weekly',days=[2,4])
        self.assertEqual(next_time(rule,now),datetime(2026,10,21,15).timestamp())
        rule.update(kind='interval',minutes=60)
        self.assertEqual(next_time(rule,datetime(2026,10,19,16,30).timestamp()),datetime(2026,10,19,17).timestamp())
        self.assertEqual(next_time(rule,datetime(2026,10,19,16).timestamp()),datetime(2026,10,19,17).timestamp())

    def test_invalid_days_interval_and_rule_rejected(self):
        for rule in (dict(kind='weekly',days=[],start='2026-10-19 15:00'),
                     dict(kind='interval',minutes=0,start='2026-10-19 15:00'),
                     dict(kind='unknown',start='2026-10-19 15:00')):
            with self.assertRaises(ValueError):next_time(rule,time.time())


class StoreTests(unittest.TestCase):
    def setUp(self):
        test_activity.mock_dpapi(self)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'scheduler.sqlite3'
        self.store=SchedulerStore(self.path);self.store.initialize()
        self.rule=dict(kind='daily',start=(datetime.now()+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'))

    def save(self,steps=None,**options):
        return self.store.save('Fixture',self.rule,steps or [command()],**options)

    def claim(self,key):
        self.store.request(key)
        return self.store.claim('test')

    def test_snapshot_encrypted_and_shared_across_independent_connections(self):
        step=command();step['parameters'].update(password='private-fixture', nested={'db-pwd':'other-private'})
        key=self.save([step])
        self.assertEqual(SchedulerStore(self.path).load(key)['steps'],[step])
        for file in self.path.parent.glob('*'):
            if file.is_file():
                self.assertNotIn(b'private-fixture',file.read_bytes())
                self.assertNotIn(b'other-private',file.read_bytes())
        self.assertEqual(len(self.store.jobs()),1)

    def test_saving_legacy_pg_job_keeps_portable_tools_and_original_credentials(self):
        old = r'C:\Users\operator\AppData\Local\Temp\_MEI00004d142\vendor\postgresql\14\bin\psql.exe'
        connection = dict(host='SOURCE', username='pg', psql=old, password='fixture-private',
                          target_connection=dict(host='TARGET', username='pg', psql=old))
        step = dict(action='pg_copy', title='Copy', parameters=dict(connection=connection,
                    source_database='one', target_database='two', path='C:/temp'))
        key = self.save([step])
        saved = self.store.load(key)['steps'][0]['parameters']['connection']
        self.assertEqual(saved['psql'], '@ka-bundle/vendor/postgresql/14/bin/psql.exe')
        self.assertEqual(saved['target_connection']['psql'], saved['psql'])
        self.assertEqual(saved['password'], connection['password'])
        self.assertEqual(connection['psql'], old)

    def test_claim_is_atomic_and_duplicate_request_is_rejected(self):
        key=self.save();self.store.request(key)
        with self.assertRaises(ValueError):self.store.request(key)
        with ThreadPoolExecutor(2) as pool:
            claims=list(pool.map(lambda i:SchedulerStore(self.path).claim(str(i)),range(2)))
        self.assertEqual(sum(value is not None for value in claims),1)
        self.assertIsNone(self.store.claim('other'))
        with self.assertRaises(ValueError):self.store.request(key)

    def test_revision_and_running_sequence_cannot_be_overwritten(self):
        key=self.save()
        self.store.save('Changed',self.rule,[command()],job_id=key,revision=1)
        with self.assertRaisesRegex(ValueError,'другом окне'):
            self.store.save('Stale',self.rule,[command()],job_id=key,revision=1)
        run=self.claim(key)
        with self.assertRaisesRegex(ValueError,'выполняемую'):
            self.store.save('Busy',self.rule,[command()],job_id=key,revision=2)
        with self.assertRaises(ValueError):self.store.delete(key)

    def test_expired_once_enable_reports_saved_time_and_leaves_job_disabled(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            key=self.store.save('Once',dict(kind='once',start='2026-10-19 19:55'),[command()],enabled=False)
            with self.assertRaises(ValueError) as error:self.store.enable(key,True,revision=1)
        self.assertIn('2026-10-19 19:55',str(error.exception))
        self.assertIn('2026-10-20 14:50',str(error.exception))
        self.assertIn('Сохранить расписание',str(error.exception))
        row=self.store.load(key)
        self.assertFalse(row['enabled']);self.assertEqual(row['revision'],1)

    def test_enable_updates_revision_atomically_and_preserves_newer_schedule(self):
        key=self.save(enabled=False)
        state=self.store.enable(key,True,revision=1)
        self.assertTrue(state['enabled']);self.assertEqual(state['revision'],2)
        self.assertEqual(state['next_at'],self.store.load(key)['next_at'])
        self.store.save('Changed',self.rule,[command()],enabled=True,job_id=key,revision=2)
        with self.assertRaisesRegex(ValueError,'другом окне'):
            self.store.enable(key,False,revision=2)
        self.assertTrue(self.store.load(key)['enabled'])
        self.assertEqual(self.store.load(key)['revision'],3)

    def test_disabling_expired_once_does_not_require_a_new_date(self):
        key=self.save()
        with self.store.db() as db:
            db.execute('UPDATE jobs SET rule=? WHERE id=?',(json.dumps(dict(kind='once',start='2000-01-01 00:00')),key))
        state=self.store.enable(key,False,revision=1)
        self.assertFalse(state['enabled']);self.assertIsNone(state['next_at'])

    def test_missed_schedule_skipped_or_claimed_once_by_policy(self):
        key=self.save()
        with self.store.db() as db:db.execute('UPDATE jobs SET next_at=? WHERE id=?',(time.time()-900,key))
        self.assertIsNone(self.store.claim('worker'))
        self.assertEqual(self.store.history()[0][0]['status'],'Пропущено')
        self.assertGreater(self.store.jobs()[0]['next_at'],time.time())
        with self.store.db() as db:db.execute('UPDATE jobs SET catch_up=1,next_at=? WHERE id=?',(time.time()-900,key))
        self.assertIsNotNone(self.store.claim('worker'))
        self.assertIsNone(self.store.claim('second'))

    def test_manual_run_does_not_change_future_schedule(self):
        key=self.save(enabled=False);before=self.store.jobs()[0]['next_at']
        run=self.claim(key);run_chain(self.store,run,lambda step,event:{'ok':True})
        row=self.store.jobs()[0]
        self.assertEqual(before,row['next_at']);self.assertFalse(row['enabled'])

    def test_chain_is_sequential_and_previous_artifact_can_feed_next_step(self):
        first=command(title='Backup');second=command(title='Restore')
        second['parameters']['path']='${previous.backup_path}'
        key=self.save([first,second]);run=self.claim(key);seen=[]
        def execute(step,event):
            seen.append(step)
            event(dict(kind='message',text='Работа'))
            event(dict(kind='artifact',path='fixture.dump',size=123))
            return ['fixture.dump'] if len(seen)==1 else {'restored':True}
        self.assertEqual(run_chain(self.store,run,execute),'Успешно')
        self.assertEqual(seen[1]['parameters']['path'],'fixture.dump')
        runs,steps=self.store.history()
        self.assertEqual([s['status'] for s in steps],['Успешно','Успешно'])
        self.assertLessEqual(steps[0]['finished'],steps[1]['started'])
        self.assertEqual(self.store.result(run['id'],1)['artifacts'][0]['size'],123)
        self.assertIsNotNone(runs[0]['finished'])

    def test_failure_stops_following_steps_and_never_retries(self):
        key=self.save([command(title='One'),command(title='Two')]);run=self.claim(key)
        execute=Mock(side_effect=RuntimeError('fixture failure'))
        self.assertEqual(run_chain(self.store,run,execute),'Ошибка')
        execute.assert_called_once()
        runs,steps=self.store.history()
        self.assertEqual([row['status'] for row in steps],['Ошибка','Пропущено'])
        self.assertEqual(runs[0]['errors'],1)
        self.assertIsNone(self.store.claim('next'))

    def fallback_chain(self, failure=''):
        fixtures=cluster_fixtures
        step=dict(action='cluster_infobase',title='Добавление ИБ в кластер',parameters=dict(
            target=fixtures.TARGET,cluster=fixtures.CLUSTER,local=True,
            values=dict(fixtures.PARAMS,**{'scheduled-jobs-deny':'on'})))
        key=self.save([command(title='Backup'),step,command(title='Next')])
        run=self.claim(key);seen=[]
        manager=Mock(stop=threading.Event())
        manager.connect.side_effect=lambda *args:nullcontext('127.0.0.1:23456')
        candidates=[('new/rac.exe','new/ras.exe','8.5.1.1529'),('local/rac.exe','local/ras.exe','8.3.27.1859')]
        calls=[]
        def rac(args,*other,**kwargs):
            calls.append(args)
            if args[0]=='new/rac.exe' or failure=='all':
                raise RuntimeError('rac: код 4294967295. Ошибка установки соединения с кластером серверов\n'
                                   'Различаются версии клиента и сервера (8.5.1.1529 - 8.3.27.1859)')
            if args[1:3]==['cluster','list']:return fixtures.CLUSTERS
            if args[1]=='help':return fixtures.HELP
            if args[1:3]==['infobase','summary']:return ''
            if args[1:3]==['infobase','create']:
                if failure=='create':raise RuntimeError('Ошибка создания ИБ: недостаточно прав')
                return fixtures.CREATED
            if args[1:3]==['infobase','info']:return fixtures.CREATED+'scheduled-jobs-deny : on\n'
            raise AssertionError(args)
        def dispatch(current,event):
            seen.append(current['title'])
            return execute_step(current,event) if current['action']=='cluster_infobase' else {}
        with (patch('console_core.find_all_rac',return_value={}),
              patch('add_cluster_infobase.local_tool_candidates',return_value=candidates),
              patch('add_cluster_infobase.LocalRasManager',return_value=manager),
              patch('ras_tools.use_tools',side_effect=lambda *args:nullcontext()),
              patch('add_cluster_infobase.run_rac',side_effect=rac)):
            status=run_chain(self.store,run,dispatch)
        return status,self.store.history(),self.store.journal(run['id']),seen,calls

    def test_successful_ras_fallback_warns_without_failing_created_base_or_next_step(self):
        status,(runs,steps),log,seen,calls=self.fallback_chain()
        self.assertEqual(status,'Успешно')
        self.assertEqual([row['status'] for row in steps],['Успешно']*3)
        self.assertEqual(runs[0]['errors'],0);self.assertGreater(runs[0]['warnings'],0)
        diagnostics=[row for row in log if 'Ошибка установки соединения' in row['text'] or 'Различаются версии' in row['text']]
        self.assertEqual(len(diagnostics),2)
        self.assertTrue(all(row['level']=='warning' for row in diagnostics))
        self.assertEqual(seen[-1],'Next')
        self.assertEqual(sum(args[1:3]==['infobase','create'] for args in calls),1)

    def test_exhausted_ras_candidates_still_fail_and_skip_next_step(self):
        status,(runs,steps),log,seen,calls=self.fallback_chain('all')
        self.assertEqual(status,'Ошибка');self.assertGreater(runs[0]['errors'],0)
        self.assertEqual([row['status'] for row in steps],['Успешно','Ошибка','Пропущено'])
        self.assertNotIn('Next',seen)
        self.assertFalse(any(args[1:3]==['infobase','create'] for args in calls))

    def test_real_create_failure_after_fallback_stops_chain_and_is_not_retried(self):
        status,(runs,steps),log,seen,calls=self.fallback_chain('create')
        self.assertEqual(status,'Ошибка');self.assertGreater(runs[0]['errors'],0)
        self.assertNotIn('Next',seen)
        self.assertEqual(sum(args[1:3]==['infobase','create'] for args in calls),1)

    def test_cancel_before_start_does_not_execute_step(self):
        key=self.save();run=self.claim(key);self.store.cancel(run['id'])
        execute=Mock()
        self.assertEqual(run_chain(self.store,run,execute),'Прервано');execute.assert_not_called()

    def test_cancel_reaches_running_backend_and_skips_next(self):
        key=self.save([command(),command()]);run=self.claim(key)
        def execute(step,event):
            self.store.cancel(run['id'])
            self.assertTrue(event.cancel_event.wait(3))
            raise OperationCancelled('fixture cancellation')
        self.assertEqual(run_chain(self.store,run,execute),'Прервано')
        self.assertEqual(self.store.history()[1][1]['status'],'Пропущено')

    def test_broken_worker_marks_unfinished_disables_job_and_keeps_history(self):
        key=self.save();run=self.claim(key);self.store.add_steps(run['id'],[command()]);self.store.step_state(run['id'],1,'Выполняется')
        self.store.interrupted()
        self.assertEqual(self.store.jobs()[0]['status'],'Не завершено')
        self.assertFalse(self.store.jobs()[0]['enabled'])
        self.assertEqual(self.store.history()[1][0]['status'],'Не завершено')
        self.store.delete(key)
        self.assertEqual(len(self.store.history()[0]),1)

    def test_logs_redact_nested_credentials_and_results_remain_encrypted(self):
        step=command();step['parameters'].update(password='very-private',target={'db-pwd':'nested-private'})
        key=self.save([step]);run=self.claim(key)
        def execute(step,event):
            event(dict(kind='warning',text='Warning very-private nested-private'))
            return {'account_rows':[{'password':'generated-private'}]}
        run_chain(self.store,run,execute)
        log='\n'.join(row['text'] for row in self.store.journal(run['id']))
        self.assertNotIn('very-private',log);self.assertNotIn('nested-private',log)
        self.assertEqual(self.store.history()[0][0]['warnings'],1)
        self.assertEqual(self.store.result(run['id'],1)['result']['account_rows'][0]['password'],'generated-private')
        for file in self.path.parent.glob('*'):
            if file.is_file():self.assertNotIn(b'generated-private',file.read_bytes())

    def test_cross_session_lock_prevents_two_executors(self):
        with executor_lock(self.path) as first:
            self.assertTrue(first)
            with executor_lock(self.path) as second:self.assertFalse(second)
        with executor_lock(self.path) as third:self.assertTrue(third)


class BackendTests(unittest.TestCase):
    def test_windows_registration_uses_password_logon_without_password_in_arguments(self):
        execute=Mock(return_value={'installed':True})
        with patch('scheduler_windows.current_user',return_value='DOMAIN\\operator'):
            result=control('register',Path('fixture.sqlite3'),'private-password',execute)
        self.assertTrue(result['installed'])
        code,data,event=execute.call_args.args
        self.assertIn('LogonType=1',code)
        self.assertIn('ExecutionTimeLimit=\'PT0S\'',code)
        self.assertEqual(data['password'],'private-password')
        self.assertNotIn('private-password',data['arguments'])
        self.assertIn('--scheduler-worker',data['arguments'])
        self.assertEqual(execute.call_args.kwargs['secrets'],('private-password',))

    def test_frozen_paths_rebased_and_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as first,tempfile.TemporaryDirectory() as second:
            value={'pg_dump':str(Path(first)/'vendor'/'postgresql'/'14'/'bin'/'pg_dump.exe')}
            with patch('sys._MEIPASS',first,create=True):packed=bundle_paths(value)
            with patch('sys._MEIPASS',second,create=True):restored=bundle_paths(packed,True)
            self.assertEqual(restored['pg_dump'],str(Path(second)/'vendor'/'postgresql'/'14'/'bin'/'pg_dump.exe'))
            with self.assertRaises(ValueError):bundle_paths('@ka-bundle/../escape',True)
            self.assertEqual(bundle_paths({'password':'@ka-bundle/private'},True)['password'],'@ka-bundle/private')

    def test_pg_and_sql_copy_use_existing_backends(self):
        event=Mock();event.cancel_event=threading.Event()
        pg=dict(action='pg_copy',title='Copy',parameters=dict(connection=dict(host='DB01',username='pg',port='5432'),source_database='one',target_database='two',path='C:/fixture'))
        with patch('postgres_jobs.run_job',return_value={'backup_path':'fixture'}) as run:
            execute_step(pg,event)
            self.assertEqual(run.call_args.args[0]['mode'],'copy')
        sql=dict(action='db_copy',title='Copy',parameters=dict(connection={'server':'SQL'},source='one',destination='two',path='D:\\Backup',compression=True,copy_only=True))
        with patch('sql_server.backup_and_restore') as run:
            execute_step(sql,event)
            self.assertEqual(run.call_args.args[:4],({'server':'SQL'},'one','two','D:\\Backup'))

    def test_legacy_extraction_paths_resolve_for_source_target_and_archive_reader(self):
        stale = r'C:\Users\operator\AppData\Local\Temp\_MEI00004d142\vendor\postgresql\14\bin\psql.exe'
        event = Mock(); event.cancel_event = threading.Event()
        connection = dict(host='SOURCE', username='pg', psql=stale,
                          restore_candidates=[stale.replace('psql.exe', 'pg_restore.exe')],
                          password=stale, target_connection=dict(host='TARGET', username='pg', psql=stale))
        step = dict(action='pg_copy', title='Copy', parameters=dict(connection=connection,
                    source_database='one', target_database='two', path='C:/backups'))
        with tempfile.TemporaryDirectory() as current, patch('sys._MEIPASS', current, create=True):
            expected = str(Path(current) / 'vendor/postgresql/14/bin/psql.exe')
            with patch('postgres_jobs.run_job', return_value={}) as run:
                execute_step(step, event)
            actual = run.call_args.args[0]['connection']
            self.assertEqual(actual['psql'], expected)
            self.assertEqual(actual['target_connection']['psql'], expected)
            self.assertEqual(actual['restore_candidates'], [str(Path(expected).with_name('pg_restore.exe'))])
            self.assertEqual(actual['password'], stale)
            self.assertEqual(step['parameters']['connection']['psql'], stale)
            custom = r'C:\Tools\vendor\postgresql\14\bin\psql.exe'
            self.assertEqual(bundle_paths(custom, True), custom)
            self.assertEqual(bundle_paths('/usr/bin/psql', True), '/usr/bin/psql')
            with self.assertRaises(ValueError):
                bundle_paths(stale.replace('bin\\psql.exe', '..\\..\\..\\escape.exe'), True)

    def test_installation_downloads_selected_distribution_and_cleans_own_workspace(self):
        event=Mock();event.cancel_event=threading.Event()
        step=dict(action='platform_install',title='Install',parameters=dict(distribution={'version':'8.3.27.2342'},
            settings={'features':['CSClient']},folder=str(Path.cwd()/'build'),its_user='fixture',its_password='private',linux=False))
        workspace={'root':'fixture-root'}
        with patch('its_portal.Portal') as portal,patch('install_workspace.create_workspace',return_value=workspace),\
             patch('install_workspace.cleanup_workspace') as cleanup,patch('platform_install.extract_distribution',return_value='fixture.msi'),\
             patch('platform_install.Msi') as msi,patch('platform_install.install_properties'),patch('platform_install.install_msi',return_value={'reboot':False}) as install:
            result=execute_step(step,event)
        portal.return_value.login.assert_called_once_with('fixture','private')
        portal.return_value.download.assert_called_once()
        cleanup.assert_called_once_with(workspace);install.assert_called_once()
        self.assertFalse(result['reboot'])

    def test_task_script_parses_in_real_powershell_without_executing_registration(self):
        if sys.platform!='win32':self.skipTest('Windows')
        from scheduler_windows import TASK_SCRIPT
        from account_creation import powershell_path
        code="""$code=[Console]::In.ReadToEnd();$t=$null;$e=$null;
        $null=[System.Management.Automation.Language.Parser]::ParseInput($code,[ref]$t,[ref]$e);
        if ($e.Count) { $e | ForEach-Object { [Console]::Error.WriteLine($_.Message) };exit 1 }"""
        import base64
        result=subprocess.run([powershell_path(),'-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(code.encode('utf-16le')).decode()],
            input=TASK_SCRIPT.encode('utf-8'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))

    def test_real_headless_entry_runs_two_read_only_steps_without_constructing_tk(self):
        if sys.platform!='win32':self.skipTest('Windows')
        test_activity.mock_dpapi(self)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'scheduler.sqlite3';store=SchedulerStore(path);store.initialize()
            future=(datetime.now()+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
            key=store.save('Headless',dict(kind='once',start=future),[command('Write-Output "headless-fixture-one"'),command('Write-Output "headless-fixture-two"')])
            store.request(key)
            entry=Path(__file__).resolve().parents[1]/'1C Console.py'
            bootstrap="""import runpy,sys,tkinter,favorite_secrets
favorite_secrets._crypt=lambda data,decrypt=False:bytes(value^0xa7 for value in data)
def forbidden(*args,**kwargs):raise AssertionError('Headless worker created Tk')
tkinter.Tk=forbidden
sys.argv=[sys.argv[1],'--scheduler-worker',sys.argv[2]]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
            process=subprocess.Popen([sys.executable,'-X','utf8','-c',bootstrap,str(entry),str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    runs,steps=store.history()
                    if runs and runs[0]['finished']:break
                    if process.poll() is not None:break
                    time.sleep(.1)
                self.assertTrue(runs and runs[0]['finished'])
                self.assertEqual(runs[0]['status'],'Успешно')
                self.assertEqual([row['status'] for row in steps],['Успешно','Успешно'])
                self.assertIn('headless-fixture-two','\n'.join(row['text'] for row in store.journal(runs[0]['id'])))
                store.meta('stop',True)
                output,error=process.communicate(timeout=8)
                self.assertEqual(process.returncode,0,error.decode(errors='replace'))
            finally:
                if process.poll() is None:process.terminate()
                process.communicate(timeout=8)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self)
        test_activity.mock_dpapi(self)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.panel=self.app.scheduler
        self.panel.store=SchedulerStore(Path(temp.name)/'scheduler.sqlite3')
        self.addCleanup(lambda:self.pump(lambda:not self.panel._reading))

    def pump(self,predicate):
        deadline=time.monotonic()+5
        while not predicate() and time.monotonic()<deadline:
            self.app.root.update();time.sleep(.01)
        self.assertTrue(predicate())

    def open_once(self,start='2026-10-19 19:55'):
        self.app._switch_mode('scheduler')
        self.panel.ensure_store()
        key=self.panel.store.save('Once',dict(kind='once',start=start),[command()],enabled=False)
        self.panel.refresh();self.pump(lambda:self.panel.jobs_tree.exists(key))
        self.panel.jobs_tree.selection_set(key)
        self.panel.open_selected();self.pump(lambda:self.panel.job_id==key)
        return key

    def test_enable_offers_to_save_future_time_from_editor_instead_of_using_expired_rule(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            key=self.open_once()
            self.panel.date.set('2026-10-20');self.panel.clock.set('14:55')
            with patch('scheduler_ui.messagebox.askyesno',return_value=True) as confirm,patch('scheduler_ui.control') as control_mock:
                self.panel.toggle();self.pump(lambda:not self.panel._saving)
            row=self.panel.store.load(key)
        self.assertTrue(row['enabled']);self.assertEqual(row['rule']['start'],'2026-10-20 14:55')
        self.assertEqual(row['next_at'],datetime(2026,10,20,14,55).timestamp())
        self.assertTrue(self.panel.enabled.get());self.assertFalse(self.panel.dirty)
        self.assertEqual(confirm.call_args_list[0].args[0],'Сохранить изменения и включить?')
        control_mock.assert_not_called()
        self.assertEqual(self.panel.store.history(),([],[]))

    def test_cancel_save_and_enable_keeps_changes_in_editor_and_old_job_disabled(self):
        key=self.open_once()
        self.panel.date.set('2026-10-20');self.panel.clock.set('14:55')
        with patch('scheduler_ui.messagebox.askyesno',return_value=False):self.panel.toggle()
        row=self.panel.store.load(key)
        self.assertFalse(row['enabled']);self.assertEqual(row['rule']['start'],'2026-10-19 19:55')
        self.assertTrue(self.panel.dirty);self.assertEqual(self.panel.clock.get(),'14:55')
        self.assertFalse(self.panel._saving)

    def test_clean_toggle_synchronizes_enabled_and_revision_then_editor_can_be_saved(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            key=self.open_once('2026-10-20 14:55')
            self.panel.toggle();self.pump(lambda:not self.panel._saving)
            self.assertTrue(self.panel.enabled.get());self.assertEqual(self.panel.revision,2)
            self.assertFalse(self.panel.dirty)
            self.panel.name.set('Renamed')
            with patch('scheduler_ui.messagebox.askyesno',return_value=True):
                self.panel.save();self.pump(lambda:not self.panel._saving)
            self.assertEqual(self.panel.store.load(key)['name'],'Renamed')
            self.assertEqual(self.panel.revision,3)

    def test_repeated_toggle_does_not_wait_for_the_periodic_list_refresh(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            key=self.open_once('2026-10-20 14:55')
            self.pump(lambda:not self.panel._reading)
            with patch.object(self.panel,'refresh'):
                self.panel.toggle();self.pump(lambda:not self.panel._saving)
                self.assertTrue(self.panel.enabled.get())
                self.panel.toggle();self.pump(lambda:not self.panel._saving)
            self.assertFalse(self.panel.store.load(key)['enabled'])
            self.assertEqual(self.panel.revision,3)
            self.assertFalse(self.panel.dirty)

    def test_enable_save_failure_preserves_disabled_state_and_unsaved_fields(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            key=self.open_once()
            self.panel.date.set('2026-10-20');self.panel.clock.set('14:55')
            with patch('scheduler_ui.messagebox.askyesno',return_value=True),patch.object(self.panel.store,'save',side_effect=ValueError('fixture write failure')):
                self.panel.toggle();self.pump(lambda:not self.panel._saving)
        self.assertFalse(self.panel.store.load(key)['enabled'])
        self.assertFalse(self.panel.enabled.get());self.assertTrue(self.panel.dirty)
        self.assertEqual(self.panel.clock.get(),'14:55')
        self.assertIn('fixture write failure',self.panel.notice.cget('text'))

    def test_expired_time_cannot_be_enabled_but_can_be_saved_disabled(self):
        now=datetime(2026,10,20,14,50).timestamp()
        with patch('scheduler_store.time.time',return_value=now):
            self.open_once()
            self.panel.enabled.set(True)
            with patch('scheduler_ui.messagebox.askyesno') as confirm:self.panel.save()
            confirm.assert_not_called();self.assertFalse(self.panel._saving)
            self.assertIn('2026-10-19 19:55',self.panel.notice.cget('text'))
            self.panel.enabled.set(False)
            with patch('scheduler_ui.messagebox.askyesno',return_value=True):
                self.panel.save();self.pump(lambda:not self.panel._saving)
            self.assertFalse(self.panel.dirty)

    def test_captures_copy_and_cluster_in_order_without_running_anything(self):
        self.app._switch_mode('pg_copy');pg=self.app.postgres_copy
        pg.host.set('source');pg.target_host.set('target');pg.path.set('C:/fixture')
        source=pg.tree.insert('','end',values=('production','',''));pg.tree.selection_set(source)
        target=pg.target_tree.insert('','end',values=('testing','',''));pg.target_tree.selection_set(target)
        with patch('postgres_jobs.run_job') as run:
            self.panel.queue_step(pg)
        run.assert_not_called();self.assertEqual(self.app.mode,'scheduler')
        self.assertEqual(self.panel.steps[0]['parameters']['target_database'],'testing')
        pg.host.set('changed')
        self.assertEqual(self.panel.steps[0]['parameters']['connection']['host'],'source')
        cluster=self.app.cluster_infobase
        cluster.targets=[dict(server='APP',port='1540',version='8.3.27.1859')]
        cluster.server_combo.configure(values=['APP']);cluster.server_combo.current(0)
        cluster.render_clusters([dict(cluster='0fa984b9-b902-4142-b751-d323e5ee52c4',name='Cluster')])
        cluster.name.set('testing');cluster.db_server.set('target');cluster.db_name.set('testing');cluster.db_user.set('pg')
        self.panel.queue_step(cluster,'cluster_infobase')
        self.assertEqual([s['action'] for s in self.panel.steps],['pg_copy','cluster_infobase'])
        self.panel.steps_tree.selection_set('1');self.panel.move(-1)
        self.assertEqual(self.panel.steps[0]['action'],'cluster_infobase')

    def test_save_never_registers_or_runs_tasks_and_history_contains_days_steps(self):
        self.app._switch_mode('scheduler');self.panel.steps=[command()];self.panel.render_steps()
        with patch('scheduler_ui.messagebox.askyesno',return_value=True),patch('scheduler_ui.control') as control_mock:
            self.panel.save();self.pump(lambda:self.panel.job_id is not None)
        control_mock.assert_not_called()
        self.pump(lambda:bool(self.panel.jobs_tree.get_children()))
        run=self.panel.store.request(self.panel.job_id)
        claimed=self.panel.store.claim('test');run_chain(self.panel.store,claimed,lambda step,event:{'ok':True})
        self.panel.refresh();self.pump(lambda:self.panel.history_tree.exists(claimed['id']+':1'))
        parent=self.panel.history_tree.parent(claimed['id'])
        self.assertTrue(parent.startswith('day:'))
        self.assertEqual(self.panel.history_tree.set(claimed['id'],'status'),'Успешно')

    def test_scheduler_does_not_block_other_tabs_or_mark_gui_busy(self):
        self.app._switch_mode('scheduler')
        self.assertFalse(self.panel.running)
        self.app._switch_mode('pg_dump');self.assertEqual(self.app.mode,'pg_dump')
        self.assertIsNotNone(self.app.postgres._scheduler_button)
        self.app._switch_mode('service_create');self.assertIsNotNone(self.app.services._scheduler_button)

    def test_create_database_steps_capture_target_credentials_without_creating_database(self):
        self.app._switch_mode('pg_copy');pg=self.app.postgres_copy
        pg.host.set('SOURCE');pg.target_host.set('TARGET');pg.target_user.set('target-user');pg.target_password.set('fixture-target')
        with patch('database_dialog.ask_create_database',return_value={'name':'new_test','owner':'target-user'}),patch('postgres_jobs.run_job') as run:
            self.panel.database_action(pg,'pg_create','target')
        run.assert_not_called()
        step=self.panel.steps[-1]
        self.assertEqual(step['parameters']['connection']['host'],'TARGET')
        self.assertEqual(step['parameters']['connection']['password'],'fixture-target')
        self.assertEqual(step['parameters']['database'],'new_test')

    def test_green_scheduler_task_does_not_mark_any_manual_panel_busy(self):
        self.panel.monitored('Night refresh')
        self.assertIn('scheduler-background',self.app.running_tasks.entries)
        self.assertFalse(self.panel.running)
        self.app._switch_mode('pg_copy')
        self.assertFalse(self.app.postgres_copy.running)
        self.panel.monitored('')
        self.assertNotIn('scheduler-background',self.app.running_tasks.entries)

    def test_scheduler_footer_buttons_stay_inside_small_window(self):
        self.app._switch_mode('scheduler');self.app.root.deiconify();self.app.root.geometry('960x540+20+20');self.app.root.update()
        footer=self.panel.notice.master
        window_bottom=self.app.root.winfo_rooty()+self.app.root.winfo_height()
        self.assertLessEqual(footer.winfo_rooty()+footer.winfo_height(),window_bottom)
        for child in footer.winfo_children():
            self.assertGreater(child.winfo_height(),10)
            self.assertLessEqual(child.winfo_rooty()+child.winfo_height(),window_bottom)

    def test_save_failure_unlocks_editor_and_duplicate_click_does_not_create_two_jobs(self):
        self.app._switch_mode('scheduler');self.panel.steps=[command()];self.panel.render_steps()
        with patch('scheduler_ui.messagebox.askyesno',return_value=True):
            self.panel.save();self.panel.save()
            self.pump(lambda:self.panel.job_id is not None)
        self.assertEqual(len(self.panel.store.jobs()),1)
        self.panel.name.set('Changed')
        with patch('scheduler_ui.messagebox.askyesno',return_value=True),patch.object(self.panel.store,'save',side_effect=ValueError('fixture storage error')):
            self.panel.save();self.pump(lambda:not self.panel._saving)
        self.assertTrue(self.panel.dirty)
        self.assertIn('fixture storage error',self.panel.notice.cget('text'))


if __name__=='__main__':unittest.main()
