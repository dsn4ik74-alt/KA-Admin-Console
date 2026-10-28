"""Deletion policy and fd-based calls; no actual files or services are removed."""
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import linux_disk_delete as task
from ssh_job_runner import Cancelled


IDENTITY=dict(device=1,inode=17,mode=stat.S_IFREG|0o600,mtime_ns=123456,size=512)


class DeleteTests(unittest.TestCase):
    def execute(self,*,directory=False,changed=False,recursive=True,cancel=None,mounted=False):
        expected=dict(IDENTITY,mode=stat.S_IFDIR|0o700) if directory else dict(IDENTITY)
        info=SimpleNamespace(st_dev=expected['device'],st_ino=18 if changed else expected['inode'],st_mode=expected['mode'],st_mtime_ns=expected['mtime_ns'],st_size=expected['size'])
        remote=SimpleNamespace(stat=Mock(return_value=info),unlink=Mock(),close=Mock())
        rmtree=Mock();rmtree.avoids_symlink_attacks=True
        mounts=Mock(side_effect=ValueError('точка монтирования') if mounted else None)
        events=[]
        with patch.object(task,'os',remote),patch.object(task,'open_parent',return_value=42),\
             patch.object(task.shutil,'rmtree',rmtree),patch.object(task,'reject_mounts',mounts):
            try:result=task.run(dict(root='/data',path='/data/item',identity=expected,recursive=recursive),events.append,cancel or threading.Event())
            except BaseException:
                remote.unlink.assert_not_called();rmtree.assert_not_called();raise
        remote.close.assert_called_once_with(42)
        return result,remote,rmtree,mounts,events

    def test_file_is_unlinked_relative_to_checked_parent_and_not_through_shell(self):
        result,remote,rmtree,mounts,events=self.execute()
        remote.stat.assert_called_once_with('item',dir_fd=42,follow_symlinks=False)
        remote.unlink.assert_called_once_with('item',dir_fd=42);rmtree.assert_not_called()
        self.assertEqual(result['deleted'],'/data/item');self.assertFalse(result['directory'])

    def test_directory_requires_recursive_confirmation_and_uses_safe_rmtree(self):
        with self.assertRaisesRegex(ValueError,'подтвердить'):self.execute(directory=True,recursive=False)
        result,remote,rmtree,mounts,events=self.execute(directory=True)
        rmtree.assert_called_once_with('item',dir_fd=42);mounts.assert_called_once_with('/data/item')
        self.assertTrue(result['directory']);self.assertFalse(events[-1]['cancellable'])

    def test_replaced_object_or_nested_mount_is_not_deleted(self):
        with self.assertRaisesRegex(ValueError,'изменился'):self.execute(changed=True)
        with self.assertRaisesRegex(ValueError,'монтирования'):self.execute(directory=True,mounted=True)

    def test_cancel_before_mutation_never_unlinks_anything(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(Cancelled):self.execute(cancel=cancel)

    def test_scope_rejects_root_traversal_prefix_alias_and_system_roots(self):
        for root,path in (('/data','/data'),('/data','/data/../outside'),('/data','/data2/file'),('/','/etc'),('//','//etc'),('/data','relative')):
            with self.subTest(root=root,path=path),self.assertRaises(ValueError):task.validate_paths(root,path)
        self.assertEqual(task.validate_paths('/data/','/data/folder/file'),('/data','/data/folder/file'))

    def test_bind_mounts_with_spaces_are_also_rejected(self):
        mountinfo='12 1 1:1 / /data/folder/a\\040b rw - ext4 /dev/sda rw\n'
        with patch.object(task,'Path') as path:
            path.return_value.read_text.return_value=mountinfo
            with self.assertRaisesRegex(ValueError,'a b'):task.reject_mounts('/data/folder')
            task.reject_mounts('/data/other')

    def test_parent_walk_never_follows_links_and_closes_open_fds_on_failure(self):
        remote=SimpleNamespace(O_RDONLY=0,O_DIRECTORY=1,O_NOFOLLOW=2,open=Mock(side_effect=[10,11,OSError('link')]),close=Mock())
        with patch.object(task,'os',remote),self.assertRaises(OSError):task.open_parent('/data/folder/file')
        self.assertEqual(remote.open.call_args_list[1].kwargs,dict(dir_fd=10))
        self.assertTrue(all(call.args[1]&remote.O_NOFOLLOW for call in remote.open.call_args_list))
        self.assertEqual([call.args[0] for call in remote.close.call_args_list],[10,11])


if __name__=='__main__':unittest.main()
