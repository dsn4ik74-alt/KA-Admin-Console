"""Local setup installs and verified template deployment to a 1C cluster."""
from contextlib import contextmanager
import configparser
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import threading

from add_cluster_infobase import ClusterClient, redact, validate_parameters
from console_core import known_folder, platform_roots
from configuration_efd import extract_efd
from infobase_address import infobase_address
from install_workspace import create_configuration_workspace, cleanup_workspace
from local_ras import decode_output
from platform_install import unpack_distribution
from postgres_runner import OperationCancelled

_SETUP_LOCK = threading.Lock()


def text_file(path, limit=2 * 1024 * 1024):
    with Path(path).open('rb') as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Слишком большой файл описания шаблона или настроек.')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp1251')


def templates_in(root, version='', event=None):
    root = Path(root).resolve()
    result = []
    missing = []
    count = 0
    for manifest in root.rglob('*.mft'):
        count += 1
        if count > 2000:
            raise ValueError('В каталоге слишком много шаблонов.')
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(root):
            raise ValueError('Описание шаблона содержит ссылку вне папки установки.')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string('[General]\n' + text_file(manifest))
        actual_version = parser.get('General', 'Version', fallback='')
        if version and actual_version != version:
            continue
        for section in parser.sections():
            if not section.casefold().startswith('config'):
                continue
            source = parser.get(section, 'Source', fallback='').strip().strip('"')
            relative = PureWindowsPath(source)
            if not source or relative.is_absolute() or relative.drive or relative.root or '..' in relative.parts or any(':' in p for p in relative.parts):
                raise ValueError('Шаблон содержит некорректный путь Source.')
            target = manifest.parent.joinpath(*relative.parts)
            if target.suffix.casefold() not in ('.cf', '.dt'):
                continue
            if not target.resolve().is_relative_to(root) or target.is_symlink():
                raise ValueError('Файл шаблона расположен вне папки установки или является ссылкой: ' + source)
            if not target.is_file():
                # Official full distributions may advertise optional variants
                # that are not actually shipped (ERP_UH 3.3.4.9: 1cv8en.cf).
                # Do not discard the valid CF/DT variants from that manifest.
                missing.append(str(target.relative_to(root)))
                continue
            title = parser.get(section, 'Catalog', fallback=section)
            demo = bool(re.search(r'(?i)демо|\bdemo\b|demonstr', title))
            result.append(dict(title=title, variant='Демонстрационная' if demo else 'Пустая', demo=demo,
                               path=str(target.resolve()), manifest=str(manifest), version=actual_version,
                               configuration=parser.get('General', 'Name', fallback=''),
                               vendor=parser.get('General', 'Vendor', fallback='')))
    if missing:
        detail = '; '.join(missing[:20])
        if not result:
            raise ValueError('Не найдены файлы шаблонов, указанные в MFT: ' + detail)
        if event:
            event(dict(kind='message', text='ПРЕДУПРЕЖДЕНИЕ: Недоступные варианты шаблонов пропущены. '
                       'В дистрибутиве отсутствуют файлы: ' + detail + '. Остальные CF/DT доступны.'))
    return result


def launcher_config_path():
    roaming = known_folder(26) or Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    return roaming / '1C' / '1CEStart' / '1cestart.cfg'


def _without_template_path(text, destination):
    lines = []
    for line in text.splitlines():
        key, equals, value = line.partition('=')
        if equals and key.strip().casefold() == 'configurationtemplateslocation':
            if os.path.normcase(os.path.normpath(value.strip().strip('"'))) == os.path.normcase(str(destination)):
                continue
        lines.append(line)
    return '\n'.join(lines).strip()


def _write_launcher_config(path, text):
    # 1cestart.cfg is UTF-16LE (unlike the UTF-8 ibases.v8i list). Template
    # setup can otherwise ignore our destination and choose the default one.
    path.write_bytes(b'\xff\xfe' + text.encode('utf-16-le'))


