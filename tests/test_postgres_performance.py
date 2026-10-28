import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from postgres_runner import OperationCancelled, Runner, VerboseProgress
import test_postgres as postgres_tests
import test_postgres_ui as postgres_ui_tests
from test_postgres import CONNECTION


class PerformanceProcessTests(unittest.TestCase):
    setUp = postgres_tests.PostgresProcessTests.setUp
    calls = postgres_tests.PostgresProcessTests.calls
    backup_file = postgres_tests.PostgresProcessTests.backup_file

    def test_parallel_directory_backup_validates_and_publishes_whole_archive(self):
        self.runner.c.update(format='directory', dump_jobs=4, compression=1)
        created = Path(self.runner.backup(['Target'], str(self.folder))[0])
        self.assertEqual(created.suffix, '.pgdir')
        self.assertEqual((created / 'toc.dat').read_bytes(), b'PGDMPfake')
        self.assertEqual((created / '123.dat.gz').read_bytes(), b'fake table')
        dump = next(call for call in self.calls() if call['tool'] == 'pg_dump')
        self.assertIn('--jobs=4', dump['args'])
        self.assertEqual(dump['args'][dump['args'].index('-Z') + 1], '1')
        checked = next(call for call in self.calls() if '--list' in call['args'])
        self.assertNotEqual(checked['args'][-1], str(created))  # Validate stage before publish.
        self.assertFalse(list(created.parent.glob('.console_*')))
        self.runner.c['restore_jobs'] = 4
        self.runner.restore('Target', str(created))
        self.assertIn('--jobs=4', self.calls()[-1]['args'])
        self.assertNotIn('--single-transaction', self.calls()[-1]['args'])

    def test_failed_parallel_dump_removes_only_its_own_stage(self):
        self.runner.c.update(format='directory', dump_jobs=2)
        self.runner.env['CONSOLE_TEST_FAIL'] = 'dump'
        unrelated = self.folder / 'Target' / '.console_unrelated.part'
        unrelated.mkdir(parents=True)
        (unrelated / 'keep').write_bytes(b'keep')
        with self.assertRaises(RuntimeError):
            self.runner.backup(['Target'], str(self.folder))
        self.assertFalse(list(self.folder.rglob('*.pgdir')))
        self.assertEqual(list(unrelated.parent.glob('.console_*')), [unrelated])
        self.assertEqual((unrelated / 'keep').read_bytes(), b'keep')

    def test_directory_retention_restricts_names_and_requires_success(self):
        self.runner.c.update(format='directory', dump_jobs=2)
        old = Path(self.runner.backup(['Target'], str(self.folder))[0])
        manual = old.parent / 'Target_manual.pgdir'
        manual.mkdir()
        (manual / 'toc.dat').write_bytes(b'manual')
        for path in (old, manual):
            os.utime(path, (time.time() - 3 * 86400,) * 2)
        self.runner.c['retention_days'] = 2
        self.runner.env['CONSOLE_TEST_FAIL'] = 'dump'
        with self.assertRaises(RuntimeError):
            self.runner.backup(['Target'], str(self.folder))
        self.assertTrue(old.exists())
        self.runner.env['CONSOLE_TEST_FAIL'] = ''
        self.runner.backup(['Target'], str(self.folder))
        self.assertFalse(old.exists())
        self.assertTrue(manual.exists())

    def test_recursive_cleanup_rejects_outside_parent_and_links(self):
        outside = self.folder / 'outside'
        outside.mkdir()
        with self.assertRaises(ValueError):
            self.runner.remove_archive_directory(outside, self.folder / 'Target')
        self.assertTrue(outside.exists())
        own = self.folder / 'own'
        own.mkdir()
        # A mocked link exercises rejection even without Windows symlink privilege.
        original = Path.is_symlink
        def link(path):
            return path == own / 'link' or original(path)
        (own / 'link').write_bytes(b'keep')
        with patch.object(Path, 'is_symlink', link), self.assertRaises(ValueError):
            self.runner.remove_archive_directory(own, self.folder)
        self.assertTrue((own / 'link').exists())

    def test_parallel_restore_custom_opt_in_and_default_transaction(self):
        source = self.backup_file()
        self.runner.restore('Target', str(source))
        self.assertIn('--single-transaction', self.calls()[-1]['args'])
        self.runner.c['restore_jobs'] = 4
        self.runner.restore('Target', str(source))
        self.assertIn('--jobs=4', self.calls()[-1]['args'])
        self.assertNotIn('--single-transaction', self.calls()[-1]['args'])
        self.assertTrue(any('без общей транзакции' in event.get('text', '') for event in self.events))

    def test_plain_parallel_restore_and_bad_settings_fail_before_mutation(self):
        self.runner.c['restore_jobs'] = 4
        with self.assertRaises(ValueError):
            self.runner.restore('Target', str(self.backup_file('.sql.gz')))
        self.assertFalse(any('ALTER DATABASE' in str(call) for call in self.calls()))
        for update in (dict(format='dump', dump_jobs=4), dict(format='sql.gz', compression=0),
                       dict(format='directory', dump_jobs=17), dict(compression=10), dict(compression='--bad')):
            with self.subTest(update=update):
                self.runner.c = dict(CONNECTION, **update)
                with self.assertRaises(ValueError):
                    self.runner.backup(['Target'], str(self.folder))
        self.assertFalse(any(call['tool'] == 'pg_dump' for call in self.calls()))

    def test_parallel_unsupported_tar_and_compatibility_rejected_before_drop(self):
        self.runner.c['restore_jobs'] = 4
        with patch.object(self.runner, 'archive_reader', return_value=('pg_restore', 14, 14)):
            self.runner._archive_toc = '; Format: TAR\n'
            with self.assertRaisesRegex(ValueError, 'CUSTOM / DIRECTORY'):
                self.runner.restore('Target', str(self.backup_file()))
        with patch.object(self.runner, 'target', return_value=dict(server_version=140003)), \
                patch.object(self.runner, 'archive_reader', return_value=('pg_restore', 17, 14)):
            self.runner._archive_toc = '; Format: CUSTOM\n'
            with self.assertRaisesRegex(ValueError, 'совместимый поток SQL'):
                self.runner.restore('Target', str(self.backup_file()))
        self.assertFalse(any('ALTER DATABASE' in str(call) for call in self.calls()))

    def test_parallel_percentage_does_not_count_inflight_objects_as_complete(self):
        progress = VerboseProgress(self.runner, 'dump', 10, {f'public.t{i}' for i in range(10)}, workers=4)
        for index in range(5):
            progress.line(f'pg_dump: dumping contents of table "public.t{index}"')
        self.assertEqual(self.events[-1]['percent'], 10)
        progress.finish()
        self.assertEqual(self.events[-1]['percent'], 100)


