"""Template setup protocol regressions; no real setup or user profile changes."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import configuration_install as backend
from test_configuration_install import DISTRIBUTION, MFT, PROJECT, write_templates


class SetupTests(unittest.TestCase):
    def test_native_utf16_reader_gets_private_path_and_existing_settings_then_bytes_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, destination = root / '1cestart.cfg', root / 'Шаблоны с пробелами'
            original = 'ConfigurationTemplatesLocation=C:\\Готовые шаблоны\r\nDefaultVersion=8.3\r\n'
            for encoding in ('utf-16', 'utf-8-sig', 'cp1251'):
                with self.subTest(encoding=encoding):
                    before = original.encode(encoding)
                    config.write_bytes(before)
                    with backend.isolated_template_destination(destination, threading.Event(), config) as selected:
                        self.assertEqual(selected, config)
                        raw = config.read_bytes()
                        self.assertEqual(raw[:2], b'\xff\xfe')
                        text = raw[2:].decode('utf-16-le')
                        self.assertTrue(text.startswith('ConfigurationTemplatesLocation=' + str(destination) + '\r\n'))
                        self.assertTrue(text.endswith(original))
                    self.assertEqual(config.read_bytes(), before)

    def test_concurrent_native_config_edit_keeps_its_encoding_and_new_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / '1cestart.cfg'
            with backend.isolated_template_destination(root / 'private', threading.Event(), config):
                config.write_text(backend.text_file(config) + 'DefaultVersion=8.5\r\n', encoding='utf-16', newline='')
            self.assertEqual(config.read_bytes()[:2], b'\xff\xfe')
            self.assertEqual(config.read_bytes()[2:].decode('utf-16-le'), 'DefaultVersion=8.5\r\n')

    def test_only_actual_cfu_files_are_diagnosed_as_update_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (('empty', None, 'мог использовать другой каталог'),
                     ('update', '1cv8.cfu', 'Найдены только файлы обновления CFU'),
                     ('unregistered', '1cv8.cf', 'манифест MFT не подтверждает'))
            for name, filename, expected in cases:
                with self.subTest(case=name):
                    folder = root / name
                    folder.mkdir()
                    if filename:
                        (folder / filename).write_bytes(b'fixture')
                    events = []
                    with self.assertRaises(RuntimeError) as error:
                        backend.installed_templates(folder, PROJECT['version'], events.append)
                    self.assertIn(expected, str(error.exception))
                    self.assertIn(str(folder), str(error.exception))
                    self.assertTrue(events)
                    if filename != '1cv8.cfu':
                        self.assertNotIn('Найдены только файлы обновления', str(error.exception))

    def test_wrong_manifest_version_is_reported_and_never_substituted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = write_templates(root)
            (folder / '1cv8.mft').write_text(MFT.replace(PROJECT['version'], '3.1.1.1'), encoding='utf-16')
            with self.assertRaisesRegex(RuntimeError, 'других версий: 3.1.1.1'):
                backend.installed_templates(root, PROJECT['version'], Mock())

    def test_failed_setup_logs_actual_directory_without_exposing_other_config_and_cleans_owned_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, downloads, existing = root / '1cestart.cfg', root / 'downloads', root / 'user-templates'
            existing.mkdir()
            keep = existing / 'keep.cf'
            keep.write_bytes(b'existing user file')
            original = ('ConfigurationTemplatesLocation=' + str(existing) + '\r\nOtherSetting=private-value\r\n').encode('utf-16')
            config.write_bytes(original)
            portal = Mock()
            def download(distribution, folder, event):
                archive = Path(folder) / 'full.zip'
                archive.write_bytes(b'fixture')
                return archive
            def unpack(archive, event, cancel):
                folder = Path(archive).parent / 'unpacked'
                folder.mkdir()
                (folder / 'setup.exe').write_bytes(b'fixture')
                return folder
            portal.download.side_effect = download
            events = []
            with patch.object(backend, 'launcher_config_path', return_value=config), \
                 patch.object(backend, 'unpack_distribution', side_effect=unpack), \
                 patch.object(backend, 'run_native') as native:
                with self.assertRaisesRegex(RuntimeError, 'Установка шаблонов не подтверждена'):
                    backend.install_configuration(portal, DISTRIBUTION, downloads, events.append, threading.Event())
            native.assert_called_once()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(keep.read_bytes(), b'existing user file')
            self.assertFalse(downloads.exists())
            log = '\n'.join(event.get('text', '') for event in events)
            self.assertIn('UTF-16LE', log)
            self.assertIn(str(existing), log)
            self.assertNotIn('private-value', log)


if __name__ == '__main__':
    unittest.main()
