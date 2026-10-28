"""Password/key SSH transport and local metadata for official Linux installers."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import socket
import time
from uuid import uuid4

import paramiko
from ssh_connection import UnknownHostKey, RequireKnownHost, connect as shared_ssh_connect, probe as shared_ssh_probe

from linux_platform_runner import COMPONENTS, SERVICE_SETTING_DEFAULTS, deb_component, linux_service_options, ras_service_options
from platform_install import unpack_distribution
from linux_python_runtime import runtime_for
from linux_packages import inspect_debs
from postgres_runner import OperationCancelled

FEATURES = {'client_full': 'Толстый клиент и конфигуратор', 'client_thin': 'Тонкий клиент',
            'client_thin_fib': 'Тонкий клиент · файловый вариант', 'server': 'Сервер 1С:Предприятия',
            'ws': 'Модуль расширения веб-сервера', 'server_admin': 'Администрирование сервера 1С',
            'config_storage_server': 'Сервер хранилища конфигураций',
            'additional_admin_functions': 'Дополнительные функции администрирования',
            'liberica_jre': 'Java Runtime Environment', 'integrity_monitoring': 'Контроль целостности'}


def distribution_features(distribution):
    return None  # Only installer/package contents can confirm availability.


def ssh_settings(values):
    result = dict(values)
    host, user = result.get('host', '').strip(), result.get('user', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:\-]*', host) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', user):
        raise ValueError('Укажите адрес и пользователя Linux-ВМ без параметров командной строки')
    try:
        port = int(result.get('port', 22))
    except ValueError:
        raise ValueError('Некорректный порт SSH') from None
    if not 1 <= port <= 65535:
        raise ValueError('Порт SSH должен быть от 1 до 65535')
    return dict(result, host=host, user=user, port=port)


def connect(values):
    return shared_ssh_connect(ssh_settings(values))


def probe(values):
    return shared_ssh_probe(ssh_settings(values))


def linux_metadata(archive, distribution, event, cancel):
    root = unpack_distribution(archive, event, cancel)
    # 7-Zip treats gzip/xz and their inner TAR as two archives.
    tarballs = list(root.glob('*.tar'))
    if tarballs:
        if len(tarballs) != 1:
            raise ValueError('В дистрибутиве несколько TAR-архивов')
        root = unpack_distribution(tarballs[0], event, cancel)
    runs = [path for path in root.rglob('*.run') if path.name.startswith(('setup-full-', 'setup-thin-', 'setup-client-', 'setup-server-'))]
    if runs:
        depth = min(len(path.relative_to(root).parts) for path in runs)
        runs = [path for path in runs if len(path.relative_to(root).parts) == depth]
        if len(runs) != 1:
            raise ValueError('Не найден единственный основной Linux RUN-установщик')
        installer = runs[0]
        match = re.search(r'8\.[35]\.\d+\.\d+', installer.name)
        if match and match.group() != distribution['version']:
            raise ValueError('Версия RUN отличается от выбранного релиза: ' + match.group())
        features = []
        capabilities = dict(capabilities_verified=False, capabilities_source='RUN: требуется чтение --help на Linux-ВМ')
        files = [path for path in root.rglob('*') if path.is_file()]
        format = 'run'
    else:
        files = [path for path in root.rglob('*.deb') if path.is_file()]
        if not files:
            raise ValueError('В архиве нет RUN или DEB. Для Ubuntu / Debian выберите единый Linux-установщик либо DEB-комплект.')
        installer, format = root, 'deb'
        event(dict(kind='stage', text='Чтение control и файлов DEB · проверка точного состава'))
        capabilities = inspect_debs(files, distribution['version'], cancel)
        features = capabilities.pop('features')
    if not features and format != 'run':
        raise ValueError('Не удалось определить компоненты Linux-дистрибутива')
    return dict(kind='linux', path=str(installer), root=str(root), format=format, files=[str(path) for path in files],
                properties=dict(ProductVersion=distribution['version'], ProductName='1С:Предприятие · Linux'),
                architecture=distribution['architecture'], features=[dict(id=key, title=FEATURES[key], parent='', display='1', level='200') for key in features], **capabilities)


def validate_settings(metadata, settings):
    if metadata.get('capabilities_verified') is False:
        raise ValueError('Сначала проверьте точный состав RUN на Linux-ВМ')
    features = {row['id'] for row in metadata['features']}
    selected = set(settings.get('features', []))
    if not selected or not selected <= features:
        raise ValueError('Выберите компоненты Linux из выбранного дистрибутива')
    if settings.get('service') and 'server' not in selected:
        raise ValueError('Для службы выберите компонент сервера 1С')
    if settings.get('service'):
        linux_service_options(settings)
    if settings.get('ras_service'):
        if not (selected & set(metadata.get('ras_features', [])) or 'common' in metadata.get('ras_features', [])):
            raise ValueError('Для службы RAS выберите компонент, содержащий ras в этом дистрибутиве')
        ras_service_options(settings)
        if settings.get('service') and linux_service_options(settings)['name'] == ras_service_options(settings)['name']:
            raise ValueError('Имена unit сервера 1С и RAS должны отличаться')
    directory = settings.get('directory', '').strip()
    if directory and (metadata['format'] == 'deb' or metadata.get('capabilities_verified') and not metadata.get('directory_option') or not directory.startswith('/') or any(c in directory for c in '\r\n\x00')):
        raise ValueError('Для RUN укажите абсолютный путь Linux, для DEB оставьте каталог пустым')


def installation_settings(settings):
    defaults = dict(features=[], language='RU', directory='', service=False, **SERVICE_SETTING_DEFAULTS)
    values = {key: settings.get(key, default) for key, default in defaults.items()}
    if settings.get('linux_service_name') is not None:
        values['linux_service_name'] = settings['linux_service_name']
    if 'linux_service_data' not in settings:
        values['linux_service_data'] = linux_service_options(dict(service_port=values['service_port']))['data']
    if 'ras_port' not in settings:
        values['ras_port'] = str(ras_service_options(dict(service_port=values['service_port']))['port'])
    return values


def command(client, args, password='', critical=False, event=None, cancel=None):
    """Small preflight/bootstrap commands. Credentials travel through SSH stdin."""
    stdin, stdout, stderr = client.exec_command(shlex.join(args), timeout=30)
    if password:
        stdin.write(password + '\n')
        stdin.flush()
    stdin.close()
    channel = stdout.channel
    data, errors = bytearray(), bytearray()
    try:
        while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
            if cancel and cancel.is_set() and not critical:
                raise OperationCancelled('SSH-запрос прерван до установки')
            if channel.recv_ready():
                chunk = channel.recv(32768)
                data.extend(chunk)
                if event:
                    event(dict(kind='message', text=chunk.decode('utf-8', errors='replace')))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(32768))
            if len(data) + len(errors) > 1024 * 1024:
                del data[:-32768]
                del errors[:-32768]
            time.sleep(.03)
        code = channel.recv_exit_status()
        if code:
            detail = bytes(errors or data).decode('utf-8', errors='replace')[-3000:]
            if password:
                detail = detail.replace(password, '[скрыто]')
            raise RuntimeError(f'SSH: код {code}. ' + detail)
        return bytes(data).decode('utf-8', errors='replace')
    finally:
        channel.close()


def inspect_distribution(metadata, connection, event, cancel):
    return install(metadata, {}, connection, event, cancel, cleanup=False, inspect_only=True)


def install(metadata, settings, connection, event, cancel, cleanup=True, inspect_only=False):
    if not inspect_only:
        validate_settings(metadata, settings)
    settings = installation_settings(settings)
    connection = ssh_settings(connection)
    connection_key = (connection['host'], connection['port'], connection['user'])
    source_key = [(str(path), Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in metadata['files']]
    cached = metadata.get('remote_prepared')
    resume = cached if cached and tuple(cached['connection']) == connection_key and cached['source_key'] == source_key else None
    token = resume['token'] if resume else uuid4().hex
    remote_root = '/tmp/ka-1c-' + token
    with connect(connection) as client:
        if cancel.is_set():
            raise OperationCancelled('SSH-установка отменена до передачи файлов')
        event(dict(kind='stage', text='Проверка Ubuntu / Debian и прав установки'))
        release = command(client, ['cat', '/etc/os-release'], cancel=cancel)
        if not re.search(r'^(?:ID|ID_LIKE)=.*(?:ubuntu|debian)', release, re.M | re.I):
            raise ValueError('Поддерживаются Ubuntu / Debian')
        root_user = command(client, ['id', '-u'], cancel=cancel).strip() == '0'
        sudo_password = connection.get('sudo_password') or (connection.get('password', '') if not connection.get('use_key') else '')
        prefix = [] if root_user else ['sudo', '-S', '-p', '', '--']
        if not root_user:
            command(client, ['sudo', '-S', '-p', '', '-v'], sudo_password, cancel=cancel)
        event(dict(kind='stage', text='Подготовка встроенного Python для Linux', cancellable=True))
        machine = command(client, ['uname', '-m'], cancel=cancel).strip()
        runtime = runtime_for(machine)
        expected_arch = {'64': 'x86_64', 'ARM64': 'aarch64'}.get(metadata['architecture'])
        if expected_arch != runtime['arch']:
            raise ValueError('Архитектура дистрибутива 1С не совпадает с ВМ: ' + machine + '. Выберите дистрибутив x64 или ARM64 для этой ВМ.')
        root = Path(metadata['root']).resolve()
        files = [Path(path) for path in metadata['files']]
        total = sum(path.stat().st_size for path in files) + Path(runtime['path']).stat().st_size
        transferred, last = 0, 0
        payload_files = resume['files'] if resume else []
        event(dict(kind='stage', text='Передача дистрибутива на Linux-ВМ', cancellable=True))
        with client.open_sftp() as sftp:
            if not resume:
                sftp.mkdir(remote_root, mode=0o700)
                with sftp.open(remote_root + '/.ka-owned', 'w') as marker:
                    marker.write(token)
            directories = {remote_root}
            for path in ([] if resume else files):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError('Файл дистрибутива находится вне каталога распаковки')
                relative = path.relative_to(root).as_posix()
                destination = remote_root + '/' + relative
                parent = PurePosixPath(destination).parent
                for part in reversed((parent, *parent.parents)):
                    name = str(part)
                    if name.startswith(remote_root + '/') and name not in directories:
                        sftp.mkdir(name, mode=0o700)
                        directories.add(name)
                def progress(done, size):
                    nonlocal last
                    if cancel.is_set():
                        raise OperationCancelled('Передача дистрибутива на ВМ прервана. Каталог: ' + remote_root)
                    if time.monotonic() - last >= .15 or done == size:
                        event(dict(kind='progress', percent=(transferred + done) / max(1, total) * 100, phase='Передача на ВМ', unit='передачи'))
                        last = time.monotonic()
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        if cancel.is_set():
                            raise OperationCancelled('Подготовка передачи прервана')
                        digest.update(chunk)
                sftp.put(str(path), destination, callback=progress, confirm=True)
                payload_files.append(dict(name=relative, sha256=digest.hexdigest()))
                transferred += path.stat().st_size
            if not resume:
                event(dict(kind='stage', text='Передача встроенного Python ' + runtime['version'] + ' · ' + runtime['arch']))
                sftp.put(runtime['path'], remote_root + '/python-runtime.tar.gz', callback=progress, confirm=True)
            else:
                event(dict(kind='message', text='Используется уже переданный и проверенный дистрибутив на ВМ'))
            source = Path(__file__).with_name('linux_platform_runner.py').read_text(encoding='utf-8')
            with sftp.open(remote_root + '/runner.py', 'w') as target:
                target.write(source)
        event(dict(kind='stage', text='Распаковка и проверка встроенного Python на ВМ'))
        # Both paths and the hash are generated locally; no shell input comes
        # from editable installation fields. System Python is never called.
        check = runtime['sha256'] + '  ' + remote_root + '/python-runtime.tar.gz'
        bootstrap = ("printf '%s\\n' " + shlex.quote(check) + ' | sha256sum -c - && mkdir -m 700 ' +
                     shlex.quote(remote_root + '/python-runtime') + ' && tar -xzf ' +
                     shlex.quote(remote_root + '/python-runtime.tar.gz') + ' -C ' +
                     shlex.quote(remote_root + '/python-runtime') + ' --no-same-owner')
        if not resume:
            command(client, ['sh', '-c', bootstrap], cancel=cancel, event=event)
        interpreter = remote_root + '/python-runtime/python/bin/python3.12'
        description = command(client, [interpreter, '-I', '--version'], cancel=cancel).strip()
        if description != 'Python ' + runtime['version']:
            raise RuntimeError('На ВМ не подтверждён запуск встроенного Python. Файлы: ' + remote_root)
        event(dict(kind='message', text='Встроенная среда Linux: ' + description))
        payload = dict(root=remote_root, token=token, files=payload_files, format=metadata['format'],
                       version=metadata['properties']['ProductVersion'], architecture=metadata['architecture'],
                       installer=Path(metadata['path']).relative_to(root).as_posix(), settings=settings, cleanup=cleanup,
                       action='inspect' if inspect_only else 'install')
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=True).encode()).decode()
        stdin, stdout, stderr = client.exec_command(shlex.join([*prefix, interpreter, '-I', '-u', remote_root + '/runner.py', encoded]), timeout=30)
        if prefix:
            stdin.write(sudo_password + '\n')
            stdin.flush()
        channel = stdout.channel
        result, errors, pending, sent_cancel, started = None, [], '', False, False
        deadline = time.monotonic() + 35
        try:
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if cancel.is_set() and not sent_cancel:
                    stdin.write(token + ':cancel\n')
                    stdin.flush()
                    sent_cancel = True
                if not started and time.monotonic() > deadline:
                    raise RuntimeError('SSH: установщик не подтвердил запуск. Проверьте пароль sudo. Файлы: ' + remote_root)
                if channel.recv_ready():
                    pending += channel.recv(65536).decode('utf-8', errors='replace')
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        try:
                            item = json.loads(line)
                        except ValueError:
                            item = dict(kind='message', text=line)
                        if item.get('kind') == 'result':
                            result = item['result']
                        elif item.get('kind') == 'error':
                            errors.append(item['text'])
                        elif item.get('kind') == 'cancelled':
                            raise OperationCancelled(item['text'])
                        else:
                            started = True
                            event(item)
                if channel.recv_stderr_ready():
                    error = channel.recv_stderr(32768).decode('utf-8', errors='replace')
                    for secret in (connection.get('password'), sudo_password):
                        if secret:
                            error = error.replace(secret, '[скрыто]')
                    errors.append(error)
                if not client.get_transport().is_active():
                    raise RuntimeError('Связь SSH потеряна. Проверьте состояние установки на ВМ перед повтором. Каталог: ' + remote_root)
                time.sleep(.05)
            code = channel.recv_exit_status()
            if errors or code or result is None:
                raise RuntimeError('Установка Linux не подтверждена. ' + '\n'.join(errors)[-5000:] + '\nФайлы на ВМ: ' + remote_root)
            if inspect_only:
                result['remote_prepared'] = dict(token=token, connection=connection_key, source_key=source_key, files=payload_files)
            return result
        finally:
            stdin.close()
            channel.close()