class WorkerCancellationTests(unittest.TestCase):
    def test_cancel_terminates_owned_workers_and_preserves_unrelated_process(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            marker, pidfile = folder / 'heartbeat', folder / 'worker.pid'
            worker = "import os,time,pathlib;pathlib.Path({!r}).write_text(str(os.getpid()));\nwhile True:\n pathlib.Path({!r}).write_text(str(time.time()));time.sleep(.05)".format(str(pidfile), str(marker))
            parent = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",' + repr(worker) + ']);time.sleep(60)'
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
            try:
                runner = Runner(CONNECTION, lambda value: None)
                timer = threading.Timer(1.5, runner.cancel_event.set)
                timer.start()
                try:
                    with self.assertRaises(OperationCancelled):
                        runner.run([sys.executable, '-c', parent], own_tree=True)
                finally:
                    timer.cancel()
                self.assertTrue(pidfile.exists())
                before = marker.read_text()
                time.sleep(.3)
                self.assertEqual(marker.read_text(), before)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)


class PerformanceUiTests(unittest.TestCase):
    setUp = postgres_ui_tests.PostgresUiTests.setUp
    load_databases = postgres_ui_tests.PostgresUiTests.load_databases

    def test_settings_preserve_connection_and_modes_and_directory_picker_explicit(self):
        panel = self.load_databases()
        panel.dump_jobs.set('4')
        panel.compression.set('1')
        panel.format.set('Каталог .pgdir · параллельный бэкап')
        self.assertEqual(panel.connected, panel.connection_key())
        self.assertEqual(panel.connection()['format'], 'directory')
        self.assertEqual(panel.performance_options.winfo_manager(), 'pack')
        self.app._switch_mode('pg_restore')
        self.assertEqual(panel.performance_options.winfo_manager(), '')
        self.assertEqual(panel.restore_options.winfo_manager(), 'pack')
        with patch('postgres_ui.filedialog.askdirectory', return_value='C:/Backup/base.pgdir') as picker:
            panel.tree.selection_set(panel.tree.get_children()[0])
            self.app.root.update()
            picker.assert_not_called()
            panel.choose_directory_archive()
            picker.assert_called_once()
        self.assertEqual(panel.path.get(), 'C:/Backup/base.pgdir')
        self.app._switch_mode('ibcmd_dump')
        self.assertEqual(panel.performance_options.winfo_manager(), '')
        self.assertEqual(panel.restore_options.winfo_manager(), '')
        self.assertEqual(panel.directory_picker.winfo_manager(), '')

    def test_parallel_restore_confirmation_discloses_partial_data(self):
        panel = self.load_databases('pg_restore')
        panel.path.set('C:/Backup/base.dump')
        panel.restore_jobs.set('4')
        with patch('postgres_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'start') as start:
            panel.execute()
        self.assertIn('Потоков: 4', confirm.call_args.args[1])
        self.assertIn('частично заполненной', confirm.call_args.args[1])
        self.assertEqual(confirm.call_args.kwargs['default'], 'no')
        start.assert_not_called()

    def test_parallel_dump_wrong_format_does_not_start(self):
        panel = self.load_databases()
        panel.path.set('C:/Backup')
        panel.dump_jobs.set('4')
        with patch.object(panel, 'start') as start:
            panel.execute()
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