def template_locations(config):
    """Read directory settings only; never log the complete user config."""
    result = []
    for line in text_file(config).splitlines():
        key, equals, value = line.partition('=')
        if equals and key.strip().casefold() == 'configurationtemplateslocation':
            value = value.strip().strip('"')
            if value and value not in result:
                result.append(value)
                if len(result) == 20:
                    break
    return result


def installed_templates(destination, version, event):
    """A zero setup exit code is not proof of an installed full template."""
    rows = templates_in(destination, event=event)
    selected = [row for row in rows if row['version'] == version]
    if selected:
        return selected
    versions = sorted({row['version'] or '(не указана)' for row in rows})
    if versions:
        detail = 'Найдены шаблоны других версий: ' + ', '.join(versions[:20]) + '. Выбрана версия ' + version + '.'
    else:
        counts = dict.fromkeys(('.mft', '.cf', '.dt', '.cfu'), 0)
        for index, path in enumerate(Path(destination).rglob('*')):
            if index >= 10000:
                break
            suffix = path.suffix.casefold()
            if suffix in counts and path.is_file() and not path.is_symlink():
                counts[suffix] += 1
        event(dict(kind='message', text='Проверка шаблонов: ' + ', '.join(key.upper()[1:] + ': ' + str(value) for key, value in counts.items())))
        if counts['.cfu'] and not counts['.cf'] and not counts['.dt']:
            detail = 'Найдены только файлы обновления CFU. Для создания базы нужен полный дистрибутив с CF/DT.'
        elif counts['.cf'] or counts['.dt']:
            detail = 'Файлы CF/DT есть, но манифест MFT не подтверждает шаблон выбранной версии ' + version + '.'
        else:
            detail = ('Файлы шаблонов CF/DT не появились. Setup мог использовать другой каталог или завершиться без установки. '
                      'Это само по себе не означает, что выбран дистрибутив обновления.')
    raise RuntimeError('Установка шаблонов не подтверждена. ' + detail + '\nПроверенный каталог: ' + str(destination))


@contextmanager
def isolated_template_destination(destination, cancel, config_path=None):
    """Use the documented default directory mechanism, then restore user config."""
    if cancel.is_set():
        raise OperationCancelled('Установка шаблона прервана до запуска setup.')
    while not _SETUP_LOCK.acquire(timeout=.1):
        if cancel.is_set():
            raise OperationCancelled('Установка шаблона прервана до запуска setup.')
    path = Path(config_path) if config_path else launcher_config_path()
    created = []
    before = None
    changed = False
    try:
        if cancel.is_set():
            raise OperationCancelled('Установка шаблона прервана до запуска setup.')
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('Файл настроек запуска является ссылкой; автоматическая установка остановлена.')
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Слишком большой файл настроек 1cestart.cfg.')
        before = path.read_bytes() if path.exists() else None
        previous = text_file(path) if before is not None else ''
        candidate = path.parent
        while not candidate.exists():
            created.append(candidate)
            candidate = candidate.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = f'ConfigurationTemplatesLocation={destination}\r\n' + previous
        _write_launcher_config(path, expected)
        changed = True
        yield path
    finally:
        try:
            if changed:
                current = text_file(path) if path.exists() else ''
                remainder = _without_template_path(current, destination)
                original = _without_template_path(previous, destination)
                if remainder == original:
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(before)
                else:
                    # Do not discard edits made by another 1C process/user.
                    _write_launcher_config(path, remainder + ('\r\n' if remainder else ''))
            for directory in created:
                try:
                    directory.rmdir()
                except OSError:
                    break
        finally:
            _SETUP_LOCK.release()


