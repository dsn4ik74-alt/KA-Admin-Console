"""Copy workflows: ordered stages, independent credentials and destructive guards."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bookmark_navigation import NavigationLayout
from postgres_runner import Runner, OperationCancelled, execute, redact
from postgres_jobs import ssh_command
from clipboard_shortcuts import bind_entry_shortcuts
import sql_server as sql
import test_sql_server as sql_tests
import test_postgres as pg_tests
import test_service_ui


ROWS = [dict(name='Original', database_id=5, state=0, is_read_only=False, source_database_id=None),
        dict(name='Target', database_id=6, state=0, is_read_only=False, source_database_id=None)]


COPY_TOOLS = pg_tests.FAKE_TOOLS.replace("'database': os.environ.get('PGDATABASE')", """'database': os.environ.get('PGDATABASE'),
      'host': os.environ.get('PGHOST'), 'user': os.environ.get('PGUSER'),
      'credential_ok': os.environ.get('PGPASSWORD') == ('target-secret' if os.environ.get('PGHOST') == 'target-host' else 'test-secret')""").replace(
    "if 'pg_get_userbyid' in sql and 'datconnlimit' in sql:", """if 'pg_postmaster_start_time()' in sql:
            print(json.dumps(dict(database=os.environ['PGDATABASE'], oid=100 if os.environ['PGDATABASE']=='Source' else 200,
                address='192.0.2.1' if os.environ['PGHOST']=='source-host' else '192.0.2.2', port=int(os.environ['PGPORT']),
                started='1234.56789', version=140003)))
        elif 'pg_get_userbyid' in sql and 'datconnlimit' in sql:""")


class CopyProcessTests(unittest.TestCase):
    def test_exact_ssh_source_runs_chain_with_distinct_credentials_and_new_archive(self):
        # Run the exact in-memory SSH bootstrap locally. Fake executables are
        # real child processes and never connect to any DB/SSH server.
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            script, trace = folder / 'tools.py', folder / 'trace.jsonl'
            script.write_text(COPY_TOOLS, encoding='utf-8')
            settings = dict(pg_tests.CONNECTION, host='source-host', execution='ssh', target_connection=dict(
                host='target-host', port='5433', username='TargetAdmin', password='target-secret', sslmode='prefer'))
            payload = dict(mode='copy', connection=settings, source_database='Source', target_database='Target', path=str(folder / 'backups'))
            source = Path('postgres_runner.py').read_text(encoding='utf-8')
            injection = """
