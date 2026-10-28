import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_service_ui
from database_dialog import CreateDatabaseDialog
from postgres_jobs import run_job, ssh_command
from postgres_runner import OperationCancelled, Runner, create_database_sql, execute
from powershell_jobs import run_script
import sql_server
import test_postgres


class DatabaseTableTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def invoke_header(self, tree, column):
        tree.tk.call(tree.heading(column, 'command'))

    def names(self, tree, column='name'):
        return [tree.set(row, column) for row in tree.get_children()]

    def test_postgres_headers_sort_names_owners_and_raw_sizes_with_selection(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        panel.databases = [dict(name='Яна', size_bytes=1024**4, owner='Борис'),
                           dict(name='Анна', size_bytes=900 * 1024**2, owner='Яков'),
                           dict(name='Ёлка', size_bytes=None, owner='Антон'),
                           dict(name='Елена', size_bytes=2 * 1024**3, owner='Вера')]
        panel.render()
        panel.tree.selection_set(panel.tree.get_children()[0])
        for column, expected in (('name', ['Анна', 'Елена', 'Ёлка', 'Яна']),
                                 ('size', ['Анна', 'Елена', 'Яна', 'Ёлка']),
                                 ('owner', ['Ёлка', 'Яна', 'Елена', 'Анна'])):
            self.invoke_header(panel.tree, column)
            self.assertEqual(self.names(panel.tree), expected)
            self.assertEqual(panel.selected(), ['Яна'])
            self.invoke_header(panel.tree, column)
            descending = list(reversed(expected)) if column != 'size' else ['Яна', 'Елена', 'Анна', 'Ёлка']
            self.assertEqual(self.names(panel.tree), descending)
            self.assertIn('↓', panel.tree.heading(column, 'text'))
        panel.filter_var.set('на')
        self.assertEqual(self.names(panel.tree), ['Анна', 'Елена', 'Яна'])
        panel.filter_var.set('')
        self.assertEqual(self.names(panel.tree), ['Анна', 'Елена', 'Яна', 'Ёлка'])

    def test_sql_sorts_all_numeric_columns_by_bytes_and_keeps_database_selected(self):
        self.app._switch_mode('db_backup')
        panel = self.app.sql
        panel.databases = [dict(name='Big', data_bytes=1024**4, log_bytes=3 * 1024**3,
                                total_bytes=1024**4 + 3 * 1024**3, recovery_model_desc='SIMPLE'),
                           dict(name='Small', data_bytes=900 * 1024**2, log_bytes=1024**2,
                                total_bytes=901 * 1024**2, recovery_model_desc='FULL'),
                           dict(name='Unknown', recovery_model_desc='BULK_LOGGED')]
        panel.render()
        panel.tree.selection_set(panel.tree.get_children()[0])
        for column in ('data', 'log', 'total'):
            self.invoke_header(panel.tree, column)
            self.assertEqual(self.names(panel.tree), ['Small', 'Big', 'Unknown'])
            self.invoke_header(panel.tree, column)
            self.assertEqual(self.names(panel.tree), ['Big', 'Small', 'Unknown'])
            self.assertEqual(panel.selected(), 'Big')
        self.invoke_header(panel.tree, 'recovery')
        self.assertEqual(self.names(panel.tree), ['Unknown', 'Small', 'Big'])
        panel.render()
        self.assertEqual(self.names(panel.tree), ['Unknown', 'Small', 'Big'])
        self.assertEqual(panel.selected(), 'Big')

    def test_account_sort_keeps_details_copy_and_later_result_updates_correct(self):
        self.app._switch_mode('accounts')
        panel = self.app.accounts
        panel.accept_row(1, dict(line=1, full_name='Яна', username='yana', status='ready', message='First'))
        panel.accept_row(1, dict(line=2, full_name='Анна', username='anna', status='error', message='Second'))
        panel.table.selection_set('1:1')
        self.invoke_header(panel.table, 'full_name')
        self.assertEqual(self.names(panel.table, 'full_name'), ['Анна', 'Яна'])
        self.assertEqual(panel.table.selection(), ('1:1',))
        panel.accept_row(1, dict(line=3, full_name='Елена', username='elena', status='ready'))
        self.assertEqual(self.names(panel.table, 'full_name'), ['Анна', 'Елена', 'Яна'])
        panel._show_detail()
        self.assertIn('First', panel.detail.cget('text'))
        panel.copy_results()
        self.assertIn('yana', self.app.root.clipboard_get())
        self.assertNotIn('anna', self.app.root.clipboard_get())
        self.assertIn('problem', panel.table.item('1:2', 'tags'))

    def test_create_database_cancel_does_not_start_and_busy_button_is_disabled(self):
        for panel, module in ((self.app.sql, 'administration_ui'), (self.app.postgres, 'postgres_ui')):
            with self.subTest(module=module), patch(module + '.ask_create_database', return_value=None), patch.object(panel, 'work') as work:
                panel.create_new_database()
                work.assert_not_called()
                self.assertFalse(panel.running)
            panel.start()
            self.assertEqual(str(panel.create_db_button.cget('state')), 'disabled')
            self.assertEqual(str(panel.cancel_button.cget('state')), 'normal')
            panel.cancel_button.invoke()
            self.assertTrue(panel.cancel_event.is_set())
            self.assertTrue(panel.running)
            panel.finish_cancelled('Cancelled')
            self.assertEqual(str(panel.create_db_button.cget('state')), 'normal')
            self.assertEqual(str(panel.cancel_button.cget('state')), 'disabled')

    def test_create_sql_database_refreshes_selects_and_does_not_use_backup_path(self):
        self.app._switch_mode('db_restore')
        panel = self.app.sql
        panel.server_path.set(r'D:\preserved.bak')
        refreshed = ([dict(name='New', recovery_model_desc='FULL')], dict(machine='SQL'))
        with (patch('administration_ui.ask_create_database', return_value=dict(name='New', owner='')),
              patch.object(panel, 'work') as work):
            panel.create_new_database()
        operation, success = work.call_args.args
        with (patch('administration_ui.create_database', return_value='New') as create,
              patch('administration_ui.list_databases', return_value=refreshed)):
            result = operation(Mock())
        create.assert_called_once()
        success(result)
        self.assertEqual(panel.selected(), 'New')
        self.assertEqual(panel.server_path.get(), r'D:\preserved.bak')
        self.assertFalse(panel.running)

    def test_create_postgres_uses_current_ssh_and_reports_success_if_refresh_fails(self):
        panel = self.app.postgres
        panel.execution.set('SSH · Linux')
        panel.ssh_host.set('linux')
        panel.host.set('db.internal')
        with (patch('postgres_ui.ask_create_database', return_value=dict(name='New', owner='Owner')),
              patch.object(panel, 'work') as work):
            panel.create_new_database()
        operation, success = work.call_args.args
        with patch('postgres_ui.run_job', side_effect=['New', RuntimeError('Refresh denied')]) as job:
            result = operation(Mock())
        request = job.call_args_list[0].args[0]
        self.assertEqual(request['mode'], 'create')
        self.assertEqual(request['owner'], 'Owner')
        self.assertEqual(request['connection']['execution'], 'ssh')
        self.assertEqual(request['connection']['host'], 'db.internal')
        success(result)
        self.assertIn('База создана: New', panel.output.get('1.0', 'end-1c'))
        self.assertIn('Refresh denied', panel.output.get('1.0', 'end-1c'))
        self.assertFalse(panel.running)

    def test_cancelled_operation_unlocks_only_after_worker_finishes(self):
        panel = self.app.postgres
        self.app._switch_mode('pg_dump')
        panel.start()
        def operation(event):
            self.assertIs(event.cancel_event, panel.cancel_event)
            self.assertTrue(event.cancel_event.is_set())
            raise OperationCancelled('Incomplete file removed')
        with patch('administration_ui.threading.Thread') as thread:
            panel.work(operation, Mock())
            panel.interrupt()
            self.app._switch_mode('accounts')
            self.assertEqual(self.app.mode, 'accounts')
            thread.call_args.kwargs['target']()
            self.app._drain_events()
        self.assertFalse(panel.running)
        self.assertEqual(panel.state_label.cget('text'), 'Прервано')
        self.assertEqual(str(panel.client_combo.cget('state')), 'readonly')

    def test_real_create_dialog_returns_reviewed_name_owner_and_cancel_returns_none(self):
        def choose():
            dialog = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, CreateDatabaseDialog))
            dialog.name.set('Новая база')
            dialog.owner.set('Owner')
            dialog.ok()
        self.app.root.after(100, choose)
        dialog = CreateDatabaseDialog(self.app.root, 'PostgreSQL', 'pg:5432', 'postgres', self.app.postgres.c)
        self.assertEqual(dialog.result, dict(name='Новая база', owner='Owner'))
        def cancel():
            dialog = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, CreateDatabaseDialog))
            dialog.cancel()
        self.app.root.after(100, cancel)
        dialog = CreateDatabaseDialog(self.app.root, 'SQL Server', 'sql', '', self.app.sql.c)
        self.assertIsNone(dialog.result)

    def test_cancel_sql_restore_attempts_multi_user_on_independent_connection(self):
        from sql_server import restore_plan
        from test_sql_server import HEADER, FILES, TARGET
        self.app._switch_mode('db_restore')
        panel = self.app.sql
        panel.start()
        plan = restore_plan('Target', r'D:\Full.bak', HEADER, FILES, TARGET)
        with patch('administration_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.confirm_restore(panel.connection(), plan)
        operation = work.call_args.args[0]
        def event(item):
            pass
        event.cancel_event = panel.cancel_event
        with patch('administration_ui.query', side_effect=[OperationCancelled('Stopped'), []]) as query:
            with self.assertRaises(OperationCancelled):
                operation(event)
        self.assertEqual(query.call_count, 2)
        cleanup = query.call_args_list[1].args
        self.assertIn('SET MULTI_USER', cleanup[1][0]['sql'])
        self.assertNotIn('cancel_event', getattr(cleanup[2], '__dict__', {}))
        panel.finish_cancelled('Stopped')


class DatabaseCreationTests(unittest.TestCase):
    def test_create_sql_quotes_name_without_dropping_an_existing_database(self):
        name = "O'Brien]; DROP DATABASE Other;--"
        with patch('sql_server.query') as query:
            self.assertEqual(sql_server.create_database(dict(server='sql'), name), name)
        sql = query.call_args.args[1][0]['sql']
        self.assertEqual(sql, 'CREATE DATABASE ' + sql_server.identifier(name) + ';')
        for invalid in ('', 'a\nb', 'x' * 129):
            with self.assertRaises(ValueError), patch('sql_server.query') as query:
                sql_server.create_database({}, invalid)
            query.assert_not_called()

    def test_create_postgres_quotes_database_owner_and_keeps_utf8_template(self):
        name, owner = 'New"; DROP DATABASE other;--', 'Owner"quoted'
        events = []
        runner = Runner(dict(host='pg', username='postgres'), events.append)
        with patch.object(runner, 'sql') as sql:
            self.assertEqual(runner.create_database(name, owner), name)
        self.assertEqual(sql.call_args.args[0], create_database_sql(name, owner))
        self.assertIn('TEMPLATE template0', sql.call_args.args[0])
        self.assertIn("ENCODING 'UTF8'", sql.call_args.args[0])
        self.assertIn('OWNER "Owner""quoted"', sql.call_args.args[0])
        for invalid in ('', 'a\x00b', 'я' * 32):
            with self.assertRaises(ValueError):
                create_database_sql(invalid)


class CancellationProcessTests(unittest.TestCase):
    def test_cancel_stops_a_silent_child_without_waiting_for_output(self):
        cancel = threading.Event()
        runner = Runner(dict(host='unused', username='unused'), Mock(), cancel)
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        real_popen, children = subprocess.Popen, []
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        before = time.monotonic()
        with patch('postgres_runner.subprocess.Popen', side_effect=launch):
            with self.assertRaises(OperationCancelled):
                runner.run([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.assertLess(time.monotonic() - before, 4)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_cancel_before_start_does_not_launch_any_process(self):
        cancel = threading.Event()
        cancel.set()
        runner = Runner(dict(host='unused', username='unused'), Mock(), cancel)
        with patch('postgres_runner.subprocess.Popen') as launch, self.assertRaises(OperationCancelled):
            runner.run(['psql'])
        launch.assert_not_called()

    def test_remote_cancel_protocol_stops_child_and_acknowledges_stop(self):
        with patch('postgres_jobs.executable', return_value='ssh'):
            bootstrap = shlex.split(ssh_command(dict(ssh_host='unused', ssh_user='unused'))[-1])[-1]
        real_source = Path(__file__).resolve().parents[1].joinpath('postgres_runner.py').read_text(encoding='utf-8')
        # Replace the DB job with a silent child, retaining the exact runner/control entrypoint.
        source = real_source.replace("if __name__ == '__main__':\n    remote_main()", """
def execute(payload, event, cancel_event=None):
    runner = Runner({'host':'unused','username':'unused'}, event, cancel_event)
    runner.run([sys.executable, '-c', 'import time; time.sleep(60)'])


if __name__ == '__main__':
    remote_main()
""")
        child = subprocess.Popen([sys.executable, '-u', '-c', bootstrap], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            child.stdin.write((json.dumps(source) + '\n' + json.dumps(dict(connection={})) + '\n').encode())
            child.stdin.flush()
            time.sleep(0.3)
            child.stdin.write(b'cancel\n')
            child.stdin.flush()
            child.wait(timeout=5)
            result = json.loads(child.stdout.read())
            self.assertEqual(result['kind'], 'cancelled')
            self.assertEqual(child.returncode, 2)
            self.assertNotIn(b'Fatal Python', child.stderr.read())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_sql_bridge_cancellation_channel_and_dotnet_command_registration(self):
        cancel = threading.Event()
        def event(item):
            if item.get('kind') == 'stage':
                cancel.set()
        event.cancel_event = cancel
        # Compile the real SQL bridge, without opening its connection, and cancel its loop.
        prefix = sql_server.SQL_BRIDGE.split('$builder=')[0]
        script = prefix + '''
$connection=New-Object System.Data.SqlClient.SqlConnection
[SqlConsoleMessages]::Attach($connection)
Send @{kind='stage';text='Ready'}
for ($i=0;$i -lt 100;$i++) {
    [SqlConsoleMessages]::SetCommand((New-Object System.Data.SqlClient.SqlCommand))
    Start-Sleep -Milliseconds 50
}
'''
        before = time.monotonic()
        with self.assertRaises(OperationCancelled):
            run_script(script, {}, event)
        self.assertLess(time.monotonic() - before, 5)

    def test_ssh_transport_delivers_cancel_and_gets_runner_acknowledgement(self):
        with patch('postgres_jobs.executable', return_value='ssh'):
            bootstrap = shlex.split(ssh_command(dict(ssh_host='unused', ssh_user='unused'))[-1])[-1]
        source = Path(__file__).resolve().parents[1].joinpath('postgres_runner.py').read_text(encoding='utf-8')
        source = source.replace("if __name__ == '__main__':\n    remote_main()", """
def execute(payload, event, cancel_event=None):
    runner = Runner({'host':'unused','username':'unused'}, event, cancel_event)
    runner.run([sys.executable, '-u', '-c', "import time; print('Running',flush=True); time.sleep(60)"])
if __name__ == '__main__':
    remote_main()
""")
        cancel = threading.Event()
        events = []
        def event(item):
            events.append(item)
            if item.get('text') == 'Running':
                cancel.set()
        event.cancel_event = cancel
        before = time.monotonic()
        def start(*args):
            return subprocess.Popen([sys.executable, '-u', '-c', bootstrap], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with (patch('postgres_jobs.start_remote', side_effect=start),
              patch('postgres_jobs.Path.read_text', return_value=source)):
            with self.assertRaises(OperationCancelled):
                run_job(dict(mode='list', connection=dict(execution='ssh', ssh_host='unused')), event)
        self.assertTrue(cancel.is_set())
        self.assertLess(time.monotonic() - before, 5)


class CancellationDatabaseTests(unittest.TestCase):
    setUp = test_postgres.PostgresProcessTests.setUp
    calls = test_postgres.PostgresProcessTests.calls
    backup_file = test_postgres.PostgresProcessTests.backup_file

    def wait_for(self, tool):
        script = Path(test_postgres.ProcessRunner.script)
        source = script.read_text(encoding='utf-8')
        marker = "if tool == 'psql':"
        source = source.replace(marker, """
if tool == %r and '--list' not in args and '--version' not in args:
    print('Waiting', flush=True)
    import time
    time.sleep(60)
""" % tool + marker)
        script.write_text(source, encoding='utf-8')
        def event(item):
            self.events.append(item)
            if item.get('text') == 'Waiting':
                self.runner.cancel_event.set()
        self.runner.event = event

    def test_cancel_backup_removes_partial_and_keeps_previous_files(self):
        old = self.folder / 'previous.dump'
        old.write_bytes(b'old')
        self.wait_for('pg_dump')
        with self.assertRaises(OperationCancelled):
            self.runner.backup(['Target'], str(self.folder))
        self.assertEqual(list(self.folder.rglob('*.dump')), [old])
        self.assertEqual(old.read_bytes(), b'old')
        self.assertFalse(list(self.folder.rglob('*.part')))

    def test_cancel_restore_reports_removed_database_and_no_success(self):
        self.wait_for('pg_restore')
        with self.assertRaisesRegex(OperationCancelled, 'Старая база уже удалена'):
            self.runner.restore('Target', str(self.backup_file()))
        self.assertTrue(any('DROP DATABASE' in str(call['args']) for call in self.calls()))
        self.assertFalse(any(item.get('text', '').startswith('Восстановление завершено') for item in self.events))



if __name__ == '__main__':
    unittest.main()
