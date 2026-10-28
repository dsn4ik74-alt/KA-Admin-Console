"""Official PostgreSQL installers. Never infer a data directory after initdb."""
import ctypes
from ctypes import wintypes
import ipaddress
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import re
import socket
import subprocess
import time

from install_workspace import _create_private_workspace, cleanup_workspace
from local_ras import decode_output
from platform_install import Msi, InstallProgress, _MSI_LOCK, property_value, unpack_distribution
from postgres_runner import OperationCancelled


def validate(values, linux=False):
    result = dict(values)
    try:
        result['port'] = int(values.get('port', ''))
    except (TypeError, ValueError):
        raise ValueError('Порт PostgreSQL должен быть числом: 1024–65535') from None
    if not 1024 <= result['port'] <= 65535:
        raise ValueError('Порт PostgreSQL: 1024–65535')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', values.get('name', '')):
        raise ValueError('Имя экземпляра: латинская буква, затем буквы, цифры и подчёркивания, до 48 символов')
    password = values.get('password', '')
    if not password or any(c in password for c in '\x00\r\n'):
        raise ValueError('Задайте пароль postgres без переносов строк')
    # The 1C Windows MSI passes custom action arguments separated by semicolons.
    if not linux and ';' in password:
        raise ValueError('Установщик PostgreSQL для Windows не поддерживает ; в пароле')
    if not re.fullmatch(r'[\w.@-]{1,80}', values.get('locale', '')):
        raise ValueError('Укажите локаль экземпляра, например ru_RU.UTF-8 или C')
    if values.get('listen') not in ('*', 'localhost', '127.0.0.1'):
        raise ValueError('Адреса прослушивания: *, localhost или 127.0.0.1')
    try:
        networks = values.get('networks', '')
        if isinstance(networks, list): networks = ','.join(networks)
        result['networks'] = [str(ipaddress.ip_network(v.strip(), strict=False)) for v in networks.split(',') if v.strip()]
    except ValueError:
        raise ValueError('Укажите сети в формате CIDR, например 192.168.20.0/24') from None
    for key in ('data', 'prefix'):
        value = values.get(key, '').strip()
        if not value or any(c in value for c in '\x00\r\n'):
            raise ValueError('Укажите отдельный абсолютный каталог ' + ('данных' if key == 'data' else 'программы'))
        path = PurePosixPath(value) if linux else PureWindowsPath(value)
        if not path.is_absolute() or '..' in path.parts or str(path) == path.anchor:
            raise ValueError('Укажите отдельный абсолютный каталог ' + ('данных' if key == 'data' else 'программы'))
        if not linux and (path.drive.startswith('\\\\') or any(c in value for c in ';"<>|?*') or ':' in value[2:]):
            raise ValueError('Каталоги PostgreSQL должны быть локальными путями Windows без ; и специальных символов')
        if linux and value in ('/etc', '/usr', '/var', '/home', '/tmp', '/opt', '/root', '/var/lib', '/var/lib/postgresql'):
            raise ValueError('Для экземпляра нужен отдельный подкаталог: ' + value)
        result[key] = str(path)
    data = PurePosixPath(result['data']) if linux else PureWindowsPath(result['data'])
    prefix = PurePosixPath(result['prefix']) if linux else PureWindowsPath(result['prefix'])
    if data == prefix or prefix.is_relative_to(data):
        raise ValueError('Каталог программы не должен находиться внутри каталога данных')
    return result


def windows_metadata(folder, version, msi=None):
    msi = msi or Msi()
    matches = []
    for path in Path(folder).rglob('*.msi'):
        props = {r['key']: r['value'] for r in msi.query(path, 'SELECT `Property`,`Value` FROM `Property`', ('key', 'value'))}
        name = props.get('ProductName', '').lower()
        if not name.startswith('postgresql') or 'wrapper' in name or 'mui' in name: continue
        if props.get('Manufacturer') not in ('1C-Soft', '1C') or version.lower() not in name:
            raise ValueError('MSI не соответствует выбранной сборке PostgreSQL фирмы 1С')
        required = {'CUSTOMDATADIR', 'DOINITDB', 'DOSERVICE', 'SERVICEID', 'SUPERPASSWORD', 'LISTENPORT'}
        if not required.issubset(props):
            raise ValueError('Эта версия MSI не поддерживает проверенную установку с заданным каталогом данных')
        features = msi.query(path, 'SELECT `Feature`,`Level` FROM `Feature`', ('id', 'level'))
        directories = msi.query(path, 'SELECT `Directory` FROM `Directory`', ('id',))
        if not {'server', 'base', 'datadir'}.issubset(r['id'] for r in features) or not {'BASEDIR', 'DATADIR'}.issubset(r['id'] for r in directories):
            raise ValueError('Неизвестная структура MSI PostgreSQL. Установка остановлена до изменений.')
        matches.append(dict(path=str(path), properties=props, features=features))
    if len(matches) != 1:
        raise ValueError('Не найден однозначный основной MSI PostgreSQL. MUI-оболочка не устанавливается.')
    return matches[0]


