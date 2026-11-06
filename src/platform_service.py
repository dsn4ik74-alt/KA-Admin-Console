"""Configure the exact Windows ragent service installed by the selected MSI."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import ntpath
import os
from pathlib import Path, PureWindowsPath
import subprocess
import time

from local_ras import decode_output
from service_management import ServiceCommand, agent_ports, clean_text, quoted_path, run_service_command, windows_arguments
from linux_platform_runner import ras_service_options

SERVICE_ROLES = ('Сервер 1С', 'Сервер лицензирования')


def default_srvinfo():
    return str(PureWindowsPath(os.environ.get('ProgramW6432') or os.environ.get('ProgramFiles') or r'C:\Program Files') / '1cv8' / 'srvinfo')


def display_template(role, port, version):
    title = '1C License Server' if role == 'Сервер лицензирования' else '1C Server'
    return f'{title} | {port} | {version}'


def service_options(metadata, settings):
    role = settings.get('service_role', SERVICE_ROLES[0])
    if role not in SERVICE_ROLES:
        raise ValueError('Выберите назначение службы 1С')
    port, registration, first, last = agent_ports(settings.get('service_port', '1540'))
    data = str(settings.get('srvinfo', default_srvinfo())).strip()
    quoted_path(data)
    path = PureWindowsPath(data)
    if path == PureWindowsPath(path.anchor) or any(char in data for char in '*?<>|'):
        raise ValueError('Укажите отдельную папку srvinfo полным путём')
    version = metadata['properties']['ProductVersion']
    display = settings.get('service_display_name', display_template(role, port, version))
    display = clean_text(display, 'Отображаемое имя службы')
    return dict(role=role, port=port, registration=registration, first=first, last=last,
                srvinfo=str(path), display=display, debug=bool(settings.get('service_debug')))


def service_image_path(executable, options):
    image = (quoted_path(executable) + f" -srvc -agent -regport {options['registration']} -port {options['port']}"
             + f" -range {options['first']}:{options['last']} -d " + quoted_path(options['srvinfo']))
    return image + (' -debug' if options['debug'] else '')


class ServiceStatus(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in ('type', 'state', 'controls', 'exit_code', 'specific_exit', 'checkpoint', 'wait_hint')]


class ServiceConfig(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('start', wintypes.DWORD), ('error', wintypes.DWORD),
                ('binary', wintypes.LPWSTR), ('group', wintypes.LPWSTR), ('tag', wintypes.DWORD),
                ('dependencies', wintypes.LPWSTR), ('account', wintypes.LPWSTR), ('display', wintypes.LPWSTR)]


class WindowsService:
    def __init__(self, handle, dll):
        self.handle, self.dll = handle, dll

    def config(self):
        buffer, needed = ctypes.create_string_buffer(8192), wintypes.DWORD()
        if not self.dll.QueryServiceConfigW(self.handle, buffer, len(buffer), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        value = ctypes.cast(buffer, ctypes.POINTER(ServiceConfig)).contents
        return dict(binary=value.binary, account=value.account, display=value.display)

    def state(self):
        status = ServiceStatus()
        if not self.dll.QueryServiceStatus(self.handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.state

    def ensure_stopped(self):
        state = self.state()
        if state == 1:
            return
        if state != 3:  # SERVICE_STOP_PENDING
            status = ServiceStatus()
            if not self.dll.ControlService(self.handle, 1, ctypes.byref(status)):
                code = ctypes.get_last_error()
                if code != 1062:  # another caller may have stopped it
                    raise ctypes.WinError(code)
        deadline = time.monotonic() + 60
        while self.state() != 1:
            if time.monotonic() >= deadline:
                raise RuntimeError('Служба 1С не остановилась за 60 с. Настройка не применена.')
            time.sleep(.2)


@contextmanager
def open_service(name):
    dll = ctypes.WinDLL('advapi32', use_last_error=True)
    declarations = {
        'OpenSCManagerW': ([wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD], wintypes.HANDLE),
        'OpenServiceW': ([wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD], wintypes.HANDLE),
        'CloseServiceHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'QueryServiceConfigW': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'QueryServiceStatus': ([wintypes.HANDLE, ctypes.POINTER(ServiceStatus)], wintypes.BOOL),
        'ControlService': ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(ServiceStatus)], wintypes.BOOL),
    }
    for key, (args, result) in declarations.items():
        function = getattr(dll, key)
        function.argtypes, function.restype = args, result
    manager = dll.OpenSCManagerW(None, None, 1)
    if not manager:
        raise ctypes.WinError(ctypes.get_last_error())
    handle = None
    try:
        handle = dll.OpenServiceW(manager, name, 1 | 4 | 0x20)  # query config/status, stop; never start
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        yield WindowsService(handle, dll)
    finally:
        if handle:
            dll.CloseServiceHandle(handle)
        dll.CloseServiceHandle(manager)


def prepare_srvinfo(path, account):
    if not account:
        raise RuntimeError('Не определён пользователь установленной службы 1С')
    Path(path).mkdir(parents=True, exist_ok=True)
    if account.startswith('.\\'):
        account = os.environ.get('COMPUTERNAME', '.') + account[1:]
    executable = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'icacls.exe')
    # Grant access to this folder, with inheritance for its contents; do not
    # walk unrelated directories or reset their existing permissions.
    result = subprocess.run([executable, path, '/grant', account + ':(OI)(CI)M', '/q'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=60)
    if result.returncode:
        raise RuntimeError('Не удалось выдать пользователю службы доступ к srvinfo: ' + decode_output(result.stdout))


def configure_installed_service(msi, definition, metadata, settings, event):
    event(dict(kind='stage', text='Настройка службы 1С · служба останется остановленной', cancellable=False))
    try:
        options = service_options(metadata, settings)
        executable = msi.component_path(definition['product'], definition['component_guid'])
        if PureWindowsPath(executable).name.casefold() != 'ragent.exe':
            raise RuntimeError('Установленный компонент службы не является ragent.exe')
        image = service_image_path(executable, options)
        name = clean_text(definition['name'], 'Системное имя службы', service_name=True)
        with open_service(name) as service:
            config = service.config()
            current = windows_arguments(config['binary'])
            if not current or ntpath.normcase(ntpath.normpath(current[0])) != ntpath.normcase(ntpath.normpath(executable)):
                raise RuntimeError('Путь службы не совпадает с ragent.exe установленной версии. Чужая служба не изменена.')
            service.ensure_stopped()
            prepare_srvinfo(options['srvinfo'], config['account'])
            command = ServiceCommand('config', name, ('config', name, 'binPath=', image, 'DisplayName=', options['display']))
            event(dict(kind='message', text=command.preview))
            event(dict(kind='message', text=run_service_command(command)))
            actual = service.config()
            if windows_arguments(actual['binary']) != windows_arguments(image) or actual['display'] != options['display']:
                raise RuntimeError('Windows не подтвердила параметры службы после sc config')
            service.ensure_stopped()
        event(dict(kind='message', text='Служба настроена и остановлена: ' + options['display'] + '\n' + image))
        return dict(name=name, display=options['display'], image=image, stopped=True)
    except Exception as exc:
        raise RuntimeError('Платформа уже установлена, но настройка службы не завершена: ' + str(exc) +
                           '\nНе запускайте установку повторно для исправления параметров: проверьте службу в Windows.') from exc


def check_ras_destination(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Services\\' + name):
            pass
    except FileNotFoundError:
        return
    raise ValueError('Служба ' + name + ' уже существует. Выберите другой порт RAS перед установкой.')


def create_ras_service(msi, metadata, settings, event):
    options = ras_service_options(settings, metadata['properties']['ProductVersion'], windows=True)
    name = options['name']
    try:
        event(dict(kind='stage', text='Создание и запуск службы RAS', cancellable=False))
        executable = msi.component_path(metadata['properties']['ProductCode'], metadata['ras_component'])
        if PureWindowsPath(executable).name.casefold() != 'ras.exe':
            raise RuntimeError('Компонент выбранной версии не является ras.exe')
        check_ras_destination(name)
        image = quoted_path(executable) + f" cluster --service --port={options['port']} {options['endpoint']}"
        command = ServiceCommand('create', name, ('create', name, 'binPath=', image, 'start=', 'auto', 'DisplayName=', options['display']), executable)
        event(dict(kind='message', text=command.preview))
        event(dict(kind='message', text=run_service_command(command)))
        with open_service(name) as service:
            config = service.config()
            if windows_arguments(config['binary']) != windows_arguments(image) or config['display'] != options['display']:
                raise RuntimeError('Windows не подтвердила параметры созданной службы RAS')
            event(dict(kind='message', text=run_service_command(ServiceCommand('start', name, ('start', name)))))
            deadline = time.monotonic() + 30
            while service.state() != 4:  # SERVICE_RUNNING
                if time.monotonic() >= deadline:
                    raise RuntimeError('Служба RAS не перешла в состояние RUNNING за 30 с')
                time.sleep(.2)
        event(dict(kind='message', text='RAS запущен; автозапуск включён: ' + name + '\nОтображаемое имя: ' + options['display'] + '\n' + image))
        return dict(name=name, display=options['display'], image=image, running=True)
    except Exception as exc:
        raise RuntimeError('Платформа установлена, но создание или запуск RAS не завершены: ' + str(exc) +
                           '\nПроверьте службу ' + name + ' и занятость порта; существующая служба не заменяется.') from exc
