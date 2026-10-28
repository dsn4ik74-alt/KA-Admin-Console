"""Read-only disk analysis on a controlled filesystem; no SSH connection."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import linux_disk_scan as scan
from ssh_job_runner import Cancelled, cleanup


class DiskTests(unittest.TestCase):
    def test_progress_is_reported_inside_a_large_flat_directory(self):
        from contextlib import nullcontext
        from itertools import count
        import stat
        info=SimpleNamespace(st_mode=stat.S_IFREG,st_size=1,st_blocks=1,st_nlink=1,st_dev=1,st_ino=2)
        root=SimpleNamespace(st_mode=stat.S_IFDIR,st_dev=1,st_ino=1)
        entries=[SimpleNamespace(name=str(i)+'.txt',path='/data/'+str(i)+'.txt',stat=lambda **kw:info) for i in range(1024)]
        remote=SimpleNamespace(lstat=lambda path:root,scandir=lambda path:nullcontext(iter(entries)),
            statvfs=lambda p:SimpleNamespace(f_blocks=1,f_frsize=4096,f_bavail=1))
        events=[]
        with patch.object(scan,'os',remote),patch.object(scan.time,'monotonic',side_effect=count(0,2)):
            result=scan.run(dict(path='/data'),events.append,threading.Event())
        self.assertEqual(result['files'],1024)
        self.assertGreaterEqual(len([item for item in events if item['kind']=='stage']),2)

    def scanner_os(self,root):
        @contextmanager
        def scandir(path):
            with os.scandir(root if path=='/data' else path) as source:
                rows=[]
                for entry in source:
                    def info(follow_symlinks=False,entry=entry):
                        real=os.stat(entry.path,follow_symlinks=follow_symlinks)
                        return SimpleNamespace(st_mode=real.st_mode,st_size=real.st_size,st_blocks=(real.st_size+511)//512,
                            st_nlink=real.st_nlink,st_ino=real.st_ino,st_dev=real.st_dev)
                    rows.append(SimpleNamespace(path=entry.path.replace('\\','/'),name=entry.name,stat=info))
                yield rows
        return SimpleNamespace(scandir=scandir,lstat=lambda p:os.lstat(root if p=='/data' else p),statvfs=lambda p:SimpleNamespace(f_blocks=100,f_frsize=4096,f_bavail=40))

    def test_directory_totals_largest_extensions_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'folder').mkdir();(root/'folder'/'large.DAT').write_bytes(b'x'*1100)
            (root/'small.txt').write_bytes(b'x'*10);os.link(root/'small.txt',root/'small-link.txt')
            before={str(p):p.stat().st_mtime_ns for p in root.rglob('*')}
            with patch.object(scan,'os',self.scanner_os(root)):result=scan.run(dict(path='/data'),Mock(),threading.Event())
            self.assertEqual(result['files'],2);self.assertEqual(result['size'],2048)
            self.assertEqual(result['children'][0]['name'],'folder');self.assertEqual(result['largest'][0]['apparent'],1100)
            self.assertEqual(result['extensions'][0]['extension'],'.dat');self.assertEqual(result['available'],163840)
            self.assertEqual(before,{str(p):p.stat().st_mtime_ns for p in root.rglob('*')})

    def test_cancel_stops_walk_without_result_or_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            cancel=threading.Event();cancel.set()
            with patch.object(scan,'os',self.scanner_os(directory)),self.assertRaises(Cancelled):scan.run(dict(path='/data'),Mock(),cancel)

    def test_ssh_worker_refuses_cleanup_of_arbitrary_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'keep.txt').write_text('keep')
            with self.assertRaises(ValueError):cleanup(path,'0'*32)
            self.assertTrue((path/'keep.txt').exists())


if __name__=='__main__':unittest.main()