def windows_properties(config):
    return dict(ADDLOCAL='base,server,datadir', REMOVE='pgadmin,stackbuilder', ALLUSERS='1', REBOOT='ReallySuppress',
        BASEDIR=config['prefix'].rstrip('\\')+'\\', DATADIR=config['data'].rstrip('\\')+'\\', CUSTOMDATADIR=config['data'],
        DOSERVICE='1', DOINITDB='1', LISTENPORT=str(config['port']), LOCALE=config['locale'], ENCODING='UTF8', CLENCODE='UTF8',
        SUPERUSER='postgres', SUPERPASSWORD=config['password'], SUPERPASSWORDV=config['password'],
        SERVICEID=config['name'], SERVICENAME='PostgreSQL 1C | '+config['name']+' | '+str(config['port']),
        SERVICEACCOUNT='NetworkService', SERVICEDOMAIN='NT AUTHORITY', SCMSERVICEDOMAIN='NT AUTHORITY',
        SERVICEPASSWORD=' ', SERVICEPASSWORDV=' ', CREATESERVICEUSER='0', SERVICEOK='1', INITDBOK='1',
        INTERNALLAUNCH='1', RUNSTACKBUILDER='0', PERMITREMOTE='0', MaintenanceType='Modify',
        MsiHiddenProperties='SUPERPASSWORD;SUPERPASSWORDV;SERVICEPASSWORD;SERVICEPASSWORDV;RunInitdb;PrepInitdb')


def native(args, event, cancel=None, env=None, accepted=(0,), timeout=90):
    # Used only for bounded configuration checks; no passwords occur in argv.
    if cancel is not None and cancel.is_set(): raise OperationCancelled('Операция отменена')
    process = subprocess.run([str(v) for v in args], stdin=subprocess.DEVNULL, capture_output=True,
        env=env, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = decode_output(process.stdout+process.stderr).strip()
    if process.returncode not in accepted: raise RuntimeError(Path(str(args[0])).name+': код '+str(process.returncode)+'. '+text[-2200:])
    if text and event: event(dict(kind='message', text=text))
    return text


def windows_preflight(config, metadata=None, msi=None):
    if os.name != 'nt': raise ValueError('Локальная установка доступна только в Windows')
    for key in ('data', 'prefix'):
        path = Path(config[key])
        if path.resolve() != path.absolute(): raise ValueError('Каталог проходит через ссылку: '+str(path))
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValueError('Каталог уже занят: '+str(path)+'. Существующий экземпляр не изменён.')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SYSTEM\\CurrentControlSet\\Services\\'+config['name']):
            raise ValueError('Служба с таким именем уже существует: '+config['name'])
    except FileNotFoundError: pass
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try: check.bind(('0.0.0.0', config['port']))
        except OSError: raise ValueError('Порт PostgreSQL уже занят: '+str(config['port'])) from None
    if metadata:
        msi = msi or Msi()
        if registered_product(msi,metadata['properties']):
            raise ValueError('Эта или связанная сборка PostgreSQL уже зарегистрирована в Windows Installer. Установка не заменяет действующий экземпляр.')


def registered_product(msi,properties):
    state=msi.dll.MsiQueryProductStateW
    state.argtypes,state.restype=[wintypes.LPCWSTR],ctypes.c_int
    if state(properties['ProductCode'])>=0:return True
    if not properties.get('UpgradeCode'):return False
    enum=msi.dll.MsiEnumRelatedProductsW
    enum.argtypes,enum.restype=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPWSTR],wintypes.UINT
    # Native MSI upgrades may remove an older related product even when a new
    # service name and data directory were chosen. Refuse that implicit upgrade.
    for index in range(1000):
        product=ctypes.create_unicode_buffer(39)
        code=enum(properties['UpgradeCode'],0,index,product)
        if code==259:return False
        if code:raise RuntimeError('Не удалось проверить связанные установки PostgreSQL: '+str(code))
        if state(product.value)>=0:return True
    raise RuntimeError('Превышено число связанных установок PostgreSQL')


