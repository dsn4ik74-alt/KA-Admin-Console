"""Native archive-reader checks and streaming failure tests; no server connections."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from postgres_runner import CompatibleSqlStream, OperationCancelled, Runner
from test_postgres import CONNECTION, TARGET


ROOT = Path(__file__).resolve().parents[1]
TOOLS = {major: str(ROOT / 'vendor' / 'postgresql' / str(major) / 'bin' / 'pg_restore.exe')
         for major in (14, 16, 17, 18)}


def empty_archive(source='14.3', dumper='17.11', minor=16):
    # A real custom archive with zero TOC entries, usable by the native reader.
    def integer(n):
        return bytes([int(n < 0)]) + struct.pack('<I', abs(n))
    def string(s):
        data = s.encode('utf-8')
        return integer(len(data)) + data
    return (b'PGDMP' + bytes([1, minor, 0, 4, 8, 1, 0]) +
            b''.join(integer(n) for n in (0, 0, 12, 12, 9, 126, 0)) +
            string('fixture') + string(source) + string(dumper) + integer(0))


@unittest.skipUnless(os.name == 'nt' and all(Path(tool).is_file() for tool in TOOLS.values()), 'Windows client sets required')
class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.backup = self.folder / 'archive with spaces.dump'
        self.backup.write_bytes(empty_archive())
        self.events = []
        self.runner = Runner(dict(CONNECTION, pg_restore=TOOLS[14], restore_candidates=list(TOOLS.values())), self.events.append)
        self.statements = []
        self.runner.sql = lambda sql, **kwargs: self.statements.append(sql) or ''
        self.runner.target = lambda db: dict(TARGET, server_version=140003)
        self.runner.tool = lambda name: 'psql-test' if name == 'psql' else TOOLS[14]

    def test_native_14_and_16_reject_116_17_reads_it(self):
        for major in (14, 16):
            result = subprocess.run([TOOLS[major], '--list', str(self.backup)], capture_output=True, env=self.runner.env)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'1.16', result.stderr)
        reader, major, source = self.runner.archive_reader(self.backup)
        self.assertEqual((reader, major, source), (TOOLS[17], 17, 14))
        self.assertFalse(self.statements)

    def test_native_17_archive_stream_restores_via_psql_not_native_db_connection(self):
        run = self.runner.run
        sql_bytes = []
        def consume(args, *other, **kwargs):
            if args[0] == 'psql-test':
                self.assertIn('ON_ERROR_STOP=1', args)
                self.assertIn('--single-transaction', args)
                stream = kwargs['source']
                while chunk := stream.read(37):
                    sql_bytes.append(chunk)
                return ''
            return run(args, *other, **kwargs)
        self.runner.run = consume
        self.runner.restore('Target', str(self.backup))
        restored = b''.join(sql_bytes)
        self.assertIn(b'SET statement_timeout = 0;', restored)
        self.assertNotIn(b'SET transaction_timeout = 0;', restored)
        self.assertIn(b'\\restrict ', restored)
        self.assertIn(b'\\unrestrict ', restored)
        self.assertTrue(any(sql.startswith('DROP DATABASE') for sql in self.statements))
        self.assertEqual(self.backup.read_bytes(), empty_archive())

    def test_explicit_reader_does_not_fallback_and_target_is_untouched(self):
        self.runner.c.pop('restore_candidates')
        with self.assertRaisesRegex(RuntimeError, '1.16'):
            self.runner.restore('Target', str(self.backup))
        self.assertFalse(self.statements)

    def test_native_18_archive_is_read_and_target_18_uses_its_reader(self):
        self.backup.write_bytes(empty_archive(source='18.3', dumper='18.6'))
        self.runner.c['pg_restore'] = TOOLS[18]
        self.runner.tool = lambda name: 'psql-test' if name == 'psql' else TOOLS[18]
        reader, major, source = self.runner.archive_reader(self.backup)
        self.assertEqual((reader, major, source), (TOOLS[18], 18, 18))
        self.assertFalse(self.statements)
        self.runner.target = lambda db: dict(TARGET, server_version=180003)
        native_run = self.runner.run
        commands = []
        def run(args, *other, **kwargs):
            if args[0] == TOOLS[18] and any(arg.startswith('--dbname=') for arg in args):
                commands.append(args)
                return ''
            return native_run(args, *other, **kwargs)
        self.runner.run = run
        self.runner.restore('Target', str(self.backup))
        self.assertEqual(len(commands), 1)
        self.assertTrue(any(sql.startswith('DROP DATABASE') for sql in self.statements))

    def test_newer_source_server_rejected_before_drop(self):
        self.backup.write_bytes(empty_archive(source='17.11'))
        with self.assertRaisesRegex(ValueError, 'новее целевого'):
            self.runner.restore('Target', str(self.backup))
        self.assertFalse(self.statements)

    def test_unknown_source_server_rejected_before_drop(self):
        self.backup.write_bytes(empty_archive(source='unknown'))
        with self.assertRaisesRegex(ValueError, 'не определена'):
            self.runner.restore('Target', str(self.backup))
        self.assertFalse(self.statements)

    def test_corrupt_and_future_archives_never_mutate_target(self):
        for data in (empty_archive()[:-10], empty_archive(minor=99)):
            self.backup.write_bytes(data)
            with self.assertRaises(RuntimeError):
                self.runner.restore('Target', str(self.backup))
            self.assertFalse(self.statements)


HEADER = b'-- PostgreSQL database dump\nSET statement_timeout = 0;\nSET transaction_timeout = 0;\nSET row_security = off;\n'


class SqlStreamTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.runner = Runner(CONNECTION, lambda event: None)

    def producer(self, data=HEADER, tail=''):
        script = self.folder / 'producer.py'
        script.write_text('import sys,time\nsys.stdout.buffer.write(' + repr(data) + ')\nsys.stdout.buffer.flush()\n' + tail, encoding='utf-8')
        return [sys.executable, '-u', str(script)]

    def test_only_prologue_setting_removed_copy_rows_and_functions_unchanged(self):
        body = b'COPY t FROM stdin;\nSET transaction_timeout = 0;\n\\.\nCREATE FUNCTION f() AS $$\nSET transaction_timeout = 0;\n$$ LANGUAGE sql;\n'
        stream = CompatibleSqlStream(self.runner, self.producer(HEADER + body))
        try:
            chunks = []
            while chunk := stream.read(19):
                chunks.append(chunk)
            self.assertEqual(b''.join(chunks), HEADER.replace(b'SET transaction_timeout = 0;\n', b'') + body)
        finally:
            stream.close()

    def test_bad_or_oversized_prologue_rejected_and_producer_stopped(self):
        for data in (b'broken\n', b'x' * 65537, HEADER.replace(b'SET row_security = off;', b'COPY t FROM stdin;')):
            with self.assertRaisesRegex(RuntimeError, 'служебный заголовок'):
                CompatibleSqlStream(self.runner, self.producer(data))

    def test_producer_failure_terminates_consumer_before_normal_eof(self):
        marker = self.folder / 'committed'
        consumer = self.folder / 'consumer.py'
        consumer.write_text('import sys,pathlib\nsys.stdin.buffer.read()\npathlib.Path(sys.argv[1]).write_text("COMMIT")\n', encoding='utf-8')
        stream = CompatibleSqlStream(self.runner, self.producer(tail='time.sleep(0.2)\nsys.stderr.write("archive corrupt\\n")\nsys.exit(9)\n'))
        try:
            with self.assertRaisesRegex(RuntimeError, 'archive corrupt'):
                self.runner.run([sys.executable, str(consumer), str(marker)], source=stream)
            self.assertFalse(marker.exists())
        finally:
            stream.close()
        self.assertIsNotNone(stream.process.poll())

    def test_consumer_failure_stops_blocked_producer(self):
        stream = CompatibleSqlStream(self.runner, self.producer(tail='time.sleep(60)\n'))
        try:
            before = time.monotonic()
            with self.assertRaises(RuntimeError):
                self.runner.run([sys.executable, '-c', 'import sys;sys.exit(7)'], source=stream)
            self.assertLess(time.monotonic() - before, 5)
            self.assertIsNotNone(stream.process.poll())
        finally:
            stream.close()

    def test_cancel_stops_both_pipeline_children(self):
        stream = CompatibleSqlStream(self.runner, self.producer(tail='time.sleep(60)\n'))
        timer = threading.Timer(0.3, self.runner.cancel_event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        try:
            before = time.monotonic()
            with self.assertRaises(OperationCancelled):
                self.runner.run([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'], source=stream)
            self.assertLess(time.monotonic() - before, 5)
            self.assertIsNotNone(stream.process.poll())
        finally:
            stream.close()

    def test_cancel_during_preflight_stops_silent_producer(self):
        timer = threading.Timer(0.3, self.runner.cancel_event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(OperationCancelled):
            CompatibleSqlStream(self.runner, self.producer(b'', 'time.sleep(60)\n'))


if __name__ == '__main__':
    unittest.main()
