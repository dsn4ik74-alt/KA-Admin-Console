"""Network failures must stop before dpkg or instance initialization."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import postgres_install_linux as linux


PLAN='Inst postgresql-14 (14.13-1.1C local-deb [amd64])\n'
DNS="E: Failed to fetch https://apt-archive.postgresql.org/package.deb  Temporary failure resolving 'apt-archive.postgresql.org'"


class NetworkTests(unittest.TestCase):
    @contextmanager
    def installer(self,behavior):
        with tempfile.TemporaryDirectory() as directory:
            policy=Path(directory)/'createcluster.conf'
            original=b'# administrator policy\ncreate_main_cluster = true\n'
            policy.write_bytes(original)
            cancel=threading.Event();calls=[];events=[]
            suppress=linux.suppress_default
            def execute(args,event,flag,**kwargs):
                if flag.is_set():raise linux.Cancelled('cancelled')
                calls.append((list(args),kwargs))
                if kwargs.get('critical'):
                    self.assertIn('create_main_cluster = false',policy.read_text())
                    self.assertTrue(kwargs.get('critical'))
                else:
                    self.assertEqual(policy.read_bytes(),original)
                    self.assertFalse(kwargs.get('critical',False))
                return behavior(args,event,flag)
            with patch.object(linux,'execute',side_effect=execute),\
                 patch.object(linux,'suppress_default',side_effect=lambda:suppress(policy)):
                try:
                    yield SimpleNamespace(run=lambda:linux.install_debs(['/tmp/server.deb'],events.append,cancel,{'LC_ALL':'C.UTF-8'}),
                                          calls=calls,events=events,cancel=cancel)
                finally:self.assertEqual(policy.read_bytes(),original)

    def assert_no_install(self,state):
        self.assertFalse(any(kw.get('critical') for args,kw in state.calls))
        self.assertFalse(any(e.get('cancellable') is False for e in state.events))

    def test_dns_failure_keeps_full_output_and_stops_before_installation(self):
        def behavior(args,event,cancel):
            if '--download-only' in args:
                event(dict(kind='message',text=DNS))
                # The command tail can omit the earlier useful diagnostic.
                raise RuntimeError('apt-get: код 100. E: Unable to fetch some archives')
            return PLAN if '-s' in args else ''
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(RuntimeError,'DNS на ВМ.*apt-archive.postgresql.org') as error:state.run()
            self.assertIn('Установка пакетов и создание экземпляра не начаты',str(error.exception))
            self.assert_no_install(state)
            self.assertIn(dict(kind='message',text=DNS),state.events)
            self.assertEqual(len(state.calls),3)

    def test_update_zero_exit_with_stale_indexes_is_not_treated_as_success(self):
        def behavior(args,event,cancel):
            return "W: Failed to fetch https://apt-archive.postgresql.org/InRelease  Temporary failure resolving 'apt-archive.postgresql.org'\nW: Some index files failed to download. They have been ignored, or old ones used instead."
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(RuntimeError,'обновить списки пакетов.*DNS на ВМ'):state.run()
            self.assertEqual(len(state.calls),1);self.assert_no_install(state)

    def test_native_retry_can_recover_and_install_runs_once_with_cached_dependencies(self):
        def behavior(args,event,cancel):
            if '--download-only' in args:
                for line in ('Ign:1 https://repository package','Get:1 https://repository package [421 kB]','Download complete and in download only mode'):
                    event(dict(kind='message',text=line))
            return PLAN if '-s' in args else ''
        with self.installer(behavior) as state:
            state.run()
            self.assertEqual(len([1 for _,kw in state.calls if kw.get('critical')]),1)
            for args,_ in state.calls:
                if 'update' in args or '--download-only' in args:
                    for setting in ('Acquire::Retries=3','Acquire::http::Timeout=30','Acquire::https::Timeout=30'):
                        self.assertIn(setting,args)
                self.assertNotIn('--fix-missing',args);self.assertNotIn('--allow-downgrades',args)
                self.assertNotIn('--allow-unauthenticated',args)
            self.assertTrue(any(e.get('cancellable') is True for e in state.events))

    def test_timeout_is_explained_as_vm_network_and_not_as_ssh_failure(self):
        def behavior(args,event,cancel):raise RuntimeError('apt-get: код 100. Could not connect to repository: Connection timed out')
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(RuntimeError,'Репозиторий недоступен с Linux-ВМ') as error:state.run()
            self.assertNotIn('DNS',str(error.exception));self.assert_no_install(state)

    def test_other_apt_errors_retain_cause_without_repeated_transactions(self):
        for message in ('E: Failed to fetch https://repository/package  404 Not Found',
                        'E: The repository is not signed.',
                        'E: Could not get lock /var/lib/apt/lists/lock'):
            def fail(*args):raise RuntimeError(message)
            with self.subTest(message=message),self.installer(fail) as state:
                with self.assertRaises(RuntimeError) as error:state.run()
                self.assertIn(message,str(error.exception));self.assertNotIn('DNS на ВМ',str(error.exception))
                self.assertEqual(len(state.calls),1);self.assert_no_install(state)

    def test_download_cancellation_is_preserved_and_never_starts_dpkg(self):
        def behavior(args,event,cancel):
            if '--download-only' in args:
                cancel.set();raise linux.Cancelled('Загрузка отменена')
            return PLAN if '-s' in args else ''
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(linux.Cancelled,'Загрузка отменена'):state.run()
            self.assert_no_install(state)

    def test_changed_plan_after_download_is_rechecked_before_applying(self):
        simulations=[]
        def behavior(args,event,cancel):
            if '-s' in args:
                simulations.append(args)
                return PLAN if len(simulations)==1 else 'Remv libpq5 [16.4-5.1C]'
            return ''
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(ValueError,'удаления'):state.run()
            self.assertTrue(any('--download-only' in args for args,_ in state.calls));self.assert_no_install(state)

    def test_actual_package_configuration_failure_is_not_retried_or_misreported_as_network(self):
        def behavior(args,event,cancel):
            if 'APT::Get::Download=true' in args:raise RuntimeError('dpkg: configuration failed')
            return PLAN if '-s' in args else ''
        with self.installer(behavior) as state:
            with self.assertRaisesRegex(RuntimeError,'^dpkg: configuration failed$'):state.run()
            self.assertEqual(len([1 for _,kw in state.calls if kw.get('critical')]),1)


if __name__=='__main__':unittest.main()
