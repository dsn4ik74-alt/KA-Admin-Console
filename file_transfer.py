"""Bounded-memory local/SMB/SFTP copying and native Windows Robocopy."""
from contextlib import ExitStack, contextmanager
import codecs
import ctypes
from ctypes import wintypes
import fnmatch
import hashlib
import os
from pathlib import Path, PureWindowsPath
import posixpath
import queue
import re
import stat
import subprocess
import threading
import time
from uuid import uuid4

from postgres_runner import OperationCancelled
from ssh_connection import connect

_lock = threading.RLock()
_shares = {}
CHUNK = 1024 * 1024


def cancelled(cancel):
    if cancel.is_set():
        raise OperationCancelled('Передача прервана. Исходные файлы не удалялись.')


def endpoint(values):
    result = dict(values)
    kind, path = result.get('kind'), str(result.get('path', '')).strip()
    if kind not in ('local', 'windows', 'linux') or not path or any(c in path for c in '\r\n\x00'):
        raise ValueError('Выберите тип подключения и полный путь')
    if kind == 'linux':
        if not path.startswith('/') or '..' in path.split('/'):
            raise ValueError('Для Linux укажите абсолютный путь без ..')
        result['path'] = posixpath.normpath(path)
    else:
        if kind == 'windows':
            host = str(result.get('host', '')).strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', host):
                raise ValueError('Укажите адрес Windows-сервера без порта')
            if re.match(r'^[A-Za-z]:[\\/]', path):
                path = '\\\\' + host + '\\' + path[0].upper() + '$\\' + path[3:]
            if not path.startswith('\\\\') or PureWindowsPath(path).parts[0].split('\\')[2].casefold() != host.casefold():
                raise ValueError('Путь Windows: C:\\папка или \\\\указанный-сервер\\общая-папка\\путь')
        p = PureWindowsPath(path)
        if not p.is_absolute() or '..' in p.parts:
            raise ValueError('Укажите абсолютный путь Windows без ..')
        result['path'] = str(p)
    return result


def validate_pair(source, target):
    source, target = endpoint(source), endpoint(target)
    same = (source['kind'] == target['kind'] and str(source.get('host', '')).casefold() == str(target.get('host', '')).casefold())
    if source['kind'] != 'linux' and target['kind'] != 'linux':
        a, b = source['path'].casefold().rstrip('\\'), target['path'].casefold().rstrip('\\')
        same = True  # Windows paths include their SMB host already.
        separator = '\\'
    else:
        a, b, separator = source['path'].rstrip('/'), target['path'].rstrip('/'), '/'
    if same and (a == b or a.startswith(b + separator) or b.startswith(a + separator)):
        raise ValueError('Источник и получатель совпадают или вложены друг в друга')
    return source, target


@contextmanager
def smb(values):
    """Reference-count only connections created here; never disconnect others."""
    if values['kind'] != 'windows' or not values.get('user'):
        yield
        return
    if os.name != 'nt':
        raise RuntimeError('Для SMB-подключения нужен Windows')
    share = PureWindowsPath(values['path']).anchor.rstrip('\\')
    key = (share.casefold(), values['user'].casefold())
    class NETRESOURCE(ctypes.Structure):
        _fields_ = [('scope', wintypes.DWORD), ('type', wintypes.DWORD), ('display', wintypes.DWORD),
                    ('usage', wintypes.DWORD), ('local', wintypes.LPWSTR), ('remote', wintypes.LPWSTR),
                    ('comment', wintypes.LPWSTR), ('provider', wintypes.LPWSTR)]
    mpr = ctypes.WinDLL('mpr')
    mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(NETRESOURCE), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    mpr.WNetGetUserW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    with _lock:
        if key not in _shares:
            size = wintypes.DWORD(1024)
            user = ctypes.create_unicode_buffer(1024)
            existing = mpr.WNetGetUserW(share, user, ctypes.byref(size)) == 0
            resource = NETRESOURCE(0, 1, 0, 0, None, share, None, None)
            code = mpr.WNetAddConnection2W(ctypes.byref(resource), values.get('password') or None, values['user'], 4)
            if code:
                if code == 1219:
                    raise RuntimeError('SMB 1219: к этому серверу уже есть подключение с другой учётной записью. Оно не отключалось.')
                raise OSError(code, 'SMB: ' + ctypes.FormatError(code))
            _shares[key] = [0, not existing]
        _shares[key][0] += 1
    try:
        yield
    finally:
        with _lock:
            _shares[key][0] -= 1
            if not _shares[key][0]:
                _, owned = _shares.pop(key)
                if owned:
                    mpr.WNetCancelConnection2W(share, 0, False)