def install_windows_msi(metadata, config, event, cancel):
    while not _MSI_LOCK.acquire(timeout=.1):
        if cancel.is_set(): raise OperationCancelled('Установка отменена при ожидании Windows Installer')
    try:
        if cancel.is_set(): raise OperationCancelled('Установка отменена до запуска')
        msi = Msi(); windows_preflight(config, metadata, msi)
        props = windows_properties(config)
        # The official package gives server Level=0, normally enabled by its UI.
        # Bind a transform to exactly this MSI instead of modifying the original.
        if any(r['id'] == 'server' and r['level'] == '0' for r in metadata['features']):
            props.update(TRANSFORMS=msi.create_transform(metadata['path'], ["UPDATE `Feature` SET `Level`=1 WHERE `Feature`='server'"]), TRANSFORMSSECURE='1')
        progress, errors, callback_errors = InstallProgress(), [], []
        def safe(text): return str(text).replace(config['password'], '[скрыто]')
        def handle(context, message, record):
            try:
                if cancel.is_set(): return 2
                kind = message & 0xff000000
                if kind == 0x0a000000:
                    value = progress.update([msi.dll.MsiRecordGetInteger(record, n) for n in range(1, 5)])
                    if value is not None: event(dict(kind='progress', percent=value, phase='PostgreSQL · Windows Installer', unit='установки'))
                elif kind == 0x09000000:
                    value = progress.action_data()
                    if value is not None: event(dict(kind='progress', percent=value, phase='PostgreSQL · Windows Installer', unit='установки'))
                elif kind in (0x01000000, 0x02000000):
                    text = safe(msi.message(record)); errors.append(text); del errors[:-10]
                    event(dict(kind='error' if kind == 0x01000000 else 'warning', text=text))
                elif kind == 0x08000000:
                    progress.step = 0; event(dict(kind='stage', text=safe(msi.text(record, 2))))
                return 1
            except Exception as exc: callback_errors.append(safe(exc)); return 2
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
        callback = callback_type(handle); previous, ignored = ctypes.c_void_p(), ctypes.c_void_p()
        old_ui = msi.dll.MsiSetInternalUI(2, None)
        try:
            code = msi.dll.MsiSetExternalUIRecord(ctypes.cast(callback, ctypes.c_void_p), sum(1 << v for v in (1, 2, 8, 9, 10)), None, ctypes.byref(previous))
            if code: raise RuntimeError('Не удалось подключить прогресс Windows Installer: '+str(code))
            command = ' '.join(k+'='+property_value(v) for k, v in props.items())
            # Native API, not msiexec argv: passwords are not visible in the process list.
            code = msi.dll.MsiInstallProductW(metadata['path'], command)
        finally:
            msi.dll.MsiSetExternalUIRecord(previous, 0, None, ctypes.byref(ignored)); msi.dll.MsiSetInternalUI(old_ui, None)
        if callback_errors: raise RuntimeError('Ошибка обработки Windows Installer: '+callback_errors[-1])
        if code == 1602: raise OperationCancelled('Установка отменена; Windows Installer выполнил откат')
        if code not in (0, 3010): raise RuntimeError('Windows Installer: код '+str(code)+'. '+'\n'.join(errors))
        if code == 3010: event(dict(kind='warning', text='Windows Installer запрашивает перезагрузку Windows'))
        return code
    finally: _MSI_LOCK.release()