def run_native(args, event, cancel, log=None, secrets=()):
    if cancel.is_set():
        raise OperationCancelled('Операция прервана до запуска процесса.')
    args = [str(a) for a in args]
    # 1C requires doubled quotes inside a quoted connection string. Python's
    # default list2cmdline instead writes backslash-escaped quotes (File=\"…\").
    command = one_c_command(args) if os.name == 'nt' and Path(args[0]).name.casefold() == '1cv8.exe' else args
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, cwd=str(Path(args[0]).resolve().parent),
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    last_log = ''
    signature = None
    def read_log():
        nonlocal last_log, signature
        if not log or not Path(log).exists():
            return
        info = Path(log).stat()
        key = info.st_size, info.st_mtime_ns
        if signature == key:
            return
        signature = key
        with Path(log).open('rb') as source:
            source.seek(max(0, info.st_size - 1024 * 1024))
            current = decode_output(source.read(1024 * 1024))
        delta = current[len(last_log):] if current.startswith(last_log) else current
        if delta.strip():
            event(dict(kind='message', text=redact(delta, secrets)))
        last_log = current
    try:
        while True:
            if cancel.is_set():
                process.terminate()
                raise OperationCancelled('Операция 1С прервана. Если загрузка уже началась, проверьте состояние базы перед повтором.')
            read_log()
            try:
                output = process.communicate(timeout=.2)[0]
                break
            except subprocess.TimeoutExpired:
                pass
        read_log()
        detail = redact(decode_output(output), secrets).strip()
        if detail:
            event(dict(kind='message', text=detail[-12000:]))
        if process.returncode:
            raise RuntimeError(Path(str(args[0])).name + ': код ' + str(process.returncode) + '. ' +
                               (detail[-3000:] or redact(last_log[-3000:], secrets)))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def clean_owned(context, event):
    """Cleanup warnings must not turn a completed database load into a retry."""
    try:
        cleanup_workspace(context)
        return ''
    except Exception as exc:
        message = 'Не удалось удалить временные файлы ' + context['root'] + ': ' + str(exc)
        event(dict(kind='message', text='Предупреждение: ' + message))
        return message


def prepare_templates(unpacked, destination, version, event, cancel, method='auto'):
    if method not in ('auto', 'setup', 'stream'):
        raise ValueError('Неизвестный способ установки шаблонов')
    unpacked, destination = Path(unpacked), Path(destination)
    setups = [p for p in unpacked.rglob('setup.exe') if p.is_file() and not p.is_symlink()]
    if len(setups) != 1:
        raise ValueError('В дистрибутиве должен быть один setup.exe конфигурации. Выберите дистрибутив полной установки.')
    efds = [p for p in unpacked.rglob('*.efd') if p.is_file() and not p.is_symlink()]
    large = len(efds) == 1 and efds[0].stat().st_size >= 2 * 1024**3
    if method == 'setup' or method == 'auto' and not large:
        event(dict(kind='stage', text='Установка шаблонов через setup.exe'))
        try:
            with isolated_template_destination(destination, cancel) as config:
                event(dict(kind='message', text='Настройки setup: ' + str(config) + ' · UTF-16LE'))
                event(dict(kind='message', text='Каталог шаблонов: ' + str(destination)))
                run_native([setups[0], '/s'], event, cancel)
                if config.exists():
                    locations = template_locations(config)
                    event(dict(kind='message', text='Каталоги в настройках после setup: ' + ('; '.join(locations) or '(не указаны)')))
                event(dict(kind='stage', text='Проверка CF/DT и версии установленных шаблонов'))
                return installed_templates(destination, version, event)
        except OperationCancelled:
            raise
        except (RuntimeError, ValueError) as exc:
            if method == 'setup' or len(efds) != 1:
                raise
            event(dict(kind='warning', text='Setup не подтвердил шаблоны. Переход к потоковому извлечению EFD. ' + str(exc)))
    if len(efds) != 1:
        raise ValueError('Для потокового извлечения нужен один файл 1cv8.efd из полного дистрибутива')
    if large and method == 'auto':
        event(dict(kind='message', text='Крупный EFD (' + str(efds[0].stat().st_size) +
                   ' байт): используется встроенное потоковое извлечение без загрузки всего файла в память.'))
    # Never mix partially installed files with the verified extraction or touch
    # any pre-existing profile/global template directory.
    fallback = destination.with_name(destination.name + '-efd')
    extract_efd(efds[0], fallback, event, cancel)
    return installed_templates(fallback, version, event)


