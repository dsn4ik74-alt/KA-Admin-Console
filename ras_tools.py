"""Portable, versioned RAS/RAC packages from the official 1C distribution."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
from uuid import uuid4

from local_ras import LocalRasManager, SearchCancelled, decode_output
from postgres_runner import OperationCancelled


def version_key(value):
    return tuple(int(part) for part in value.split('.'))


def bundled_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'vendor' / '1c-ras'


def cache_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'KAConsole' / 'ras'


def package_tools(directory, verify=False):
    directory = Path(directory)
    metadata = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    version = metadata['version']
    if not re.fullmatch(r'8\.[35]\.\d+\.\d+', version):
        raise ValueError('Некорректная версия комплекта RAS')
    files = metadata['files']
    if not {'bin/rac.exe', 'bin/ras.exe'} <= files.keys():
        raise ValueError('В комплекте нет RAC/RAS')
    for name, digest in files.items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError('Неполный комплект RAS: ' + name)
        if verify and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Повреждён файл RAS: ' + name)
    return str(directory / 'bin/rac.exe'), str(directory / 'bin/ras.exe'), version


def managed_tools():
    """Only published packages are visible; an update's staging tree is private."""
    directories = [bundled_root()]
    try:
        directories.extend(path for path in cache_root().iterdir() if path.is_dir() and not path.name.startswith('.'))
    except OSError:
        pass
    found = []
    for directory in directories:
        try:
            if not (directory / '.retired').exists():
                found.append(package_tools(directory))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    # Prefer an updated package over the bundled copy of the same version.
    return sorted(found, key=lambda tools: (version_key(tools[2]), not Path(tools[1]).is_relative_to(bundled_root())), reverse=True)


def tools_label(ras, version):
    path = Path(ras)
    source = 'встроенный' if path.is_relative_to(bundled_root()) else 'обновлённый' if path.is_relative_to(cache_root()) else 'локальный'
    return f'{source} RAS {version} ({ras})'


