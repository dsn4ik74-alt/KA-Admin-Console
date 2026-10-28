"""Standalone Ubuntu/Debian installer. Sent through SSH; contains no credentials."""
import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading

COMPONENTS = ('client_full', 'client_thin', 'client_thin_fib', 'server', 'ws', 'server_admin',
              'config_storage_server', 'additional_admin_functions', 'liberica_jre', 'integrity_monitoring')
LANGUAGES = ('ar', 'az', 'bg', 'de', 'el', 'es', 'fr', 'hu', 'hy', 'it', 'ka', 'kk', 'lt', 'lv',
             'pl', 'ro', 'ru', 'tk', 'tr', 'uk', 'vi', 'zh')

LINUX_DATA_ROOT = '/home/usr1cv8/.1cv8/1C/1cv8'
LINUX_DATA_DEFAULT = LINUX_DATA_ROOT + '/srvinfo_1541'
SERVICE_SETTING_DEFAULTS = dict(service_port='1540', service_debug=False, linux_service_data=LINUX_DATA_DEFAULT,
                                ras_service=False, ras_port='1545')


def ras_service_options(settings, version='', windows=False):
    agent = linux_service_options(dict(service_port=settings.get('service_port', '1540')))
    value = str(settings.get('ras_port', agent['port'] // 100 * 100 + 45)).strip()
    if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('Порт RAS должен быть числом от 1 до 65535')
    port = int(value)
    if port in (agent['port'], agent['registration']) or agent['first'] <= port <= agent['last']:
        raise ValueError('Порт RAS пересекается с портами сервера 1С')
    if windows and not re.fullmatch(r'8\.\d+\.\d+\.\d+', version):
        raise ValueError('Не определена версия платформы для службы RAS')
    name = f'1C:RAS | {port}' if windows else f'1c_ras_{port}.service'
    display = f'{name} | {version}' if windows else name
    return dict(name=name, display=display, port=port, agent_port=agent['port'], endpoint='localhost:' + str(agent['port']))


def linux_service_options(settings):
    value = str(settings.get('service_port', '1540')).strip()
    if not value.isascii() or not value.isdigit():
        raise ValueError('Введите числовой порт агента, например 1540 или 4540')
    port = int(value)
    family = port // 100 * 100
    registration, first, last = family + 41, family + 60, family + 91
    if not 0 < port <= 65535 or last > 65535 or port == registration or first <= port <= last:
        raise ValueError('Проверьте порт агента: порты должны быть в диапазоне 1–65535 и не пересекаться')
    name = str(settings.get('linux_service_name', 'srv1cv8_' + str(registration) + '.service')).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,230}\.service', name):
        raise ValueError('Имя файла службы: латинские буквы, цифры, _, - и точки с окончанием .service, без папок и @')
    data = str(settings.get('linux_service_data', LINUX_DATA_ROOT + '/srvinfo_' + str(registration))).strip()
    path = PurePosixPath(data)
    if not path.is_absolute() or path == PurePosixPath(path.anchor) or '..' in path.parts or any(ord(c) < 32 for c in data):
        raise ValueError('Укажите полный путь Linux к отдельной папке данных сервера 1С')
    return dict(name=name, data=str(path), port=port, registration=registration, first=first, last=last,
                debug=bool(settings.get('service_debug')))


def environment_lines(options):
    values = dict(SRV1CV8_PORT=str(options['port']), SRV1CV8_REGPORT=str(options['registration']),
                  SRV1CV8_RANGE=str(options['first']) + ':' + str(options['last']),
                  SRV1CV8_DEBUG='-debug' if options['debug'] else '', SRV1CV8_DATA=options['data'])
    result = []
    for key, value in values.items():
        assignment = key + '=' + value.replace('%', '%%')
        if any(c.isspace() or c in '\\"\'' for c in assignment):
            assignment = '"' + assignment.replace('\\', '\\\\').replace('"', '\\"') + '"'
        result.append('Environment=' + assignment)
    return result


def ras_environment_lines(options):
    return [f"Environment=RAS_PORT={options['port']}", 'Environment=RAS_CLUSTER_ADDRESS=' + options['endpoint']]


def render_service_unit(source, options, *, ras=False):
    """Copy the vendor unit settings, overriding only the five chosen values."""
    if not re.search(r'^\s*\[Service\]\s*$', source, re.M) or not re.search(r'^\s*ExecStart\s*=', source, re.M):
        raise ValueError('Штатный unit 1С не содержит секцию Service или ExecStart')
    keys = ('RAS_PORT', 'RAS_CLUSTER_ADDRESS') if ras else ('SRV1CV8_PORT', 'SRV1CV8_REGPORT', 'SRV1CV8_RANGE', 'SRV1CV8_DEBUG', 'SRV1CV8_DATA')
    values = ras_environment_lines(options) if ras else environment_lines(options)
    if not all(re.search(r'\$(?:' + key + r'\b|\{' + key + r'\})', source) for key in keys):
        raise ValueError('Штатный unit 1С не использует ожидаемые переменные службы; настройки не применены')
    result, section, added = [], '', False
    assignment = r'(?:' + '|'.join(keys) + ')='
    for line in source.splitlines():
        match = re.fullmatch(r'\s*\[([^]]+)\]\s*', line)
        if match:
            if section == 'Service':
                result.extend(values)
                added = True
            section = match.group(1)
        if section == 'Service' and re.fullmatch(r'\s*Environment\s*=\s*(?:' + assignment + r'[^\s"\']*|"' + assignment + r'(?:[^"\\]|\\.)*")\s*', line):
            continue
        if section == 'Install' and re.match(r'\s*DefaultInstance\s*=', line):
            continue
        # A copied standalone unit has no instance; substitute only instance
        # specifiers while keeping escaped percent signs and other specifiers.
        if not ras:
            line = re.sub(r'%%|%[iI]', lambda m: '%%' if m[0] == '%%' else str(options['registration']), line)
        result.append(line)
    if section == 'Service':
        result.extend(values)
        added = True
    if not added:
        raise ValueError('Не удалось добавить параметры в секцию Service unit-файла')
    return '\n'.join(result) + '\n'


def service_destination(options, directory='/etc/systemd/system'):
    destination = Path(directory) / options['name']
    # Do not replace an existing unit (including a mask or a broken symlink).
    # Another name can be entered in the form before installation starts.
    directories = (Path(directory), Path('/run/systemd/system'), Path('/usr/local/lib/systemd/system'),
                   Path('/usr/lib/systemd/system'), Path('/lib/systemd/system'))
    for base in directories:
        if os.path.lexists(base / options['name']):
            raise ValueError('Служба ' + options['name'] + ' уже существует. Укажите другое имя файла службы перед установкой.')
    return destination


def deb_component(name):
    name = name.lower().split('_', 1)[0]
    name = re.sub(r'-nls$', '', name)
    if re.search(r'[-.]common(?:[-.]|$)', name):
        return 'common'
    for suffix, feature in (('thin-client', 'client_thin'), ('thinclient', 'client_thin'),
                            ('client', 'client_full'), ('server', 'server'), ('ws', 'ws'),
                            ('crs', 'config_storage_server'), ('admin', 'additional_admin_functions')):
        if name.endswith('-' + suffix):
            return feature
    return ''


def run_capabilities(help_text):
    block = re.search(r'^\s*--enable-components\b(.*?)(?=^\s*--[\w-]+\b|\Z)', help_text, re.M | re.S)
    if not block:
        raise ValueError('RUN не сообщил --enable-components; точный состав не подтверждён')
    allowed = re.search(r'(?:Allowed(?: values)?|Valid values|Допустимые значения)\s*:\s*(.*)', block.group(1), re.I | re.S)
    if not allowed:
        raise ValueError('RUN не сообщил полный список допустимых компонентов; состав не подтверждён')
    tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', allowed.group(1)))
    features = [key for key in COMPONENTS if key in tokens]
    if not features:
        raise ValueError('В справке RUN не обнаружены поддерживаемые компоненты 1С')
    return dict(features=features, languages=['EN'] + [key.upper() for key in LANGUAGES if key in tokens],
                component_languages=[key for key in LANGUAGES if key in tokens],
                directory_option=next((key for key in ('--prefix', '--installdir') if re.search(r'^\s*' + key + r'\b', help_text, re.M)), None),
                capabilities_verified=True, capabilities_source='RUN: --help выбранного установщика на Linux',
                hasp_supported=False, ras_features=['server_admin'] if 'server_admin' in features else [])


def run_arguments(path, settings, help_text):
    capabilities = run_capabilities(help_text)
    available = set(capabilities['features']) | set(capabilities['component_languages'])
    selected = list(dict.fromkeys(settings['features']))
    if not selected or not set(selected) <= set(COMPONENTS) or not set(selected) <= available:
        raise ValueError('Выбранные компоненты отсутствуют в этом Linux-установщике')
    language = settings.get('language', 'RU').lower()
    if language != 'en':
        if language not in LANGUAGES or language not in available:
            raise ValueError('Выбранного языка нет в Linux-установщике')
        selected.append(language)
    args = [str(path), '--mode', 'unattended', '--enable-components', ','.join(selected)]
    disabled = sorted(available - set(selected))
    if disabled:
        args.extend(('--disable-components', ','.join(disabled)))
    directory = settings.get('directory', '').strip()
    if directory:
        if not directory.startswith('/') or any(c in directory for c in '\n\r\x00'):
            raise ValueError('Укажите абсолютный каталог установки на Linux')
        option = capabilities['directory_option']
        if not option:
            raise ValueError('Этот Linux-установщик не поддерживает изменение каталога')
        args.extend((option, directory))
    return args


def selected_debs(rows, settings, machine):
    selected, result = set(settings['features']), []
    supported = set()
    for row in rows:
        kind = deb_component(row['package'])
        if kind:
            supported.add(kind)
        if kind == 'common' or kind in selected or kind == 'client_full' and 'client_thin_fib' in selected:
            if row['architecture'] not in (machine, 'all'):
                raise ValueError('Архитектура DEB не совпадает с архитектурой ВМ')
            if settings.get('language', 'RU').upper() == 'EN' and row['package'].endswith('-nls'):
                continue
            result.append(row)
    if not result or not selected <= (supported | ({'client_thin_fib'} if 'client_full' in supported else set())):
        raise ValueError('В DEB-комплекте нет выбранных компонентов')
    if settings.get('directory', '').strip():
        raise ValueError('Каталог DEB задаётся пакетами. Оставьте поле каталога пустым.')
    by_name = {row['package']: row for row in rows}
    included = {row['package'] for row in result}
    for row in result:
        localized = by_name.get(row['package'] + '-nls') if settings.get('language', 'RU').upper() != 'EN' else None
        if localized and localized['package'] not in included:
            included.add(localized['package'])
            result.append(localized)
        for group in row.get('depends', '').split(','):
            names = [re.split(r'[\s(:]', name.strip(), maxsplit=1)[0] for name in group.split('|')]
            dependency = next((by_name[name] for name in names if name in by_name), None)
            if dependency and dependency['package'] not in included:
                if dependency['architecture'] not in (machine, 'all'):
                    raise ValueError('Архитектура зависимого DEB не совпадает с архитектурой ВМ')
                included.add(dependency['package'])
                result.append(dependency)
    return [row['path'] for row in result]


def validate_stage(path, token):
    root = Path(path)
    if not re.fullmatch(r'[0-9a-f]{32}', token) or root != Path('/tmp') / ('ka-1c-' + token) or root.resolve() != root:
        raise ValueError('Неверный временный каталог установки на ВМ')
    if root.is_symlink() or (root / '.ka-owned').read_text(encoding='ascii') != token:
        raise ValueError('Метка временного каталога установки на ВМ не совпадает')
    return root


def inside(path, root):
    # relative_to exists on older Ubuntu Python too (is_relative_to is 3.9+).
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class Cancelled(Exception):
    pass


class Runner:
    def __init__(self, payload, event):
        self.payload, self.event = payload, event
        self.cancel = threading.Event()
        self.env = dict(os.environ, LANG='C.UTF-8', LC_ALL='C.UTF-8', DEBIAN_FRONTEND='noninteractive')

    def check(self):
        if self.cancel.is_set():
            raise Cancelled('Установка прервана между этапами. Уже завершённые изменения сохраняются.')

    def stage(self, text, critical=False):
        self.check()
        self.event(dict(kind='stage', text=text, cancellable=not critical))

    def run(self, args, capture=False, critical=False):
        self.check()
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self.env)
        lines, tail = queue.Queue(maxsize=256), deque(maxlen=30)
        def read():
            try:
                for line in iter(process.stdout.readline, b''):
                    lines.put(line)
            finally:
                lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        chunks = []
        try:
            ended = False
            while not ended:
                if self.cancel.is_set() and not critical:
                    process.terminate()
                    raise Cancelled('Операция на Linux прервана до установки пакетов')
                try:
                    item = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if item is None:
                    ended = True
                    continue
                text = item.decode('utf-8', errors='replace').rstrip('\r\n')
                tail.append(text)
                if capture:
                    chunks.append(text)
                elif text:
                    self.event(dict(kind='message', text=text))
            code = process.wait()
            if code:
                raise RuntimeError(f'{Path(args[0]).name}: код {code}. ' + '\n'.join(tail)[-5000:])
            return '\n'.join(chunks)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()

    def run_dependencies(self, features):
        self.stage('Обновление списка пакетов Ubuntu / Debian')
        self.run(['apt-get', 'update'])
        self.stage('Установка библиотек для Linux-компонентов 1С', critical=True)
        groups = [('ca-certificates',), ('fontconfig',), ('libfontconfig1',), ('libfreetype6',), ('libstdc++6',),
                  ('libgcc-s1', 'libgcc1'), ('fonts-liberation',),
                  ('libglib2.0-0t64', 'libglib2.0-0'), ('libgssapi-krb5-2',)]
        if set(features) & {'client_full', 'client_thin', 'client_thin_fib'}:
            groups.extend((('libgtk-3-0t64', 'libgtk-3-0'), ('libnss3',), ('libx11-6',), ('libxext6',),
                           ('libsm6',), ('libice6',), ('libxrender1',), ('libxi6',), ('libasound2t64', 'libasound2'),
                           ('libcups2t64', 'libcups2')))
        packages = []
        for candidates in groups:
            found = next((name for name in candidates if re.search(r'Candidate:\s*(?!\(none\))\S+',
                                self.run(['apt-cache', 'policy', name], capture=True, critical=True))), None)
            if not found:
                raise RuntimeError('В репозиториях ВМ нет необходимой библиотеки: ' + ' / '.join(candidates))
            packages.append(found)
        self.run(['apt-get', 'install', '-y', '--no-remove', '--no-install-recommends', *packages], critical=True)

    def validate_files(self):
        p = self.payload
        root = validate_stage(p['root'], p['token'])
        if os.geteuid() != 0:
            raise RuntimeError('Для установки 1С на Linux нужны root или sudo')
        release = Path('/etc/os-release').read_text()
        if not re.search(r'^(?:ID|ID_LIKE)=.*(?:ubuntu|debian)', release, re.M | re.I) or not shutil.which('apt-get'):
            raise RuntimeError('Автоустановка поддерживается на Ubuntu / Debian с apt-get')
        machine = self.run(['dpkg', '--print-architecture'], capture=True).strip()
        expected = {'64': 'amd64', '32': 'i386', 'ARM64': 'arm64'}.get(p['architecture'])
        if machine != expected:
            raise ValueError('Архитектура выбранного дистрибутива не совпадает с архитектурой ВМ: ' + machine)
        self.stage('Проверка переданных файлов 1С')
        for item in p['files']:
            path = root / item['name']
            if not inside(path, root) or path.is_symlink() or not path.is_file():
                raise ValueError('Недопустимый путь файла дистрибутива')
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    self.check()
                    digest.update(chunk)
            if digest.hexdigest() != item['sha256']:
                raise ValueError('Повреждён файл, переданный на ВМ: ' + item['name'])
        return root, machine

    def inspect(self):
        root, machine = self.validate_files()
        if self.payload['format'] != 'run':
            raise ValueError('Удалённая проверка справки предназначена для RUN')
        installer = root / self.payload['installer']
        if not inside(installer, root) or not installer.is_file():
            raise ValueError('Неверный путь RUN-установщика')
        installer.chmod(0o700)
        self.stage('Чтение точного состава RUN · установка не выполняется')
        capabilities = run_capabilities(self.run([str(installer), '--help'], capture=True))
        self.event(dict(kind='message', text='Состав RUN подтверждён: ' + ', '.join(capabilities['features'])))
        return dict(capabilities=capabilities, inspected=True, reboot=False)

    def install(self):
        p, settings = self.payload, self.payload['settings']
        if settings.get('service') and 'server' not in settings.get('features', []):
            raise ValueError('Для службы выберите компонент сервера 1С')
        if settings.get('service'):
            service_destination(linux_service_options(settings))
        if settings.get('ras_service'):
            ras_options = ras_service_options(settings)
            if settings.get('service') and ras_options['name'] == linux_service_options(settings)['name']:
                raise ValueError('Имена unit сервера 1С и RAS должны отличаться')
            service_destination(ras_options)
        root, machine = self.validate_files()
        self.event(dict(kind='message', text=('Создание службы включено: ' + linux_service_options(settings)['name']) if settings.get('service') else
                        'Установка без создания службы: флажок службы выключен.'))
        if p['format'] == 'run':
            installer = root / p['installer']
            if not inside(installer, root) or not installer.is_file():
                raise ValueError('Неверный путь RUN-установщика')
            installer.chmod(0o700)
            help_text = self.run([str(installer), '--help'], capture=True)
            args = run_arguments(installer, settings, help_text)
            self.run_dependencies(settings['features'])
            self.stage('Установка компонентов 1С на Linux · дождитесь завершения', critical=True)
            self.run(args, critical=True)
        elif p['format'] == 'deb':
            rows = []
            for item in p['files']:
                if not item['name'].endswith('.deb'):
                    continue
                path = str(root / item['name'])
                output = self.run(['dpkg-deb', '-W', '--showformat=${Package}\t${Architecture}\t${Version}\t${Depends}', path], capture=True)
                fields = output.split('\t', 3)
                package, architecture, version = fields[:3]
                depends = fields[3] if len(fields) > 3 else ''
                if re.sub(r'[^0-9]', '', version) != re.sub(r'[^0-9]', '', p['version']):
                    raise ValueError('Версия DEB не совпадает с выбранным релизом: ' + version)
                rows.append(dict(path=path, package=package, architecture=architecture, depends=depends))
            packages = selected_debs(rows, settings, machine)
            self.run_dependencies(settings['features'])
            self.stage('Установка DEB и зависимостей · дождитесь завершения', critical=True)
            self.run(['apt-get', 'install', '-y', '--no-remove', *packages], critical=True)
        else:
            raise ValueError('На Ubuntu / Debian поддерживаются дистрибутивы RUN и DEB')
        self.verify_runtime(settings, p['version'], machine)
        if settings.get('service'):
            self.install_service(settings, p['version'])
        if settings.get('ras_service'):
            self.install_ras_service(settings, p['version'])
        self.event(dict(kind='message', text='Компоненты 1С установлены на Linux: ' + p['version']))
        if p.get('cleanup', True):
            # The only recursively removed remote path is this marked /tmp tree.
            validate_stage(root, p['token'])
            shutil.rmtree(root)
            self.event(dict(kind='message', text='Временные файлы установки удалены с ВМ'))
        return dict(reboot=False, remote_root=str(root), installed=True)

    def install_service(self, settings, version, unit_directory='/etc/systemd/system'):
        self.stage('Копирование и настройка службы сервера 1С', critical=True)
        options = linux_service_options(settings)
        destination = service_destination(options, unit_directory)
        base = Path(settings.get('directory') or '/opt/1cv8')
        units = list(base.rglob('srv1cv8-' + version + '@.service'))
        if len(units) != 1 or units[0].is_symlink() or not inside(units[0], base):
            raise RuntimeError('Платформа установлена. Не найден единственный штатный unit выбранной версии.')
        source = units[0].read_text(encoding='utf-8')
        content = render_service_unit(source, options)
        service_section = re.search(r'^\[Service\]\s*\n(.*?)(?=^\[|\Z)', content, re.M | re.S).group(1)
        user = re.search(r'^\s*User\s*=\s*([^\n]+)', service_section, re.M)
        group = re.search(r'^\s*Group\s*=\s*([^\n]+)', service_section, re.M)
        if not user:
            raise ValueError('В штатном unit 1С не указан пользователь службы')
        self.prepare_service_data(options['data'], user.group(1).strip(), group.group(1).strip() if group else None)
        return self.activate_unit(destination, content, options, str(units[0]) + '\n' + '\n'.join(environment_lines(options)))

    def activate_unit(self, destination, content, options, detail):
        created = False
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            created = True
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            destination.chmod(0o644)
        except BaseException:
            if created:
                destination.unlink()
            raise
        self.event(dict(kind='message', text='Unit создан: ' + str(destination) + '\n' + detail))
        self.run(['systemctl', 'daemon-reload'], critical=True)
        actual = self.run(['systemctl', 'show', options['name'], '--property=FragmentPath', '--value'], capture=True, critical=True).strip()
        if actual != str(destination):
            raise RuntimeError('Платформа установлена, но systemd не подтвердил загрузку unit: ' + str(destination))
        self.stage('Регистрация и запуск ' + options['name'], critical=True)
        self.run(['systemctl', 'enable', '--now', options['name']], critical=True)
        self.run(['systemctl', 'is-active', '--quiet', options['name']], critical=True)
        self.event(dict(kind='message', text='Служба установлена и запущена: ' + options['name']))
        return str(destination)

    def install_ras_service(self, settings, version, unit_directory='/etc/systemd/system'):
        self.stage('Создание и запуск службы RAS', critical=True)
        options = ras_service_options(settings)
        destination = service_destination(options, unit_directory)
        executable = self.installed_binary(settings, version, 'ras')
        units = list(executable.parent.glob('ras-' + version + '.service'))
        if len(units) == 1 and not units[0].is_symlink():
            content = render_service_unit(units[0].read_text(encoding='utf-8'), options, ras=True)
            # Never run another version from a vendor unit with an unexpected path.
            start = re.search(r'^ExecStart=(.*)$', content, re.M)
            if not start or str(executable) not in start.group(1):
                raise ValueError('Штатный unit RAS ссылается на другой исполняемый файл')
        else:
            # Administration-only packages may omit the vendor unit and account.
            # RAS needs no persistent data and can use a transient systemd user.
            binary = '"' + str(executable).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'
            content = ('[Unit]\nDescription=1C RAS | ' + str(options['port']) + ' | ' + version +
                       '\nAfter=network.target\n[Service]\nType=simple\nDynamicUser=yes\n' +
                       '\n'.join(ras_environment_lines(options)) + '\nExecStart=' + binary +
                       ' cluster --port=${RAS_PORT} ${RAS_CLUSTER_ADDRESS}\nRestart=on-failure\nRestartSec=3\n'
                       '[Install]\nWantedBy=multi-user.target\n')
        return self.activate_unit(destination, content, options, str(executable) + '\n' + '\n'.join(ras_environment_lines(options)))

    @staticmethod
    def installed_binary(settings, version, name):
        base = Path(settings.get('directory') or '/opt/1cv8')
        locations = [*(base / arch / version for arch in ('x86_64', 'i386', 'x86', 'aarch64', 'arm64')), base / version]
        if settings.get('directory'):
            locations.append(base)
        path = next((candidate for folder in locations for candidate in (folder / name, folder / 'bin' / name)
                     if candidate.is_file() and inside(candidate, base)), None)
        if path is None:
            raise RuntimeError('Установщик завершился, но не найден компонент выбранной версии: ' + name)
        return path

    @staticmethod
    def prepare_service_data(path, user, group):
        directory = Path(path)
        missing, parent = [], directory
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        directory.mkdir(parents=True, exist_ok=True)
        # The service must be able to create its cluster data. Preserve existing
        # file ownership below the directory; do not recursively chown data.
        for created in reversed(missing):
            shutil.chown(created, user=user, group=group)
        if not missing:
            shutil.chown(directory, user=user, group=group)

    def verify_runtime(self, settings, version, machine):
        self.stage('Проверка установленных компонентов и библиотек', critical=True)
        names = []
        for feature, binaries in (('server', ('ragent', 'rphost')), ('server_admin', ('rac',)),
                                  ('client_full', ('1cv8',)), ('client_thin', ('1cv8c',)),
                                  ('client_thin_fib', ('1cv8c',)), ('config_storage_server', ('crserver',))):
            if feature in settings['features']:
                names.extend(binaries)
        if settings.get('ras_service'):
            names.append('ras')
        paths = []
        for name in dict.fromkeys(names):
            paths.append(self.installed_binary(settings, version, name))
        def missing():
            result = set()
            for path in paths:
                output = self.run(['ldd', str(path)], capture=True, critical=True)
                result.update(re.findall(r'^\s*(\S+)\s+=>\s+not found', output, re.M))
            return result
        absent = missing()
        if not absent:
            return
        self.stage('Поиск недостающих библиотек в репозиториях Ubuntu / Debian', critical=True)
        self.run(['apt-get', 'install', '-y', '--no-remove', 'apt-file'], critical=True)
        self.run(['apt-file', 'update'], critical=True)
        packages = set()
        arch_path = {'amd64': 'x86_64-linux-gnu', 'i386': 'i386-linux-gnu', 'arm64': 'aarch64-linux-gnu'}[machine]
        for library in sorted(absent):
            output = self.run(['apt-file', 'search', '--regexp', '/' + re.escape(library) + '$'], capture=True, critical=True)
            candidates = set()
            for line in output.splitlines():
                if ': ' not in line:
                    continue
                package, filename = line.split(': ', 1)
                package = package.split(':', 1)[0]
                if arch_path in filename and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9+.-]+', package) and not package.endswith(('-dev', '-dbg', '-udeb')):
                    candidates.add(package)
            if len(candidates) != 1:
                raise RuntimeError('Не удалось однозначно найти библиотеку ' + library + '. Установка компонентов уже выполнена.')
            packages.update(candidates)
        self.run(['apt-get', 'install', '-y', '--no-remove', *sorted(packages)], critical=True)
        absent = missing()
        if absent:
            raise RuntimeError('Платформа установлена, но остаются недостающие библиотеки: ' + ', '.join(sorted(absent)))


def main():
    payload = json.loads(base64.b64decode(sys.argv[1]))
    disconnected = threading.Event()
    def event(item):
        if not disconnected.is_set():
            try:
                print(json.dumps(item, ensure_ascii=True), flush=True)
            except (BrokenPipeError, OSError):
                disconnected.set()
    runner = Runner(payload, event)
    def control():
        for line in sys.stdin:
            if line.strip() == payload['token'] + ':cancel':
                runner.cancel.set()
    threading.Thread(target=control, daemon=True).start()
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        event(dict(kind='stage', text='Запуск установщика на Ubuntu / Debian'))
        result = runner.inspect() if payload.get('action') == 'inspect' else runner.install()
        event(dict(kind='result', result=result))
        return 0
    except Cancelled as exc:
        event(dict(kind='cancelled', text=str(exc)))
        return 2
    except Exception as exc:
        event(dict(kind='error', text=str(exc)))
        return 1


if __name__ == '__main__':
    sys.exit(main())
