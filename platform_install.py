"""Read 1C MSI metadata and install through Windows Installer with native progress."""
import ctypes
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
import os
import re
import shutil
import subprocess
import sys
import threading
from uuid import uuid4

from postgres_runner import OperationCancelled
from local_ras import decode_output
from platform_service import configure_installed_service, service_options, create_ras_service, check_ras_destination
from linux_platform_runner import ras_service_options

COMPONENT_PROPERTIES = {'FileVersion': 'DESIGNERALLCLIENTS', 'MCWFileVersion': 'THINCLIENT',
                        'MCVersion': 'THINCLIENTFILE', 'CSServer': 'SERVER', 'CSClient': 'SERVERCLIENT',
                        'WebServices': 'WEBSERVEREXT', 'DepotServer': 'CONFREPOSSERVER',
                        'AdminFunc': 'ADMINISTRATIONFUNC', 'Convertor': 'CONVERTER77'}
_MSI_LOCK = threading.Lock()


def seven_zip():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    path = root / 'vendor' / '7zip' / '7z.exe'
    if not path.is_file():
        raise RuntimeError('Встроенный 7-Zip не найден')
    return str(path)


def unpack_distribution(archive, event, cancel):
    archive = Path(archive).resolve()
    destination = archive.parent / ('unpacked-' + archive.stem + '-' + uuid4().hex[:8])
    destination.mkdir()
    event(dict(kind='stage', text='Проверка и распаковка дистрибутива'))
    listing = run_archive([seven_zip(), 'l', str(archive), '-slt', '-sccUTF-8'], cancel)
    entries = listing.decode('utf-8', errors='replace').replace('\r\n', '\n').split('\n----------\n', 1)
    if len(entries) != 2:
        raise RuntimeError('Не удалось прочитать состав архива 7-Zip')
    for block in entries[1].split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        path = PureWindowsPath(fields.get('Path', ''))
        if path.root or path.drive or '..' in path.parts or any(':' in part for part in path.parts) or fields.get('Symbolic Link') or fields.get('Hard Link'):
            raise ValueError('Архив содержит путь вне каталога распаковки или ссылку')
    args = [seven_zip(), 'x', str(archive), '-o' + str(destination), '-y', '-bb0', '-bsp1', '-sccUTF-8']
    run_archive(args, cancel, event)
    event(dict(kind='message', text='Распаковано: ' + str(destination)))
    return destination


def extract_distribution(archive, event, cancel):
    destination = unpack_distribution(archive, event, cancel)
    candidates = [path for path in destination.rglob('*.msi') if path.is_file() and not path.is_symlink()]
    if not candidates:
        raise RuntimeError('В дистрибутиве не найден MSI установщика Windows')
    preferred = [path for path in candidates if '1centerprise' in path.name.lower().replace(' ', '')]
    candidates = preferred or candidates
    depth = min(len(path.relative_to(destination).parts) for path in candidates)
    candidates = [path for path in candidates if len(path.relative_to(destination).parts) == depth]
    if len(candidates) != 1:
        raise RuntimeError('В дистрибутиве несколько основных MSI. Выберите отдельный дистрибутив платформы.')
    return str(candidates[0])


def run_archive(args, cancel, event=None):
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = b''
    try:
        while True:
            if cancel.is_set():
                process.terminate()
                raise OperationCancelled('Распаковка прервана; архив сохранён')
            try:
                output = process.communicate(timeout=.15)[0]
                break
            except subprocess.TimeoutExpired as exc:
                if event and exc.output:
                    matches = re.findall(rb'(\d+)%', exc.output[-200:])
                    if matches:
                        event(dict(kind='progress', percent=int(matches[-1]), phase='Распаковка', unit='распаковки'))
                continue
        if process.returncode:
            raise RuntimeError('Ошибка 7-Zip: ' + output.decode('utf-8', errors='replace')[-1200:])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return output


