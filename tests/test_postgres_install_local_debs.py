"""APT 2.0 requires normal file acquisition for local DEBs after prefetch."""
from contextlib import nullcontext
from pathlib import PurePosixPath
import threading
import unittest
from unittest.mock import patch

import postgres_install_linux as linux


PATH_ERROR="E: Internal Error, Pathname to install is not absolute 'postgresql-client-14_14.13-2.1C_amd64.deb'"
FILES=['/tmp/ka-job-example/unpacked/archive with spaces/postgresql-client-14_14.13-2.1C_amd64.deb',
       '/tmp/ka-job-example/unpacked/archive with spaces/postgresql-14_14.13-2.1C_amd64.deb']


class LocalDebTests(unittest.TestCase):
    def run_apt(self,packages,download_default=True):
        calls=[];dpkg_files=[];cache=set();cancel=threading.Event()
        def execute(args,event,flag,**kwargs):
            if flag.is_set():raise linux.Cancelled('cancelled')
            calls.append((list(args),kwargs))
            if 'install' not in args:return ''
            wanted=args[args.index('install')+1:]
            if '-s' in args:return 'Inst postgresql-14 (14.13-2.1C local-deb [amd64])'
            if '--download-only' in args:
                cache.update(wanted);return 'Download complete and in download only mode'
            self.assertTrue(set(wanted)<=cache,'Package installation started before prefetch completed')
            local=[name for name in wanted if name.endswith('.deb')]
            allowed=download_default
            for arg in args:
                if arg=='--no-download' or arg=='APT::Get::Download=false':allowed=False
                elif arg=='APT::Get::Download=true':allowed=True
            # In APT 2.0 the local item is resolved to its absolute filename by
            # file acquisition. Prefetch in a different process does not do it.
            filenames=local if allowed else [PurePosixPath(name).name for name in local]
            if any(not PurePosixPath(name).is_absolute() for name in filenames):raise RuntimeError(PATH_ERROR)
            dpkg_files.extend(filenames)
            return ''
        with patch.object(linux,'execute',side_effect=execute),patch.object(linux,'suppress_default',return_value=nullcontext()):
            linux.install_apt_packages(packages,lambda item:None,cancel,{})
        return calls,dpkg_files

    def test_local_14_13_debs_reach_dpkg_as_absolute_paths_after_download_only(self):
        calls,files=self.run_apt([*FILES,'locales'])
        self.assertEqual(files,FILES)
        installing=[args for args,kw in calls if kw.get('critical')]
        self.assertEqual(len(installing),1)
        self.assertNotIn('--no-download',installing[0])
        self.assertIn('--no-remove',installing[0])
        self.assertIn('Dpkg::Options::=--force-confold',installing[0])

    def test_local_acquisition_also_overrides_a_global_no_download_setting(self):
        calls,files=self.run_apt([*FILES,'locales'],download_default=False)
        self.assertEqual(files,FILES)
        self.assertTrue(any('APT::Get::Download=true' in args for args,kw in calls if kw.get('critical')))

    def test_repository_only_libraries_still_install_from_prefetched_cache(self):
        calls,files=self.run_apt(['libicu66','locales'])
        self.assertEqual(files,[])
        installing=[args for args,kw in calls if kw.get('critical')]
        self.assertEqual(len(installing),1);self.assertIn('--no-download',installing[0])

    def test_pathname_failure_is_reproduced_when_local_acquisition_is_disabled(self):
        # Applying the previous release's options reproduces the user's failure
        # even though the command-line DEB paths themselves are absolute.
        with patch.object(linux,'APT_NETWORK_OPTIONS',('--no-download',)):
            with self.assertRaisesRegex(RuntimeError,'Pathname to install is not absolute'):self.run_apt(FILES)


if __name__=='__main__':unittest.main()