class Files:
    def __init__(self, values, stack):
        self.values = values
        self.remote = values['kind'] == 'linux'
        self.sftp = None
        if self.remote:
            client = stack.enter_context(connect(values))
            self.sftp = stack.enter_context(client.open_sftp())
            self.sftp.get_channel().settimeout(20)
        else:
            stack.enter_context(smb(values))

    def join(self, root, name):
        return posixpath.join(root, name) if self.remote else str(Path(root) / name)

    def info(self, path):
        return self.sftp.lstat(path) if self.remote else os.lstat(path)

    def exists(self, path):
        try:
            self.info(path)
            return True
        except FileNotFoundError:
            return False

    def names(self, path):
        return self.sftp.listdir(path) if self.remote else os.listdir(path)

    def open(self, path, mode):
        return self.sftp.open(path, mode, bufsize=CHUNK) if self.remote else open(path, mode)

    def mkdirs(self, path):
        parent = posixpath.dirname(path) if self.remote else str(Path(path).parent)
        if self.exists(path):
            info = self.info(path)
            if not stat.S_ISDIR(info.st_mode) or is_link(info):
                raise ValueError('Папка получателя содержит ссылку или файл: ' + path)
            if parent and parent != path:
                self.mkdirs(parent)
            return
        if parent and parent != path:
            self.mkdirs(parent)
        if self.remote:
            self.sftp.mkdir(path, mode=0o700)
        else:
            os.mkdir(path)

    def remove(self, path):
        self.sftp.remove(path) if self.remote else os.unlink(path)

    def replace(self, temporary, path, overwrite):
        if self.exists(path):
            if is_link(self.info(path)):
                raise ValueError('Получатель является ссылкой: ' + path)
            if not overwrite:
                raise FileExistsError('Файл появился у получателя во время передачи: ' + path)
        if self.remote:
            if self.exists(path):
                self.sftp.posix_rename(temporary, path)  # OpenSSH atomic replacement; no delete-before-rename fallback.
            else:
                self.sftp.rename(temporary, path)
        else:
            (os.replace if overwrite else os.rename)(temporary, path)


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 1024)


def manifest(fs, path, pattern, cancel):
    if any(character in pattern for character in '/\\\r\n\x00'):
        raise ValueError('Укажите маску имени файла, например *.bak, без пути')
    if is_link(fs.info(path)):
        raise ValueError('Источник является ссылкой. Укажите путь к самим файлам.')
    rows, stack = [], [(path, '')]
    while stack:
        cancelled(cancel)
        current, relative = stack.pop()
        info = fs.info(current)
        if is_link(info):
            continue
        if stat.S_ISDIR(info.st_mode):
            rows.append(dict(relative=relative, path=current, directory=True, size=0))
            for name in sorted(fs.names(current), reverse=True):
                if name in ('.', '..') or '/' in name or '\\' in name:
                    raise ValueError('Недопустимое имя файла: ' + repr(name))
                stack.append((fs.join(current, name), relative + '/' + name if relative else name))
        elif stat.S_ISREG(info.st_mode) and fnmatch.fnmatchcase(
                posixpath.basename(current.replace('\\', '/')) if fs.remote else posixpath.basename(current.replace('\\', '/')).casefold(),
                pattern if fs.remote else pattern.casefold()):
            rows.append(dict(relative=relative or posixpath.basename(current.replace('\\', '/')), path=current,
                             directory=False, size=info.st_size, mtime=info.st_mtime, mode=stat.S_IMODE(info.st_mode)&0o777))
        if len(rows) > 200000:
            raise ValueError('Слишком много записей для одного задания: более 200 000')
    return rows


