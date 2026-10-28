import hashlib
import io
from pathlib import Path, PurePosixPath
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock

from dt_download import download_dt
from postgres_runner import OperationCancelled


class LocalSFTPClient:
    """A virtual Linux filesystem backed only by the test executor's own directory."""
    def __init__(self, root, corrupt=False, cancel=None):
        self.root, self.corrupt, self.cancel = Path(root), corrupt, cancel
        self.removed = []

    def open_sftp(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get_channel(self):
        return Mock()

    def resolve(self, path):
        relative = PurePosixPath(path).relative_to('/executor')
        if '..' in relative.parts:
            raise AssertionError('Unsafe fixture access')
        return self.root.joinpath(*relative.parts)

    def lstat(self, path):
        return self.resolve(path).lstat()

    def getfo(self, source, output, **kwargs):
        data = self.resolve(source).read_bytes()
        if self.corrupt:
            data = b'X' * len(data)
        for offset in range(0, len(data), 7):
            output.write(data[offset:offset + 7])
            if self.cancel is not None:
                self.cancel.set()
        return len(data)

    def remove(self, path):
        self.removed.append(path)
        self.resolve(path).unlink()

    def rmdir(self, path):
        self.removed.append(path)
        self.resolve(path).rmdir()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executor, self.console = self.root / 'executor', self.root / 'console'
        self.executor.mkdir()
        self.console.mkdir()
        self.relative = Path('ka-ibcmd-fixture') / 'One' / 'One_20261019_123456_123456_abcdef12.dt'
        self.source = self.executor / self.relative
        self.source.parent.mkdir(parents=True)
        self.data = b'FAKE_DT_CONTENT_FROM_LINUX_12345'
        self.source.write_bytes(self.data)
        self.result = dict(workspace='/executor/ka-ibcmd-fixture', path='/executor/' + self.relative.as_posix(),
                           size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        self.client = LocalSFTPClient(self.executor)
        self.events = []

    def test_verified_local_copy_is_published_before_own_remote_files_are_removed(self):
        foreign = self.executor / 'unrelated.dt'
        foreign.write_bytes(b'keep')
        result = download_dt(self.client, self.result, str(self.console), self.events.append)
        target = Path(result[0])
        self.assertEqual(target.parent, self.console / 'One')
        self.assertEqual(target.read_bytes(), self.data)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.source.parent.parent.exists())
        self.assertEqual(foreign.read_bytes(), b'keep')
        self.assertFalse(list(self.console.rglob('*.part')))
        self.assertEqual([event['path'] for event in self.events if event['kind'] == 'artifact'], [str(target)])
        self.assertEqual([event['percent'] for event in self.events if event['kind'] == 'progress'][-1], 100)

    def test_bad_sha256_never_publishes_or_deletes_the_source(self):
        self.client.corrupt = True
        with self.assertRaisesRegex(RuntimeError, 'SHA256'):
            download_dt(self.client, self.result, str(self.console), self.events.append)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertFalse(list(self.console.rglob('*.dt')))
        self.assertFalse(list(self.console.rglob('*.part')))
        self.assertEqual(self.client.removed, [])

    def test_cancellation_during_download_removes_partial_local_file_and_retains_remote_dt(self):
        cancel = threading.Event()
        self.client.cancel = cancel
        with self.assertRaisesRegex(OperationCancelled, 'DT на Linux сохранён'):
            download_dt(self.client, self.result, str(self.console), self.events.append, cancel)
        self.assertTrue(self.source.exists())
        self.assertFalse(list(self.console.rglob('*.dt')))
        self.assertFalse(list(self.console.rglob('*.part')))

    def test_existing_local_file_is_preserved(self):
        target = self.console / 'One' / self.source.name
        target.parent.mkdir()
        target.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            download_dt(self.client, self.result, str(self.console), self.events.append)
        self.assertEqual(target.read_bytes(), b'existing')
        self.assertTrue(self.source.exists())

    def test_unsafe_remote_paths_are_rejected_before_opening_sftp(self):
        for path in ('/executor/foreign/One/x.dt', '/executor/ka-ibcmd-fixture/../x.dt',
                     '/executor/ka-ibcmd-fixture/One/x:ads.dt', '/executor/ka-ibcmd-fixture/CON/x.dt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                download_dt(Mock(), dict(self.result, path=path), str(self.console), Mock())
        self.assertTrue(self.source.exists())

    def test_retention_applies_only_to_matching_local_dt_after_success(self):
        local = self.console / 'One'
        local.mkdir()
        old = local / 'One_20200101_010101_123456_123456ab.dt'
        old.write_bytes(b'old')
        os.utime(old, (1000000, 1000000))
        foreign = local / 'manual_copy.dt'
        foreign.write_bytes(b'keep')
        result = download_dt(self.client, self.result, str(self.console), self.events.append, retention_days=1)
        self.assertFalse(old.exists())
        self.assertEqual(foreign.read_bytes(), b'keep')
        self.assertTrue(Path(result[0]).exists())
