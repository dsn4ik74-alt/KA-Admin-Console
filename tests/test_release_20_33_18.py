"""Restore approval order, verification policy, receiver logs and independent jobs."""
from datetime import datetime,timedelta
from pathlib import Path
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

import sql_server as sql
from operation_logs import OperationLogs,RETENTION
from activity_store import ActivityStore
from scheduler_actions import capture,execute_step
from scheduler_store import SchedulerStore
from postgres_runner import OperationCancelled
import test_service_ui
import test_database_copy
import test_administration_ui
from test_sql_server import CONNECTION,HEADER,FILES,TARGET


class RestoreBackendTests(unittest.TestCase):
    def test_chain_verifies_once_or_not_at_all_but_always_inspects_metadata(self):
        for verify in (True,False):
            responses=[[test_database_copy.ROWS],[[dict(source_id=5,target_id=6)]],
                       [[],[dict(CompressedBackupSize=123)]],[[HEADER],TARGET,[]],[FILES,[]],[]]
            with self.subTest(verify=verify),patch('sql_server.query',side_effect=responses) as query:
                result=sql.backup_and_restore(CONNECTION,'Original','Target',r'D:\Backup',verify=verify)
            commands=[item['sql'] for call in query.call_args_list for item in call.args[1]]
            self.assertEqual(sum('RESTORE VERIFYONLY' in command for command in commands),int(verify))
            self.assertTrue(any('FILELISTONLY' in command for command in commands))
            restore=next(command for command in commands if 'RESTORE DATABASE' in command)
            self.assertIn(sql.literal(result['backup_path']),restore)
            self.assertIn('RESTORE DATABASE [Target]',restore)
            self.assertNotIn('ALTER DATABASE [Original]',restore)

    def test_receiver_restored_then_simple_shrink_full_and_never_shrinks_data(self):
        files=FILES+[dict(LogicalName='OtherLog',Type='L',FileID=3)]
        plan=sql.restore_plan('Target',r'D:\Full.bak',HEADER,files,TARGET)
        command=plan.sql
        order=[command.index(fragment) for fragment in ('RESTORE DATABASE [Target]','SET RECOVERY SIMPLE',
            "DBCC SHRINKFILE (N'Target_log', 512)", 'SET RECOVERY FULL','SET MULTI_USER')]
        self.assertEqual(order,sorted(order))
        self.assertIn("DBCC SHRINKFILE (N'Target_log2', 512)",command)
        self.assertNotIn("DBCC SHRINKFILE (N'Target',",command)
        self.assertIn('CHECKPOINT',command)
        self.assertIn('@ConsoleLogMB>1024',command)
        self.assertIn('@ConsoleChangedRecovery=1',command)

    def test_failed_verify_does_not_restore_receiver(self):
        responses=[[test_database_copy.ROWS],[[dict(source_id=5,target_id=6)]],[],
                   [[HEADER],TARGET,[]],RuntimeError('verification failed')]
        with patch('sql_server.query',side_effect=responses) as query,self.assertRaisesRegex(RuntimeError,'Бэкап сохранён'):
            sql.backup_and_restore(CONNECTION,'Original','Target',r'D:\Backup')
        self.assertFalse(any('RESTORE DATABASE' in str(call.args[1]) for call in query.call_args_list))

    def test_cancel_after_restore_recovers_full_only_for_receiver(self):
        callback=Mock()
        callback.cancel_event=threading.Event()
        emit,state=sql.restore_events(callback)
        self.assertIs(emit.cancel_event,callback.cancel_event)
        self.assertNotIn('RECOVERY FULL',sql.restore_cleanup('Target',state['restored'],6))
        emit(dict(kind='message',text=sql.RESTORE_LOG_STAGE))
        cleanup=sql.restore_cleanup('Target',state['restored'],6)
        self.assertIn('ALTER DATABASE [Target] SET RECOVERY FULL',cleanup)
        self.assertIn('database_id=6',cleanup)
        self.assertNotIn('Original',cleanup)


class RestoreUiTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp
    load_database=test_administration_ui.AdministrationUiTests.load_database

    def test_confirmation_precedes_io_and_checked_choice_is_snapshotted_without_second_prompt(self):
        panel=self.load_database()
        self.app._switch_mode('db_restore')
        panel.server_path.set(r'D:\Full.bak')
        panel.verify_restore.set(False)
        plan=sql.restore_plan('Target',r'D:\Full.bak',HEADER,FILES,TARGET)
        sequence=[]
        with patch('administration_ui.messagebox.askyesno',side_effect=lambda *a,**k:sequence.append('confirm') or True) as ask, \
             patch('administration_ui.inspect_restore',side_effect=lambda *a,**k:sequence.append(('inspect',k['verify'])) or plan), \
             patch.object(panel,'work') as work,patch('administration_ui.query',return_value=[]) as query:
            panel.execute()
            self.assertEqual(sequence,['confirm'])
            prepare,prepared=work.call_args.args
            panel.verify_restore.set(True)
            self.assertIs(prepare(Mock()),plan)
            self.app._switch_mode('accounts')
            prepared(plan)
            restore,done=work.call_args.args
            restore(Mock());done([])
            ask.assert_called_once()
            self.assertEqual(sequence,['confirm',('inspect',False)])
            self.assertIsNone(panel._pending_review)
            query.assert_called_once()
        self.assertFalse(panel.running)

    def test_scheduler_captures_verify_and_old_jobs_default_to_verification(self):
        panel=self.load_database()
        self.app._switch_mode('db_restore');panel.server_path.set(r'D:\Full.bak')
        panel.verify_restore.set(False)
        step=capture(panel)
        self.assertFalse(step['parameters']['verify'])
        plan=sql.restore_plan('Target',r'D:\Full.bak',HEADER,FILES,TARGET)
        for value in (False,True):
            if value:step['parameters'].pop('verify',None)
            with patch('sql_server.inspect_restore',return_value=plan) as inspect,patch('sql_server.query'):
                event=Mock();event.cancel_event=threading.Event()
                execute_step(step,event)
            self.assertEqual(inspect.call_args.kwargs['verify'],value)

    def test_home_opens_colored_journal_and_clear_preserves_active_task(self):
        store=self.app.activity
        token=store.begin(dict(operation='Completed',server='SQL',database='Target'))
        writer=store.logs.writer(token[0],['private-password'])
        writer.write('INFO private-password\nПРЕДУПРЕЖДЕНИЕ: fixture\nОШИБКА: fixture')
        writer.close();store.end(token,'Ошибка')
        active=store.begin(dict(operation='Still running'))
        active_writer=store.logs.writer(active[0]);active_writer.write('Still running')
        self.app._switch_mode('home')
        home=self.app.home
        row=next(str(index) for index,item in enumerate(home.rows['history']) if item['id']==token[0])
        home.tables['history'].selection_set(row)
        window=home.open_log()
        deadline=time.monotonic()+3
        while window.loading and time.monotonic()<deadline:self.app.root.update();time.sleep(.01)
        text=window.output.get('1.0','end-1c')
        self.assertNotIn('private-password',text)
        self.assertIn('fixture',text)
        self.assertTrue(window.output.tag_ranges('warning'))
        self.assertTrue(window.output.tag_ranges('error'))
        window.destroy()
        with patch('activity_ui.messagebox.askyesno',return_value=True):home.clear_history()
        deadline=time.monotonic()+3
        while len(store.snapshot('history'))>1 and time.monotonic()<deadline:self.app.root.update();time.sleep(.01)
        self.assertEqual([row['id'] for row in store.snapshot('history')],[active[0]])
        self.assertIn('Still running',store.logs.read(active[0])[0])
        active_writer.close();store.end(active,'Успешно')


class ParallelCopyTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp
    sql_panel=test_database_copy.CopyUiTests.sql_panel
    pg_panel=test_database_copy.CopyUiTests.pg_panel

    def exercise(self,mode):
        first=self.sql_panel() if mode=='db_copy' else self.pg_panel()
        entered=[threading.Event(),threading.Event()]
        released=[threading.Event(),threading.Event()]
        calls=[]
        def operation(*args,**kwargs):
            number=len(calls);calls.append(args)
            event=args[4] if mode=='db_copy' else args[1]
            event(dict(kind='message',text='independent-job-'+str(number)))
            entered[number].set()
            if not released[number].wait(5):raise RuntimeError('Fixture timeout')
            return dict(backup_path='fixture-'+str(number)+'.bak')
        backend='backup_and_restore' if mode=='db_copy' else 'run_job'
        second=None
        try:
            with patch('database_copy_ui.messagebox.askyesno',return_value=True),patch('database_copy_ui.'+backend,side_effect=operation):
                first.execute()
                self.assertTrue(entered[0].wait(1))
                self.assertEqual(first.new_task_button.cget('state'),'normal')
                first.new_task_button.invoke()
                second=self.app._current_panel()
                self.assertIsNot(first,second)
                self.assertIsNot(first.cancel_event,second.cancel_event)
                if mode=='db_copy':
                    second.server.set('OTHER-SQL')
                    second.loaded(second.connection(),([dict(name=name,recovery_model_desc='FULL') for name in ('Source2','Target2')],{}))
                    second.tree.selection_set(second.tree.get_children()[0]);second.destination.set('Target2')
                    second.server_path.set(r'D:\OtherBackup')
                else:
                    second.host.set('source2');second.target_host.set('target2')
                    second.loaded(second.connection_key(),[dict(name='Source2',owner='fixture')])
                    second.target_loaded(second.target_key(),[dict(name='Target2',owner='fixture')])
                    second.tree.selection_set(second.tree.get_children()[0]);second.target_tree.selection_set(second.target_tree.get_children()[0])
                    second.path.set('C:/OtherBackup')
                second.execute()
                self.assertTrue(entered[1].wait(1))
                self.assertTrue(first.running and second.running)
                released[0].set()
                deadline=time.monotonic()+2
                while first.running and time.monotonic()<deadline:self.app.root.update();time.sleep(.01)
                self.assertFalse(first.running);self.assertTrue(second.running)
                released[1].set()
                deadline=time.monotonic()+2
                while second.running and time.monotonic()<deadline:self.app.root.update();time.sleep(.01)
                self.assertFalse(second.running)
                logs=[self.app.activity.logs.read(row['id'])[0] for row in self.app.activity.snapshot('history')]
                self.assertTrue(any('independent-job-0' in text and 'independent-job-1' not in text for text in logs))
                self.assertTrue(any('independent-job-1' in text and 'independent-job-0' not in text for text in logs))
        finally:
            for signal in released:signal.set()
            deadline=time.monotonic()+2
            while any(panel and panel.running for panel in (first,second)) and time.monotonic()<deadline:self.app.root.update();time.sleep(.01)

    def test_two_mssql_copy_tabs_run_at_the_same_time(self):self.exercise('db_copy')
    def test_two_postgres_copy_tabs_run_at_the_same_time(self):self.exercise('pg_copy')


class JournalStorageTests(unittest.TestCase):
    def test_disk_journal_survives_restart_and_unicode_chunks_and_expires_after_week(self):
        with tempfile.TemporaryDirectory() as folder:
            store=OperationLogs(Path(folder)/'logs')
            identifier='a'*32
            writer=store.writer(identifier,['private-pass'])
            writer.write('private-pass '+'ЖР🙂'*1000);writer.close()
            reopened=OperationLogs(store.directory)
            parts=[];offset=0
            while True:
                text,offset,more=reopened.read(identifier,offset,size=101)
                parts.append(text)
                if not more:break
            text=''.join(parts)
            self.assertNotIn('private-pass',text);self.assertEqual(text.count('ЖР🙂'),1000)
            path=store.directory/(identifier+'.log')
            os.utime(path,(time.time()-RETENTION-1,)*2)
            reopened.prune()
            self.assertFalse(path.exists())

    def test_scheduler_weekly_cleanup_preserves_active_runs_and_last_job_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store=SchedulerStore(Path(folder)/'jobs.sqlite3',seal=lambda value:value,unseal=lambda value:value)
            store.initialize()
            rule=dict(kind='daily',start=(datetime.now()+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'))
            job=store.save('Fixture',rule,[dict(action='command',title='Fixture',parameters=dict(code='fixture'))])
            store.request(job);run=store.claim('fixture')
            store.append([(run['id'],1,time.time(),'info','old log')]);store.finish(run['id'],'Успешно')
            with store.db() as db:db.execute('UPDATE runs SET finished=? WHERE id=?',(time.time()-RETENTION-1,run['id']))
            store.request(job);active=store.claim('fixture')
            store.prune_history()
            self.assertEqual(store.journal(run['id']),[])
            self.assertEqual([row['id'] for row in store.history()[0]],[active['id']])
            self.assertEqual(store.jobs()[0]['status'],'Выполняется')