def install_windows(archive, row, config, event, cancel):
    from configuration_install import executable_version
    from platform_service import open_service
    windows_preflight(config)
    unpacked = unpack_distribution(archive, event, cancel)
    metadata = windows_metadata(unpacked, row['version'])
    windows_preflight(config, metadata)
    # The signed prerequisite comes from the very same official archive.
    for prerequisite in unpacked.rglob('vcredist*.exe'):
        version = executable_version(prerequisite)
        if not version: raise ValueError('Не удалось определить версию библиотеки Visual C++ из дистрибутива')
        flags = ['/install', '/quiet', '/norestart'] if int(version.split('.')[0]) >= 11 else ['/q', '/norestart']
        event(dict(kind='stage', text='Проверка / установка Microsoft Visual C++ '+version))
        # Do not kill Windows Installer midway through dependency installation.
        native([str(prerequisite), *flags], event, accepted=(0, 3010, 1638), timeout=600)
    install_windows_msi(metadata, config, event, cancel)
    data = Path(config['data']); conf = data/'postgresql.conf'; hba = data/'pg_hba.conf'
    if not (data/'PG_VERSION').is_file() or not conf.is_file() or not hba.is_file():
        raise RuntimeError('MSI завершился, но экземпляр в выбранном каталоге не подтверждён: '+str(data))
    with conf.open('a', encoding='utf-8') as stream:
        stream.write("\n# KA installation\nlisten_addresses = '"+config['listen']+"'\n")
    auth = 'scram-sha-256' if int(row['version'].split('.')[0]) >= 10 else 'md5'
    with hba.open('a', encoding='utf-8') as stream:
        for network in config['networks']: stream.write('host all all '+network+' '+auth+'\n')
    # Only the newly installed service is restarted. Existing instances are untouched.
    with open_service(config['name']) as service:service.ensure_stopped()
    native(['sc.exe', 'start', config['name']], event)
    psql = Path(config['prefix'])/'bin'/'psql.exe'
    env = dict(os.environ, PGPASSWORD=config['password'], PGCLIENTENCODING='UTF8', PGCONNECT_TIMEOUT='2')
    deadline = time.monotonic()+40
    while True:
        try:
            output = native([psql, '-X', '-w', '-h', '127.0.0.1', '-p', str(config['port']), '-U', 'postgres', '-d', 'postgres', '-At', '-c',
                "SELECT current_setting('data_directory') || E'\\t' || current_setting('server_version');"], None, env=env)
            break
        except RuntimeError:
            if time.monotonic() >= deadline: raise
            time.sleep(.5)
    actual, _, version = output.partition('\t')
    if PureWindowsPath(actual) != PureWindowsPath(config['data']) or not version.startswith(row['version'].split('-')[0]):
        raise RuntimeError('Не подтверждены выбранные каталог данных и версия PostgreSQL')
    return dict(data=config['data'], port=config['port'], service=config['name'], version=row['version'])


def install(portal, row, values, folder, event, cancel, connection=None):
    linux = connection is not None
    config = validate(values, linux=linux)
    if linux == (row.get('os') == 'windows'): raise ValueError('Дистрибутив не соответствует выбранному режиму установки')
    if not re.fullmatch(r'\d[\w.-]{1,45}', row.get('version', '')): raise ValueError('Некорректная версия PostgreSQL')
    if not folder or not Path(folder).is_absolute(): raise ValueError('Укажите абсолютную временную папку дистрибутива на компьютере консоли')
    if linux:
        from linux_platform import command
        from ssh_connection import connect
        from postgres_install_linux import compatible
        with connect(connection) as client:
            source = command(client, ['cat', '/etc/os-release'], cancel=cancel)
            release = dict(line.split('=', 1) for line in source.splitlines() if '=' in line)
            release = {k: v.strip('"\'') for k, v in release.items()}
            compatible(row, release, command(client, ['uname', '-m'], cancel=cancel).strip())
    else: windows_preflight(config)
    context = _create_private_workspace(Path(folder).resolve()/'postgresql'/row['version'])
    try:
        event(dict(kind='stage', text='Скачивание PostgreSQL '+row['version']))
        archive = portal.download(row, context['root'], event)
        if cancel.is_set(): raise OperationCancelled('Установка отменена после скачивания')
        if linux:
            from ssh_jobs import run_script
            result = run_script('postgres_install_linux', connection, dict(settings=config, distribution=row), event, cancel, files=(archive,), elevate=True)
        else: result = install_windows(archive, row, config, event, cancel)
        event(dict(kind='message', text='PostgreSQL установлен: '+result['service']+' · '+result['data']))
        return result
    finally:
        try: cleanup_workspace(context)
        except (OSError, ValueError) as exc: event(dict(kind='warning', text='Не удалось удалить временный дистрибутив: '+str(exc)))