@contextlib.contextmanager
def package_lock(directory, exclusive=False):
    """Shared process leases keep an updater from deleting a running package."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    with (Path(directory) / '.use.lock').open('a+b') as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b'0')
            handle.flush()
        native, overlap = msvcrt.get_osfhandle(handle.fileno()), Overlapped()
        if not kernel.LockFileEx(native, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlap)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            kernel.UnlockFileEx(native, 0, 1, 0, ctypes.byref(overlap))


def remove_retired(directory, root):
    directory, root = Path(directory), Path(root).resolve()
    if (directory.is_symlink() or directory.resolve().parent != root or
            not re.fullmatch(r'8\.[35]\.\d+\.\d+-[a-f0-9]{12}', directory.name) or
            not (directory / '.retired').is_file()):
        return False
    try:
        marker = json.loads((directory / '.retired').read_text(encoding='utf-8'))
        if marker != dict(package=directory.name):
            return False
        with package_lock(directory, exclusive=True):
            pending = [directory]
            while pending:
                path = pending.pop()
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) &
                        getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024) or not path.resolve().is_relative_to(root)):
                    return False
                if path.is_dir():
                    pending.extend(path.iterdir())
        # The retirement marker prevents any new lease after the exclusive
        # lock is released. Windows cannot unlink an open lock file.
        # Keep the ownership marker until all payload files are gone, so a
        # temporary sharing/antivirus failure can be retried safely.
        for path in directory.iterdir():
            if path.name in ('.retired', '.use.lock'):
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        (directory / '.use.lock').unlink(missing_ok=True)
        (directory / '.retired').unlink()
        directory.rmdir()
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextlib.contextmanager
def use_tools(ras):
    directory = Path(ras).parent.parent
    if directory.resolve().parent != cache_root().resolve():
        yield
        return
    try:
        with package_lock(directory):
            if (directory / '.retired').exists():
                raise OSError('Этот комплект RAS заменён обновлением')
            yield
    finally:
        if (directory / '.retired').exists():
            remove_retired(directory, cache_root())


def cleanup_old_tools(keep, event, root=None):
    root, keep = Path(root) if root is not None else cache_root(), Path(keep).resolve()
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if (directory.is_symlink() or directory.resolve().parent != root.resolve() or directory.resolve() == keep or
                not re.fullmatch(r'8\.[35]\.\d+\.\d+-[a-f0-9]{12}', directory.name)):
            continue
        try:
            if not (directory / '.retired').exists():
                _, _, version = package_tools(directory)
                (directory / '.retired').write_text(json.dumps(dict(package=directory.name)), encoding='utf-8')
            else:
                version = directory.name.rsplit('-', 1)[0]
            removed = remove_retired(directory, root)
            event(dict(kind='message', text=('Удалён старый комплект RAS ' if removed else
                'Старый комплект RAS будет удалён после завершения использующей его задачи: ') + version))
        except (OSError, ValueError, KeyError, TypeError):
            continue


def check_cancel(cancel):
    if cancel.is_set():
        raise OperationCancelled('Обновление RAS прервано')


def probe_command(args, cancel):
    check_cancel(cancel)
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        env=dict(os.environ, PATH=str(Path(args[0]).parent) + os.pathsep +
                 os.environ.get('SystemRoot', r'C:\Windows') + r'\System32'))
    try:
        for _ in range(100):
            check_cancel(cancel)
            try:
                output = process.communicate(timeout=.1)[0]
                return process.returncode, output
            except subprocess.TimeoutExpired:
                continue
        raise RuntimeError('RAS/RAC не ответил на проверку за 10 с')
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def probe_package(directory, cancel):
    """Read-only self tests, without services or a remote 1C server."""
    rac, ras, version = package_tools(directory, verify=True)
    for tool in (rac, ras):
        code, output = probe_command([tool, '--version'], cancel)
        if code or version not in decode_output(output):
            raise RuntimeError('Не прошёл проверку ' + Path(tool).name + ': ' + decode_output(output).strip())
    manager = LocalRasManager(cancel)
    try:
        clean_env = dict(os.environ, PATH=str(Path(ras).parent) + os.pathsep +
                         os.environ.get('SystemRoot', r'C:\Windows') + r'\System32')
        with manager.connect(ras, '127.0.0.1:1', env=clean_env) as endpoint:
            _, output = probe_command([rac, 'cluster', 'list', endpoint], cancel)
            detail = decode_output(output)
            if '127.0.0.1' not in detail or re.search(r'loading component|ошибка загрузки компонент', detail, re.I):
                raise RuntimeError('Не прошёл проверку связи RAC/RAS: ' + detail)
    except SearchCancelled as exc:
        raise OperationCancelled('Обновление RAS прервано') from exc
    finally:
        manager.close()
    return version


def extract_tools(msi, destination, release, event, cancel):
    """Read MSI/CAB as archives. No MSI actions or registration are executed."""
    from platform_install import Msi, run_archive, seven_zip
    import pefile

    msi, destination = Path(msi), Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Для извлечения RAS нужна новая пустая папка')
    database = Msi()
    properties = database.query(msi, 'SELECT `Property`,`Value` FROM `Property`', ('key', 'value'))
    if dict((row['key'], row['value']) for row in properties).get('ProductVersion') != release['version']:
        raise ValueError('Версия MSI не совпадает с релизом RAS')
    rows = database.query(msi, 'SELECT `File`,`FileName`,`Component_`,`Sequence` FROM `File`',
                          ('id', 'name', 'component', 'sequence'))
    media = database.query(msi, 'SELECT `LastSequence`,`Cabinet` FROM `Media`', ('last', 'cabinet'))
    media.sort(key=lambda row: int(row['last']))
    names = {}
    for row in rows:
        row['name'] = row['name'].split('|')[-1]
        names.setdefault(row['name'].casefold(), row)
    seeds = [row for row in rows if row['component'] in ('Base_SharedDLL_noUI', 'CS_Server_Admin', 'Licenses')]
    for name in ('ras.exe', 'rac.exe'):
        if name not in names:
            raise ValueError('В дистрибутиве отсутствует ' + name)
        seeds.append(names[name])
    # Loaded by component name rather than a PE import when RAS starts.
    for name in ('rtrsrvc.dll', 'rscalls.dll', 'pack.dll'):
        if name in names:
            seeds.append(names[name])
    destination.mkdir(parents=True, exist_ok=True)
    raw = destination / '.cab-files'
    raw.mkdir(exist_ok=True)
    pending, copied = seeds, set()
    while pending:
        check_cancel(cancel)
        groups, batch = {}, {}
        for row in pending:
            name = row['name']
            if row['id'] in copied:
                continue
            if not re.fullmatch(r'[\w.-]+', row['id']) or Path(name).name != name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
                raise ValueError('Недопустимое имя файла в MSI')
            cabinet = next((item['cabinet'] for item in media if int(row['sequence']) <= int(item['last'])), '')
            cab_path = (msi.parent / cabinet).resolve()
            if not cabinet or not cab_path.is_relative_to(msi.parent.resolve()) or not cab_path.is_file():
                raise ValueError('Не найден внешний CAB дистрибутива')
            groups.setdefault(str(cab_path), []).append(row['id'])
            batch[row['id']] = row
        if not batch:
            break
        for cabinet, identifiers in groups.items():
            event(dict(kind='stage', text='Извлечение файлов RAS/RAC и библиотек'))
            run_archive([seven_zip(), 'x', cabinet, '-o' + str(raw), '-y', '-bb0', '-sccUTF-8', *identifiers], cancel)
        pending = []
        for identifier, row in batch.items():
            source = raw / identifier
            if not source.is_file() or source.is_symlink():
                raise ValueError('Не извлечён файл RAS: ' + row['name'])
            target = destination / ('licenses' if row['component'] == 'Licenses' else 'bin') / row['name']
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            copied.add(identifier)
            if target.suffix.casefold() in ('.exe', '.dll'):
                with pefile.PE(str(target), fast_load=True) as pe:
                    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                                                         pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
                    for entry in (*getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []), *getattr(pe, 'DIRECTORY_ENTRY_DELAY_IMPORT', [])):
                        library = entry.dll.decode('ascii').casefold()
                        # API sets are supplied by supported Windows versions;
                        # MSI's VC redistributable merge module is not Data1.cab.
                        dependency = None if library.startswith(('api-ms-', 'ext-ms-')) else names.get(library)
                        if dependency and dependency['id'] not in copied:
                            pending.append(dependency)
        # Include the Russian and fallback message resources for each included
        # module. They are loaded dynamically, outside the PE import table.
        for row in batch.values():
            if row['name'].lower().endswith(('.dll', '.exe')):
                for suffix in ('_root.res', '_ru.res'):
                    resource = names.get(Path(row['name']).stem.casefold() + suffix)
                    if resource and resource['id'] not in copied:
                        pending.append(resource)
        if not pending:
            write_manifest(destination, release)
            try:
                probe_package(destination, cancel)
            except RuntimeError as exc:
                # Some platform components use 1C's loader, not PE imports.
                # Resolve only a named DLL present in this official MSI, then
                # test again. Each file is considered once, so this is bounded.
                match = re.search(r'(?:loading component|загрузки компонент[ыа])\s+([\w.-]+)', str(exc), re.I)
                name = match.group(1).casefold() if match else ''
                dependency = names.get(name if name.endswith('.dll') else name + '.dll')
                if not dependency or dependency['id'] in copied:
                    raise
                event(dict(kind='stage', text='Дополнение библиотек RAS: ' + dependency['name']))
                pending.append(dependency)
    # The deletion target is the fixed, private child created just above.
    if raw.resolve().parent != destination.resolve():
        raise ValueError('Неверная папка временных CAB-файлов')
    shutil.rmtree(raw)
    return str(destination)


def write_manifest(destination, release):
    destination = Path(destination)
    files = {file.relative_to(destination).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
             for folder in ('bin', 'licenses') for file in (destination / folder).rglob('*') if file.is_file()}
    (destination / 'manifest.json').write_text(json.dumps(dict(version=release['version'], date=release.get('date', ''),
        source=release['url'], files=files), ensure_ascii=False, indent=2), encoding='utf-8')


_UPDATE_LOCK = threading.Lock()


def update_tools(portal, event, cancel, root=None):
    from install_workspace import create_workspace, cleanup_workspace
    from platform_install import extract_distribution

    if not _UPDATE_LOCK.acquire(blocking=False):
        raise ValueError('Обновление RAS уже выполняется в другой вкладке')
    context = None
    try:
        event(dict(kind='stage', text='Проверка последнего релиза RAS на ИТС'))
        release = max(portal.releases('8.5'), key=lambda row: version_key(row['version']))
        root = Path(root) if root is not None else cache_root()
        existing = [root] if (root / 'manifest.json').is_file() else []
        if root.is_dir():
            existing.extend(path for path in root.iterdir() if path.is_dir() and not path.name.startswith('.'))
        existing.append(bundled_root())
        for directory in existing:
            try:
                current = package_tools(directory)[2]
                if version_key(current) >= version_key(release['version']) and not (directory / '.retired').exists():
                    probe_package(directory, cancel)
                    cleanup_old_tools(directory, event, root)
                    return dict(version=current, updated=False, path=str(directory))
            except OperationCancelled:
                raise
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                continue
        rows = portal.distributions(release, 'windows')
        distribution = next((row for row in rows if row['architecture'] == '64' and
                             row['filename'].casefold().startswith('windows64full_')), None)
        if distribution is None:
            raise ValueError('На ИТС не найден полный 64-разрядный дистрибутив Windows с RAS/RAC')
        context = create_workspace(root / '.downloads', release['version'])
        work = Path(context['root'])
        archive = portal.download(distribution, work, event)
        msi = extract_distribution(archive, event, cancel)
        package = work / 'package'
        extract_tools(msi, package, release, event, cancel)
        check_cancel(cancel)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / (release['version'] + '-' + uuid4().hex[:12])
        package.rename(destination)
        cleanup_old_tools(destination, event, root)
        return dict(version=release['version'], updated=True, path=str(destination))
    finally:
        try:
            if context is not None:
                cleanup_workspace(context)
        finally:
            _UPDATE_LOCK.release()
