import contextlib
import io
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import console_core as core
import local_ras


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.manager = local_ras.LocalRasManager()
        self.addCleanup(self.manager.close)
        self.process = MagicMock()
        self.process.poll.return_value = None
        self.process.wait.side_effect = self.exited
        patcher = patch.object(local_ras.subprocess, "Popen", return_value=self.process)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(self.manager, "_reserve_port", side_effect=self.reserve)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reserve(self):
        self.manager._check_cancelled()
        self.manager._ports.add(49152)
        return 49152

    def exited(self, timeout):
        self.process.poll.return_value = 0
        return 0

    def test_launch_arguments_hidden_process_and_cleanup(self):
        with patch.object(self.manager, "_wait_ready"):
            with self.manager.connect(r"C:\1C platform\ras.exe", "server:2540") as endpoint:
                self.assertEqual(endpoint, "127.0.0.1:49152")
                self.assertEqual(self.popen.call_args.args[0],
                                 [r"C:\1C platform\ras.exe", "cluster", "--port=49152", "server:2540"])
                self.assertEqual(self.popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                self.assertNotIn("--service", self.popen.call_args.args[0])
            self.process.terminate.assert_called_once()
            self.process.wait.assert_called_once()
        self.assertEqual(self.manager._processes, set())
        self.assertEqual(self.manager._ports, set())

    def test_query_exception_still_stops_child(self):
        with patch.object(self.manager, "_wait_ready"):
            with self.assertRaisesRegex(RuntimeError, "RAC failed"):
                with self.manager.connect("ras.exe", "server:1540"):
                    raise RuntimeError("RAC failed")
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once()

    def test_startup_failure_stops_child(self):
        with patch.object(self.manager, "_wait_ready", side_effect=RuntimeError("startup failed")):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                with self.manager.connect("ras.exe", "server:1540"):
                    self.fail("Must not run a query before RAS is ready")
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once()
        self.assertEqual(self.manager._ports, set())

    def test_failed_spawn_releases_reserved_port(self):
        self.popen.side_effect = OSError("failed")
        with self.assertRaises(OSError):
            with self.manager.connect("ras.exe", "server:1540"):
                self.fail("Spawn failed")
        self.assertEqual(self.manager._ports, set())
        self.assertEqual(self.manager._processes, set())

    def test_close_stops_active_child_and_prevents_late_spawn(self):
        with patch.object(self.manager, "_wait_ready"):
            with self.manager.connect("ras.exe", "server:1540"):
                self.manager.close()
                self.process.terminate.assert_called_once()
                with self.assertRaises(local_ras.SearchCancelled):
                    with self.manager.connect("ras.exe", "server:2540"):
                        self.fail("Manager was closed")
        self.popen.assert_called_once()
        self.process.wait.assert_called_once()

    def test_cancel_during_startup_reaps_process(self):
        def cancel(*args):
            self.manager.stop.set()
            self.manager._check_cancelled()
        with patch.object(self.manager, "_wait_ready", side_effect=cancel):
            with self.assertRaises(local_ras.SearchCancelled):
                with self.manager.connect("ras.exe", "server:1540"):
                    self.fail("Cancelled")
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once()

    def test_hung_child_is_killed_and_reaped(self):
        def wait(timeout):
            if not self.process.kill.called:
                raise subprocess.TimeoutExpired("ras.exe", timeout)
            return self.exited(timeout)
        self.process.wait.side_effect = wait
        with patch.object(self.manager, "_wait_ready"):
            with self.manager.connect("ras.exe", "server:1540"):
                pass
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.call_count, 2)

    def test_early_exit_reports_ras_output(self):
        self.process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "порт занят"):
            self.manager._wait_ready(self.process, 49152, io.BytesIO("порт занят".encode("cp866")))

    def test_startup_timeout_is_bounded(self):
        with patch.object(local_ras.time, "monotonic", side_effect=[0, 0, 9]), \
                patch.object(local_ras.socket, "create_connection", side_effect=OSError), \
                patch.object(self.manager.stop, "wait"):
            with self.assertRaisesRegex(RuntimeError, "не открыл порт"):
                self.manager._wait_ready(self.process, 49152, io.BytesIO())

    def test_normalizes_windows_console_newlines(self):
        self.assertEqual(local_ras.decode_output(b"a\r\r\nb\r\nc\r"), "a\nb\nc\n")