def install_configuration(portal, distribution, folder, event, cancel, method='auto'):
    project, version = distribution['project']['nick'], distribution['version']
    download = create_configuration_workspace(folder, project, version)
    installed = None
    residue = []
    try:
        event(dict(kind='stage', text='Скачивание конфигурации ' + distribution['project']['title'] + ' ' + version))
        archive = portal.download(distribution, download['root'], event)
        unpacked = unpack_distribution(archive, event, cancel)
        installed = create_configuration_workspace(folder, project, version, inherited_parents=download['created_parents'])
        destination = Path(installed['root']) / 'templates'
        templates = prepare_templates(unpacked, destination, version, event, cancel, method)
        event(dict(kind='message', text='Шаблоны установлены: ' + str(len(templates)) + '. Выберите пустую или демонстрационную базу.'))
        return dict(workspace=installed, templates=templates, distribution=dict(distribution), cleanup_workspaces=residue)
    except Exception:
        if installed:
            clean_owned(installed, event)
        raise
    finally:
        if clean_owned(download, event):
            residue.append(download)


def cleanup_configuration(package, event):
    errors = []
    for workspace in [package['workspace'], *package.get('cleanup_workspaces', ())]:
        error = clean_owned(workspace, event)
        if error:
            errors.append(error)
    return '\n'.join(errors)


def installed_designers():
    result = []
    for root in platform_roots():
        try:
            folders = list(root.iterdir())
        except OSError:
            continue
        for folder in folders:
            path = folder / 'bin' / '1cv8.exe'
            if re.fullmatch(r'8\.[35]\.\d+\.\d+', folder.name) and path.is_file():
                result.append(dict(version=folder.name, path=str(path)))
    return sorted(result, key=lambda r: tuple(map(int, r['version'].split('.'))), reverse=True)