_native_run = Runner.run
Runner.tool = lambda self, name: name
Runner.run = lambda self, args, *other, **kwargs: _native_run(self, [sys.executable, FIXTURE_SCRIPT, *args], *other, **kwargs)
""".replace('FIXTURE_SCRIPT', repr(str(script)))
            source = source.replace("if __name__ == '__main__':", injection + "\nif __name__ == '__main__':")
            with patch('postgres_jobs.executable', return_value='ssh'):
                bootstrap = shlex.split(ssh_command(dict(ssh_host='fixture', ssh_user='fixture'))[-1])
            request = (json.dumps(source, ensure_ascii=True) + '\n' + json.dumps(payload, ensure_ascii=True) + '\n').encode('ascii')
            env = dict(os.environ, CONSOLE_TEST_TRACE=str(trace), CONSOLE_TEST_TARGET=json.dumps([pg_tests.TARGET]), CONSOLE_TEST_FAIL='')
            # Keep stdin open until the job ends: EOF intentionally cancels SSH jobs.
            process = subprocess.Popen([sys.executable, *bootstrap[1:]], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                process.stdin.write(request)
                process.stdin.flush()
                lines = []
                while line := process.stdout.readline():
                    lines.append(json.loads(line))
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            result = next(event['result'] for event in lines if event['kind'] == 'result')
            archive = Path(result['backup_path'])
            self.assertEqual(archive.read_bytes(), b'PGDMPfake')
            calls = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(all(call['credential_ok'] for call in calls))
            dump = next(i for i, call in enumerate(calls) if call['tool'] == 'pg_dump')
            drop = next(i for i, call in enumerate(calls) if 'DROP DATABASE' in str(call['args']))
            self.assertLess(dump, drop)
            self.assertEqual(calls[dump]['host'], 'source-host')
            self.assertEqual(calls[drop]['host'], 'target-host')
            restore = calls[-1]
            self.assertEqual(restore['tool'], 'pg_restore')
            self.assertEqual(restore['args'][-1], str(archive))
            self.assertEqual(restore['database'], 'Target')
            self.assertNotIn('target-secret', str(lines))
            self.assertNotIn('test-secret', str(lines))


class SqlCopyTests(unittest.TestCase):
    def run_copy(self, responses, event=None):
        with patch('sql_server.query', side_effect=responses) as query, \
                patch('sql_server.inspect_restore', side_effect=lambda connection, database, path, event, **kwargs:
                      sql.restore_plan(database, path, sql_tests.HEADER, sql_tests.FILES, sql_tests.TARGET)) as reader:
            result = sql.backup_and_restore(sql_tests.CONNECTION, 'Original', 'Target', r'D:\Backup', event or Mock())
        return result, query, reader

    def test_newly_created_verified_backup_is_used_on_same_connection(self):
        result, query, reader = self.run_copy([[ROWS], [[dict(source_id=5, target_id=6)]], [], []])
        self.assertTrue(result['backup_path'].endswith('.bak'))
        backup = query.call_args_list[2].args[1]
        self.assertIn('BACKUP DATABASE [Original]', backup[0]['sql'])
        self.assertNotIn('RESTORE VERIFYONLY', str(backup))
        self.assertIn('COPY_ONLY', backup[0]['sql'])
        self.assertEqual(reader.call_args.args[:3],(sql_tests.CONNECTION,'Target',result['backup_path']))
        self.assertEqual(reader.call_args.kwargs,dict(verify=True))
        restore = query.call_args_list[3].args[1][0]['sql']
        self.assertIn("DB_ID(N'Original'),0)<>5", restore)
        self.assertIn("DB_ID(N'Target'),0)<>6", restore)
        self.assertIn('RESTORE DATABASE [Target]', restore)
        self.assertIn(sql.literal(result['backup_path']), restore)
        self.assertFalse(any('RESTORE DATABASE [Original]' in str(call) for call in query.call_args_list))
        self.assertTrue(all(call.args[0] == sql_tests.CONNECTION for call in query.call_args_list))

    def test_backup_failure_or_failed_verify_never_reaches_restore(self):
        for failure in (RuntimeError('backup failed'), OperationCancelled('cancel')):
            with self.subTest(failure=failure), patch('sql_server.query', side_effect=[
                    [ROWS], [[dict(source_id=5, target_id=6)]], failure]) as query, \
                    patch('sql_server.inspect_restore') as reader:
                with self.assertRaises(type(failure)):
                    sql.backup_and_restore(sql_tests.CONNECTION, 'Original', 'Target', r'D:\Backup')
                reader.assert_not_called()
                self.assertEqual(query.call_count, 3)

    def test_same_system_and_unavailable_targets_rejected_before_backup(self):
        for source, target in (('Original', 'original'), ('master', 'Target'), ('Original', 'tempdb')):
            with patch('sql_server.query') as query, self.assertRaises(ValueError):
                sql.backup_and_restore(sql_tests.CONNECTION, source, target, r'D:\Backup')
            query.assert_not_called()
        with patch('sql_server.query', return_value=[[ROWS[0]]]) as query, self.assertRaises(ValueError):
            sql.backup_and_restore(sql_tests.CONNECTION, 'Original', 'Target', r'D:\Backup')
        self.assertEqual(query.call_count, 1)

    def test_cancel_between_stages_keeps_backup_and_never_starts_restore(self):
        event = Mock()
        event.cancel_event = threading.Event()
        def query(connection, steps, callback):
            if steps[0]['query'] and 'WHERE name IN' in steps[0]['sql']:
                return [ROWS]
            if steps[0]['query']:
                return [[dict(source_id=5, target_id=6)]]
            event.cancel_event.set()
            return []
        with patch('sql_server.query', side_effect=query), patch('sql_server.inspect_restore') as reader:
            with self.assertRaisesRegex(OperationCancelled, 'Бэкап сохранён:'):
                sql.backup_and_restore(sql_tests.CONNECTION, 'Original', 'Target', r'D:\Backup', event)
        reader.assert_not_called()

    def test_restore_cancellation_restores_only_original_target_access(self):
        with patch('sql_server.query', side_effect=[
                [ROWS], [[dict(source_id=5, target_id=6)]], [], OperationCancelled('stop'), []]) as query, \
                patch('sql_server.inspect_restore', return_value=sql.restore_plan(
                    'Target', r'D:\Backup\new.bak', sql_tests.HEADER, sql_tests.FILES, sql_tests.TARGET)):
            with self.assertRaisesRegex(OperationCancelled, 'Бэкап сохранён:'):
                sql.backup_and_restore(sql_tests.CONNECTION, 'Original', 'Target', r'D:\Backup')
        cleanup = query.call_args.args[1][0]['sql']
        self.assertIn('database_id=6', cleanup)
        self.assertIn('ALTER DATABASE [Target]', cleanup)
        self.assertNotIn('ALTER DATABASE [Original]', cleanup)


def identity(name, address='192.0.2.1', oid=100, version=140003, started='12345.6789'):
    return dict(database=name, oid=oid, address=address, port=5432, version=version, started=started)


class PgCopyTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.config = dict(pg_tests.CONNECTION, host='source-host', target_connection=dict(
            host='target-host', port='5433', username='target-admin', password='target-secret', sslmode='require'))
        self.runner = Runner(self.config, self.events.append)
        self.runner.copy_identity = Mock(return_value=identity('Source'))
        self.runner.backup = Mock(return_value=['/BACKUP/Source/fresh.dump'])
        self.target = Mock()
        self.target.c = self.config['target_connection']
        self.target.env = dict(PGPORT='5433')
        self.target.copy_identity.return_value = identity('Target', address='192.0.2.2', oid=200)
        self.target.workers.return_value = 1

    def test_order_same_new_archive_and_separate_target_credentials(self):
        order = []
        self.runner.backup.side_effect = lambda *args: order.append('backup') or ['/BACKUP/Source/fresh.dump']
        self.target.restore.side_effect = lambda *args: order.append('restore')
        with patch('postgres_runner.Runner', return_value=self.target) as factory:
            result = self.runner.backup_and_restore('Source', 'Target', '/BACKUP')
        self.assertEqual(order, ['backup', 'restore'])
        self.target.restore.assert_called_once_with('Target', '/BACKUP/Source/fresh.dump')
        self.assertEqual(result['backup_path'], '/BACKUP/Source/fresh.dump')
        config = factory.call_args.args[0]
        self.assertEqual(config['host'], 'target-host')
        self.assertEqual(config['username'], 'target-admin')
        self.assertEqual(config['password'], 'target-secret')
        self.assertEqual(config['sslmode'], 'require')
        self.assertNotIn('target_connection', config)
        self.assertIs(factory.call_args.args[2], self.runner.cancel_event)
        self.assertEqual(self.target.copy_identity.call_count, 2)

    def test_failed_backup_and_cancellation_never_restore(self):
        for failure in (RuntimeError('dump failed'), OperationCancelled('stop')):
            with self.subTest(failure=failure):
                self.runner.backup.side_effect = failure
                with patch('postgres_runner.Runner', return_value=self.target), self.assertRaises(type(failure)):
                    self.runner.backup_and_restore('Source', 'Target', '/BACKUP')
                self.target.restore.assert_not_called()

    def test_failed_restore_reports_retained_path_and_redacts_both_passwords(self):
        self.target.restore.side_effect = RuntimeError('test-secret target-secret failure')
        with patch('postgres_runner.Runner', return_value=self.target), self.assertRaises(RuntimeError) as error:
            self.runner.backup_and_restore('Source', 'Target', '/BACKUP')
        self.assertIn('/BACKUP/Source/fresh.dump', str(error.exception))
        self.assertNotIn('test-secret', str(error.exception))
        self.assertNotIn('target-secret', str(error.exception))
        self.assertEqual(redact('test-secret target-secret', self.config), '•••• ••••')

    def test_host_aliases_cannot_overwrite_source(self):
        self.runner.copy_identity.return_value = identity('Source')
        self.target.copy_identity.return_value = identity('Source', address='127.0.0.1')
        with patch('postgres_runner.Runner', return_value=self.target), self.assertRaisesRegex(ValueError, 'одной базе'):
            self.runner.backup_and_restore('Source', 'Source', '/BACKUP')
        self.runner.backup.assert_not_called()
        self.target.restore.assert_not_called()

    def test_version_downgrade_and_changed_target_never_restore(self):
        self.runner.copy_identity.return_value = identity('Source', version=170011)
        with patch('postgres_runner.Runner', return_value=self.target), self.assertRaisesRegex(ValueError, 'новее'):
            self.runner.backup_and_restore('Source', 'Target', '/BACKUP')
        self.runner.backup.assert_not_called()
        self.runner.copy_identity.return_value = identity('Source')
        self.target.copy_identity.side_effect = [identity('Target'), identity('Target', oid=999)]
        with patch('postgres_runner.Runner', return_value=self.target), self.assertRaisesRegex(RuntimeError, 'изменились'):
            self.runner.backup_and_restore('Source', 'Target', '/BACKUP')
        self.target.restore.assert_not_called()

    def test_dispatch_and_identity_read_are_nondestructive(self):
        runner = Runner(pg_tests.CONNECTION, Mock())
        with patch.object(runner, 'sql', return_value=json.dumps(identity('Source'))) as query:
            self.assertEqual(runner.copy_identity('Source')['database'], 'Source')
        self.assertTrue(query.call_args.args[0].startswith('SELECT'))
        with patch('postgres_runner.Runner', return_value=self.target):
            execute(dict(mode='copy', connection=self.config, source_database='Source', target_database='Target', path='/BACKUP'), Mock())
        self.target.backup_and_restore.assert_called_once_with('Source', 'Target', '/BACKUP')


class CopyUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def sql_panel(self):
        self.app._switch_mode('db_copy')
        panel = self.app.sql_copy
        panel.loaded(panel.connection(), ([dict(name=name, recovery_model_desc='SIMPLE') for name in ('Original', 'Target')],
                                          dict(machine='REMOTE', server='REMOTE')))
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.destination.set('Target')
        panel.server_path.set(r'D:\Backup')
        return panel

    def pg_panel(self):
        self.app._switch_mode('pg_copy')
        panel = self.app.postgres_copy
        panel.host.set('source-host')
        panel.target_host.set('target-host')
        panel.loaded(panel.connection_key(), [dict(name='Original', owner='Owner', size_bytes=123)])
        panel.target_loaded(panel.target_key(), [dict(name='Target', owner='Owner', size_bytes=456)])
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.target_tree.selection_set(panel.target_tree.get_children()[0])
        panel.path.set('C:/Backup')
        return panel

    def test_new_pages_upgrade_without_resetting_custom_layout(self):
        old = NavigationLayout()
        old.move('accounts', 'folder-1c')
        snapshot = old.snapshot()
        for node, _ in old.walk(snapshot):
            if 'children' in node:
                node['children'][:] = [child for child in node['children'] if child['id'] not in ('db_copy', 'pg_copy')]
        restored = NavigationLayout(snapshot)
        self.assertEqual(restored.location('db_copy')[0], 'folder-mssql')
        self.assertEqual(restored.location('pg_copy')[0], 'folder-postgres')
        self.assertEqual(restored.location('accounts')[0], 'folder-1c')

    def test_sql_confirmation_no_is_nondestructive_and_picker_selects_folder(self):
        panel = self.sql_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'work') as work:
            panel.execute()
        self.assertIn('Источник: Original', confirm.call_args.args[1])
        self.assertIn('Целевая база: Target', confirm.call_args.args[1])
        self.assertEqual(confirm.call_args.kwargs['default'], 'no')
        work.assert_not_called()
        with patch('administration_ui.filedialog.askdirectory', return_value='') as folder, \
                patch('administration_ui.filedialog.askopenfilename') as file:
            panel.choose_path()
        folder.assert_called_once()
        file.assert_not_called()
        panel.destination.set('Original')
        with patch('database_copy_ui.messagebox.askyesno') as confirm:
            panel.execute()
        confirm.assert_not_called()

    def test_sql_snapshots_source_and_target_and_allows_navigation_while_running(self):
        panel = self.sql_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work:
            panel.execute()
        operation, success = work.call_args.args
        with patch('database_copy_ui.backup_and_restore', return_value=dict(backup_path='fresh.bak')) as backend:
            operation(Mock())
        self.assertEqual(backend.call_args.args[1:4], ('Original', 'Target', r'D:\Backup'))
        with patch.object(self.app.root, 'destroy') as destroy:
            self.app._on_close()
        destroy.assert_not_called()
        self.app._switch_mode('servers')
        self.assertEqual(self.app.mode, 'servers')
        success(dict(backup_path='fresh.bak'))
        self.assertFalse(panel.running)
        self.assertEqual(panel.destination.get(), 'Target')

    def test_pg_confirmation_snapshots_independent_credentials_and_preserves_backup_path(self):
        panel = self.pg_panel()
        panel.password.set('source-password')
        panel.target_password.set('target-password')
        # Credential edits deliberately require both lists to be refreshed.
        self.assertIsNone(panel.connected)
        self.assertIsNone(panel.target_connected)
        panel = self.pg_panel()
        with patch('database_copy_ui.messagebox.askyesno', return_value=True) as confirm, patch.object(panel, 'work') as work:
            panel.execute()
        operation, success = work.call_args.args
        with patch('database_copy_ui.run_job', return_value=dict(backup_path='fresh.dump')) as backend:
            operation(Mock())
        payload = backend.call_args.args[0]
        self.assertEqual(payload['mode'], 'copy')
        self.assertEqual(payload['connection']['host'], 'source-host')
        self.assertEqual(payload['connection']['password'], 'source-password')
        self.assertEqual(payload['connection']['target_connection']['host'], 'target-host')
        self.assertEqual(payload['connection']['target_connection']['password'], 'target-password')
        self.assertEqual(payload['target_database'], 'Target')
        self.assertEqual(confirm.call_args.kwargs['default'], 'no')
        self.assertNotIn('target-password', panel.output.get('1.0', 'end-1c'))
        success(dict(backup_path='fresh.dump'))
        self.app._switch_mode('servers')
        self.app._switch_mode('pg_copy')
        self.assertEqual(panel.path.get(), 'C:/Backup')
        self.assertIn('fresh.dump', panel.output.get('1.0', 'end-1c'))

    def test_small_window_keeps_actions_visible_and_wheel_never_changes_target(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = self.sql_panel() if mode == 'db_copy' else self.pg_panel()
            self.app.root.deiconify()
            self.app.root.geometry('960x540+10+10')
            self.app.root.update()
            self.assertTrue(panel.execute_button.winfo_viewable())
            self.assertLessEqual(panel.execute_button.winfo_rooty() + panel.execute_button.winfo_height(),
                                 self.app.root.winfo_rooty() + self.app.root.winfo_height())
            for widget in panel.inputs:
                if isinstance(widget, __import__('tkinter').ttk.Combobox):
                    before = widget.get()
                    widget.event_generate('<MouseWheel>', delta=-120)
                    self.app.root.update()
                    self.assertEqual(widget.get(), before)

    def test_server_ctrl_v_works_with_physical_key_and_replaces_selection_once(self):
        panel = self.sql_panel()
        root = self.app.root
        root.deiconify()
        root.update()
        entry = next(widget for widget in panel.inputs if isinstance(widget, (tk.Entry, tk.ttk.Combobox)) and
                     widget.cget('textvariable') == str(panel.server))
        entry.focus_force()
        root.clipboard_clear()
        root.clipboard_append('SQLSERVER01\\INSTANCE')
        entry.event_generate('<KeyPress>', keycode=65, state=4)
        entry.event_generate('<KeyPress>', keycode=86, state=4)
        root.update()
        self.assertEqual(panel.server.get(), 'SQLSERVER01\\INSTANCE')
        self.assertEqual(str(entry.cget('state')), 'normal')
        entry.event_generate('<KeyPress>', keycode=65, state=4)
        entry.event_generate('<KeyPress>', keycode=67, state=4)
        root.update()
        self.assertEqual(root.clipboard_get(), 'SQLSERVER01\\INSTANCE')
        panel.start()
        root.clipboard_clear()
        root.clipboard_append('MUST_NOT_PASTE')
        entry.event_generate('<KeyPress>', keycode=86, state=4)
        root.update()
        self.assertEqual(panel.server.get(), 'SQLSERVER01\\INSTANCE')
        panel.finish('Done')

    def test_cyrillic_shortcut_does_not_depend_on_tk_latin_keysym(self):
        widget = Mock()
        bind_entry_shortcuts(widget)
        handler = widget.bind.call_args.args[1]
        self.assertEqual(handler(SimpleNamespace(state=4, keycode=86, keysym='Cyrillic_em')), 'break')
        widget.event_generate.assert_called_once_with('<<Paste>>')
        widget.event_generate.reset_mock()
        self.assertIsNone(handler(SimpleNamespace(state=4 | 0x20000, keycode=86, keysym='Cyrillic_em')))
        widget.event_generate.assert_not_called()

    def test_both_lists_are_uncovered_columns_and_mouse_selects_actual_source(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = self.sql_panel() if mode == 'db_copy' else self.pg_panel()
            root = self.app.root
            root.deiconify()
            root.geometry('1400x900+20+20')
            root.update()
            self.assertEqual(panel.database_split.cget('orient'), tk.HORIZONTAL)
            self.assertGreater(panel.target_tree.winfo_rootx(), panel.tree.winfo_rootx() + panel.tree.winfo_width())
            self.assertAlmostEqual(panel.tree.winfo_rooty(), panel.target_tree.winfo_rooty(), delta=3)
            for tree, area in ((panel.tree, panel.source_area), (panel.target_tree, panel.target_area)):
                self.assertIs(tree.master, area)
                box = tree.bbox(tree.get_children()[0])
                x, y = box[0] + 10, box[1] + box[3] // 2
                self.assertIs(root.winfo_containing(tree.winfo_rootx() + x, tree.winfo_rooty() + y), tree)
                tree.selection_remove(*tree.selection())
                tree.event_generate('<ButtonPress-1>', x=x, y=y)
                tree.event_generate('<ButtonRelease-1>', x=x, y=y)
                root.update()
                self.assertEqual(len(tree.selection()), 1)
            self.assertGreater(panel.output.winfo_height(), 80)
            old_log_height = panel.output.winfo_height()
            x, y = panel.log_split.sash_coord(0)
            panel.log_split.event_generate('<ButtonPress-1>', x=x+2, y=y+3)
            panel.log_split.event_generate('<B1-Motion>', x=x+2, y=y-60)
            panel.log_split.event_generate('<ButtonRelease-1>', x=x+2, y=y-60)
            root.update()
            self.assertGreater(panel.output.winfo_height(), old_log_height + 25)

    def test_source_and_target_filters_sort_and_selection_are_independent(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = self.sql_panel() if mode == 'db_copy' else self.pg_panel()
            original_selection = panel.target_tree.set(panel.target_tree.selection()[0], 'name')
            panel.filter_var.set('not found')
            self.app.root.update()
            self.assertFalse(panel.tree.get_children())
            self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), original_selection)
            panel.filter_var.set('')
            self.assertTrue(panel.tree.get_children())
            panel.target_filter.set('not found')
            self.assertFalse(panel.target_tree.get_children())
            self.assertTrue(panel.tree.get_children())
            with patch('database_copy_ui.messagebox.askyesno') as confirm, patch.object(panel, 'work') as work:
                panel.execute()
            confirm.assert_not_called()
            work.assert_not_called()
            panel.target_filter.set('')
            self.assertTrue(panel.target_tree.get_children())
            for tree in (panel.tree, panel.target_tree):
                tree.heading('name', 'command')  # Sort callbacks exist on each independent table.
                tree.tk.call(tree.heading('name', 'command'))
                self.assertTrue(tree.get_children())
