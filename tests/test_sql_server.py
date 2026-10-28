import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from powershell_jobs import run_script
import sql_server as sql
from administration_ui import server_picker_path


HEADER = dict(BackupType=1, Position=2, DatabaseName='Original', BackupFinishDate='2026-01-01', HasBackupChecksums=True)
FILES = [dict(LogicalName='SourceData', Type='D', FileID=1), dict(LogicalName='SourceLog', Type='L', FileID=2)]
TARGET = [dict(name='OldData', type=0, physical_name=r'D:\SQL\Target.mdf', file_id=1),
          dict(name='OldLog', type=1, physical_name=r'L:\SQL\Target.ldf', file_id=2)]
CONNECTION = dict(server='localhost', windows_auth=False, username='user;name', password='secret;value',
                  encrypt=True, trust_certificate=False)


class SqlPlansTests(unittest.TestCase):
    def test_backup_full_unique_checked_and_escaped(self):
        path, steps = sql.backup_plan("O'Brien]", r'D:\Backup', now=datetime(2026, 10, 12, 14, 15, 16))
        self.assertIn('_FULL_20261012_141516_', path)
        self.assertTrue(path.endswith('.bak'))
        self.assertNotEqual(path, sql.backup_plan("O'Brien]", r'D:\Backup', now=datetime(2026, 10, 12, 14, 15, 16))[0])
        self.assertIn("[O'Brien]]]", steps[0]['sql'])
        self.assertIn("O''Brien]", steps[0]['sql'])
        self.assertIn('COPY_ONLY, COMPRESSION, CHECKSUM, NOFORMAT, NOINIT', steps[0]['sql'])
        self.assertNotIn('DIFFERENTIAL', steps[0]['sql'])
        self.assertIn('RESTORE VERIFYONLY', steps[1]['sql'])
        self.assertTrue(all(item['timeout'] == 0 for item in steps))
        _, plain = sql.backup_plan('DB', r'\\server\backups', compression=False, copy_only=False)
        self.assertNotIn('COPY_ONLY', plain[0]['sql'])
        self.assertNotIn('COMPRESSION', plain[0]['sql'])

    def test_restore_reads_original_names_maps_every_file_and_renames(self):
        files = FILES + [dict(LogicalName='MoreData', Type='D', FileID=3)]
        plan = sql.restore_plan('Target', r"D:\Backup\O'Brien.bak", HEADER, files, TARGET)
        self.assertEqual(plan.logical_names, ('Target', 'Target_log', 'Target_data2'))
        self.assertEqual(plan.moves[0], ('SourceData', r'D:\SQL\Target.mdf'))
        self.assertTrue(plan.moves[2][1].startswith(r'D:\SQL\Target_d2_'))
        self.assertIn('FILE = 2', plan.sql)
        self.assertIn("O''Brien.bak", plan.sql)
        self.assertIn('SINGLE_USER WITH ROLLBACK IMMEDIATE', plan.sql)
        self.assertIn('REPLACE, RECOVERY', plan.sql)
        self.assertIn("NAME = N'SourceData'", plan.sql)
        self.assertIn("NEWNAME = N'Target'", plan.sql)
        self.assertEqual(plan.sql.count('SET MULTI_USER'), 2)
        self.assertIn('BEGIN CATCH', plan.sql)
        self.assertNotIn('xp_dirtree', plan.sql)
        self.assertNotIn('RESTORE LOG', plan.sql)
        self.assertNotIn('SELECT INTO', plan.sql)

    def test_restore_rejects_wrong_backup_filestream_and_cross_database_files(self):
        for header, files, target, others in (
                ({**HEADER, 'BackupType': 2}, FILES, TARGET, []),
                ({**HEADER, 'IsDamaged': True}, FILES, TARGET, []),
                (HEADER, FILES + [dict(LogicalName='Stream', Type='S', FileID=3)], TARGET, []),
                (HEADER, FILES, TARGET + [dict(type=2)], []),
                (HEADER, FILES, TARGET, [dict(database_name='Other', physical_name=TARGET[0]['physical_name'])]),
                (HEADER, FILES, [], [])):
            with self.subTest(header=header, files=files, target=target), self.assertRaises(ValueError):
                sql.restore_plan('Target', r'D:\full.bak', header, files, target, others)

    def test_unicode_name_limit_and_log_name_collision_swap(self):
        name = 'Б' * 128
        plan = sql.restore_plan(name, r'D:\full.bak', HEADER, FILES, TARGET)
        self.assertTrue(all(len(item.encode('utf-16-le')) <= 256 for item in plan.logical_names))
        files = [dict(LogicalName='Target_log', Type='D', FileID=1), dict(LogicalName='Target', Type='L', FileID=2)]
        plan = sql.restore_plan('Target', r'D:\full.bak', HEADER, files, TARGET)
        self.assertLess(plan.sql.index("NAME = N'Target'"), plan.sql.index("NEWNAME = N'Target'"))
        for invalid in ('', 'a\nb', 'x' * 129):
            with self.assertRaises(ValueError):
                sql.identifier(invalid)
        with self.assertRaises(ValueError):
            sql.sql_path(r'relative\file.bak')

    def test_inspection_chooses_last_full_set_and_verifies_before_plan(self):
        header_old = {**HEADER, 'Position': 1}
        header_log = {**HEADER, 'Position': 3, 'BackupType': 2}
        with patch.object(sql, 'query', side_effect=[[[header_old, HEADER, header_log], TARGET, []], [FILES, []]]) as call:
            plan = sql.inspect_restore(CONNECTION, 'Target', r'D:\full.bak')
        self.assertEqual(plan.position, 2)
        steps = call.call_args_list[1].args[1]
        self.assertIn('FILELISTONLY', steps[0]['sql'])
        self.assertIn('FILE = 2', steps[0]['sql'])
        self.assertIn('VERIFYONLY', steps[1]['sql'])
        for invocation in call.call_args_list:
            self.assertTrue(all('SINGLE_USER' not in item['sql'] for item in invocation.args[1]))

    def test_restore_handles_actual_fileid_casing_and_mixed_metadata_fields(self):
        files = [{key.lower() if key != 'FileID' else 'FileId': value for key, value in row.items()} for row in FILES]
        header = {key.swapcase(): value for key, value in HEADER.items()}
        targets = [{key.upper(): value for key, value in row.items()} for row in TARGET]
        with patch.object(sql, 'query', side_effect=[[[header], targets, []], [files, []]]):
            plan = sql.inspect_restore(CONNECTION, 'Target', r'D:\full.bak')
        self.assertEqual(plan.moves, (('SourceData', r'D:\SQL\Target.mdf'), ('SourceLog', r'L:\SQL\Target.ldf')))
        self.assertEqual(plan.logical_names, ('Target', 'Target_log'))
        self.assertIn('FILE = 2', plan.sql)

    def test_missing_fileid_stops_preparation_with_diagnostic_not_guessed_order(self):
        files = [{key: value for key, value in row.items() if key != 'FileID'} for row in FILES]
        with self.assertRaisesRegex(ValueError, 'RESTORE FILELISTONLY.*FileID.*LogicalName'):
            sql.restore_plan('Target', r'D:\full.bak', HEADER, files, TARGET)
        for value in (None, '', 0, -1, 'not-an-id'):
            files = [{**row, 'FileID': value} for row in FILES]
            with self.subTest(value=value), self.assertRaises(ValueError):
                sql.restore_plan('Target', r'D:\full.bak', HEADER, files, TARGET)

    def test_picker_distinguishes_client_and_server_files(self):
        self.assertEqual(server_picker_path('D:/Backups', 'LOCAL', 'LOCAL', 'local'), r'D:\Backups')
        self.assertEqual(server_picker_path('D:/Backups', 'REMOTE', 'REMOTE', 'LOCAL'), '')
        self.assertEqual(server_picker_path('D:/Backups', 'REMOTE', 'localhost', 'LOCAL'), '')
        self.assertEqual(server_picker_path('//remote/share/db.bak', 'REMOTE', 'REMOTE', 'LOCAL'), r'\\remote\share\db.bak')

    def test_sizes_use_read_only_catalog_query_and_preserve_unavailable_metadata(self):
        rows = [dict(name='DB', data_bytes=8 * 1024**3, log_bytes=512 * 1024**2, total_bytes=8704 * 1024**2),
                dict(name='Restricted', data_bytes=None, log_bytes=None, total_bytes=None)]
        with patch.object(sql, 'query', return_value=[rows, [dict(machine='SQL', server='SQL')]]) as query:
            returned, server = sql.list_databases(CONNECTION)
        self.assertEqual(returned, rows)
        command = query.call_args.args[1][0]['sql']
        self.assertIn('CONVERT(bigint,size)*8192', command)
        self.assertIn('LEFT JOIN', command)
        self.assertIn('sys.master_files', command)
        self.assertNotIn('COALESCE', command)
        self.assertIn('GROUP BY database_id, type', command)
        self.assertNotIn('CASE WHEN', command)
        self.assertNotIn('ANSI_WARNINGS OFF', command)
        self.assertNotIn('USE [DB]', command)
        for value, expected in ((None, '—'), (0, '0 Б'), (1024, '1 КБ'),
                                (256 * 1024**2, '256 МБ'), (int(1.25 * 1024**3), '1,25 ГБ'),
                                (2 * 1024**4, '2 ТБ'), (3 * 1024**5, '3 ПБ')):
            with self.subTest(value=value):
                self.assertEqual(sql.format_size(value), expected)


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell bridge')
class PowerShellBridgeTests(unittest.TestCase):
    def test_net_reader_preserves_fileid_spelling_and_restore_accepts_it(self):
        prefix = sql.SQL_BRIDGE.split('$builder=', 1)[0]
        reader_start = sql.SQL_BRIDGE.index("$rows=New-Object")
        reader_end = sql.SQL_BRIDGE.index("$results.Add(@($rows.ToArray()))")
        reader_code = sql.SQL_BRIDGE[reader_start:reader_end]
        code = prefix + r'''
$table=New-Object System.Data.DataTable
[void]$table.Columns.Add('LogicalName',[string]); [void]$table.Columns.Add('Type',[string])
[void]$table.Columns.Add('FileId',[long]); [void]$table.Columns.Add('IsPresent',[bool])
[void]$table.Rows.Add('SourceData','D',1,$true); [void]$table.Rows.Add('SourceLog','L',2,$true)
$reader=$table.CreateDataReader()
''' + reader_code + r'''
$results=New-Object 'System.Collections.Generic.List[object]'
$results.Add(@($rows.ToArray())); Send @{kind='done';results=@($results.ToArray())}
'''
        raw = run_script(code, {}, lambda event: None)['results'][0]
        self.assertIn('FileId', raw[0])
        self.assertNotIn('FileID', raw[0])
        plan = sql.restore_plan('Target', r'D:\full.bak', HEADER, raw, TARGET)
        self.assertEqual(plan.moves, (('SourceData', r'D:\SQL\Target.mdf'), ('SourceLog', r'L:\SQL\Target.ldf')))

    def test_actual_net_builder_and_json_shapes_without_sql_server(self):
        # Compile the real InfoMessage adapter and build the real connection
        # string, then exercise the same nested result containers without Open.
        code = sql.SQL_BRIDGE.split('$connection=New-Object', 1)[0] + r'''
$results=New-Object 'System.Collections.Generic.List[object]'
$rows=New-Object 'System.Collections.Generic.List[object]'
$rows.Add(@{name='База';value=$null})
$results.Add(@($rows.ToArray())); $results.Add(@())
Send @{kind='message';text='Прогресс 10%'}
Send @{kind='done';results=@($results.ToArray());server=$builder.DataSource;
    user=$builder.UserID;password=$builder.Password;retry=$builder.ConnectRetryCount;encrypt=$builder.Encrypt}
'''
        events = []
        result = run_script(code, {'connection': CONNECTION}, events.append)
        self.assertEqual(result['results'], [[{'name': 'База', 'value': None}], []])
        self.assertEqual(result['user'], CONNECTION['username'])
        self.assertEqual(result['password'], CONNECTION['password'])
        self.assertEqual(result['retry'], 0)
        self.assertTrue(result['encrypt'])
        self.assertEqual(events, [{'kind': 'message', 'text': 'Прогресс 10%'}])

    def test_streamed_failure_redacts_secret_and_does_not_report_success(self):
        with self.assertRaisesRegex(RuntimeError, r'\[скрыто\]') as caught:
            run_script("param($request); throw ('failed: ' + $request.password)", {'password': 'SUPER_SECRET'},
                       lambda event: None, secrets=('SUPER_SECRET',))
        self.assertNotIn('SUPER_SECRET', str(caught.exception))
        with self.assertRaisesRegex(RuntimeError, 'не вернул результат'):
            run_script("Write-Host 'hello'", {}, lambda event: None)


if __name__ == '__main__':
    unittest.main()