def executable_version(path):
    """Read the PE version resource without executing the selected program."""
    import ctypes
    from ctypes import wintypes
    class VersionInfo(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ('signature', 'structure', 'file_ms', 'file_ls',
            'product_ms', 'product_ls', 'flags_mask', 'flags', 'os', 'type', 'subtype', 'date_ms', 'date_ls')]
    dll = ctypes.WinDLL('version', use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    dll.GetFileVersionInfoW.restype = wintypes.BOOL
    dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    dll.VerQueryValueW.restype = wintypes.BOOL
    size = dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ''
    buffer = ctypes.create_string_buffer(size)
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not dll.GetFileVersionInfoW(str(path), 0, size, buffer) or not dll.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        return ''
    if length.value < ctypes.sizeof(VersionInfo):
        return ''
    info = ctypes.cast(pointer, ctypes.POINTER(VersionInfo)).contents
    if info.signature != 0xFEEF04BD:
        return ''
    return '.'.join(map(str, (info.file_ms >> 16, info.file_ms & 65535, info.file_ls >> 16, info.file_ls & 65535)))


def designer_for_version(version, preferred='', versions=None):
    if not re.fullmatch(r'8\.[35]\.\d+\.\d+', version):
        raise ValueError('Не получена версия платформы выбранного кластера. Обновите список кластеров.')
    path = _versioned_client(version, '1cv8.exe', preferred, versions)
    if path:
        return path
    raise ValueError('Сервер 1С работает на платформе ' + version + '. На компьютере консоли не найден полный '
                     'клиент этой версии (1cv8.exe). Установите его или укажите нестандартный путь к 1cv8.exe.')


def _versioned_client(version, filename, preferred='', versions=None, roots=None):
    candidates = [Path(preferred)] if preferred else []
    rac = (versions or {}).get(version)
    if rac:
        candidates.append(Path(rac).with_name(filename))
    for root in platform_roots() if roots is None else roots:
        candidates.extend((root / version / 'bin' / filename, root / 'bin' / filename))
    seen = set()
    for path in candidates:
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.name.casefold() == filename and path.is_file() and executable_version(path) == version:
                return str(path.resolve())
        except OSError:
            continue
    return ''


def platform_client(version, versions=None):
    if not re.fullmatch(r'8\.[35]\.\d+\.\d+', version):
        raise ValueError('Не получена версия платформы выбранной базы. Обновите список ИБ.')
    roots = platform_roots()
    for filename, kind in (('1cv8.exe', 'толстый'), ('1cv8c.exe', 'тонкий')):
        path = _versioned_client(version, filename, versions=versions, roots=roots)
        if path:
            return dict(path=path, kind=kind, version=version)
    raise ValueError('Для открытия базы нужен толстый или тонкий клиент 1С версии ' + version +
                     '. На компьютере консоли не найдены 1cv8.exe и 1cv8c.exe этой версии. Установите подходящий клиент.')


def one_c_command(args):
    parts = []
    for index, value in enumerate(args):
        value = str(value)
        if index and str(args[index - 1]).casefold() in ('createinfobase', '/ibconnectionstring'):
            parts.append('"' + value.replace('"', '""') + '"')
        else:
            parts.append(subprocess.list2cmdline([value]))
    return ' '.join(parts)


def _connection_file(path):
    return 'File="' + str(path).replace('"', '""') + '";'


def require_native_result(path, action):
    if not Path(path).is_file() or text_file(path).strip() != '0':
        raise RuntimeError(action + ' не подтверждена файлом результата 1С. Проверьте журнал перед повтором.')


def deploy_configuration(package, template, plan, event, cancel):
    workspace = package['workspace']
    root = Path(workspace['root']).resolve()
    path = Path(template['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.casefold() not in ('.cf', '.dt'):
        raise ValueError('Шаблон не принадлежит выбранной установке.')
    parameters = validate_parameters(plan['parameters']) if plan['new'] else None
    name = parameters['name'] if parameters else plan['base']['name']
    if cancel.is_set():
        raise OperationCancelled('Загрузка отменена до изменения базы.')
    client = ClusterClient(plan['target'], plan['versions'], plan.get('cluster_user', ''), plan.get('cluster_password', ''),
                           plan.get('local', True), plan.get('ras_port', 1545), event, cancel,
                           plan.get('ib_user', ''), plan.get('ib_password', ''))
    cluster = client.deployment_context(plan['cluster']['cluster'])
    connection = infobase_address(cluster, name, plan['target']['server'])
    if not connection:
        raise ValueError('Агент 1С не вернул адрес/порт выбранного кластера. Обновите список кластеров.')
    previous = infobase_address(plan['cluster'], name, plan['target']['server'])
    if previous and previous != connection or (plan['cluster'].get('platform_version') and
                                               plan['cluster']['platform_version'] != cluster['platform_version']):
        raise ValueError('Адрес или версия выбранного кластера изменились. Обновите список и подтвердите загрузку заново.')
    executable = Path(designer_for_version(cluster['platform_version'], plan.get('executable', ''), plan['versions']))
    event(dict(kind='message', text='Версия агента 1С: ' + cluster['platform_version'] + '\nКонфигуратор: ' + str(executable) + '\n' + connection))
    temp = create_configuration_workspace(plan['folder'], package['distribution']['project']['nick'], template['version'],
                                          inherited_parents=workspace['created_parents'])
    base_created = None
    outcome = None
    secrets = [plan.get('password', ''), plan.get('cluster_password', ''), plan.get('ib_password', ''), (plan.get('parameters') or {}).get('db-pwd', '')]
    try:
        temp_root = Path(temp['root'])
        dt = path
        if path.suffix.casefold() == '.cf':
            event(dict(kind='stage', text='Подготовка чистой базы из CF-шаблона'))
            file_base = temp_root / 'empty-base'
            result_file = temp_root / 'create-result.txt'
            run_native([executable, 'CREATEINFOBASE', _connection_file(file_base), '/UseTemplate', path,
                        '/DumpResult', result_file, '/Out', temp_root / 'create.log', '/Lru'], event, cancel,
                       temp_root / 'create.log', secrets)
            if not result_file.is_file() or text_file(result_file).strip() != '0' or not (file_base / '1Cv8.1CD').is_file():
                raise RuntimeError('Создание чистой файловой базы не подтверждено. Целевая база не изменена.')
            dt = temp_root / 'empty.dt'
            dump_result = temp_root / 'dump-result.txt'
            run_native([executable, 'DESIGNER', '/F', file_base, '/DumpIB', dt,
                        '/DumpResult', dump_result, '/DisableStartupDialogs', '/DisableStartupMessages',
                        '/Out', temp_root / 'dump.log'], event, cancel,
                       temp_root / 'dump.log', secrets)
            require_native_result(dump_result, 'Выгрузка чистой базы')
            if not dt.is_file() or not dt.stat().st_size:
                raise RuntimeError('Не получена выгрузка чистой базы. Целевая база не изменена.')
        if plan['new']:
            event(dict(kind='stage', text='Создание новой информационной базы в кластере'))
            base_created = client.create(plan['cluster']['cluster'], parameters)
            infobase, name = base_created['infobase_id'], base_created['name']
            if not infobase:
                raise RuntimeError('Команда создания завершилась, но UUID не получен. Проверьте список; повторное создание не выполняется.')
        else:
            infobase, name = plan['base']['infobase'], plan['base']['name']
        with client.connect() as (rac, endpoint):
            client.require_base(rac, endpoint, plan['cluster']['cluster'], infobase, name)
        auth = (['/N', plan['ib_user'], '/P', plan.get('ib_password', '')] if plan.get('ib_user') else [])
        event(dict(kind='stage', text='Загрузка ' + template['variant'].casefold() + ' базы → ' + name))
        restore_result = temp_root / 'restore-result.txt'
        run_native([executable, 'DESIGNER', '/IBConnectionString', connection, *auth, '/RestoreIB', dt,
                    '/DumpResult', restore_result, '/DisableStartupDialogs', '/DisableStartupMessages',
                    '/Out', temp_root / 'restore.log'], event, cancel,
                   temp_root / 'restore.log', secrets)
        require_native_result(restore_result, 'Загрузка базы')
        # Mutation has completed. Cleanup failure must never cause another restore.
        cleanup_error = cleanup_configuration(package, event)
        if not cleanup_error:
            event(dict(kind='message', text='Скачанные, распакованные и установленные временные файлы конфигурации удалены.'))
        if base_created and base_created.get('settings_error'):
            event(dict(kind='message', text='Предупреждение: ' + base_created['settings_error']))
        outcome = dict(name=name, infobase_id=infobase, address=connection, created=base_created,
                       cleanup_error=cleanup_error, loaded=True,
                       settings_error='\n'.join(filter(None, (cleanup_error, (base_created or {}).get('settings_error', '')))))
        return outcome
    except Exception as exc:
        if base_created:
            event(dict(kind='message', text='ИБ уже создана: ' + base_created['name'] + '. Она не удалялась. '
                       'Для повтора выберите её как существующую; не создавайте заново.'))
        raise
    finally:
        error = clean_owned(temp, event)
        if error:
            package.setdefault('cleanup_workspaces', []).append(temp)
            if outcome is not None:
                outcome['cleanup_error'] = '\n'.join(filter(None, (outcome['cleanup_error'], error)))
                outcome['settings_error'] = '\n'.join(filter(None, (outcome['settings_error'], error)))
