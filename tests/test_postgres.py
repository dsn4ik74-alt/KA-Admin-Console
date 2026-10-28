import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from postgres_jobs import run_job, ssh_command
from postgres_runner import Runner, database_name, decode_output, execute, identifier, literal, redact


CONNECTION = dict(host='localhost', port='5432', username='postgres', password='test-secret',
                  ib_password='test-ib-secret', data_path=os.path.abspath('test-data'),
                  ib_user='Admin', format='dump')
TARGET = dict(datname='Target', owner='Owner', encoding='UTF8', datcollate='C', datctype='C',
              datconnlimit=-1, datallowconn=True, tablespace='pg_default', locale_provider='c')


# Stand-ins for native executables: run as real child processes, never contact a DB.
FAKE_TOOLS = r'''
import gzip, json, os, pathlib, sys
tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ['CONSOLE_TEST_TRACE'], 'a', encoding='utf-8') as trace:
    trace.write(json.dumps({'tool': tool, 'args': args, 'database': os.environ.get('PGDATABASE')}) + '\n')
if tool == 'psql':
    if '-c' in args:
        sql = args[args.index('-c') + 1]
        if 'pg_get_userbyid' in sql and 'datconnlimit' in sql:
            print(os.environ['CONSOLE_TEST_TARGET'])
        elif 'pg_database_size' in sql:
            print('[{"name":"Target","size_bytes":1048576,"owner":"Owner"}]')
        elif 'pg_terminate_backend' in sql and os.environ.get('CONSOLE_TEST_FAIL') == 'terminate':
            print('failed test-secret')
            sys.exit(5)
    else:
        content = sys.stdin.buffer.read()
        if os.environ.get('CONSOLE_TEST_FAIL') == 'restore':
            sys.exit(7)
        print('Read bytes: ' + str(len(content)))
elif tool == 'pg_dump':
    path = next(a.split('=', 1)[1] for a in args if a.startswith('--file='))
    if '--format=directory' in args:
        pathlib.Path(path).mkdir()
        pathlib.Path(path, 'toc.dat').write_bytes(b'PGDMPfake')
        pathlib.Path(path, '123.dat.gz').write_bytes(b'fake table')
    elif '--format=plain' in args:
        with gzip.open(path, 'wb') as output:
            output.write(b'-- PostgreSQL database dump\nSELECT 1;\n' * 20000)
    else:
        pathlib.Path(path).write_bytes(b'PGDMPfake')
    if os.environ.get('CONSOLE_TEST_FAIL') == 'dump':
        print('failure test-secret')
        sys.exit(6)
    print('dump finished')
elif tool == 'pg_restore':
    if '--version' in args:
        print('pg_restore (PostgreSQL) 14.24')
    elif '--list' in args:
        print('; Format: ' + ('DIRECTORY' if pathlib.Path(args[-1]).is_dir() else 'CUSTOM'))
        print('TOC list')
    elif os.environ.get('CONSOLE_TEST_FAIL') == 'restore':
        print('restore failure')
        sys.exit(8)
elif tool == 'ibcmd':
    if 'dump' in args:
        pathlib.Path(args[-1]).write_bytes(b'DTfake')
    print('finished test-secret test-ib-secret')
'''


class ProcessRunner(Runner):
    script = ''

    def tool(self, name):
        return name

    def run(self, args, *other, **kwargs):
        return super().run([sys.executable, self.script, *args], *other, **kwargs)


class PostgresProcessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.trace = self.folder / 'trace.jsonl'
        ProcessRunner.script = str(self.folder / 'tools.py')
        Path(ProcessRunner.script).write_text(FAKE_TOOLS, encoding='utf-8')
        env = patch.dict(os.environ, CONSOLE_TEST_TRACE=str(self.trace), CONSOLE_TEST_TARGET=json.dumps([TARGET]),
                         CONSOLE_TEST_FAIL='')
        env.start()
        self.addCleanup(env.stop)
        self.events = []
        self.runner = ProcessRunner(dict(CONNECTION), self.events.append)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text(encoding='utf-8').splitlines()]

    def backup_file(self, suffix='.dump'):
        file = self.folder / ('source' + suffix)
        if suffix == '.sql.gz':
            with gzip.open(file, 'wb') as stream:
                stream.write(b'SELECT 1;\n' * 20000)
        else:
            file.write_bytes(b'PGDMPfake')
        return file

    def test_list_and_custom_backup_verify_before_publish(self):
        self.assertEqual(self.runner.databases()[0]['name'], 'Target')
        created = self.runner.backup(['Target', 'Other'], str(self.folder))
        self.assertEqual(len(created), 2)
        for path in created:
            self.assertEqual(Path(path).read_bytes(), b'PGDMPfake')
            self.assertRegex(Path(path).name, r'_\d{8}_\d{6}_\d{6}_[0-9a-f]{8}\.dump$')
        self.assertEqual([item['database'] for item in self.calls() if item['tool'] == 'pg_dump'], ['Target', 'Other'])
        self.assertFalse(list(self.folder.rglob('*.part')))

    def test_dump_failure_never_publishes_or_cleans_old_backups(self):
        self.runner.env['CONSOLE_TEST_FAIL'] = 'dump'
        old = self.folder / 'old.dump'
        old.write_bytes(b'old')
        with self.assertRaises(RuntimeError):
            self.runner.backup(['Target'], str(self.folder))
        self.assertEqual(old.read_bytes(), b'old')
        self.assertEqual(list(self.folder.rglob('*.dump')), [old])
        self.assertFalse(list(self.folder.rglob('*.part')))
        self.assertNotIn('test-secret', str(self.events))

    def test_gzip_backup_and_streamed_restore_use_psql(self):
        self.runner.c['format'] = 'sql.gz'
        created = self.runner.backup(['Target'], str(self.folder))
        with gzip.open(created[0], 'rb') as stream:
            expected = stream.read()
        self.runner.restore('Target', created[0])
        calls = self.calls()
        self.assertFalse(any(item['tool'] == 'pg_restore' for item in calls))
        restore = calls[-1]
        self.assertEqual(restore['database'], 'Target')
        self.assertIn('--single-transaction', restore['args'])
        self.assertIn('ON_ERROR_STOP=1', restore['args'])
        self.assertEqual(restore['args'][-2:], ['-f', '-'])
        self.assertTrue(any(item['text'] == f'Read bytes: {len(expected)}' for item in self.events))

    def test_corrupt_gzip_aborts_before_any_mutation(self):
        file = self.backup_file('.sql.gz')
        file.write_bytes(file.read_bytes()[:-5])
        with self.assertRaises((EOFError, OSError)):
            self.runner.restore('Target', str(file))
        self.assertFalse(any('ALTER DATABASE' in str(item['args']) or 'DROP DATABASE' in str(item['args']) for item in self.calls()))

    def test_restore_failure_reports_old_database_removed(self):
        self.runner.env['CONSOLE_TEST_FAIL'] = 'restore'
        with self.assertRaisesRegex(RuntimeError, 'Старая база уже удалена'):
            self.runner.restore('Target', str(self.backup_file()))
        calls = self.calls()
        self.assertLess(next(i for i, item in enumerate(calls) if '--list' in item['args']),
                        next(i for i, item in enumerate(calls) if 'DROP DATABASE' in str(item['args'])))
        self.assertIn('--single-transaction', calls[-1]['args'])
        self.assertFalse(any('ALLOW_CONNECTIONS true' in str(item['args']) for item in calls))

    def test_failure_before_drop_returns_connections_and_redacts_errors(self):
        self.runner.env['CONSOLE_TEST_FAIL'] = 'terminate'
        with self.assertRaises(RuntimeError) as error:
            self.runner.restore('Target', str(self.backup_file()))
        self.assertNotIn('test-secret', str(error.exception))
        self.assertTrue(any('ALLOW_CONNECTIONS true' in str(item['args']) for item in self.calls()))
        self.assertFalse(any('DROP DATABASE' in str(item['args']) for item in self.calls()))

    def test_ibcmd_long_options_auth_force_and_final_dt_path(self):
        created = self.runner.backup(['Target'], str(self.folder), ibcmd=True)
        self.assertEqual(Path(created[0]).suffix, '.dt')
        self.runner.restore('Target', created[0], ibcmd=True)
        calls = [item for item in self.calls() if item['tool'] == 'ibcmd']
        self.assertIn('--database-server=localhost port=5432', calls[0]['args'])
        self.assertIn('--data=' + CONNECTION['data_path'], calls[0]['args'])
        self.assertIn('--user=Admin', calls[0]['args'])
        self.assertNotIn('--force', calls[0]['args'])
        self.assertIn('--force', calls[1]['args'])
        self.assertEqual(calls[1]['args'][-1], created[0])
        self.assertNotIn('test-secret', str(self.events))
        self.assertNotIn('test-ib-secret', str(self.events))

    def test_ibcmd_empty_auth_does_not_supply_1c_credentials(self):
        self.runner.c['ib_user'] = ''
        args = self.runner.ibcmd_args('Target', restore=True)
        self.assertFalse(any(arg.startswith(('--user=', '--password=')) for arg in args))
        self.assertIn('--database-password=test-secret', args)
        with self.assertRaises(ValueError):
            self.runner.backup(['Target', 'Other'], str(self.folder), ibcmd=True)
        self.assertFalse(self.trace.exists())

    def test_service_database_and_bad_file_prevent_mutation(self):
        with self.assertRaises(ValueError):
            self.runner.restore('postgres', str(self.backup_file()))
        self.assertFalse(self.trace.exists())
        with self.assertRaises(ValueError):
            self.runner.restore('Target', str(self.folder / 'missing.dump'))
        self.assertFalse(any('ALTER DATABASE' in str(item['args']) for item in self.calls()))

    def test_retention_only_deletes_matching_files_after_success(self):
        created = Path(self.runner.backup(['Target'], str(self.folder))[0])
        os.utime(created, (time.time() - 3 * 86400,) * 2)
        other = created.parent / 'Target_manual.dump'
        other.write_bytes(b'manual')
        os.utime(other, (time.time() - 3 * 86400,) * 2)
        failed = created.parent / '.console_failed.part'
        failed.write_bytes(b'failed')
        self.runner.c['retention_days'] = '2'
        self.runner.backup(['Target'], str(self.folder))
        self.assertFalse(created.exists())
        self.assertTrue(other.exists())
        self.assertTrue(failed.exists())

    def test_quoted_names_are_sql_identifiers_and_not_options(self):
        database = "Target'\"; DROP DATABASE postgres;--"
        self.runner.env['CONSOLE_TEST_TARGET'] = json.dumps([dict(TARGET, datname=database)])
        self.runner.restore(database, str(self.backup_file()))
        calls = self.calls()
        drop = next(item for item in calls if item['args'][-1].startswith('DROP DATABASE'))
        self.assertEqual(drop['args'][-1], 'DROP DATABASE ' + identifier(database) + ';')
        self.assertEqual(calls[-1]['database'], database)
        self.assertIn('--dbname=dbname=', ' '.join(calls[-1]['args']))

    def test_locale_and_tablespace_preserved_and_unknown_provider_rejected(self):
        sql = self.runner.create_sql('Target', dict(TARGET, locale_provider='i', locale='ru-RU', icu_rules="a'b"))
        self.assertIn('LOCALE_PROVIDER icu', sql)
        self.assertIn("ICU_LOCALE E'ru-RU'", sql)
        self.assertIn("ICU_RULES E'a''b'", sql)
        self.assertIn('TABLESPACE "pg_default"', sql)
        with self.assertRaises(ValueError):
            self.runner.create_sql('Target', dict(TARGET, locale_provider='unknown'))


