"""Regression for 14.11 bundle proposing libpq5 16.4 -> 14.11 downgrade."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock,patch

import postgres_install_linux as linux


ROW=dict(version='14.11-3.1C',architecture='x86_64')
PLAN='''Inst postgresql-common (278.pgdg20.04+1 repository [all])
Inst libpq5 [16.4-5.1C] (14.11-3.1C local-deb [amd64])
Inst postgresql-client-14 (14.11-3.1C local-deb [amd64])'''


class PackageTests(unittest.TestCase):
    def select(self,installed=None,arch='amd64'):
        versions=installed or {}
        packages=[Path('/tmp/'+name+'.deb') for name in ('libpq5','postgresql-client-14','postgresql-14')]
        def control(args,**kw):
            name=Path(args[2]).stem
            return 'Package: '+name+'\nVersion: 14.11-3.1C\nArchitecture: '+arch+'\n'
        def run(args,**kw):
            if args[0]=='dpkg-query':
                value=versions.get(args[-1].split(':')[0])
                return SimpleNamespace(returncode=0 if value else 1,stdout='install ok installed|'+value if value else '')
            self.assertEqual(args,['dpkg','--compare-versions','16.4-5.1C','gt','14.11-3.1C'])
            return SimpleNamespace(returncode=0)
        events=[]
        with patch.object(linux.subprocess,'check_output',side_effect=control),patch.object(linux.subprocess,'run',side_effect=run):
            result=linux.select_packages(packages,ROW,events.append)
        return result,events

    def test_newer_libpq_is_omitted_from_archive_install_and_server_is_kept(self):
        selected,events=self.select(dict(libpq5='16.4-5.1C'))
        self.assertEqual(selected,[str(Path('/tmp/postgresql-client-14.deb')),str(Path('/tmp/postgresql-14.deb'))])
        self.assertIn('libpq5 16.4-5.1C',events[0]['text'])

    def test_clean_system_still_receives_libpq_from_official_archive(self):
        selected,events=self.select()
        self.assertEqual(len(selected),3);self.assertTrue(any('libpq5' in p for p in selected))

    def test_equal_packages_are_not_reinstalled_and_different_server_is_rejected(self):
        selected,_=self.select({name:'14.11-3.1C' for name in ('libpq5','postgresql-client-14','postgresql-14')})
        self.assertEqual(selected,[])
        with self.assertRaisesRegex(ValueError,'Другая сборка'):self.select({'postgresql-14':'14.13-1.1C'})

    def test_wrong_package_architecture_stops_before_querying_or_changing_system(self):
        with self.assertRaisesRegex(ValueError,'Архитектура'):self.select(arch='arm64')

    def test_downgrade_in_apt_simulation_stops_before_real_install(self):
        calls=[]
        def execute(args,*a,**kw):calls.append(args);return PLAN if '-s' in args else ''
        with patch.object(linux,'execute',side_effect=execute),patch.object(linux,'suppress_default',return_value=nullcontext()),\
             patch.object(linux.subprocess,'run',return_value=SimpleNamespace(returncode=0)),self.assertRaisesRegex(ValueError,'libpq5.*16.4.*14.11'):
            linux.install_debs(['/tmp/server.deb'],Mock(),threading.Event(),{'LC_ALL':'C.UTF-8'})
        self.assertEqual(len(calls),2);self.assertFalse(any('-y' in args for args in calls))

    def test_valid_plan_installs_with_removal_and_forced_downgrade_disabled(self):
        calls=[]
        def execute(args,*a,**kw):calls.append(args);return 'Inst postgresql-14 (14.11-3.1C local-deb [amd64])'
        with patch.object(linux,'execute',side_effect=execute),patch.object(linux,'suppress_default',return_value=nullcontext()):
            linux.install_debs(['/tmp/server.deb'],Mock(),threading.Event(),{})
        self.assertEqual(len(calls),5);self.assertIn('--no-remove',calls[-1]);self.assertNotIn('--allow-downgrades',calls[-1])
        self.assertIn('--download-only',calls[2]);self.assertNotIn('--no-download',calls[-1])
        self.assertIn('APT::Get::Download=true',calls[-1]);self.assertIn('/tmp/server.deb',calls[-1])

    def test_removal_or_unreadable_comparison_blocks_plan(self):
        with self.assertRaisesRegex(ValueError,'удаления'):linux.check_apt_plan('Remv libpq5 [16.4-5.1C]')
        with patch.object(linux.subprocess,'run',return_value=SimpleNamespace(returncode=2)),self.assertRaisesRegex(ValueError,'сравнить'):
            linux.check_apt_plan(PLAN)


if __name__=='__main__':unittest.main()
