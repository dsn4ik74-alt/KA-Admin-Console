import ctypes
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import service_management as services


class CommandTests(unittest.TestCase):
    def test_edited_create_roundtrip_preserves_quoted_binary_options_and_custom_flags(self):
        for path in (r'G:\srvinfo_4541', 'D:\\Данные 1С\\'):
            original = services.create_command('4540', '8.3.25.1286', path, agents={})
            edited = services.edited_create_command(original.preview)
            self.assertEqual(edited, original)
        text = original.preview.replace('start= auto', 'start= demand') + ' obj= "DOMAIN\\service" password= "a&b$(test)"'
        edited = services.edited_create_command(text)
        self.assertIn('demand', edited.arguments)
        self.assertEqual(edited.arguments[-4:], ('obj=', 'DOMAIN\\service', 'password=', 'a&b$(test)'))

    def test_edited_create_accepts_multiline_and_case_insensitive_sc_parameters(self):
        original = services.create_command('4540', '8.3.25.1286', agents={})
        text = original.preview.replace('sc.exe create ', 'SC CREATE ').replace(' start=', '\nSTART=')
        command = services.edited_create_command(text)
        self.assertEqual(command.name, original.name)
        self.assertEqual(command.executable, original.executable)
        self.assertIn('START=', command.arguments)

    def test_edited_create_rejects_other_operations_bad_option_pairs_and_chained_commands(self):
        for text in ('', 'sc delete "Service"', 'cmd /c sc create "Service" binPath= test',
                     'sc \\\\remote create "Service" binPath= test',
                     'sc create "Service" start= auto',
                     'sc create "Service" binPath=',
                     'sc create "Service" binPath= relative.exe',
                     'sc create "Service" binPath= C:\\agent.exe & sc delete Other',
                     'sc create "Service" binPath= C:\\agent.exe start= auto start= demand',
                     'sc create "Service" binPath= C:\\agent.exe\x00 start= auto'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                services.edited_create_command(text)

    def test_installed_agent_discovery_uses_actual_files_and_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "8.3.25.1286" / "bin" / "ragent.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"test fixture, never executed")
            (root / "8.3.27.1000").mkdir()
            (root / "8x3x25x1286").mkdir()
            with patch.object(services, "platform_roots", return_value=[root / "missing", root]):
                self.assertEqual(services.installed_agents(), {"8.3.25.1286": str(executable)})

    def test_port_families_version_data_path_and_display_name(self):
        for port in (1540, 1640, 4540):
            command = services.create_command(str(port), "8.3.25.1286", agents={})
            binary = command.arguments[3]
            self.assertEqual(command.name, f"1C:Enterprise 8.3 Server Agent {port}")
            self.assertIn(f'-port {port} -regport {port + 1} -range {port + 20}:{port + 51}', binary)
            self.assertIn(rf'-d "G:\srvinfo_{port + 1}"', binary)
            self.assertIn(r'1cv8\8.3.25.1286\bin\ragent.exe"', binary)
            self.assertEqual(command.arguments[4:6], ("start=", "auto"))
            self.assertEqual(command.arguments[-1], f"1C:Enterprise 8.3 Server | 8.3.25.1286 | {port}")

    def test_family_prefix_and_custom_installed_executable(self):
        self.assertEqual(services.agent_ports("4550"), (4550, 4541, 4560, 4591))
        command = services.create_command("2540", "8.3.27.1000", r"D:\Данные 1С\srvinfo",
                                           {"8.3.27.1000": r"E:\Платформы 1С\8.3.27.1000\bin\ragent.exe"})
        self.assertIn(r'"E:\Платформы 1С\8.3.27.1000\bin\ragent.exe"', command.arguments[3])
        self.assertIn(r'-d "D:\Данные 1С\srvinfo"', command.arguments[3])

    def test_invalid_ports_versions_and_paths_do_not_build_commands(self):
        for port in ("bad", "-1", "0", "65540", "65500", "4541", "4560", "٤٥٤٠"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                services.create_command(port, "8.3.25.1286", agents={})
        for version in ("8.3", "8.3.25.1286 -anything", "8x3x25x1286"):
            with self.assertRaises(ValueError):
                services.create_command("4540", version, agents={})
        for path in ("relative", 'G:\\bad"path', "G:\\bad\npath"):
            with self.assertRaises(ValueError):
                services.create_command("4540", "8.3.25.1286", path, agents={})

    def test_windows_argument_roundtrip_keeps_nested_quotes_and_trailing_slashes(self):
        parse = ctypes.windll.shell32.CommandLineToArgvW
        parse.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
        parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = (ctypes.c_void_p,)
        for path in (r"G:\srvinfo_4541", "G:\\", "D:\\Данные 1С\\"):
            command = services.create_command("4540", "8.3.25.1286", path, agents={})
            count = ctypes.c_int()
            parsed = parse(command.preview, ctypes.byref(count))
            try:
                values = [parsed[index] for index in range(count.value)]
                self.assertEqual(values, ["sc.exe", *command.arguments])
            finally:
                free(parsed)
            parsed = parse(command.arguments[3], ctypes.byref(count))
            try:
                self.assertEqual(parsed[count.value - 1], path)
            finally:
                free(parsed)

    def test_rename_delete_preserve_literals_and_reject_invalid_service_names(self):
        rename = services.rename_command("1C Agent", "Имя & $(anything)")
        self.assertEqual(rename.arguments, ("config", "1C Agent", "DisplayName=", "Имя & $(anything)"))
        self.assertEqual(services.delete_command("1C Agent").arguments, ("delete", "1C Agent"))
        for name in ("", "\\\\server", "Bad/Name", "Name\nOther"):
            with self.assertRaises(ValueError):
                services.delete_command(name)


class ExecutionTests(unittest.TestCase):
    """All mutations are mocked: no Windows services are changed by tests."""
    def test_sc_runs_as_argument_list_without_shell_and_decodes_oem(self):
        command = services.rename_command("1C Agent", "Новое имя")
        result = Mock(returncode=0, stdout="Успешно".encode("cp866"))
        with patch.object(services.subprocess, "run", return_value=result) as execute, \
                patch.object(services.ctypes.windll.kernel32, "GetOEMCP", return_value=866):
            self.assertEqual(services.run_service_command(command), "Успешно")
        self.assertEqual(execute.call_args.args[0][1:], list(command.arguments))
        self.assertFalse(execute.call_args.kwargs["shell"])
        self.assertEqual(execute.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_missing_agent_never_executes_sc(self):
        command = services.create_command("4540", "8.3.25.1286", agents={})
        with patch.object(services.Path, "is_file", return_value=False), patch.object(services.subprocess, "run") as execute:
            with self.assertRaisesRegex(RuntimeError, "ragent.exe"):
                services.run_service_command(command)
        execute.assert_not_called()

    def test_manual_create_executes_only_the_parsed_arguments_through_system_sc(self):
        original = services.create_command('4540', '8.3.25.1286', agents={})
        command = services.edited_create_command(original.preview.replace('start= auto', 'start= demand'))
        with patch.object(services.Path, 'is_file', return_value=True), \
                patch.object(services.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'OK')) as run, \
                patch.object(services.ctypes.windll.kernel32, 'GetOEMCP', return_value=866):
            self.assertEqual(services.run_service_command(command), 'OK')
        self.assertEqual(run.call_args.args[0][1:], list(command.arguments))
        self.assertEqual(Path(run.call_args.args[0][0]).name, 'sc.exe')
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_nonzero_and_timeout_report_failure_without_retry(self):
        command = services.delete_command("Test Agent")
        with patch.object(services.subprocess, "run", return_value=Mock(returncode=1060, stdout=b'not found')):
            with self.assertRaisesRegex(RuntimeError, "1060"):
                services.run_service_command(command)
        with patch.object(services.subprocess, "run", side_effect=subprocess.TimeoutExpired('sc.exe', 20)) as execute:
            with self.assertRaisesRegex(RuntimeError, "20"):
                services.run_service_command(command)
        execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
