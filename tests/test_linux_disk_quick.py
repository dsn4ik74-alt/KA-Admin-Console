"""GNU du stream parsing, partial reads, filesystem limits and cancellation."""
import os
import stat
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import linux_disk_scan as scan
from ssh_job_runner import Cancelled


class QuickTests(unittest.TestCase):
    def execute(self,output,code=0,errors=b'',cross=False,cancel_during=False):
        cancel=threading.Event();events=[]
        streams=[Mock(fileno=lambda:10),Mock(fileno=lambda:11)]
        chunks={10:[output[:13],output[13:],b''],11:[errors,b''] if errors else [b'']}
        process=Mock(stdout=streams[0],stderr=streams[1],pid=12345);process.poll.return_value=None
        def wait():process.poll.return_value=code;return code
        process.wait.side_effect=wait
        registered={}
        poll=Mock();poll.register.side_effect=lambda stream,flags,data:registered.update({stream:SimpleNamespace(fileobj=stream,data=data)})
        poll.get_map.side_effect=lambda:registered
        poll.unregister.side_effect=lambda stream:registered.pop(stream)
        def select(timeout):
            if cancel_during:cancel.set();return []
            return [(key,1) for key in list(registered.values())]
        poll.select.side_effect=select
        def lstat(path):return SimpleNamespace(st_mode=stat.S_IFDIR if path=='/data/folder\nwith\ttabs' else stat.S_IFREG)
        remote=SimpleNamespace(environ=os.environ,read=lambda fd,size:chunks[fd].pop(0),fsdecode=os.fsdecode,lstat=Mock(side_effect=lstat),
                               statvfs=lambda p:SimpleNamespace(f_blocks=100,f_frsize=4096,f_bavail=40),killpg=Mock())
        with patch.object(scan,'os',remote),patch.object(scan,'signal',SimpleNamespace(SIGKILL=9)),patch.object(scan.selectors,'DefaultSelector',return_value=poll),\
             patch.object(scan.subprocess,'Popen',return_value=process) as spawn:
            try:result=scan.quick_scan('/data',cross,events.append,cancel)
            except Cancelled:
                remote.killpg.assert_called_once_with(12345,scan.signal.SIGKILL);process.wait.assert_called_once()
                self.assertTrue(all(stream.close.called for stream in streams));raise
        self.assertTrue(all(stream.close.called for stream in streams));remote.killpg.assert_not_called()
        return result,events,spawn.call_args.args[0],remote

    def test_null_records_preserve_newlines_tabs_and_bytes_are_allocated_size(self):
        result,events,args,remote=self.execute(b'8192\t/data/folder\nwith\ttabs\0' b'4096\t/data/file\0' b'16384\t/data\0')
        self.assertEqual(result['size'],16384);self.assertEqual(result['children'][0]['name'],'folder\nwith\ttabs')
        self.assertTrue(result['children'][0]['directory']);self.assertEqual(result['files'],None)
        self.assertEqual(result['largest'],[]);self.assertEqual(result['extensions'],[])
        self.assertEqual(result['available'],163840)
        for option in ('--null','--block-size=1','--no-dereference','--max-depth=1','--one-file-system'):self.assertIn(option,args)

    def test_cross_filesystem_option_is_respected(self):
        result,events,args,remote=self.execute(b'0\t/data\0',cross=True)
        self.assertNotIn('--one-file-system',args)

    def test_permission_failure_keeps_partial_totals_and_visible_warning(self):
        result,events,args,remote=self.execute(b'4096\t/data/file\0' b'8192\t/data\0',code=1,errors=b'du: Permission denied\n')
        self.assertTrue(result['partial']);self.assertEqual(result['size'],8192)
        self.assertTrue(any(e['kind']=='warning' and 'Permission denied' in e['text'] for e in events))

    def test_missing_root_total_or_unsupported_du_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError,'GNU du'):
            self.execute(b'',code=1,errors=b'du: unrecognized option\n')

    def test_cancel_stops_the_own_process_group_and_closes_both_streams(self):
        with self.assertRaises(Cancelled):self.execute(b'0\t/data\0',cancel_during=True)

    def test_many_root_files_are_bounded_and_others_still_count(self):
        output=b''.join(('512\t/data/f'+str(i)+'\0').encode() for i in range(5002))+b'2561024\t/data\0'
        result,events,args,remote=self.execute(output)
        self.assertEqual(len(result['children']),5001)
        other=next(row for row in result['children'] if row['name']=='Остальные элементы')
        self.assertEqual(other['size'],1024);self.assertEqual(remote.lstat.call_count,5000)


if __name__=='__main__':unittest.main()
