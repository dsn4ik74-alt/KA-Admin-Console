import os
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from archive_jr import archive_logs


@unittest.skipUnless(os.name == 'nt', 'Windows event-log archiver')
class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Only generated fixtures are ever eligible for deletion in these tests.
        self.temp = tempfile.TemporaryDirectory(prefix='console_jr_')
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / '1Cv8Log'
        self.source.mkdir()
        self.destination = Path(self.temp.name) / 'archives'
        for name in ('20200101000000.lgp', '20200101000000.lgx', '1Cv8.lgf', '20261012000000.lgp'):
            (self.source / name).write_text(name + '\nданные ЖР', encoding='utf-8')
        old = time.time() - 365 * 86400
        for name in ('20200101000000.lgp', '20200101000000.lgx'):
            os.utime(self.source / name, (old, old))

    def run_archive(self, delete):
        self.events = []
        archive_logs(str(self.source), str(self.destination), 'TestBase', '1', delete, self.events.append)

    def test_real_zip_then_verified_deletion_preserves_dictionary_and_recent_files(self):
        self.run_archive(True)
        archives = list(self.destination.rglob('*.zip'))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            self.assertEqual(set(archive.namelist()), {'20200101000000.lgp', '20200101000000.lgx', '1Cv8.lgf'})
            self.assertIn('данные ЖР', archive.read('20200101000000.lgp').decode('utf-8'))
        self.assertFalse((self.source / '20200101000000.lgp').exists())
        self.assertFalse((self.source / '20200101000000.lgx').exists())
        self.assertTrue((self.source / '1Cv8.lgf').exists())
        self.assertTrue((self.source / '20261012000000.lgp').exists())
        self.assertTrue(any('удалено LGP/LGX: 2' in event.get('text', '') for event in self.events))

    def test_archive_only_and_existing_zip_mismatch_prevents_deletion(self):
        self.run_archive(False)
        old_file = self.source / '20200101000000.lgp'
        self.assertTrue(old_file.exists())
        before = old_file.stat().st_mtime
        old_file.write_text('changed file', encoding='utf-8')
        os.utime(old_file, (before, before))
        with self.assertRaises(RuntimeError):
            self.run_archive(True)
        self.assertTrue(old_file.exists())
        self.assertTrue((self.source / '20200101000000.lgx').exists())

    def test_invalid_destination_missing_dictionary_and_sqlite_are_rejected(self):
        with self.assertRaises(ValueError):
            archive_logs(str(self.source), str(self.source / 'inside'), '', 1, True)
        (self.source / '1Cv8.lgf').unlink()
        with self.assertRaises(RuntimeError):
            self.run_archive(True)
        self.assertTrue((self.source / '20200101000000.lgp').exists())
        (self.source / 'journal.lgd').write_bytes(b'sqlite fixture')
        with self.assertRaises(RuntimeError):
            self.run_archive(True)
        self.assertEqual(list(self.destination.rglob('*.zip')), [])


if __name__ == '__main__':
    unittest.main()
