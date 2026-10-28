"""Portable runtime integrity and compatibility, with no remote execution."""
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from linux_python_runtime import runtime_for
from linux_platform_runner import inside
import linux_platform


class RuntimeTests(unittest.TestCase):
    def test_both_bundled_runtimes_match_sha_and_have_elf_interpreter_and_standard_library(self):
        for machine in ('x86_64', 'aarch64'):
            entry = runtime_for(machine)
            with tarfile.open(entry['path']) as archive:
                for member in archive.getmembers():
                    path = PurePosixPath(member.name)
                    self.assertFalse(path.is_absolute())
                    self.assertNotIn('..', path.parts)
                    self.assertEqual(path.parts[0], 'python')
                    if member.issym():
                        target = posixpath.normpath(posixpath.join(str(path.parent), member.linkname))
                        self.assertTrue(target.startswith('python/'), target)
                interpreter = archive.extractfile('python/bin/python3.12').read()
                self.assertEqual(interpreter[:4], b'\x7fELF')
                self.assertEqual(int.from_bytes(interpreter[18:20], 'little'), 62 if machine == 'x86_64' else 183)
                self.assertIsNotNone(archive.getmember('python/lib/python3.12/json/__init__.py'))
                self.assertIsNotNone(archive.getmember('python/lib/python3.12/LICENSE.txt'))
            self.assertEqual(entry['version'], '3.12.15')
        with self.assertRaisesRegex(ValueError, 'x64 и ARM64'):
            runtime_for('i686')

    def test_modified_runtime_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'vendor' / 'python-linux'
            root.mkdir(parents=True)
            (root / 'runtime.tar.gz').write_bytes(b'changed')
            (root / 'manifest.json').write_text(json.dumps(dict(x86_64=dict(file='runtime.tar.gz', sha256='0' * 64))))
            with patch('linux_python_runtime.sys._MEIPASS', directory, create=True), self.assertRaisesRegex(RuntimeError, 'Повреждён'):
                runtime_for('x86_64')

    def test_runner_path_checks_work_without_python39_is_relative_to(self):
        class OldPath:
            def __init__(self, text):
                self.value = PurePosixPath(text)
            def resolve(self):
                return self
            def relative_to(self, other):
                return self.value.relative_to(other.value)
        self.assertTrue(inside(OldPath('/tmp/ka-owned/file'), OldPath('/tmp/ka-owned')))
        self.assertFalse(inside(OldPath('/tmp/other/file'), OldPath('/tmp/ka-owned')))
        source = Path('linux_platform_runner.py').read_text(encoding='utf-8')
        ast.parse(source, feature_version=(3, 8))
        self.assertNotIn('.is_relative_to(', source)

    def test_archive_name_alone_does_not_predict_available_components(self):
        components = linux_platform.distribution_features(dict(filename='deb64_8_3_27_2342.tar.gz'))
        self.assertIsNone(components)