class TransportTests(unittest.TestCase):
    def test_streamed_failure_contains_actual_cause_and_redacts_passwords(self):
        runner = Runner(CONNECTION, Mock())
        script = "import sys; print('pg_dump: server version 18.3; client version 17.11; test-secret'); sys.exit(3)"
        with self.assertRaises(RuntimeError) as failure:
            runner.run([sys.executable, '-c', script])
        self.assertIn('server version 18.3; client version 17.11', str(failure.exception))
        self.assertNotIn('test-secret', str(failure.exception))

    def test_decode_preserves_utf8_and_legacy_russian_messages(self):
        message = 'pg_dump: выгрузка содержимого таблицы "public.config"'
        self.assertEqual(decode_output(message.encode('utf-8'), 'cp1251'), message)
        self.assertEqual(decode_output(message.encode('cp1251'), 'cp1251'), message)
        self.assertEqual(decode_output(message.encode('cp866'), 'cp866'), message)
        self.assertEqual(decode_output(b'ASCII message'), 'ASCII message')

    def test_gettext_charset_is_forced_without_changing_parent_environment(self):
        with patch.dict(os.environ, OUTPUT_CHARSET='CP1251'):
            runner = Runner(CONNECTION, Mock())
            self.assertEqual(runner.env['OUTPUT_CHARSET'], 'UTF-8')
            self.assertEqual(os.environ['OUTPUT_CHARSET'], 'CP1251')

    def test_local_dispatch_does_not_invoke_ssh(self):
        payload = dict(mode='list', connection=dict(CONNECTION, execution='local'))
        with patch('postgres_jobs.execute', return_value=['local']) as local, patch('postgres_jobs.ssh_command') as ssh:
            self.assertEqual(run_job(payload, Mock()), ['local'])
        local.assert_called_once()
        ssh.assert_not_called()

    def test_ssh_has_strict_key_auth_and_no_credentials_in_command(self):
        config = dict(CONNECTION, ssh_host='linux.example', ssh_user='admin', ssh_port='2222', execution='ssh')
        with patch('postgres_jobs.executable', return_value='ssh'):
            args = ssh_command(config)
        self.assertIn('BatchMode=yes', args)
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertIn('2222', args)
        self.assertNotIn('test-secret', ' '.join(args))
        self.assertNotIn('test-ib-secret', ' '.join(args))
        self.assertLess(len(subprocess.list2cmdline(args)), 32000)

    def test_ssh_rejects_injected_host_user_and_missing_key(self):
        config = dict(ssh_host='linux', ssh_user='user', ssh_port='22')
        for change in (dict(ssh_host='-oProxyCommand=bad'), dict(ssh_host='host;bad'), dict(ssh_user='user;bad'),
                       dict(ssh_port='65536'), dict(ssh_key='missing-key')):
            with self.subTest(change=change), patch('postgres_jobs.executable', return_value='ssh'):
                with self.assertRaises(ValueError):
                    ssh_command(dict(config, **change))

    def test_remote_runner_executes_in_memory_and_transports_json(self):
        # Execute the exact SSH bootstrap and stdin source locally with an invalid payload.
        # This checks quoting/entrypoint without accessing any PostgreSQL server.
        with patch('postgres_jobs.executable', return_value='ssh'):
            command = ssh_command(dict(ssh_host='linux', ssh_user='user'))[-1]
        import shlex
        parts = shlex.split(command)
        source = Path(__file__).resolve().parents[1].joinpath('postgres_runner.py').read_text(encoding='utf-8')
        request = (json.dumps(source, ensure_ascii=True) + '\n' + '{"mode":"invalid","connection":{}}\n').encode('ascii')
        result = subprocess.run([sys.executable, *parts[1:]], input=request,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stdout)
        self.assertEqual(error['kind'], 'error')
        self.assertIn('Неизвестная операция', error['text'])

    def test_ssh_payload_stdin_and_failure_protocol(self):
        payload = dict(mode='list', connection=dict(CONNECTION, execution='ssh', ssh_host='linux'))
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdin.close = Mock()
        process.stdout = io.BytesIO(b'{"kind":"result","result":[]}\n')
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch('postgres_jobs.start_remote', return_value=process):
            self.assertEqual(run_job(payload, Mock()), [])
        source, sent = process.stdin.getvalue().decode('ascii').splitlines()
        self.assertIn('def remote_main', json.loads(source))
        self.assertEqual(json.loads(sent), payload)
        process.stdout = io.BytesIO(b'{"kind":"error","text":"test-secret failure"}\n')
        process.wait.return_value = 1
        with patch('postgres_jobs.start_remote', return_value=process):
            with self.assertRaises(RuntimeError) as error:
                run_job(payload, Mock())
        self.assertNotIn('test-secret', str(error.exception))

    def test_dispatch_requires_single_restore_target_and_valid_mode(self):
        with self.assertRaises(ValueError):
            execute(dict(mode='invalid', connection={}), Mock())
        with self.assertRaises(ValueError):
            execute(dict(mode='pg_restore', connection=CONNECTION, databases=['one', 'two'], path='file'), Mock())
        with self.assertRaises(ValueError):
            database_name('bad\x00name')
        self.assertEqual(literal("a'b"), "E'a''b'")
        self.assertEqual(literal("a\\'b"), "E'a\\\\''b'")
        self.assertNotIn('test-secret', redact('test-secret test-ib-secret', CONNECTION))


if __name__ == '__main__':
    unittest.main()
