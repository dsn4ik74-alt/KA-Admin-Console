import importlib.util
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from bookmark_navigation import PAGES

import console_core as core

spec = importlib.util.spec_from_file_location("console_app", Path(__file__).parents[1] / "1C Console.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
# Never persist fixture connections or history into the real user's profile.
app_module.ACTIVITY_FILE = None


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make(self, path, text="", encoding="utf-8"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_other_profiles_are_not_searched(self):
        current = self.root / "current" / "AppData" / "Local" / "1C"
        other = self.root / "a.giniyatullin" / "AppData" / "Local" / "1C"
        one = self.make(current / "1cv8" / "appsrvrs.lst")
        two = self.make(other / "1cv8" / "appsrvrs.lst")
        report = core.discover_local_lst([(current, True), (other, False)])
        self.assertEqual(report.paths, [str(one)])
        self.assertEqual(report.current_paths, [str(one)])
        self.assertNotIn(str(two), report.checked)

    def test_default_discovery_uses_only_process_user(self):
        home = self.root / "current"
        expected = self.make(home / "AppData" / "Local" / "1C" / "1cv8" / "appsrvrs.lst")
        self.make(self.root / "other" / "AppData" / "Local" / "1C" / "1cv8" / "appsrvrs.lst")
        with patch.object(core.Path, "home", return_value=home), \
                patch.object(core, "known_folder", return_value=None), \
                patch.dict("os.environ", {"LOCALAPPDATA": "", "APPDATA": ""}):
            report = core.discover_local_lst()
        self.assertEqual(report.paths, [str(expected)])
        self.assertTrue(all(Path(p).is_relative_to(home) for p in report.checked))

    def test_discovery_rescans_files_created_after_first_call(self):
        root = self.root / "1C"
        self.assertEqual(core.discover_local_lst([(root, True)]).paths, [])
        path = self.make(root / "1cv83" / "appsrvrs.lst")
        self.assertEqual(core.discover_local_lst([(root, True)]).paths, [str(path)])

    def test_location_chain_cycle_relative_path_and_unicode(self):
        root = self.root / "1C"
        first = root / "1cv8"
        target = root / "Перенос"
        self.make(first / "location.cfg", 'location="../Перенос"', "utf-16")
        expected = self.make(target / "appsrvrs.lst")
        self.make(target / "location.cfg", "location=../1cv8")
        report = core.discover_local_lst([(root, True), (root, True)])
        self.assertEqual([Path(p).resolve() for p in report.paths], [expected.resolve()])
        self.assertLess(len(report.checked), 10)

    def test_location_environment_variable(self):
        root = self.root / "1C"
        target = self.root / "redirect"
        self.make(root / "1cv82" / "location.cfg", "location=%CONSOLE_TEST_REDIRECT%")
        expected = self.make(target / "appsrvrs.lst")
        with patch.dict("os.environ", {"CONSOLE_TEST_REDIRECT": str(target)}):
            report = core.discover_local_lst([(root, True)])
        self.assertIn(str(expected), report.paths)

    def test_denied_file_does_not_abort_other_current_user_locations(self):
        first, second = self.root / "first", self.root / "second"
        denied = first / "1cv8" / "appsrvrs.lst"
        good = self.make(second / "1cv82" / "appsrvrs.lst")
        original_open = Path.open
        def open_path(path, *args, **kwargs):
            if path == denied:
                raise PermissionError(13, "Access denied", str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", open_path):
            report = core.discover_local_lst([(first, True), (second, True)])
        self.assertEqual(report.paths, [str(good)])
        self.assertTrue(any("Access denied" in error for error in report.errors))

    def test_read_lst_encodings_whitespace_quotes_and_duplicates(self):
        content = '{1, { "tcp", "server", 1540, "8.3.27.1000 | База ""Тест""" },\n' \
                  '{ "tcp", "server", 1540, "8.3.27.1000 | База ""Тест""" },\n' \
                  '{"tcp","bad",99999,"invalid"}}'
        for encoding in ("utf-8-sig", "cp1251", "utf-16"):
            path = self.make(self.root / "appsrvrs.lst", content, encoding)
            servers = core.read_lst(path)
            self.assertEqual(list(servers), ["server"])
            self.assertEqual(len(servers["server"]), 1)
            self.assertEqual(servers["server"][0]["version"], "8.3.27.1000")
            self.assertIn('База "Тест"', servers["server"][0]["descr"])

    def test_write_list_round_trip_quotes(self):
        path = self.root / "appsrvrs.lst"
        app_module.write_lst_entry(path, "server", "1540", "8.3.27.1000", 'База "Тест"')
        self.assertIn('База "Тест"', core.read_lst(path)["server"][0]["descr"])

    def test_rac_version_selection_is_numeric(self):
        self.assertEqual(core.find_rac_best({"8.3.9.100": "old", "8.3.27.100": "new"}), "new")

    def test_per_user_platform_paths_and_85_discovery_prefer_complete_rac_ras_pair(self):
        local = self.root / 'AppData' / 'Local'
        version = '8.5.1.1529'
        client = local / 'Programs' / '1cv8_x86' / version / 'bin'
        server = local / 'Programs' / '1cv8_x64' / version / 'bin'
        self.make(client / 'rac.exe')
        expected = self.make(server / 'rac.exe')
        self.make(server / 'ras.exe')
        with patch.dict('os.environ', {'LOCALAPPDATA': str(local)}), \
                patch.object(core, 'local_roots', return_value=[]), \
                patch.object(core, 'registered_platform_roots', return_value=[]):
            roots = core.platform_roots()
        self.assertIn(server.parent.parent, roots)
        self.assertIn(client.parent.parent, roots)
        with patch.object(core, 'platform_roots', return_value=[client.parent.parent, server.parent.parent]):
            self.assertEqual(core.find_all_rac()[version], str(expected))

    def test_registry_discovery_uses_only_1c_install_locations_including_custom_bin(self):
        import winreg
        from contextlib import contextmanager
        custom = self.root / 'Custom install' / '8.5.1.1529' / 'bin'
        records = [dict(DisplayName='1C:Enterprise 8.5 (x86-64)', InstallLocation=str(custom)),
                   dict(DisplayName='1С:Предприятие 8', InstallLocation=str(custom.parent)),
                   dict(DisplayName='Unrelated software', InstallLocation=str(self.root / 'ignore'))]
        @contextmanager
        def open_key(parent, name, *args):
            yield records[int(name)] if name.isdigit() else 'uninstall'
        with patch.object(winreg, 'OpenKey', side_effect=open_key), \
                patch.object(winreg, 'QueryInfoKey', return_value=(len(records), 0, 0)), \
                patch.object(winreg, 'EnumKey', side_effect=lambda key, index: str(index)), \
                patch.object(winreg, 'QueryValueEx', side_effect=lambda key, name: (key[name], winreg.REG_SZ)):
            roots = core.registered_platform_roots()
        self.assertTrue(roots)
        self.assertEqual(set(roots), {custom.parent.parent})

    def test_rac_blocks_preserve_colons_and_escaped_quotes(self):
        result = core.parse_rac('infobase : id\nname : "База ""Тест"":1"\n\ninfobase : id2\n')
        self.assertEqual(result, [{"infobase": "id", "name": 'База "Тест":1'}, {"infobase": "id2"}])

    def test_rac_two_stages_deduplication_version_and_port_override(self):
        calls = []
        def runner(cmd):
            calls.append(cmd)
            if cmd[1:3] == ["cluster", "list"]:
                return 'cluster : abc-123\nname : "Основной"\n'
            return 'infobase : base-guid\nname : "Бухгалтерия"\n\nwarning : ignored\n'
        item = {"server": "host", "port": "1540", "version": "8.3.9.100"}
        bases, errors = core.collect_bases([item, item], {"8.3.9.100": "exact", "8.3.27.100": "new"},
                                           "user", "pwd", runner=runner, ras_ports={"host:1540": 2545})
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(cmd[0] == "exact" and cmd[-1] == "host:2545" for cmd in calls))
        self.assertIn("--cluster=abc-123", calls[1])
        self.assertIn("--cluster-user=user", calls[1])
        self.assertIn("--cluster-pwd=pwd", calls[1])
        self.assertEqual(bases[0]["base"], "Бухгалтерия")
        self.assertEqual(len(bases), 1)

    def test_partial_rac_failure_preserves_other_results(self):
        def runner(cmd):
            if cmd[-1] == "down:1545":
                raise RuntimeError("unavailable")
            return "cluster : abc\n" if cmd[1] == "cluster" else "infobase : guid\nname : db\n"
        items = [{"server": server, "port": "1540"} for server in ("down", "up")]
        bases, errors = core.collect_bases(items, {"8.3.27.1": "rac"}, "", "", runner=runner)
        self.assertEqual([base["server"] for base in bases], ["up"])
        self.assertEqual(len(errors), 1)
        self.assertIn("down:1545", errors[0])

    def test_default_ras_port_and_cluster_without_administrators(self):
        calls = []
        def runner(cmd):
            calls.append(cmd)
            self.assertTrue(cmd[-1].endswith(":1545"))
            self.assertFalse(any(arg.startswith("--cluster-user=") or arg.startswith("--cluster-pwd=")
                                 for arg in cmd))
            return "cluster : abc\n" if cmd[1] == "cluster" else "infobase : guid\nname : db\n"
        items = [{"server": host, "port": port} for host, port in (("one", "1540"), ("two", "2540"))]
        bases, errors = core.collect_bases(items, {"8.3.27.1": "rac"}, "", "", runner=runner)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 4)
        self.assertEqual({base["port"] for base in bases}, {"1540", "2540"})

    def test_named_administrator_with_empty_password(self):
        def runner(cmd):
            if cmd[1] == "cluster":
                return "cluster : abc\n"
            self.assertIn("--cluster-user=operator", cmd)
            self.assertIn("--cluster-pwd=", cmd)
            return "infobase : guid\nname : db\n"
        bases, errors = core.collect_bases([{"server": "host", "port": "1540"}],
                                           {"8.3.27.1": "rac"}, "operator", "", runner=runner)
        self.assertEqual(errors, [])
        self.assertEqual(len(bases), 1)

    def test_empty_credentials_replace_old_cached_credentials(self):
        with patch.object(app_module, "CACHE_FILE", self.root / "cache.dat"):
            app_module.save_cache("admin", "admin")
            app_module.save_cache("", "")
            self.assertEqual(app_module.load_cache(), ("", ""))

    def test_missing_rac_and_cancelled_scan(self):
        with self.assertRaisesRegex(RuntimeError, "rac.exe"):
            core.collect_bases([], {}, "", "")
        stop = threading.Event()
        stop.set()
        def runner(cmd):
            self.fail("Cancelled search must not run RAC")
        self.assertEqual(core.collect_bases([{"server": "s", "port": "1540"}],
                                           {"8.3.27.1": "rac"}, "", "", stop=stop, runner=runner), ([], []))

    def test_rac_timeout_exit_status_encoding_and_redaction(self):
        with patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("rac", 8)):
            with self.assertRaisesRegex(RuntimeError, "8 с"):
                core.run_rac(["rac"])
        result = subprocess.CompletedProcess([], 1, b"", "Ошибка password123".encode("cp866"))
        with patch.object(core.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Ошибка \\*\\*\\*"):
                core.run_rac(["rac", "--cluster-pwd=password123"])
        result = subprocess.CompletedProcess([], 0, "name : База".encode("cp866"), b"")
        with patch.object(core.subprocess, "run", return_value=result):
            self.assertEqual(core.run_rac(["rac"]), "name : База")


class StartupTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(app_module.ctypes, "windll")
        self.windows = patcher.start()
        self.addCleanup(patcher.stop)
        self.windows.shell32.IsUserAnAdmin.return_value = 0
        self.windows.shell32.ShellExecuteW.return_value = 42

    def test_already_elevated_does_not_relaunch(self):
        self.windows.shell32.IsUserAnAdmin.return_value = 1
        self.assertTrue(app_module.ensure_administrator())
        self.windows.shell32.ShellExecuteW.assert_not_called()

    def test_source_launch_quotes_arguments_and_requests_uac(self):
        with patch.object(app_module.sys, "frozen", False, create=True), \
                patch.object(app_module.sys, "argv", ["1C Console.py", "a b", 'x"y']):
            self.assertFalse(app_module.ensure_administrator())
        args = self.windows.shell32.ShellExecuteW.call_args.args
        self.assertEqual(args[1], "runas")
        self.assertEqual(args[2], app_module.sys.executable)
        self.assertEqual(args[3], subprocess.list2cmdline(
            [str(Path(app_module.__file__).resolve()), "a b", 'x"y', "--console-elevated"]))

    def test_frozen_launch_does_not_pass_python_source_path(self):
        with patch.object(app_module.sys, "frozen", True, create=True), \
                patch.object(app_module.sys, "argv", ["1C Console.exe"]):
            self.assertFalse(app_module.ensure_administrator())
        self.assertEqual(self.windows.shell32.ShellExecuteW.call_args.args[3], "--console-elevated")

    def test_cancelled_uac_does_not_start_unelevated_app(self):
        self.windows.shell32.ShellExecuteW.return_value = 5
        with patch.object(app_module.sys, "argv", ["1C Console.py"]):
            self.assertFalse(app_module.ensure_administrator())
        self.windows.user32.MessageBoxW.assert_called_once()

    def test_failed_elevation_does_not_create_relaunch_loop(self):
        with patch.object(app_module.sys, "argv", ["1C Console.py", "--console-elevated"]):
            self.assertFalse(app_module.ensure_administrator())
        self.windows.shell32.ShellExecuteW.assert_not_called()
        self.windows.user32.MessageBoxW.assert_called_once()


class UiTests(unittest.TestCase):
    """Hidden Tk tests: no real settings, credentials, MMC or network requests."""
    def setUp(self):
        for name, value in (("_discover", None),):
            patcher = patch.object(app_module.App, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("load_cache", ("", "")), ("load_settings", {})):
            patcher = patch.object(app_module, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        from activity_store import ActivityStore
        patcher=patch.object(app_module,'ActivityStore',side_effect=lambda path:ActivityStore())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = app_module.App()
        self.app._switch_mode('servers')
        self.app.root.withdraw()
        self.addCleanup(self.app.root.destroy)
        self.addCleanup(self.app.bookmarks.close)
        self.addCleanup(self.app._cancel_ui_jobs)
        self.app.all_items = [
            {"server": "alpha", "port": "1540", "version": "8.3.27.1", "descr": "Основной"},
            {"server": "beta", "port": "2540", "version": "8.3.27.1", "descr": "Тестовый"},
        ]

    def test_empty_credentials_are_not_replaced_by_defaults(self):
        self.assertEqual(self.app.user_var.get(), "")
        self.assertEqual(self.app.pwd_var.get(), "")

    def test_local_ras_is_default_and_receives_application_stop_event(self):
        self.assertTrue(self.app.local_ras_var.get())
        self.app.rac_map = {"8.3.27.1": "rac.exe"}
        with patch.object(app_module, "save_cache"), \
                patch.object(app_module.threading, "Thread") as thread, \
                patch.object(app_module, "collect_bases", return_value=([], [])) as collect:
            self.app._do_rac_search()
            manager = self.app._ras_manager
            self.assertIs(manager.stop, self.app._stop)
            thread.call_args.kwargs["target"]()
            self.assertIs(collect.call_args.kwargs["local_ras"], manager)
            self.assertTrue(manager._closed)

    def test_embedded_ras_search_starts_without_a_locally_installed_rac_and_does_not_scan_on_ui_thread(self):
        self.app.rac_map = {}
        with (patch.object(app_module, 'find_all_rac', side_effect=AssertionError('scan must run in worker')),
              patch.object(app_module, 'save_cache'), patch.object(app_module.threading, 'Thread') as thread,
              patch.object(app_module, 'collect_bases', return_value=([], [])) as collect,
              patch.object(app_module.messagebox, 'showwarning') as warning):
            self.app._do_rac_search()
            self.assertTrue(self.app.search_running)
            thread.call_args.kwargs['target']()
            self.assertEqual(collect.call_args.args[1], {})
            self.assertIsNotNone(collect.call_args.kwargs['local_ras'])
            warning.assert_not_called()

    def test_old_saved_source_from_another_user_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "current"
            current = home / "AppData" / "Local" / "1C" / "1cv8" / "appsrvrs.lst"
            current.parent.mkdir(parents=True)
            app_module.write_lst_entry(current, "current-server", 1540, "", "")
            other = Path(directory) / "other" / "appsrvrs.lst"
            other.parent.mkdir()
            app_module.write_lst_entry(other, "other-server", 1540, "", "")
            self.app._lst_path = str(other)
            report = core.LstDiscovery(paths=[str(current)], current_paths=[str(current)])
            with patch.object(app_module.Path, "home", return_value=home):
                self.app._discovered(report, {}, startup=True)
            self.assertEqual(self.app._lst_path, str(current))
            self.assertEqual(self.app.all_items[0]["server"], "current-server")

    def test_source_selection_allows_own_redirects_and_rejects_other_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "current"
            redirect = Path(directory) / "redirect" / "appsrvrs.lst"
            report = core.LstDiscovery(current_paths=[str(redirect)])
            with patch.object(app_module.Path, "home", return_value=home):
                self.assertTrue(self.app._is_user_source(home / "appsrvrs.lst", report))
                self.assertTrue(self.app._is_user_source(redirect, report))
                self.assertFalse(self.app._is_user_source(Path(directory) / "other" / "appsrvrs.lst", report))

    def test_filter_survives_sorting(self):
        self.app.search_var.set("beta")
        self.app._render()
        self.app._sort_tree("server")
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app._visible_items[0]["server"], "beta")

    def test_modes_and_visible_columns_copied(self):
        self.assertEqual({node["id"] for node, _ in self.app.bookmarks.layout.walk()
                      if "children" not in node}, set(PAGES))
        self.app._render()
        self.app.tree.selection_set("0")
        self.app._copy_selection()
        self.assertEqual(self.app.root.clipboard_get(), "alpha\t1540\t8.3.27.1\tОсновной")
        self.app._switch_mode("bases")
        self.assertEqual(self.app.tree.cget("displaycolumns"), ("base", "address"))

    def test_base_address_uses_cluster_host_and_port_and_is_searchable(self):
        self.app._all_bases = [dict(server='agent', port='1540', base='erp',
                                   cluster_host='SRV-1C01', cluster_port='4541')]
        self.app._switch_mode('bases')
        self.assertEqual(self.app.tree.set('0', 'address'), 'Srvr="SRV-1C01:4541";Ref="erp";')
        self.app.search_var.set('srv-1c01:4541')
        self.app._render()
        self.assertEqual(len(self.app._visible_items), 1)
        self.app._all_bases[0]['cluster_port'] = ''
        self.app.search_var.set('')
        self.app._render()
        self.assertEqual(self.app.tree.set('0', 'address'), '')

    def test_accounts_tab_hides_server_controls_and_restores_them(self):
        self.app._switch_mode("accounts")
        self.assertEqual(self.app.accounts.winfo_manager(), "pack")
        self.assertEqual(self.app._source_panel.winfo_manager(), "")
        self.assertEqual(self.app.btn_update.winfo_manager(), "")
        with patch.object(self.app, "_do_rac_search") as search:
            self.app._on_update_clicked()
            search.assert_not_called()
        self.app._switch_mode("servers")
        self.assertEqual(self.app.accounts.winfo_manager(), "")
        self.assertEqual(self.app._source_panel.winfo_manager(), "pack")
        self.assertEqual(self.app.btn_update.winfo_manager(), "pack")
        self.assertEqual(self.app._visible_items[0]["server"], "alpha")
        ordered = self.app.root.pack_slaves()
        self.assertLess(ordered.index(self.app._navigation), ordered.index(self.app._source_panel))
        self.assertLess(ordered.index(self.app._source_panel), ordered.index(self.app._server_layout[0][0]))

    def test_navigation_changes_save_and_reload_in_settings(self):
        bar = self.app.bookmarks
        with patch.object(app_module, "save_settings") as save:
            bar._move_to("bases", "folder-win")
            save.assert_called_once_with(self.app._settings)
        saved = self.app._settings["navigation_layout"]
        restored = type(bar.layout)(saved)
        self.assertEqual(restored.location("bases")[0], "folder-win")

    def test_account_updates_preserve_password_and_previous_batches(self):
        panel = self.app.accounts
        row = dict(line=1, full_name="Иванов Иван", username="ivanov.i", email="ivanov.i@example.ru",
                   company="", password="test-password", status="created", message="RDP pending")
        panel.accept_row(1, row)
        panel.accept_row(1, dict(row, status="created_no_rdp", message="RDP failed"))
        panel.accept_row(2, dict(row, status="exists", password=""))
        self.assertEqual(len(panel.table.get_children()), 2)
        self.assertEqual(panel.rows["1:1"]["password"], "test-password")
        panel.table.selection_set("1:1")
        panel.copy_results()
        self.assertIn("test-password", self.app.root.clipboard_get())
        self.assertIn("Создана без RDP", self.app.root.clipboard_get())
        panel.running = True
        panel.clear_results()
        self.assertEqual(len(panel.rows), 2)
        panel.running = False
        panel.names.insert("1.0", "Следующий сотрудник")
        panel.clear_button.invoke()
        self.assertEqual(panel.rows, {})
        self.assertEqual(panel.table.get_children(), ())
        self.assertEqual(panel.count.cget("text"), "Результаты · 0")
        self.assertEqual(panel.names.get("1.0", "end-1c"), "Следующий сотрудник")
        self.assertEqual(panel.clear_button.master, panel.copy_button.master)

    def test_account_batch_error_unlocks_input_without_losing_rows(self):
        panel = self.app.accounts
        panel.accept_row(1, dict(line=1, full_name="Иванов Иван", password="test-password", status="created"))
        panel.running = True
        panel.finish(1, False, "PowerShell interrupted")
        self.assertFalse(panel.running)
        self.assertEqual(str(panel.create_button.cget("state")), "normal")
        self.assertEqual(panel.rows["1:1"]["password"], "test-password")
        self.assertEqual(panel.detail.cget("text"), "PowerShell interrupted")

    def test_close_and_repeat_are_blocked_while_accounts_are_being_created(self):
        self.app.accounts.running = True
        with patch.object(self.app.root, "destroy") as destroy:
            self.app._on_close()
            destroy.assert_not_called()
        self.assertFalse(self.app._stop.is_set())
        with patch("accounts_ui.threading.Thread") as thread:
            self.app.accounts.start()
            thread.assert_not_called()

    def test_no_clusters_clears_stale_base_results_and_unlocks(self):
        self.app._switch_mode("bases")
        self.app._all_bases = [{"server": "alpha", "port": "1540", "base": "old", "cluster": "old"}]
        self.app.search_running = True
        self.app._finalize_rac_search([], ["unavailable"])
        self.assertFalse(self.app.search_running)
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(str(self.app.btn_update.cget("state")), "normal")

    def test_loading_source_clears_cached_bases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "appsrvrs.lst"
            app_module.write_lst_entry(path, "new", 1540, "", "")
            self.app._lst_path = str(path)
            self.app._all_bases = [{"base": "stale"}]
            self.app._load_lst()
            self.assertEqual(self.app._all_bases, [])
            self.assertEqual(self.app.all_items[0]["server"], "new")

    def test_mmc_failure_restores_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "appsrvrs.lst"
            original = b'original list bytes'
            path.write_bytes(original)
            report = core.LstDiscovery(paths=[str(path)], current_paths=[str(path)])
            with patch.object(app_module, "discover_local_lst", return_value=report), \
                    patch.object(app_module, "open_mmc", return_value=False), \
                    patch.object(app_module.messagebox, "showerror"):
                self.app._do_connect(self.app.all_items[0])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(Path(str(path) + ".bak").exists())
            self.assertEqual(self.app._backups, {})


if __name__ == "__main__":
    unittest.main()
