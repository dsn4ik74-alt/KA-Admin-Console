"""Local execution and password/key SSH transport for the PostgreSQL runner."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import threading

from postgres_runner import OperationCancelled, decode_output, execute, executable, redact
from ssh_connection import connect as ssh_connect, UnknownHostKey
from dt_download import download_dt


BOOTSTRAP = ("import io,json,sys;sys.stdin=io.TextIOWrapper(io.FileIO(0,'rb',closefd=False),encoding='utf-8');"
             "exec(compile(json.loads(sys.stdin.buffer.readline()),'<console-postgres>','exec'),{'__name__':'__main__'})")


class SSHProcess:
    def __init__(self, client, command):
        self.client = client
        self.channel = client.get_transport().open_session(timeout=15)
        self.channel.set_combine_stderr(True)
        self.channel.exec_command(command)
        self.stdin, self.stdout = self.channel.makefile_stdin('wb'), self.channel.makefile('rb')

    def poll(self):
        return self.channel.recv_exit_status() if self.channel.exit_status_ready() else None

    def wait(self):
        return self.channel.recv_exit_status()

    def terminate(self):
        self.channel.close()

    def close(self):
        self.channel.close()
        self.client.close()


def ssh_values(settings):
    return dict(host=settings.get('ssh_host', ''), user=settings.get('ssh_user', ''),
        port=settings.get('ssh_port', 22), password=settings.get('ssh_password', ''),
        use_key=settings.get('ssh_use_key', False), key=settings.get('ssh_key', ''),
        host_keys=settings.get('ssh_host_keys', {}))


def start_remote(settings, event, cancel=None):
    client = ssh_connect(ssh_values(settings))
    try:
        command = 'python3 -u -c ' + shlex.quote(BOOTSTRAP)
        if cancel is not None and cancel.is_set():
            raise OperationCancelled('Операция прервана пользователем')
        return SSHProcess(client, command)
    except Exception:
        client.close()
        raise


def read_only_job(payload, event, runner=None):
    """Only metadata/probe callers may offer first-connection trust and retry."""
    if payload.get('mode') not in ('list', 'ibcmd_probe'):
        raise ValueError('Подтверждение SSH допустимо только для чтения')
    try:
        return (runner or run_job)(payload, event)
    except UnknownHostKey as exc:
        return dict(ssh_host_key=exc.key)


def ssh_command(settings):
    host = settings.get('ssh_host', '').strip()
    user = settings.get('ssh_user', '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:\-]*', host):
        raise ValueError('Укажите адрес SSH без пробелов и параметров')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.\-]*', user):
        raise ValueError('Укажите пользователя SSH')
    port = int(settings.get('ssh_port', 22))
    if not 1 <= port <= 65535:
        raise ValueError('Порт SSH должен быть от 1 до 65535')
    args = [executable('ssh'), '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-p', str(port), '-l', user]
    key = settings.get('ssh_key', '').strip()
    if key:
        if not Path(key).is_file():
            raise ValueError('Файл закрытого ключа SSH не найден на компьютере консоли')
        args += ['-i', key, '-o', 'IdentitiesOnly=yes']
    # Send source through stdin: no files and no Windows command-line length limit.
    code = ("import io,json,sys;sys.stdin=io.TextIOWrapper(io.FileIO(0,'rb',closefd=False),encoding='utf-8');"
            "exec(compile(json.loads(sys.stdin.buffer.readline()),'<console-postgres>','exec'),{'__name__':'__main__'})")
    args += [host, 'python3 -u -c ' + shlex.quote(code)]
    return args


def run_job(payload, event):
    settings = payload['connection']
    cancel_event = getattr(event, '__dict__', {}).get('cancel_event')
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled('Операция прервана пользователем')
    if settings.get('execution', 'local') == 'local':
        return execute(payload, event, cancel_event)
    if settings['execution'] != 'ssh':
        raise ValueError('Неизвестный режим выполнения')
    export_dt = payload['mode'] == 'ibcmd_dump' and not settings.get('dt_keep_remote', False)
    if export_dt and not Path(payload['path']).is_absolute():
        raise ValueError('Укажите абсолютную локальную папку DT на компьютере консоли')
    days = int(settings.get('retention_days', 0)) if export_dt else 0
    if days < 0:
        raise ValueError('Срок хранения не может быть отрицательным')
    event({'kind': 'stage', 'text': 'SSH: ' + settings['ssh_host'] + ' · запуск на Linux'})
    process = start_remote(settings, event, cancel_event)
    result, received, errors, cancelled = None, False, [], ''
    stopped = threading.Event()
    timed_out_cancel = threading.Event()
    def watch_cancel():
        while not stopped.wait(0.1):
            if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
                try:
                    process.stdin.write(b'cancel\n')
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                if not stopped.wait(15) and process.poll() is None:
                    timed_out_cancel.set()
                    process.terminate()
                return
    watcher = threading.Thread(target=watch_cancel, daemon=True)
    try:
        # Credentials are sent through the encrypted stdin channel, never SSH arguments.
        source = Path(__file__).with_name('postgres_runner.py').read_text(encoding='utf-8')
        remote_settings = {key: value for key, value in settings.items() if key not in ('ssh_password', 'ssh_host_keys')}
        remote_payload = dict(payload, connection=remote_settings)
        if export_dt:
            remote_payload['mode'] = 'ibcmd_export'
            remote_payload.pop('path', None)  # A Windows destination must never reach ibcmd on Linux.
            remote_settings['retention_days'] = '0'
        process.stdin.write((json.dumps(source, ensure_ascii=True) + '\n' +
                             json.dumps(remote_payload, ensure_ascii=True) + '\n').encode('ascii'))
        process.stdin.flush()
        watcher.start()
        for line in process.stdout:
            text = decode_output(line).rstrip('\r\n')
            try:
                item = json.loads(text)
                if not isinstance(item, dict) or 'kind' not in item:
                    raise ValueError('Not a runner event')
            except ValueError:
                event({'kind': 'message', 'text': redact(text, settings)})
                continue
            if item['kind'] == 'result':
                result, received = item['result'], True
            elif item['kind'] == 'error':
                errors.append(redact(item['text'], settings))
            elif item['kind'] == 'cancelled':
                cancelled = redact(item['text'], settings)
            else:
                if export_dt and item['kind'] == 'artifact':
                    continue  # Operation history refers to the verified local DT, not the remote staging file.
                if 'text' in item:
                    item['text'] = redact(item['text'], settings)
                event(item)
        code = process.wait()
        if cancelled:
            raise OperationCancelled(cancelled)
        if timed_out_cancel.is_set():
            raise OperationCancelled('SSH не подтвердил остановку за 15 с. Соединение закрыто; '
                                     'проверьте состояние операции на компьютере выполнения перед повторным запуском.')
        if errors:
            raise RuntimeError('\n'.join(errors))
        if code or not received:
            raise RuntimeError(f'SSH: код {code}. Проверьте журнал и утилиты на компьютере выполнения')
        if export_dt:
            return download_dt(process.client, result, payload['path'], event, cancel_event, retention_days=days)
        return result
    finally:
        stopped.set()
        if process.poll() is None:
            process.terminate()
            process.wait()
        if watcher.ident is not None:
            watcher.join(timeout=2)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        if callable(getattr(process, 'close', None)):
            process.close()
