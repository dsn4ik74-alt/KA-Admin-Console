"""Build and execute local SC commands without a command shell."""
from dataclasses import dataclass
import ctypes
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess

from console_core import platform_roots


def installed_agents():
    agents = {}
    for root in platform_roots():
        try:
            for directory in root.iterdir():
                if re.fullmatch(r"8[.]\d+[.]\d+[.]\d+", directory.name):
                    executable = directory / "bin" / "ragent.exe"
                    if executable.is_file():
                        agents.setdefault(directory.name, str(executable))
        except OSError:
            continue
    return agents


def agent_ports(value):
    value = str(value).strip()
    if not value.isascii() or not value.isdigit():
        raise ValueError("Введите числовой порт агента, например 1540 или 4540")
    port = int(value)
    family = port // 100 * 100
    registration, first, last = family + 41, family + 60, family + 91
    if not 0 < port <= 65535 or last > 65535:
        raise ValueError("Все порты должны находиться в диапазоне 1–65535")
    if port == registration or first <= port <= last:
        raise ValueError("Порт агента совпадает с автоматически выбранным портом регистрации или рабочим диапазоном")
    return port, registration, first, last


def clean_text(value, title, service_name=False):
    value = value.strip()
    if not value or len(value) > 256 or any(ord(char) < 32 for char in value) or '"' in value:
        raise ValueError(f"{title}: введите от 1 до 256 символов без кавычек и переводов строк")
    if service_name and any(char in value for char in ('/', '\\')):
        raise ValueError("Имя службы не может содержать / или обратную косую черту")
    return value


def quoted_path(value):
    # Inner quoting is stored in ImagePath. list2cmdline escapes it for sc.exe.
    value = str(value)
    if '"' in value or any(ord(char) < 32 for char in value):
        raise ValueError("Путь содержит недопустимые символы")
    if not PureWindowsPath(value).is_absolute():
        raise ValueError("Укажите полный путь с диском или UNC-путь")
    trailing = len(value) - len(value.rstrip('\\'))
    return '"' + value + '\\' * trailing + '"'


@dataclass(frozen=True)
class ServiceCommand:
    action: str
    name: str
    arguments: tuple
    executable: str = ""

    @property
    def preview(self):
        return subprocess.list2cmdline(["sc.exe", *self.arguments])


def create_command(port, version, data_path="", agents=None):
    port, registration, first, last = agent_ports(port)
    version = version.strip()
    if not re.fullmatch(r"8[.]\d+[.]\d+[.]\d+", version):
        raise ValueError("Введите полную версию платформы, например 8.3.25.1286")
    agents = installed_agents() if agents is None else agents
    executable = agents.get(version) or ntpath.join(os.environ.get("ProgramW6432", r"C:\Program Files"),
                                                  "1cv8", version, "bin", "ragent.exe")
    data_path = data_path.strip() or rf"G:\srvinfo_{registration}"
    binary = (f"{quoted_path(executable)} -srvc -agent -port {port} -regport {registration} "
              f"-range {first}:{last} -d {quoted_path(data_path)}")
    name = f"1C:Enterprise 8.3 Server Agent {port}"
    display = f"1C:Enterprise 8.3 Server | {version} | {port}"
    return ServiceCommand("create", name, ("create", name, "binPath=", binary,
                                           "start=", "auto", "displayname=", display), executable)


def rename_command(name, display_name):
    name = clean_text(name, "Имя службы", service_name=True)
    display_name = clean_text(display_name, "Новое отображаемое имя")
    return ServiceCommand("rename", name, ("config", name, "DisplayName=", display_name))


def delete_command(name):
    name = clean_text(name, "Имя службы", service_name=True)
    return ServiceCommand("delete", name, ("delete", name))


def windows_arguments(text):
    """Use the same Windows quoting rules as the generated SC preview."""
    if os.name != 'nt':
        raise ValueError('Редактирование команды службы требует Windows')
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ValueError('Введите команду sc.exe create')
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    values = parse(text.strip().replace('\r', ' ').replace('\n', ' '), ctypes.byref(count))
    if not values:
        raise ValueError('Не удалось разобрать кавычки команды службы')
    try:
        return [values[index] for index in range(count.value)]
    finally:
        free(values)


def edited_create_command(text):
    args = windows_arguments(text)
    controller = ntpath.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'sc.exe')
    if (len(args) < 5 or args[0].casefold() not in ('sc', 'sc.exe', controller.casefold())
            or args[1].casefold() != 'create'):
        raise ValueError('Введите одну команду sc.exe create "Имя службы" binPath= "..."')
    name = clean_text(args[2], 'Имя службы', service_name=True)
    options = args[3:]
    if len(options) % 2:
        raise ValueError('После каждого параметра с = укажите значение через пробел; проверьте кавычки')
    values = {}
    for key, value in zip(options[::2], options[1::2]):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*=', key):
            raise ValueError('Параметры sc.exe указываются парами: binPath= "..." start= auto')
        key = key.casefold()
        if key in values:
            raise ValueError('Параметр указан повторно: ' + key)
        values[key] = value
    binary = values.get('binpath=', '')
    if not binary.strip():
        raise ValueError('Укажите binPath= с полным путём исполняемого файла службы')
    executable = windows_arguments(binary)[0]
    if not PureWindowsPath(executable).is_absolute():
        raise ValueError('В binPath= нужен полный путь; путь с пробелами заключите во внутренние кавычки')
    return ServiceCommand('create', name, ('create', name, *options), executable)


def run_service_command(command):
    if os.name != "nt":
        raise RuntimeError("Управление службами доступно только в Windows")
    if command.action == "create" and not Path(command.executable).is_file():
        raise RuntimeError(f"Не найден исполняемый файл службы: {command.executable}")
    controller = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "sc.exe")
    try:
        result = subprocess.run([controller, *command.arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=20, check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SC не ответил за 20 секунд. Проверьте состояние службы перед повтором.") from exc
    encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    output = result.stdout.decode(encoding, errors="replace").strip()
    if result.returncode:
        raise RuntimeError(f"SC завершился с кодом {result.returncode}\n{output}")
    return output or "Операция выполнена успешно"