class LocalSearchTests(unittest.TestCase):
    def setUp(self):
        discovery = patch.object(core, 'find_all_rac', return_value={})
        discovery.start()
        self.addCleanup(discovery.stop)
        embedded = patch.object(core, 'managed_tools', return_value=[])
        embedded.start()
        self.addCleanup(embedded.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.versions = {}
        for version in ("8.3.25.1", "8.3.27.1"):
            bin_dir = self.root / version / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("rac.exe", "ras.exe"):
                (bin_dir / name).touch()
            self.versions[version] = str(bin_dir / "rac.exe")
        self.manager = MagicMock()
        self.manager.stop = threading.Event()
        self.connections = []
        self.closed = []
        @contextlib.contextmanager
        def connect(ras, agent):
            self.connections.append((ras, agent))
            try:
                yield "127.0.0.1:49152"
            finally:
                self.closed.append(agent)
        self.manager.connect.side_effect = connect

    def item(self, host="server", port="1540", version="8.3.25.1"):
        return dict(server=host, port=port, version=version)

    def test_uses_exact_version_original_agent_port_and_local_rac_endpoint(self):
        calls = []
        def runner(cmd):
            calls.append(cmd)
            self.assertEqual(cmd[0], self.versions["8.3.25.1"])
            self.assertEqual(cmd[-1], "127.0.0.1:49152")
            self.assertFalse(any(arg.startswith("--cluster-user=") for arg in cmd))
            return "cluster : cluster-guid\nname : Main\n" if cmd[1] == "cluster" else "infobase : ib-guid\nname : Test\n"
        item = self.item(port="2540")
        bases, errors = core.collect_bases([item, item], self.versions, "", "", runner=runner,
                                           local_ras=self.manager, ras_ports={"server:2540": 9999})
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.connections), 1)
        self.assertEqual(self.connections[0][1], "server:2540")
        self.assertEqual(Path(self.connections[0][0]).parent.name, "bin")
        self.assertEqual(bases[0]["port"], "2540")
        self.assertEqual(self.closed, ["server:2540"])
        self.manager.close.assert_called_once()

    def test_stale_lst_version_tries_available_local_version(self):
        bases, errors = core.collect_bases([self.item(version="8.3.26.1")], self.versions, "", "",
            local_ras=self.manager, runner=lambda cmd: 'cluster : guid\n' if cmd[1] == 'cluster' else 'infobase : ib\nname : Test\n')
        self.assertEqual(errors, [])
        self.assertEqual(len(bases), 1)
        self.assertEqual(self.connections[0][0], str(Path(self.versions['8.3.27.1']).with_name('ras.exe')))

    def test_no_available_tools_explains_update_action(self):
        bases, errors = core.collect_bases([self.item()], {}, '', '', local_ras=self.manager)
        self.assertEqual(bases, [])
        self.assertIn('Обновите RAS', errors[0])
        self.manager.connect.assert_not_called()

    def test_missing_ras_explains_requirement(self):
        Path(self.versions["8.3.25.1"]).with_name("ras.exe").unlink()
        tools = core.select_local_tools(self.item(), self.versions)
        self.assertEqual(tools[2], '8.3.27.1')
        with self.assertRaisesRegex(RuntimeError, 'локальные ras.exe/rac.exe не найдены'):
            core.select_local_tools(self.item(), {})

    def test_unknown_version_selects_latest_available_pair(self):
        rac, ras, version = core.select_local_tools(self.item(version=""), self.versions)
        self.assertEqual(version, "8.3.27.1")
        self.assertEqual(rac, self.versions[version])
        self.assertEqual(Path(ras).name, "ras.exe")

    def test_platform_installed_after_startup_is_found_for_exact_85_version(self):
        folder = self.root / '8.5.1.1529' / 'bin'
        folder.mkdir(parents=True)
        for name in ('rac.exe', 'ras.exe'):
            (folder / name).touch()
        discovered = {'8.5.1.1529': str(folder / 'rac.exe')}
        with patch.object(core, 'find_all_rac', return_value=discovered) as refresh:
            rac, ras, version = core.select_local_tools(self.item(version='8.5.1.1529'), self.versions)
        self.assertEqual(version, '8.5.1.1529')
        self.assertEqual(rac, discovered[version])
        self.assertEqual(ras, str(folder / 'ras.exe'))
        refresh.assert_called_once_with()

    def test_local_candidates_rescan_installs_and_missing_hint_uses_latest(self):
        core.select_local_tools(self.item(), self.versions)
        core.find_all_rac.assert_called_once()
        self.assertEqual(core.select_local_tools(self.item(version='8.5.1.1529'), self.versions)[2], '8.3.27.1')

    def test_cluster_failure_keeps_other_cluster_results_and_closes_ras(self):
        def runner(cmd):
            if cmd[1] == "cluster":
                return "cluster : good\n\ncluster : bad\n"
            self.assertIn("--cluster-user=operator", cmd)
            self.assertIn("--cluster-pwd=", cmd)
            if "--cluster=bad" in cmd:
                raise RuntimeError("access denied")
            return "infobase : guid\nname : Test\n"
        bases, errors = core.collect_bases([self.item()], self.versions, "operator", "",
                                           runner=runner, local_ras=self.manager)
        self.assertEqual(len(bases), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("server:1540", errors[0])
        self.assertIn("bad", errors[0])
        self.assertEqual(self.closed, ["server:1540"])

    def test_agent_failure_is_reported_with_source_port_and_releases_ras(self):
        def runner(cmd):
            raise RuntimeError("agent unavailable")
        bases, errors = core.collect_bases([self.item(port="2540")], self.versions, "", "",
                                           runner=runner, local_ras=self.manager)
        self.assertEqual(bases, [])
        self.assertIn("server:2540", errors[0])
        self.assertEqual(self.closed, ["server:2540", "server:2540"])

    def test_cancelled_search_does_not_launch_ras(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(core.collect_bases([self.item()], self.versions, "", "", stop=stop,
                                           local_ras=self.manager), ([], []))
        self.manager.connect.assert_not_called()
        self.manager.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
