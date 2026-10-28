"""Launch the unmodified portable WinDirStat and host only its own window."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4


def vendor_directory():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'vendor' / 'windirstat'


def cache_file(source, destination, digest):
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Контрольная сумма встроенного файла WinDirStat не совпала: ' + source.name)
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
        return
    temporary = destination.with_name(destination.name + '-' + uuid4().hex + '.tmp')
    try:
        shutil.copyfile(source, temporary)
        try:
            temporary.replace(destination)
        except OSError:
            # Another tab may have finished preparing the identical file first.
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise
    finally:
        temporary.unlink(missing_ok=True)


def prepare_executable(cache=None):
    root = vendor_directory()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    machine = (os.environ.get('PROCESSOR_ARCHITEW6432') or os.environ.get('PROCESSOR_ARCHITECTURE', 'AMD64')).upper()
    architecture = 'arm64' if machine == 'ARM64' else 'x64' if sys.maxsize > 2 ** 32 else 'x86'
    relative = f'portable/{architecture}/WinDirStat.exe'
    source = root / relative
    digest = manifest['files'][relative]
    base = Path(cache) if cache else Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'KAConsole' / 'WinDirStat'
    destination = base / manifest['version'] / architecture
    destination.mkdir(parents=True, exist_ok=True)
    executable = destination / 'WinDirStat.exe'
    cache_file(source, executable, digest)
    # Upstream loads a full external translation before its built-in strings.
    # Deploy it for existing caches too, without changing the portable INI.
    cache_file(root / 'lang_ru.txt', destination / 'lang_ru.txt', manifest['files']['lang_ru.txt'])
    ini = executable.with_suffix('.ini')
    if not ini.exists():
        # The upstream application explicitly treats a sibling INI as portable.
        try:
            with ini.open('x', encoding='utf-16') as output:
                output.write('[Options]\nLanguageId=1049\nDarkMode=0\nShowElevationPrompt=0\nAutoElevate=0\n')
        except FileExistsError:
            pass  # Another console instance already prepared the same cache.
    return executable


def launch(executable, path=''):
    arguments = [str(executable)]
    if path:
        if not Path(path).is_dir() or any(c in str(path) for c in '\r\n\x00|'):
            raise ValueError('Выберите существующую папку или диск для анализа')
        arguments.append(str(Path(path).resolve()))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    # Hiding the startup also hides WinDirStat's initial modal drive picker.
    # Only reparent a fully initialized, responsive main frame afterwards.
    startup.wShowWindow = 1
    return subprocess.Popen(arguments, cwd=Path(executable).parent, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup)


def fitted_widths(available, preferred, scale=1):
    """Fit visible columns, retaining hidden columns and meaningful proportions."""
    preferred = [max(0, int(width)) for width in preferred]
    visible = [index for index, width in enumerate(preferred) if width]
    result = [0] * len(preferred)
    available = max(0, int(available))
    if not visible or not available:return result
    flex = max(visible, key=lambda index:preferred[index])
    minimum = [min(preferred[index], round((150 if index == flex else 55)*scale))
               for index in visible]
    if sum(preferred) <= available:
        result = preferred[:]
        result[flex] += available-sum(result)
        return result
    # Allocate integer pixels with a stable remainder; repeated resizes do not drift.
    if sum(minimum) >= available:
        baseline = [1 if available >= len(visible) else 0] * len(visible)
        weights = [max(1,width-base) for width,base in zip(minimum,baseline)]
    else:
        baseline = minimum
        weights = [preferred[index]-width for index,width in zip(visible,minimum)]
    remaining = available-sum(baseline)
    total = sum(weights)
    shares = [remaining*weight//total for weight in weights]
    remainder = remaining-sum(shares)
    order = sorted(range(len(visible)), key=lambda position:-(remaining*weights[position]%total))
    for position in order[:remainder]:shares[position] += 1
    for position,index in enumerate(visible):result[index] = baseline[position]+shares[position]
    return result


class NativeHost:
    def __init__(self):
        self.dll = ctypes.WinDLL('user32', use_last_error=True)
        self._menus = {}
        self._frames = {}
        self._column_profiles = {}
        pointer = ctypes.c_ssize_t
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        definitions = {
            'EnumWindows': ([self.callback, wintypes.LPARAM], wintypes.BOOL),
            'EnumChildWindows': ([wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            'GetWindowThreadProcessId': ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            'GetWindow': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetWindowLongPtrW': ([wintypes.HWND, ctypes.c_int], pointer),
            'SetWindowLongPtrW': ([wintypes.HWND, ctypes.c_int, pointer], pointer),
            'SetParent': ([wintypes.HWND, wintypes.HWND], wintypes.HWND),
            'GetParent': ([wintypes.HWND], wintypes.HWND),
            'GetMenu': ([wintypes.HWND], wintypes.HMENU),
            'SetMenu': ([wintypes.HWND, wintypes.HMENU], wintypes.BOOL),
            'GetSubMenu': ([wintypes.HMENU, ctypes.c_int], wintypes.HMENU),
            'GetMenuItemCount': ([wintypes.HMENU], ctypes.c_int),
            'GetMenuState': ([wintypes.HMENU, wintypes.UINT, wintypes.UINT], wintypes.UINT),
            'GetMenuStringW': ([wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT], ctypes.c_int),
            'TrackPopupMenuEx': ([wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            'SendMessageTimeoutW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], pointer),
            'ShowWindow': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            'SetWindowPos': ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowEnabled': ([wintypes.HWND], wintypes.BOOL),
            'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'GetClientRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'SetForegroundWindow': ([wintypes.HWND], wintypes.BOOL),
            'EnableWindow': ([wintypes.HWND, wintypes.BOOL], wintypes.BOOL),
        }
        for key, (args, result) in definitions.items():
            function = getattr(self.dll, key)
            function.argtypes, function.restype = args, result

    def find_window(self, pid):
        found = []
        def visit(hwnd, unused):
            owner = wintypes.DWORD()
            self.dll.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and not self.dll.GetWindow(hwnd, 4) and self.dll.GetMenu(hwnd):
                found.append(hwnd)
            return True
        self.dll.EnumWindows(self.callback(visit), 0)
        return found[0] if found else None

    def dialogs(self, pid):
        found = []
        def visit(hwnd, unused):
            owner = wintypes.DWORD()
            self.dll.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                name = ctypes.create_unicode_buffer(256)
                self.dll.GetClassNameW(hwnd, name, len(name))
                if name.value == '#32770':found.append(hwnd)
            return True
        self.dll.EnumWindows(self.callback(visit), 0)
        return found

    def ready(self, hwnd):
        if not (self.dll.IsWindow(hwnd) and self.dll.IsWindowVisible(hwnd) and
                self.dll.IsWindowEnabled(hwnd) and self.dll.GetWindow(hwnd, 5)):
            return False
        menus = self.menus(hwnd)
        if not menus or any(not title or title.startswith('IDS_') for _, title, _ in menus):
            return False
        result = ctypes.c_size_t()
        return bool(self.dll.SendMessageTimeoutW(hwnd, 0, 0, 0, 2, 50, ctypes.byref(result)))

    def show_dialog(self, hwnd, owner=None):
        if self.dll.IsWindow(hwnd):
            current = self.dll.GetWindow(hwnd, 4)
            pid = wintypes.DWORD()
            if current:self.dll.GetWindowThreadProcessId(current, ctypes.byref(pid))
            if owner and (not current or pid.value == os.getpid()):
                # A native picker must belong to WinDirStat, not the cross-
                # process Tk ancestor returned by its GetDialogOwner helper.
                self.dll.SetWindowLongPtrW(hwnd, -8, owner)
                if current and pid.value == os.getpid():self.dll.EnableWindow(current, True)
            if not self.dll.IsWindowVisible(hwnd):self.dll.ShowWindow(hwnd, 1)
            self.dll.SetForegroundWindow(hwnd)

    def attach(self, hwnd, parent):
        # GetMenu is not defined for a child window. Keep the original native
        # submenu handles before converting the main frame into a child.
        self._menus.pop(hwnd, None)
        menus = self.menus(hwnd)
        if not menus or any(title.startswith('IDS_') for _,title,_ in menus):
            raise RuntimeError('WinDirStat ещё не завершил создание меню')
        style = self.dll.GetWindowLongPtrW(hwnd, -16)
        main_menu = self.dll.GetMenu(hwnd)
        rect = wintypes.RECT()
        if not self.dll.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A child has no system menu/caption; the original application menu is
        # exposed by popup handles below. Client controls remain unmodified.
        self.dll.SetWindowLongPtrW(hwnd, -16, (style & ~(0x80000000 | 0x00cf0000 | 0x21000000)) | 0x40000000 | 0x02000000)
        ctypes.set_last_error(0)
        self.dll.SetParent(hwnd, parent)
        if self.dll.GetParent(hwnd) != parent:
            self.dll.SetWindowLongPtrW(hwnd, -16, style)
            self.dll.ShowWindow(hwnd, 4)
            raise RuntimeError('Windows не разрешила встроить окно WinDirStat: ' + str(ctypes.get_last_error()))
        self._frames[hwnd] = (style, (rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top), main_menu)
        self._menus[hwnd] = menus
        self.dll.ShowWindow(hwnd, 4)

    def detach(self, hwnd, activate=True):
        if not self.dll.IsWindow(hwnd):return
        original = self._frames.get(hwnd)
        if original:
            style, rect, menu = original
            child_style = self.dll.GetWindowLongPtrW(hwnd, -16)
            self.dll.SetWindowLongPtrW(hwnd, -16, style & ~0x40000000)
            self.dll.SetParent(hwnd, None)
            if self.dll.GetParent(hwnd):
                self.dll.SetWindowLongPtrW(hwnd, -16, child_style)
                raise RuntimeError('Windows не разрешила вынести WinDirStat в отдельное окно')
            self.dll.SetMenu(hwnd, menu)
            self.dll.SetWindowPos(hwnd, None, *rect, 0x4000 | 0x10 | 0x4 | 0x20)
        self._menus.pop(hwnd, None)
        self.dll.ShowWindow(hwnd, 9 if activate else 4)
        if activate:self.dll.SetForegroundWindow(hwnd)

    def command(self, hwnd, command):
        if command == 32779:
            for index, _, menu in self.menus(hwnd):
                if self.dll.GetMenuState(menu, command, 0) == 0xffffffff:continue
                result = ctypes.c_size_t()
                if not self.dll.SendMessageTimeoutW(hwnd, 0x117, menu, index, 2, 200, ctypes.byref(result)):
                    raise RuntimeError('WinDirStat пока не отвечает. Дождитесь завершения текущего действия.')
                if self.dll.GetMenuState(menu, command, 0) & 3:
                    raise ValueError('Выберите файл или папку в таблице WinDirStat для открытия в Проводнике.')
                break
        if not self.dll.IsWindow(hwnd) or not self.dll.PostMessageW(hwnd, 0x111, command, 0):
            raise RuntimeError('Не удалось передать команду в WinDirStat')

    def resize(self, hwnd, width, height):
        self.dll.SetWindowPos(hwnd, None, 0, 0, max(1, width), max(1, height), 0x4000 | 0x10 | 0x4 | 0x20)

    def fit_columns(self, hwnd, pid):
        # Only numeric Win32 messages: no remote buffers, file reads or clipboard.
        # Bound a whole pass so an unresponsive native control cannot freeze Tk.
        deadline = time.monotonic()+.06
        controls = []
        def visit(control, unused):
            if not self.dll.IsWindowVisible(control):return True
            owner = wintypes.DWORD()
            self.dll.GetWindowThreadProcessId(control, ctypes.byref(owner))
            if owner.value != pid:return True
            name = ctypes.create_unicode_buffer(128)
            self.dll.GetClassNameW(control, name, len(name))
            if name.value == 'SysListView32' and self.dll.GetWindowLongPtrW(control,-16)&3 == 1:
                controls.append(control)
            return True
        self.dll.EnumChildWindows(hwnd,self.callback(visit),0)
        def send(control,message,index=0,value=0):
            if time.monotonic() >= deadline:return None
            result = ctypes.c_size_t()
            if not self.dll.SendMessageTimeoutW(control,message,index,value,2,15,ctypes.byref(result)):
                return None
            return result.value
        for control in controls:
            header = send(control,0x101f)  # LVM_GETHEADER
            count = send(header,0x1200) if header else None  # HDM_GETITEMCOUNT
            if count is None:return
            if not 1 <= count <= 64:continue
            rect = wintypes.RECT()
            if not self.dll.GetClientRect(control,ctypes.byref(rect)):continue
            available = rect.right-rect.left-2
            if available < 96:continue
            current = [send(control,0x101d,index) for index in range(count)]  # LVM_GETCOLUMNWIDTH
            if any(width is None for width in current):return
            dpi = getattr(self.dll,'GetDpiForWindow',None)
            if dpi:
                dpi.argtypes,dpi.restype = [wintypes.HWND],wintypes.UINT
            scale = max(1,(dpi(control) if dpi else 96)/96)
            key = control,header,count
            profile = self._column_profiles.setdefault(key,tuple(current))
            # Hidden columns remain hidden; newly shown columns get their own baseline.
            preferred = [min(width or actual,round((480 if index == 0 else 240)*scale)) if actual else 0
                         for index,(width,actual) in enumerate(zip(profile,current))]
            self._column_profiles[key] = tuple(width or actual for width,actual in zip(profile,current))
            desired = fitted_widths(available,preferred,scale)
            changed = False
            for index,(actual,width) in enumerate(zip(current,desired)):
                if actual and actual != width:
                    if not send(control,0x101e,index,width):return  # LVM_SETCOLUMNWIDTH
                    changed = True
            if changed:self.dll.PostMessageW(control,0x114,6,0)  # WM_HSCROLL / SB_LEFT

    def menus(self, hwnd):
        if hwnd in self._menus:
            return self._menus[hwnd]
        menu, result = self.dll.GetMenu(hwnd), []
        for index in range(self.dll.GetMenuItemCount(menu)):
            title = ctypes.create_unicode_buffer(256)
            self.dll.GetMenuStringW(menu, index, title, len(title), 0x400)
            submenu = self.dll.GetSubMenu(menu, index)
            if submenu:
                result.append((index, title.value.replace('&', ''), submenu))
        return result

    def popup(self, hwnd, host, index, menu, x, y):
        result = ctypes.c_size_t()
        # Let WinDirStat update enable/check states. A hung worker may not block
        # the console's message loop indefinitely.
        self.dll.SendMessageTimeoutW(hwnd, 0x117, menu, index, 2, 200, ctypes.byref(result))
        command = self.dll.TrackPopupMenuEx(menu, 0x100 | 0x2, x, y, host, None)
        return command

    def close(self, hwnd):
        if self.dll.IsWindow(hwnd):
            self.dll.PostMessageW(hwnd, 0x10, 0, 0)