class Msi:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('Установка платформы доступна только в Windows')
        self.dll = ctypes.WinDLL('msi', use_last_error=True)
        declarations = {
            'MsiOpenDatabaseW': ([wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)], wintypes.UINT),
            'MsiDatabaseOpenViewW': ([wintypes.UINT, wintypes.LPCWSTR, ctypes.POINTER(wintypes.UINT)], wintypes.UINT),
            'MsiViewExecute': ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            'MsiViewFetch': ([wintypes.UINT, ctypes.POINTER(wintypes.UINT)], wintypes.UINT),
            'MsiCloseHandle': ([wintypes.UINT], wintypes.UINT),
            'MsiRecordGetStringW': ([wintypes.UINT, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.UINT),
            'MsiRecordGetInteger': ([wintypes.UINT, wintypes.UINT], ctypes.c_int),
            'MsiFormatRecordW': ([wintypes.UINT, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.UINT),
            'MsiInstallProductW': ([wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.UINT),
            'MsiSetInternalUI': ([wintypes.UINT, ctypes.c_void_p], wintypes.UINT),
            'MsiSetExternalUIRecord': ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], wintypes.UINT),
            'MsiDatabaseCommit': ([wintypes.UINT], wintypes.UINT),
            'MsiDatabaseGenerateTransformW': ([wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int], wintypes.UINT),
            'MsiCreateTransformSummaryInfoW': ([wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int], wintypes.UINT),
            'MsiGetComponentPathW': ([wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], ctypes.c_int),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, result

    def text(self, record, column):
        length = wintypes.DWORD(4096)
        buffer = ctypes.create_unicode_buffer(length.value)
        code = self.dll.MsiRecordGetStringW(record, column, buffer, ctypes.byref(length))
        if code == 234:
            length.value += 1
            buffer = ctypes.create_unicode_buffer(length.value)
            code = self.dll.MsiRecordGetStringW(record, column, buffer, ctypes.byref(length))
        if code:
            raise RuntimeError('Не удалось прочитать MSI: ' + str(code))
        return buffer.value

    def query(self, path, sql, columns):
        database, view, record = wintypes.UINT(), wintypes.UINT(), wintypes.UINT()
        try:
            for code in (self.dll.MsiOpenDatabaseW(str(path), None, ctypes.byref(database)),):
                if code:
                    raise RuntimeError('Не удалось открыть MSI: ' + str(code))
            code = self.dll.MsiDatabaseOpenViewW(database, sql, ctypes.byref(view))
            if code or self.dll.MsiViewExecute(view, 0):
                raise RuntimeError('Не удалось прочитать таблицу MSI: ' + str(code))
            rows = []
            while True:
                code = self.dll.MsiViewFetch(view, ctypes.byref(record))
                if code == 259:
                    return rows
                if code:
                    raise RuntimeError('Не удалось прочитать строки MSI: ' + str(code))
                rows.append({name: self.text(record, index + 1) for index, name in enumerate(columns)})
                self.dll.MsiCloseHandle(record)
                record.value = 0
        finally:
            for handle in (record, view, database):
                if handle.value:
                    self.dll.MsiCloseHandle(handle)

    def message(self, record):
        length = wintypes.DWORD(4096)
        buffer = ctypes.create_unicode_buffer(length.value)
        code = self.dll.MsiFormatRecordW(0, record, buffer, ctypes.byref(length))
        if code == 234:
            length.value += 1
            buffer = ctypes.create_unicode_buffer(length.value)
            code = self.dll.MsiFormatRecordW(0, record, buffer, ctypes.byref(length))
        return self.text(record, 0) if code else buffer.value

    def component_path(self, product, component):
        length = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        state = self.dll.MsiGetComponentPathW(product, component, buffer, ctypes.byref(length))
        if state != 3 or not buffer.value or not Path(buffer.value).is_file():
            raise RuntimeError('Не найден установленный компонент службы 1С: ' + str(state))
        return buffer.value

    def create_transform(self, path, statements):
        """Generate a secure-cacheable MST; never edit the downloaded MSI."""
        path = Path(path).resolve()
        if ';' in str(path.parent):
            raise ValueError('Папка дистрибутива не должна содержать ; для настройки установки службы')
        modified = path.with_name('ka-service-' + uuid4().hex + '.msi')
        transform = modified.with_suffix('.mst')
        original, changed = wintypes.UINT(), wintypes.UINT()
        try:
            shutil.copyfile(path, modified)
            for source, mode, handle in ((path, None, original), (modified, ctypes.c_void_p(1), changed)):
                code = self.dll.MsiOpenDatabaseW(str(source), mode, ctypes.byref(handle))
                if code:
                    raise RuntimeError('Не удалось подготовить MSI без запуска службы: ' + str(code))
            for sql in statements:
                view = wintypes.UINT()
                try:
                    code = self.dll.MsiDatabaseOpenViewW(changed, sql, ctypes.byref(view))
                    if not code:
                        code = self.dll.MsiViewExecute(view, 0)
                    if code:
                        raise RuntimeError('Не удалось отключить запуск службы в MSI: ' + str(code))
                finally:
                    if view.value:
                        self.dll.MsiCloseHandle(view)
            code = self.dll.MsiDatabaseCommit(changed)
            if not code:
                code = self.dll.MsiDatabaseGenerateTransformW(changed, original, str(transform), 0, 0)
            if not code:
                # Bind the transform to this product and exactly this version.
                code = self.dll.MsiCreateTransformSummaryInfoW(changed, original, str(transform), 0, 2 | 0x20 | 0x100)
            if code:
                raise RuntimeError('Не удалось сохранить настройку установки без запуска службы: ' + str(code))
            return str(transform)
        except BaseException:
            transform.unlink(missing_ok=True)
            raise
        finally:
            for handle in (changed, original):
                if handle.value:
                    self.dll.MsiCloseHandle(handle)
            modified.unlink(missing_ok=True)

    def metadata(self, path, version=''):
        properties = {row['key']: row['value'] for row in self.query(path, 'SELECT `Property`,`Value` FROM `Property`', ('key', 'value'))}
        if not properties.get('ProductName', '').lower().startswith(('1c', '1с')):
            raise ValueError('MSI не является установщиком платформы 1С')
        if version and properties.get('ProductVersion') != version:
            raise ValueError('Версия MSI отличается от выбранного релиза: ' + properties.get('ProductVersion', 'неизвестна'))
        features = self.query(path, 'SELECT `Feature`,`Feature_Parent`,`Title`,`Description`,`Display`,`Level` FROM `Feature`',
                              ('id', 'parent', 'title', 'description', 'display', 'level'))
        drivers = self.query(path, "SELECT `File`,`Sequence` FROM `File` WHERE `FileName`='haspdinst.exe'", ('file', 'sequence'))
        hasp = None
        if drivers:
            driver = drivers[0]
            media = self.query(path, 'SELECT `LastSequence`,`Cabinet` FROM `Media`', ('last', 'cabinet'))
            media = sorted((row for row in media if int(row['last']) >= int(driver['sequence'])), key=lambda row: int(row['last']))
            if media and media[0]['cabinet'] and not media[0]['cabinet'].startswith('#'):
                cabinet = Path(path).parent / media[0]['cabinet']
                if cabinet.resolve().is_relative_to(Path(path).parent.resolve()) and cabinet.is_file():
                    hasp = dict(file=driver['file'], cabinet=str(cabinet))
        directories = self.query(path, 'SELECT `Directory` FROM `Directory`', ('id',))
        ras = self.query(path, "SELECT `File`,`Component_` FROM `File` WHERE `FileName`='ras.exe'", ('id', 'component'))
        ras_component, ras_features = None, []
        if len(ras) == 1:
            component = ras[0]['component'].replace("'", "''")
            rows = self.query(path, "SELECT `ComponentId`,`KeyPath` FROM `Component` WHERE `Component`='" + component + "'", ('guid', 'key'))
            if len(rows) == 1 and rows[0]['key'] == ras[0]['id']:
                ras_component = rows[0]['guid']
                ras_features = [row['feature'] for row in self.query(path, "SELECT `Feature_` FROM `FeatureComponents` WHERE `Component_`='" + component + "'", ('feature',))]
        return dict(path=str(path), properties=properties, features=features, hasp_driver=hasp,
                    ras_component=ras_component, ras_features=ras_features,
                    directories=[row['id'] for row in directories], capabilities_verified=True,
                    capabilities_source='MSI: таблица Feature и состав выбранного дистрибутива')


def property_value(value):
    value = str(value)
    if any(char in value for char in '\r\n\x00'):
        raise ValueError('Параметры установщика не должны содержать переводы строк или NUL')
    return '"' + value.replace('"', '""') + '"'


def installable_features(metadata):
    return {item['id'] for item in metadata['features'] if int(item.get('level') or 0) > 0}


def install_properties(metadata, settings):
    feature_ids = {item['id'] for item in metadata['features']}
    selected = list(dict.fromkeys(settings.get('features', [])))
    if not selected or not set(selected) <= installable_features(metadata):
        raise ValueError('Выберите компоненты из выбранного дистрибутива')
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', key) for key in selected):
        raise ValueError('Некорректное имя компонента MSI')
    if settings.get('hasp') and not metadata.get('hasp_driver'):
        raise ValueError('В выбранном дистрибутиве нет доступного установщика драйвера HASP')
    if settings.get('ras_service'):
        if not metadata.get('ras_component') or not set(selected) & set(metadata.get('ras_features', [])):
            raise ValueError('Для RAS выберите компонент, содержащий ras.exe: сервер или администрирование сервера 1С')
        ras_service_options(settings, metadata['properties']['ProductVersion'], windows=True)
    language = settings.get('language', 'RU').upper()
    if not re.fullmatch(r'[A-Z]{2,3}', language):
        raise ValueError('Выберите язык интерфейса')
    if 'Languages' in feature_ids and language not in feature_ids:
        raise ValueError('В выбранном дистрибутиве нет выбранного языка интерфейса')
    expanded = list(selected)
    # Use actual MSI Feature identifiers, not command-line property names.
    for item in metadata['features']:
        if not item.get('parent') and int(item.get('level') or 0) == 1 and int(item.get('display') or 0) == 0 and item['id'] != 'AdmUpdate':
            expanded.append(item['id'])
    if 'Languages' in feature_ids and language in feature_ids:
        expanded.extend(('Languages', language))
        for key in selected:
            if key + '_' + language in feature_ids:
                expanded.append(key + '_' + language)
    by_id = {item['id']: item for item in metadata['features']}
    for key in list(expanded):
        parent = by_id[key].get('parent')
        while parent and parent not in expanded:
            expanded.append(parent)
            parent = by_id.get(parent, {}).get('parent')
    values = dict(ADDLOCAL=','.join(dict.fromkeys(expanded)), REBOOT='ReallySuppress', ALLUSERS='1',
                  DEFLANGUAGE=language, LANGUAGES=language, HASPInstall='yes' if settings.get('hasp') else 'no',
                  USEHWLICENSES='1' if settings.get('hardware_licenses') else '0', INSTALLSRVRASSRVC='1' if settings.get('service') else '0',
                  LAUNCHREADME='No', SHOWLAUNCHREADME='0', READMEFILETOLAUNCHATEND='0', STOP_OLD_SRVC='0')
    values.update({prop: '1' if feature in selected else '0' for feature, prop in COMPONENT_PROPERTIES.items()
                   if feature in feature_ids and prop not in metadata.get('directories', ())})
    if settings.get('directory', '').strip():
        directory = Path(settings['directory'].strip())
        if not directory.is_absolute():
            raise ValueError('Укажите полный каталог установки')
        values['INSTALLDIR'] = str(directory)
    if settings.get('service'):
        if 'CSServer' not in selected:
            raise ValueError('Для установки службы выберите компонент сервера 1С')
        service_options(metadata, settings)
        username = settings.get('service_user', '').strip()
        password = settings.get('service_password', '')
        if not username or not password:
            raise ValueError('Укажите пользователя службы и пароль')
        if password != settings.get('service_password_confirm', ''):
            raise ValueError('Пароли пользователя службы не совпадают')
        if settings.get('new_user') and username.upper() != 'USR1CV8':
            raise ValueError('Стандартный установщик создаёт пользователя USR1CV8. Для другого имени выберите существующего пользователя.')
        values.update(SRVCUSERSELECTMODE='new' if settings.get('new_user') else 'existing',
                      USER1CV82SERVER=username, PASSWORD1CV82SERVER=password)
        values['MsiHiddenProperties'] = metadata['properties'].get('MsiHiddenProperties', '') + ';PASSWORD1CV82SERVER'
    return values


def prepare_service(msi, metadata):
    """Find the ragent service in this MSI and remove only its install-start bit."""
    path = metadata['path']
    services = msi.query(path, 'SELECT `ServiceInstall`,`Name`,`Component_` FROM `ServiceInstall`', ('id', 'name', 'component'))
    components = {row['id']: row for row in msi.query(path, 'SELECT `Component`,`ComponentId`,`KeyPath` FROM `Component`', ('id', 'guid', 'file'))}
    files = {row['id']: row['name'].split('|')[-1].casefold() for row in msi.query(path, 'SELECT `File`,`FileName` FROM `File`', ('id', 'name'))}
    targets = [dict(row, component_guid=components[row['component']]['guid']) for row in services
               if row['component'] in components and files.get(components[row['component']]['file']) == 'ragent.exe']
    if len(targets) != 1 or '[' in targets[0]['name'] or ']' in targets[0]['name']:
        raise ValueError('В MSI не удалось однозначно определить службу агента 1С. Установка не начата.')
    service = targets[0]
    product = metadata['properties'].get('ProductCode', '')
    if not re.fullmatch(r'\{[0-9A-Fa-f-]{36}\}', product) or not re.fullmatch(r'\{[0-9A-Fa-f-]{36}\}', service['component_guid']):
        raise ValueError('В MSI отсутствует идентификатор установленного компонента службы')
    controls = msi.query(path, 'SELECT `ServiceControl`,`Name`,`Event`,`Component_` FROM `ServiceControl`', ('id', 'name', 'event', 'component'))
    matching = [row for row in controls if row['name'] == service['name']]
    if not matching:
        raise ValueError('В MSI не найдено управление запуском службы. Установка не начата.')
    statements = ["UPDATE `ServiceControl` SET `Event`=" + str(int(row['event']) & ~1) +
                  " WHERE `ServiceControl`='" + row['id'].replace("'", "''") + "'"
                  for row in matching if int(row['event']) & 1]
    service.update(product=product, transform=msi.create_transform(path, statements) if statements else '')
    return service


def prepare_hasp(metadata, event, cancel):
    driver = metadata['hasp_driver']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', driver['file']):
        raise ValueError('Некорректное имя установщика HASP в MSI')
    destination = Path(metadata['path']).parent / ('hasp-' + uuid4().hex[:8])
    destination.mkdir()
    event(dict(kind='stage', text='Подготовка драйвера HASP из выбранного дистрибутива'))
    run_archive([seven_zip(), 'e', driver['cabinet'], driver['file'], '-o' + str(destination), '-y', '-sccUTF-8'], cancel)
    path = destination / driver['file']
    if not path.is_file():
        raise RuntimeError('В CAB не найден установщик драйвера HASP')
    with path.open('rb') as source:
        if source.read(2) != b'MZ':
            raise RuntimeError('Некорректный установщик драйвера HASP')
    return str(path)


def install_hasp(path, event, cancel):
    if cancel.is_set():
        raise OperationCancelled('Платформа уже установлена. Установка драйвера HASP отменена до запуска.')
    # This standalone driver utility has no rollback/cancellation API. Do not
    # kill it in the middle of a driver update or use its process-killing flags.
    event(dict(kind='stage', text='Установка драйвера HASP · дождитесь завершения этого этапа', cancellable=False))
    result = subprocess.run([path, '-i', '-nomsg'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.stdout:
        event(dict(kind='message', text=decode_output(result.stdout)))
    if result.returncode not in (0, 3010, 1641):
        raise RuntimeError(f'Платформа уже установлена. Установщик драйвера HASP завершился с кодом {result.returncode}.')
    if cancel.is_set():
        event(dict(kind='message', text='Запрос прерывания получен во время установки драйвера. Драйвер завершил работу; платформа установлена.'))
    return result.returncode in (3010, 1641)


class InstallProgress:
    def __init__(self):
        self.total, self.position, self.backward, self.step = 0, 0, False, 0

    def update(self, fields):
        kind = fields[0]
        if kind == 0:
            self.total, self.backward = max(0, fields[1]), bool(fields[2])
            self.position = self.total if self.backward else 0
            self.step = 0
        elif kind == 1:
            self.step = fields[1] if fields[2] else 0
        elif kind == 2:
            self.position += -fields[1] if self.backward else fields[1]
        elif kind == 3:
            self.total += fields[1]
        return max(0, min(100, self.position / self.total * 100)) if self.total else None

    def action_data(self):
        return self.update((2, self.step, 0, 0)) if self.step else None


def install_msi(metadata, settings, event, cancel=None):
    cancel = cancel or threading.Event()
    waiting = False
    while not _MSI_LOCK.acquire(timeout=.1):
        if cancel.is_set():
            raise OperationCancelled('Установка отменена во время ожидания Windows Installer')
        if not waiting:
            event(dict(kind='stage', text='Ожидание другой установки Windows Installer на этом компьютере'))
            waiting = True
    try:
        return _install_msi(metadata, settings, event, cancel)
    finally:
        _MSI_LOCK.release()


def _install_msi(metadata, settings, event, cancel=None):
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise OperationCancelled('Установка отменена до запуска')
    properties = install_properties(metadata, settings)
    if settings.get('ras_service'):
        check_ras_destination(ras_service_options(settings, metadata['properties']['ProductVersion'], windows=True)['name'])
    driver = prepare_hasp(metadata, event, cancel) if settings.get('hasp') else None
    msi = Msi()
    service = prepare_service(msi, metadata) if settings.get('service') else None
    if service and service['transform']:
        properties.update(TRANSFORMS=service['transform'], TRANSFORMSSECURE='1')
    progress, errors, callback_errors = InstallProgress(), [], []
    secret = settings.get('service_password', '')
    def emit(text, kind='message'):
        event(dict(kind=kind, text=str(text).replace(secret, '[скрыто]') if secret else str(text)))
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
    def handle(context, message, record):
        try:
            if cancel.is_set():
                return 2  # IDCANCEL: Windows Installer performs rollback.
            kind = message & 0xff000000
            if kind == 0x0a000000:
                percent = progress.update([msi.dll.MsiRecordGetInteger(record, column) for column in range(1, 5)])
                if percent is not None:
                    event(dict(kind='progress', percent=percent, phase='Windows Installer', unit='установки'))
            elif kind == 0x09000000:
                percent = progress.action_data()
                if percent is not None:
                    event(dict(kind='progress', percent=percent, phase='Windows Installer', unit='установки'))
            elif kind in (0x01000000, 0x02000000):
                text = ('ОШИБКА: ' if kind == 0x01000000 else 'ПРЕДУПРЕЖДЕНИЕ: ') + msi.message(record)
                errors.append(text)
                del errors[:-12]
                emit(text)
            elif kind == 0x08000000:
                progress.step = 0
                emit(msi.text(record, 2), 'stage')
            return 1
        except Exception as exc:
            callback_errors.append(str(exc))
            return 2
    callback = callback_type(handle)
    previous, ignored = ctypes.c_void_p(), ctypes.c_void_p()
    old_ui = msi.dll.MsiSetInternalUI(2, None)
    try:
        code = msi.dll.MsiSetExternalUIRecord(ctypes.cast(callback, ctypes.c_void_p),
                                            (1 << 1) | (1 << 2) | (1 << 8) | (1 << 9) | (1 << 10), None, ctypes.byref(previous))
        if code:
            raise RuntimeError('Не удалось включить прогресс Windows Installer: ' + str(code))
        emit('Установка: ' + metadata['properties'].get('ProductName', '1С'), 'stage')
        command = ' '.join(key + '=' + property_value(value) for key, value in properties.items())
        code = msi.dll.MsiInstallProductW(metadata['path'], command)
    finally:
        msi.dll.MsiSetExternalUIRecord(previous, 0, None, ctypes.byref(ignored))
        msi.dll.MsiSetInternalUI(old_ui, None)
    if callback_errors:
        detail = callback_errors[-1].replace(secret, '[скрыто]') if secret else callback_errors[-1]
        raise RuntimeError('Ошибка чтения хода установки; Windows Installer завершил откат: ' + detail)
    if code == 1602:
        raise OperationCancelled('Установка отменена. Windows Installer завершил откат.')
    if code not in (0, 3010, 1641):
        detail = '\n'.join(errors)
        if secret:
            detail = detail.replace(secret, '[скрыто]')
        raise RuntimeError(f'Windows Installer: код {code}. ' + detail)
    reboot = code in (3010, 1641)
    configured = configure_installed_service(msi, service, metadata, settings, event) if service else None
    ras = create_ras_service(msi, metadata, settings, event) if settings.get('ras_service') else None
    if driver:
        reboot = install_hasp(driver, event, cancel) or reboot
    if reboot:
        emit('ПРЕДУПРЕЖДЕНИЕ: для завершения установки требуется перезагрузка Windows.')
    event(dict(kind='progress', percent=100, phase='Windows Installer', unit='установки'))
    return dict(code=code, reboot=reboot, service=configured, ras=ras)