def scan(source, pattern, event, cancel):
    source = endpoint(source)
    with ExitStack() as stack:
        fs = Files(source, stack)
        rows = manifest(fs, source['path'], pattern or '*', cancel)
    event(dict(kind='message', text=f'Найдено файлов: {sum(not r["directory"] for r in rows)}'))
    return rows


def transfer(source, target, options, event, cancel):
    source, target = validate_pair(source, target)
    engine=options.get('engine','auto')
    if engine=='auto' and source['kind']!='linux' and target['kind']!='linux':
        with ExitStack() as access:
            access.enter_context(smb(source))
            engine='stream' if os.path.isfile(source['path']) else 'robocopy'
    if engine == 'robocopy':
        return robocopy(source, target, options, event, cancel)
    with ExitStack() as stack:
        src, dst = Files(source, stack), Files(target, stack)
        event(dict(kind='stage', text='Чтение списка файлов'))
        rows = manifest(src, source['path'], options.get('pattern') or '*', cancel)
        keys = set()
        if not dst.remote:
            for row in rows:
                key = row['relative'].casefold()
                if key in keys:
                    raise ValueError('Имена Linux совпадают без учёта регистра: ' + row['relative'])
                keys.add(key)
                for part in row['relative'].split('/'):
                    if part and (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.rstrip(' .') != part or
                                 re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
                        raise ValueError('Имя несовместимо с Windows: ' + row['relative'])
        total = sum(row['size'] for row in rows)
        done = copied = skipped = 0
        last = 0.
        for row in rows:
            cancelled(cancel)
            path = target['path']
            for part in row['relative'].split('/'):
                if part:
                    path = dst.join(path, part)
            if row['directory']:
                dst.mkdirs(path)
                continue
            dst.mkdirs(posixpath.dirname(path) if dst.remote else str(Path(path).parent))
            if dst.exists(path) and not options.get('overwrite'):
                skipped += 1
                done += row['size']
                event(dict(kind='message', text='Пропущен существующий файл: ' + row['relative']))
                continue
            temporary = dst.join(posixpath.dirname(path) if dst.remote else str(Path(path).parent), '.ka-transfer-'+uuid4().hex+'.part')
            event(dict(kind='stage', text='Передача: ' + row['relative']))
            digest = hashlib.sha256()
            written = 0
            try:
                with src.open(row['path'], 'rb') as reader, dst.open(temporary, 'xb' if not dst.remote else 'wx') as writer:
                    if dst.remote:
                        dst.sftp.chmod(temporary,0o600)
                        writer.set_pipelined(True)
                    while block := reader.read(CHUNK):
                        cancelled(cancel)
                        writer.write(block)
                        digest.update(block)
                        written += len(block)
                        now = time.monotonic()
                        if now-last > .25:
                            event(dict(kind='progress', percent=min(99, (done+written)/max(1,total)*100), phase='Передача файлов'))
                            last = now
                current = src.info(row['path'])
                if written != row['size'] or current.st_size != row['size'] or current.st_mtime != row['mtime']:
                    raise RuntimeError('Исходный файл изменился во время передачи: ' + row['relative'])
                if dst.info(temporary).st_size != written:
                    raise RuntimeError('Размер принятого файла не совпадает')
                if options.get('verify', True):
                    check = hashlib.sha256()
                    with dst.open(temporary, 'rb') as reader:
                        while block := reader.read(CHUNK):
                            cancelled(cancel)
                            check.update(block)
                    if check.digest() != digest.digest():
                        raise RuntimeError('SHA-256 принятого файла не совпадает')
                cancelled(cancel)
                dst.replace(temporary, path, bool(options.get('overwrite')))
                try:
                    if dst.remote:
                        dst.sftp.utime(path, (row['mtime'], row['mtime']))
                        if src.remote:dst.sftp.chmod(path,row['mode'])
                    else:os.utime(path, (row['mtime'], row['mtime']))
                except OSError:
                    event(dict(kind='warning',text='Файл передан и проверен, но время изменения сохранить не удалось: '+row['relative']))
                copied += 1
                done += written
                event(dict(kind='message', text='Передан: ' + row['relative']))
            finally:
                try:
                    if dst.exists(temporary):
                        dst.remove(temporary)
                except Exception:
                    # A disconnected SFTP channel must not hide the original failure.
                    event(dict(kind='warning',text='Не удалось убрать временный файл получателя: '+temporary))
        event(dict(kind='progress', percent=100, phase='Передача файлов'))
        return dict(copied=copied, skipped=skipped, bytes=done, engine='SFTP / SMB')


def robocopy_args(source, target, options):
    source, target = validate_pair(source, target)
    if source['kind'] == 'linux' or target['kind'] == 'linux':
        raise ValueError('Robocopy работает только между Windows-путями. Для Linux выберите автоматический режим.')
    threads = int(options.get('threads', 8))
    if not 1 <= threads <= 128:
        raise ValueError('Число потоков Robocopy: от 1 до 128')
    pattern = options.get('pattern') or '*'
    if any(c in pattern for c in '/\\\r\n\x00') or pattern.startswith('/'):
        raise ValueError('Укажите маску имени файла, например *.bak')
    args = ['robocopy.exe', source['path'], target['path'], pattern, '/E', '/XJ', '/R:2', '/W:2',
            '/COPY:DAT', '/DCOPY:T', '/Z', '/NP', '/FP', '/BYTES', '/UNICODE', '/MT:'+str(threads)]
    if not options.get('overwrite'):
        args.extend(['/XC', '/XN', '/XO'])
    if options.get('preview'):
        args.append('/L')
    return args


def robocopy(source, target, options, event, cancel):
    args = robocopy_args(source, target, options)
    if os.name != 'nt':
        raise RuntimeError('Robocopy выполняется на Windows-компьютере консоли')
    with ExitStack() as stack:
        stack.enter_context(smb(source)); stack.enter_context(smb(target))
        if not os.path.isdir(source['path']):
            raise ValueError('Для Robocopy источник должен быть папкой. Для одного файла используйте маску или потоковую передачу.')
        event(dict(kind='stage', text='Robocopy · предварительный просмотр' if options.get('preview') else 'Robocopy'))
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        lines = queue.Queue()
        def read():
            decoder = codecs.getincrementaldecoder('utf-16-le')(errors='replace')
            pending = ''
            try:
                while block := process.stdout.read(8192):
                    pending += decoder.decode(block)
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        lines.put(line.strip('\r\ufeff'))
                if pending:
                    lines.put(pending)
            finally:
                lines.put(None)
        thread = threading.Thread(target=read, daemon=True); thread.start()
        try:
            while True:
                if cancel.is_set():
                    process.terminate()
                    raise OperationCancelled('Robocopy прерван. Часть файлов могла быть уже скопирована; источник не удалялся.')
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line:
                    event(dict(kind='message', text=line))
            code = process.wait()
            if code < 0 or code >= 8:
                raise RuntimeError('Robocopy: код ' + str(code) + '. Некоторые файлы не скопированы; подробности в журнале.')
            event(dict(kind='message', text='Robocopy завершён, код '+str(code)+' (0–7 — без ошибок копирования).'))
            event(dict(kind='progress', percent=100, phase='Robocopy'))
            return dict(engine='Robocopy', code=code, preview=bool(options.get('preview')))
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=10)
            thread.join(2)
            process.stdout.close()
