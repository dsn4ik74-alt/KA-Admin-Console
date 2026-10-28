from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from postgres_clients import bundled_tools, server_major
from postgres_runner import Runner


class ClientDiscoveryTests(unittest.TestCase):
    def test_real_clients_emit_readable_russian_without_connecting_to_database(self):
        tools = bundled_tools()
        if not tools:
            self.skipTest('Bundled Windows clients are not available')
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / 'invalid.dump'
            invalid.write_bytes(b'not a PostgreSQL archive')
            for major, clients in tools.items():
                with self.subTest(major=major), patch.dict(os.environ, LANGUAGE='ru', LANG='ru_RU',
                                                         LC_ALL='ru_RU', OUTPUT_CHARSET='CP1251'):
                    events = []
                    runner = Runner(dict(host='localhost', username='postgres', port=5432), events.append)
                    runner.env['PGLOCALEDIR'] = str(Path(clients['pg_restore']).parent.parent / 'share' / 'locale')
                    with self.assertRaises(RuntimeError):
                        runner.run([clients['pg_restore'], '--list', str(invalid)])
                    message = '\n'.join(item['text'] for item in events)
                    self.assertIn('ошибка', message)
                    self.assertIn('архив', message)
                    self.assertNotIn('\ufffd', message)

    def test_requires_all_three_tools_and_keeps_versions_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for version in ('14', '16', '17', '18', 'invalid'):
                folder = root / version / 'bin'
                folder.mkdir(parents=True)
                for name in ('psql', 'pg_dump', 'pg_restore'):
                    (folder / (name + '.exe')).write_bytes(b'test')
            self.assertEqual(set(bundled_tools(root)), {14, 16, 17, 18})
            (root / '17' / 'bin' / 'psql.exe').unlink()
            self.assertEqual(set(bundled_tools(root)), {14, 16, 18})
            self.assertEqual(bundled_tools(root / 'missing'), {})

    def test_server_version_is_read_from_metadata(self):
        self.assertEqual(server_major([dict(server_version=140003)]), 14)
        self.assertEqual(server_major([dict(server_version=160015)]), 16)
        self.assertEqual(server_major([dict(server_version='170011')]), 17)
        self.assertEqual(server_major([dict(server_version='180003')]), 18)
        for rows in ([], [{}], [dict(server_version='invalid')], [dict(server_version=None)]):
            self.assertIsNone(server_major(rows))


if __name__ == '__main__':
    unittest.main()
