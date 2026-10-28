"""Shared password/key authentication and explicit host-key verification."""
import base64
import hashlib
from pathlib import Path
import re

import paramiko


class UnknownHostKey(RuntimeError):
    def __init__(self, host, key):
        self.key = dict(host=host, algorithm=key.get_name(), data=key.get_base64(),
                        fingerprint='SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('='))
        super().__init__('Не подтверждён ключ SSH: ' + host + ' · ' + self.key['fingerprint'] +
                         '. Нажмите «Проверить SSH», «Подключиться» или «Проверить ibcmd», чтобы подтвердить ключ сервера.')


class RequireKnownHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


def connect(values):
    host, user = values.get('host', '').strip(), values.get('user', '').strip()
    # Usernames go to the SSH authentication API, never to a shell command.
    # Windows accounts may contain Cyrillic characters, spaces and a domain.
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:\-]*', host) or not user or len(user) > 256 or
            any(ord(char) < 32 or ord(char) == 127 for char in user)):
        raise ValueError('Укажите корректные адрес и пользователя SSH')
    port = int(values.get('port', 22))
    if not 1 <= port <= 65535:
        raise ValueError('Порт SSH должен быть от 1 до 65535')
    use_key = bool(values.get('use_key', False))
    password, key = values.get('password', ''), values.get('key', '').strip()
    if not use_key and not password:
        raise ValueError('Введите пароль SSH или включите «По SSH-ключу»')
    if use_key and key and not Path(key).is_file():
        raise ValueError('Файл закрытого ключа SSH не найден на компьютере консоли')
    client = paramiko.SSHClient()
    try:
        known = Path.home() / '.ssh/known_hosts'
        if known.is_file():
            client.load_system_host_keys(str(known))
        for name, item in values.get('host_keys', {}).items():
            client.get_host_keys().add(name, item['algorithm'],
                paramiko.PKey.from_type_string(item['algorithm'], base64.b64decode(item['data'], validate=True)))
        client.set_missing_host_key_policy(RequireKnownHost())
        options = dict(hostname=host, port=port, username=user, timeout=12, auth_timeout=15, banner_timeout=15,
                       channel_timeout=15, allow_agent=use_key, look_for_keys=use_key)
        if use_key:
            if key:
                options['key_filename'] = key
            if password:
                options['passphrase'] = password
        else:
            options['password'] = password
        client.connect(**options)
        client.get_transport().set_keepalive(15)
        return client
    except Exception:
        client.close()
        raise


def probe(values):
    """Authenticate and read the Linux environment without Python or DB tools."""
    with connect(values) as client:
        stdin, stdout, stderr = client.exec_command('uname -m; cat /etc/os-release; id -u', timeout=15)
        stdin.close()
        description = stdout.read(32768).decode('utf-8', errors='replace')
        error = stderr.read(4096).decode('utf-8', errors='replace')
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Проверка SSH: ' + error.strip())
        return dict(description=description.strip())
