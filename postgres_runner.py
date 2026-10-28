"""PostgreSQL jobs; stdlib-only so the same runner can execute over SSH.

The remote runner is sent in memory and receives credentials through stdin.
It creates only requested backup files, not scripts or credential files.
"""
import gzip
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from collections import deque
from pathlib import Path
from uuid import uuid4


MODES = ('pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore', 'ibcmd_probe', 'ibcmd_export', 'list', 'sizes', 'create', 'delete', 'copy')


class OperationCancelled(RuntimeError):
    pass


def stop_process(process):
    tree = getattr(process, '_console_tree', None)
    if tree:
        tree.close()
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class ProcessTree:
    """Own only this operation's parallel workers, on Windows and Linux."""
    def __init__(self, process):
        self.process, self.handle = process, None
        self.lock = threading.Lock()
        self.closed = False
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Basic(ctypes.Structure):
                _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                            ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                            ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                            ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
            class Extended(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                            ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                            ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.CreateJobObjectW(None, None)
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or
                    not kernel.AssignProcessToJobObject(handle, int(process._handle))):
                error = ctypes.get_last_error()
                kernel.CloseHandle(handle)
                stop_process(process)
                raise ctypes.WinError(error)
            self.kernel, self.handle = kernel, handle
        process._console_tree = self

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.handle is not None:
                self.kernel.CloseHandle(self.handle)
            elif os.name != 'nt':
                import signal
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class JobProgress:
    """Report a measured unit of a phase, never a timer-generated percentage."""
    def __init__(self, runner, phase, unit, estimated=False):
        self.runner, self.phase, self.unit, self.estimated = runner, phase, unit, estimated
        self.last_time, self.last_percent = 0, -1

    def report(self, percent, force=False):
        now = time.monotonic()
        if force or now - self.last_time >= 1 or percent - self.last_percent >= 1:
            self.last_time, self.last_percent = now, percent
            self.runner.event(dict(kind='progress', text='', phase=redact(self.phase, self.runner.c),
                                   percent=percent, unit=self.unit, estimated=self.estimated))

    def line(self, text):
        pass

    def feed(self):
        pass

    def finish(self):
        self.report(100, force=True)


class ByteProgress(JobProgress):
    def __init__(self, runner, phase, unit, size, position):
        super().__init__(runner, phase, unit)
        self.size, self.position = size, position
        self.report(0, force=True)

    def feed(self):
        if self.size > 0:
            self.report(min(99.9, 100 * self.position() / self.size))


class VerboseProgress(JobProgress):
    def __init__(self, runner, phase, total, table_names=None, generated=False, workers=1):
        unit = 'таблиц' if table_names is not None else 'объектов SQL' if generated else 'объектов'
        super().__init__(runner, phase, unit, estimated=True)
        self.total, self.table_names, self.seen = total, table_names, set()
        self.workers = workers

    def line(self, text):
        if self.total <= 0:
            return
        if self.table_names is not None:
            match = re.search(r'(?:dumping contents of table|выгрузка содержимого таблицы)\s+"(.*)"\s*$', text, re.I)
            if not match or match[1] not in self.table_names:
                return
            key = match[1]
        else:
            match = re.match(r'pg_restore:\s*(?:creating|созда[её]тся|processing data for table|'
                             r'обрабатываются данные таблицы|executing|выполняется)\s+(.+)$', text, re.I)
            if not match:
                return
            key = match[0]
        if key not in self.seen:
            self.seen.add(key)
            # Verbose messages announce the START of an object. The preceding
            # one is finished; the last one counts only on successful exit.
            self.report(min(99.9, 100 * max(0, len(self.seen) - self.workers) / self.total))

    def finish(self):
        if self.total > 0 and self.seen:
            super().finish()


class CompatibleSqlStream:
    """Stream a newer reader's SQL, adjusting only its fixed session prologue.

    stdout is binary SQL; stderr is the log. Both process status and cancellation
    are checked before the consumer is allowed to see end-of-input and commit.
    """
    def __init__(self, runner, args, progress=None):
        self.runner = runner
        self.progress = progress
        runner.check_cancel()
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=runner.env,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.stopped = threading.Event()
        self.errors = []
        self.finished = False
        self.prefix = b''
        self.log_thread = threading.Thread(target=self.log, daemon=True)
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.log_thread.start()
        self.watcher.start()
        try:
            # Validate and adapt the bounded prologue BEFORE changing the database.
            lines, length = [], 0
            while length < 65536:
                line = self.process.stdout.readline(65536 - length + 1)
                runner.check_cancel()
                if not line:
                    self.check_exit()
                    break
                lines.append(line)
                length += len(line)
                if line.rstrip(b'\r\n') in (b'SET row_security = off;', b'SET row_security = on;'):
                    break
            stripped = [line.rstrip(b'\r\n') for line in lines]
            if (length > 65536 or not stripped or
                    stripped[-1] not in (b'SET row_security = off;', b'SET row_security = on;') or
                    stripped.count(b'SET statement_timeout = 0;') != 1 or
                    stripped.count(b'SET transaction_timeout = 0;') != 1):
                raise RuntimeError('Не распознан служебный заголовок SQL pg_restore 17. Целевая база не изменена.')
            self.prefix = b''.join(line for line in lines if line.rstrip(b'\r\n') != b'SET transaction_timeout = 0;')
        except Exception:
            self.close()
            raise

    def log(self):
        for line in self.process.stderr:
            text = decode_output(line).rstrip('\r\n')
            self.errors.append(text)
            self.errors = self.errors[-20:]
            self.runner.emit(text)
            if self.progress:
                self.progress.line(text)

    def watch(self):
        while not self.stopped.wait(0.1):
            if self.runner.cancel_event.is_set():
                self.abort()
                return

    def check_exit(self):
        code = self.process.wait()
        self.log_thread.join()
        self.runner.check_cancel()
        if code:
            raise RuntimeError('pg_restore: код ' + str(code) + '. ' +
                               redact('\n'.join(self.errors)[-3000:], self.runner.c))
        self.finished = True

    def read(self, size):
        self.runner.check_cancel()
        if self.prefix:
            chunk, self.prefix = self.prefix[:size], self.prefix[size:]
            return chunk
        if self.finished:
            return b''
        chunk = self.process.stdout.read1(size)
        if not chunk:
            self.check_exit()
        return chunk

    def abort(self):
        stop_process(self.process)

    def close(self):
        self.stopped.set()
        self.abort()
        self.log_thread.join(timeout=4)
        self.watcher.join(timeout=4)
        self.process.stdout.close()
        self.process.stderr.close()


def literal(value):
    # Explicit escape strings remain safe even if the server's standard_conforming_strings is off.
    return "E'" + value.replace('\\', '\\\\').replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def database_name(value):
    if not isinstance(value, str) or not value or '\x00' in value or len(value.encode('utf-8')) > 63:
        raise ValueError('Укажите имя PostgreSQL-базы длиной до 63 байт без NUL')
    return value


def create_database_sql(database, owner=''):
    sql = 'CREATE DATABASE ' + identifier(database_name(database)) + " WITH TEMPLATE template0 ENCODING 'UTF8'"
    if owner:
        sql += ' OWNER ' + identifier(database_name(owner))
    return sql + ';'


def redact(text, config):
    secrets = {config.get('password', ''), config.get('ib_password', ''), config.get('ssh_password', '')}
    target = config.get('target_connection')
    if isinstance(target, dict):
        secrets.update((target.get('password', ''), target.get('ib_password', '')))
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, '••••')
    return text


def executable(value):
    if not value or '\x00' in value or value.startswith('-') or value.lower().endswith(('.bat', '.cmd')):
        raise ValueError('Укажите исполняемый файл утилиты, а не командный скрипт')
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError('Не найдена утилита: ' + value)
    return resolved


def decode_output(data, fallback_encoding=None):
    """Keep UTF-8 intact; legacy Windows tools may still emit the ANSI code page."""
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        if fallback_encoding is None:
            if os.name == 'nt':
                import ctypes
                fallback_encoding = 'cp' + str(ctypes.windll.kernel32.GetACP())
            else:
                fallback_encoding = locale.getpreferredencoding(False)
        return data.decode(fallback_encoding, errors='replace')


def ibcmd_output_encoding():
    # Native Windows 1C console messages use the OEM page, unlike libpq clients.
    if os.name == 'nt':
        import ctypes
        return 'cp' + str(ctypes.windll.kernel32.GetOEMCP())
    return 'utf-8'


class Runner:
    def __init__(self, config, event, cancel_event=None, validate_connection=True):
        self.c, self.event = config, event
        self.cancel_event = cancel_event or threading.Event()
        if validate_connection and (not config.get('host') or not config.get('username')):
            raise ValueError('Укажите сервер и пользователя PostgreSQL')
        port = int(config.get('port', 5432)) if validate_connection else 5432
        if not 1 <= port <= 65535:
            raise ValueError('Порт PostgreSQL должен быть от 1 до 65535')
        if validate_connection and config.get('sslmode', 'prefer') not in ('disable', 'prefer', 'require', 'verify-ca', 'verify-full'):
            raise ValueError('Некорректный режим SSL')
        self.env = os.environ.copy()
        for key in ('PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS', 'PGDATABASE', 'PGHOSTADDR'):
            self.env.pop(key, None)
        self.env.update(PGHOST=config.get('host', 'localhost'), PGPORT=str(port), PGUSER=config.get('username', 'postgres'),
                        PGPASSWORD=config.get('password', ''), PGSSLMODE=config.get('sslmode', 'prefer'),
                        PGCONNECT_TIMEOUT='10', PGCLIENTENCODING='UTF8', OUTPUT_CHARSET='UTF-8')

    def emit(self, text, kind='message'):
        self.event({'kind': kind, 'text': redact(str(text), self.c)})

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise OperationCancelled('Операция прервана пользователем')

    def create_database(self, database, owner=''):
        sql = create_database_sql(database, owner)
        self.emit('Создание базы ' + database, 'stage')
        self.sql(sql)
        self.emit('База создана: ' + database)
        return database

    def delete_database(self, database):
        self.target(database)  # Validate existence and reject system/template DBs.
        self.emit('Удаление базы ' + database, 'stage')
        try:
            self.sql('ALTER DATABASE ' + identifier(database) + ' ALLOW_CONNECTIONS false;')
            self.sql('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=' +
                     literal(database) + ' AND pid<>pg_backend_pid();')
            self.sql('DROP DATABASE ' + identifier(database) + ';')
        except Exception:
            try:
                exists = self.sql('SELECT 1 FROM pg_database WHERE datname=' + literal(database) + ';', ignore_cancel=True)
                if exists.strip() == '1':
                    self.sql('ALTER DATABASE ' + identifier(database) + ' ALLOW_CONNECTIONS true;', ignore_cancel=True)
            except Exception as exc:
                self.emit('Не удалось вернуть подключения: ' + str(exc))
            raise
        self.emit('База удалена: ' + database)
        return database

    def tool(self, name):
        if name == 'ibcmd' and os.name == 'nt' and self.c.get(name, name).lower() in ('ibcmd', 'ibcmd.exe'):
            found = []
            for key in ('ProgramFiles', 'ProgramFiles(x86)'):
                if os.environ.get(key):
                    found.extend((Path(os.environ[key]) / '1cv8').glob('*/bin/ibcmd.exe'))
            if found:
                found.sort(key=lambda path: tuple(int(part) for part in path.parent.parent.name.split('.') if part.isdigit()), reverse=True)
                return str(found[0])
        return executable(self.c.get(name, name))

    def run(self, args, database='postgres', capture=False, source=None, ignore_cancel=False, progress=None, own_tree=False, timeout=None, fallback_encoding=None):
        if not ignore_cancel:
            self.check_cancel()
        env = dict(self.env, PGDATABASE=database)
        process = subprocess.Popen(args, stdin=subprocess.PIPE if source is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                   start_new_session=own_tree and os.name != 'nt',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            tree = ProcessTree(process) if own_tree else None
        except Exception:
            stop_process(process)
            process.stdout.close()
            raise
        if progress and progress.last_percent >= 0:
            progress.report(progress.last_percent, force=True)
        stopped = threading.Event()
        interrupted = threading.Event()
        timed_out = threading.Event()
        deadline = time.monotonic() + timeout if timeout is not None else None
        def watch_cancel():
            while not stopped.wait(0.1):
                if not ignore_cancel and self.cancel_event.is_set():
                    interrupted.set()
                    stop_process(process)
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out.set()
                    stop_process(process)
                    return
                if tree and process.poll() is not None:
                    tree.close()
                    return
        watcher = threading.Thread(target=watch_cancel, daemon=True)
        watcher.start()
        write_errors = []

        def feed():
            try:
                while chunk := source.read(1024 * 1024):
                    process.stdin.write(chunk)
                    if progress:
                        progress.feed()
            except BrokenPipeError:
                pass  # The consumer's exit code supplies the actual error.
            except Exception as exc:
                write_errors.append(exc)
                # A failed producer must not turn into normal EOF and commit a
                # partial restore. Terminating psql rolls back its transaction.
                stop_process(process)
            finally:
                try:
                    process.stdin.close()
                except OSError as exc:
                    if process.poll() is None:
                        write_errors.append(exc)
                        stop_process(process)

        writer = None
        if source is not None:
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        rows = []
        tail = deque(maxlen=20)
        try:
            for line in process.stdout:
                text = decode_output(line, fallback_encoding).rstrip('\r\n')
                if capture:
                    rows.append(text)
                else:
                    tail.append(text[-1500:])
                    self.emit(text)
                    if progress:
                        progress.line(text)
            code = process.wait()
            if writer:
                if writer.is_alive() and hasattr(source, 'abort'):
                    source.abort()
                writer.join()
            if interrupted.is_set():
                raise OperationCancelled('Операция прервана пользователем')
            if timed_out.is_set():
                raise RuntimeError(f'{Path(args[0]).name}: проверка не завершилась за {timeout:g} с')
            if write_errors:
                if isinstance(write_errors[0], OperationCancelled):
                    raise write_errors[0]
                raise RuntimeError('Не удалось прочитать файл восстановления: ' + str(write_errors[0]))
            if code:
                detail = '\n'.join(rows if capture else tail)[-3000:] or 'Подробности выше в журнале'
                raise RuntimeError(f'{Path(args[0]).name}: код {code}. {redact(detail, self.c)}')
            if progress:
                progress.finish()
            return '\n'.join(rows)
        finally:
            stopped.set()
            if tree:
                tree.close()
            if process.poll() is None:
                stop_process(process)
            watcher.join(timeout=4)
            process.stdout.close()

    def sql(self, sql, database='postgres', ignore_cancel=False):
        return self.run([self.tool('psql'), '-X', '-w', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-c', sql],
                        database=database, capture=True, ignore_cancel=ignore_cancel)

    def databases(self, fast=False):
        self.emit('Подключение к PostgreSQL и чтение списка баз', 'stage')
        size = 'NULL::bigint AS size_bytes, true AS sizes_pending, ' if fast else 'pg_database_size(oid) AS size_bytes, '
        result = self.sql("SELECT COALESCE(json_agg(t ORDER BY name), '[]'::json) FROM "
                          "(SELECT oid AS database_oid, datname AS name, " + size +
                          "pg_get_userbyid(datdba) AS owner, current_setting('server_version_num')::integer AS server_version FROM pg_database "
                          "WHERE NOT datistemplate AND datallowconn AND has_database_privilege(oid,'CONNECT')) t;")
        return json.loads(result)

    def database_sizes(self, names):
        names = list(dict.fromkeys(database_name(name) for name in names))
        self.env['PGOPTIONS'] = '-c statement_timeout=5000 -c lock_timeout=1000'
        deadline = time.monotonic() + 30
        errors = 0
        processed = 0
        for index in range(0, len(names), 8):
            self.check_cancel()
            if time.monotonic() >= deadline:
                break
            batch = names[index:index + 8]
            sql = ("SELECT COALESCE(json_agg(t), '[]'::json) FROM "
                   "(SELECT oid AS database_oid, datname AS name, pg_database_size(oid) AS size_bytes "
                   "FROM pg_database WHERE datname IN (" + ','.join(map(literal, batch)) +
                   ") AND has_database_privilege(oid,'CONNECT')) t;")
            try:
                rows = json.loads(self.sql(sql))
                self.event(dict(kind='database_sizes', rows=rows))
            except OperationCancelled:
                raise
            except (RuntimeError, ValueError):
                errors += len(batch)
            processed += len(batch)
        return dict(unavailable=errors + len(names) - processed)

    def target(self, database):
        database_name(database)
        if database in ('postgres', 'template0', 'template1'):
            raise ValueError('Восстановление служебной базы PostgreSQL запрещено')
        rows = json.loads(self.sql("SELECT COALESCE(json_agg(t), '[]'::json) FROM "
            "(SELECT datname, pg_get_userbyid(datdba) AS owner, pg_encoding_to_char(encoding) AS encoding, "
            "datcollate, datctype, datconnlimit, datallowconn, current_setting('server_version_num')::integer AS server_version, "
            "(SELECT spcname FROM pg_tablespace WHERE oid=d.dattablespace) AS tablespace, "
            "to_jsonb(d)->>'datlocprovider' AS locale_provider, "
            "COALESCE(to_jsonb(d)->>'datlocale', to_jsonb(d)->>'daticulocale') AS locale, "
            "to_jsonb(d)->>'daticurules' AS icu_rules FROM pg_database d WHERE datname=" + literal(database) +
            " AND NOT datistemplate) t;"))
        if len(rows) != 1 or not rows[0]['datallowconn']:
            raise ValueError('Целевая база не найдена или подключения к ней запрещены')
        return rows[0]

    def copy_identity(self, database):
        """Read the actual server/database identity to catch host aliases."""
        row = json.loads(self.sql("SELECT json_build_object('database', current_database(), "
            "'oid', (SELECT oid FROM pg_database WHERE datname=current_database()), "
            "'address', inet_server_addr()::text, 'port', inet_server_port(), "
            "'started', extract(epoch FROM pg_postmaster_start_time())::text, 'version', current_setting('server_version_num')::integer);", database))
        if (not isinstance(row, dict) or row.get('database') != database or not row.get('oid') or
                not row.get('started') or not row.get('version') or not row.get('address') or not row.get('port')):
            raise ValueError('Не удалось проверить фактический сервер и базу для бэкапа + восстановления')
        return row

    def backup_and_restore(self, source, destination, folder):
        """Use one executor and a retained archive for two independent PG hosts."""
        database_name(source)
        database_name(destination)
        settings = self.c.get('target_connection')
        if not isinstance(settings, dict):
            raise ValueError('Укажите подключение к целевому серверу PostgreSQL')
        # Executables, archive and SSH executor are shared; DB credentials are not.
        target_config = dict(self.c)
        target_config.pop('target_connection', None)
        for key in ('host', 'port', 'username', 'password', 'sslmode'):
            target_config[key] = settings.get(key, '' if key == 'password' else None)
        def safe_event(value):
            if 'text' in value:
                value = dict(value, text=redact(str(value['text']), self.c))
            self.event(value)
        target = Runner(target_config, safe_event, self.cancel_event)
        # Fail closed for identical names on the same explicit endpoint. The
        # actual backend identity below also catches localhost/DNS aliases.
        if (source == destination and self.c['host'].casefold() == target.c['host'].casefold() and
                int(self.env['PGPORT']) == int(target.env['PGPORT'])):
            raise ValueError('Источник и получатель — одна база. Выберите другую целевую базу')
        self.emit('Проверка источника и получателя до бэкапа', 'stage')
        source_identity = self.copy_identity(source)
        target_identity = target.copy_identity(destination)
        target.target(destination)  # Protect system/template/unavailable targets before backup.
        if all(source_identity[key] == target_identity[key] for key in ('database', 'oid', 'port', 'started', 'version')):
            raise ValueError('Источник и получатель ведут к одной базе через разные адреса')
        source_major, target_major = int(source_identity['version']) // 10000, int(target_identity['version']) // 10000
        if source_major > target_major:
            raise ValueError(f'Источник PostgreSQL {source_major} новее получателя {target_major}. Базы не изменены')
        # Validate incompatible options before spending time creating an archive.
        jobs = target.workers('restore_jobs')
        fmt = self.c.get('format', 'dump')
        if jobs > 1 and fmt == 'sql.gz':
            raise ValueError('SQL gzip восстанавливается в 1 поток. Выберите .dump / каталог для параллельности')
        target.tool('psql')
        target.tool('pg_restore')
        self.emit('1/2 · Бэкап источника ' + source, 'stage')
        # Retention (if explicitly enabled) excludes this newly created archive.
        created = self.backup([source], folder)
        path = created[0]
        self.emit('Бэкап источника сохранён: ' + path)
        try:
            self.check_cancel()
            if self.copy_identity(source) != source_identity or target.copy_identity(destination) != target_identity:
                raise ValueError('Источник или целевая база изменились во время бэкапа. Восстановление не запускалось')
            self.emit('2/2 · Восстановление в ' + target.c['host'] + ' / ' + destination, 'stage')
            target.restore(destination, path)
        except Exception as exc:
            cls = OperationCancelled if isinstance(exc, OperationCancelled) else RuntimeError
            raise cls('Бэкап сохранён: ' + path + '\nВосстановление не завершено: ' + redact(str(exc), self.c)) from exc
        return dict(backup_path=path, source=source, destination=destination)

    def create_sql(self, database, target):
        sql = ('CREATE DATABASE ' + identifier(database) + ' WITH TEMPLATE template0 OWNER ' +
               identifier(target['owner']) + ' ENCODING ' + literal(target['encoding']) +
               ' LC_COLLATE ' + literal(target['datcollate']) + ' LC_CTYPE ' + literal(target['datctype']) +
               ' CONNECTION LIMIT ' + str(int(target['datconnlimit'])))
        if target.get('tablespace'):
            sql += ' TABLESPACE ' + identifier(target['tablespace'])
        provider = target.get('locale_provider')
        if provider:
            providers = {'c': 'libc', 'i': 'icu', 'b': 'builtin'}
            if provider not in providers:
                raise ValueError('Неизвестный провайдер локали целевой базы')
            sql += ' LOCALE_PROVIDER ' + providers[provider]
            if provider in ('i', 'b'):
                if not target.get('locale'):
                    raise ValueError('Не удалось прочитать локаль целевой базы')
                sql += (' ICU_LOCALE ' if provider == 'i' else ' BUILTIN_LOCALE ') + literal(target['locale'])
            if provider == 'i' and target.get('icu_rules'):
                sql += ' ICU_RULES ' + literal(target['icu_rules'])
        return sql + ';'

    def ibcmd_args(self, database, restore=False):
        data = self.c.get('data_path', '')
        if not os.path.isabs(data):
            raise ValueError('Укажите абсолютный путь --data на компьютере выполнения ibcmd')
        args = [self.tool('ibcmd'), 'infobase', 'restore' if restore else 'dump', '--dbms=PostgreSQL',
                '--database-server=' + self.c['host'] + ' port=' + str(self.env['PGPORT']),
                '--database-user=' + self.c['username'], '--database-password=' + self.c.get('password', ''),
                '--database-name=' + database, '--data=' + data]
        if self.c.get('ib_user'):
            args += ['--user=' + self.c['ib_user'], '--password=' + self.c.get('ib_password', '')]
        if restore:
            args.append('--force')
        return args

    def inspect_ibcmd(self):
        import platform
        import socket
        tool = self.tool('ibcmd')
        self.emit('Проверка ibcmd на компьютере выполнения: ' + socket.gethostname(), 'stage')
        version = self.run([tool, '--version'], capture=True, timeout=15, fallback_encoding=ibcmd_output_encoding()).strip()
        if not version:
            raise ValueError('ibcmd не вернул версию. Проверьте путь к утилите на компьютере выполнения')
        return dict(computer=socket.gethostname(), system=platform.system(), ibcmd=tool,
                    version=redact(version, self.c), python=platform.python_version())

    def workers(self, key):
        value = self.c.get(key, 1)
        if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 16:
            raise ValueError('Число потоков должно быть целым числом от 1 до 16')
        return int(value)

    def export_dt(self, databases):
        """A private remote staging area; the caller downloads and removes the verified DT."""
        if len(databases) != 1:
            raise ValueError('Для DT выберите одну базу')
        root = Path(tempfile.mkdtemp(prefix='ka-ibcmd-'))
        try:
            files = self.backup(databases, str(root), ibcmd=True)
            target = Path(files[0])
            self.emit('Проверка DT перед скачиванием в консоль', 'stage')
            digest = hashlib.sha256()
            with target.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    self.check_cancel()
                    digest.update(chunk)
            self.check_cancel()
            return dict(workspace=str(root), path=str(target), size=target.stat().st_size, sha256=digest.hexdigest())
        except BaseException:
            self.remove_archive_directory(root, root.parent)
            raise

    def remove_archive_directory(self, path, parent):
        # Check the final absolute target before recursive deletion. Never follow
        # symlinks/junctions, including those inside an archive directory.
        path, parent = Path(path), Path(parent).resolve()
        if (path.is_symlink() or getattr(path, 'is_junction', lambda: False)() or path.resolve().parent != parent or
                not path.is_dir()):
            raise ValueError('Отказ от очистки каталога за пределами папки бэкапов')
        archive_root = path.resolve()
        for root, folders, files in os.walk(path, followlinks=False):
            for name in folders + files:
                item = Path(root) / name
                if (item.is_symlink() or getattr(item, 'is_junction', lambda: False)() or
                        archive_root not in item.resolve().parents):
                    raise ValueError('Очистка каталога со ссылками не выполняется')
        shutil.rmtree(path)

    def report_artifact(self, path):
        self.event(dict(kind='artifact', text='', path=str(path), size=None))
        try:
            path = Path(path)
            if path.is_dir():
                size = 0
                for directory, folders, files in os.walk(path, followlinks=False):
                    folders[:] = [name for name in folders if not os.path.islink(os.path.join(directory, name))]
                    for name in files:
                        entry = os.path.join(directory, name)
                        if not os.path.islink(entry):
                            size += os.stat(entry, follow_symlinks=False).st_size
            else:
                size = path.stat().st_size
            self.event(dict(kind='artifact', text='', path=str(path), size=size))
        except OSError:
            pass  # A metadata failure must not invalidate a completed backup.

    def backup(self, databases, folder, ibcmd=False):
        if ibcmd and len(databases) != 1:
            raise ValueError('Для DT выберите одну базу: каталог --data относится к одной информационной базе')
        if not os.path.isabs(folder):
            raise ValueError('Укажите абсолютную папку бэкапов на компьютере выполнения')
        days = int(self.c.get('retention_days', 0))
        if days < 0:
            raise ValueError('Срок хранения не может быть отрицательным')
        fmt = self.c.get('format', 'dump')
        jobs = 1 if ibcmd else self.workers('dump_jobs')
        compression = str(self.c.get('compression', 6))
        if not ibcmd:
            if fmt not in ('dump', 'sql.gz', 'directory'):
                raise ValueError('Неизвестный формат pg_dump')
            if not compression.isdigit() or not 0 <= int(compression) <= 9:
                raise ValueError('Сжатие должно быть от 0 до 9')
            if fmt == 'sql.gz' and int(compression) == 0:
                raise ValueError('Для SQL gzip выберите сжатие от 1 до 9; без сжатия используйте .dump или каталог')
            if jobs > 1 and fmt != 'directory':
                raise ValueError('Параллельный pg_dump требует формат «Каталог .pgdir». Для .dump / SQL выберите 1 поток')
        # Check all executables and names before making any backup.
        databases = [database_name(name) for name in databases]
        for database in databases:
            self.ibcmd_args(database) if ibcmd else self.tool('pg_dump')
        if not ibcmd and self.c.get('format') != 'sql.gz':
            self.tool('pg_restore')
        created = []
        for database in databases:
            self.check_cancel()
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', database).rstrip('. ') or 'database'
            # One subdirectory per database; no traversal or Windows device names.
            reserved = re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', safe.split('.')[0], re.I)
            if safe != database or len(safe) > 60 or reserved:
                safe = safe[:60] + '_' + hashlib.sha256(database.encode()).hexdigest()[:8]
            directory = Path(folder) / safe
            directory.mkdir(parents=True, exist_ok=True)
            suffix = '.dt' if ibcmd else {'sql.gz': '.sql.gz', 'directory': '.pgdir', 'dump': '.dump'}[fmt]
            name = safe + '_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid4().hex[:8] + suffix
            target = directory / name
            container = None
            if suffix == '.pgdir':
                container = Path(tempfile.mkdtemp(prefix='.console_', suffix='.part', dir=directory))
                stage = str(container / 'archive')  # pg_dump must create this directory itself.
            else:
                fd, stage = tempfile.mkstemp(prefix='.console_', suffix='.dt' if ibcmd else '.part', dir=directory)
                os.close(fd)
            self.emit('Выгрузка ' + database + ' → ' + str(target), 'stage')
            try:
                if ibcmd:
                    self.run(self.ibcmd_args(database) + [stage], database, fallback_encoding=ibcmd_output_encoding())
                else:
                    progress = self.dump_progress(database, jobs)
                    args = [self.tool('pg_dump'), '-w', '--verbose', '--file=' + stage,
                            '--format=' + {'sql.gz': 'plain', 'directory': 'directory', 'dump': 'custom'}[fmt], '-Z', compression]
                    if jobs > 1:
                        args.append('--jobs=' + str(jobs))
                    self.emit(f'Потоки pg_dump: {jobs} · сжатие: {compression}')
                    self.run(args, database, progress=progress, own_tree=jobs > 1)
                contents = Path(stage) / 'toc.dat' if suffix == '.pgdir' else Path(stage)
                if not contents.is_file() or not contents.stat().st_size:
                    raise RuntimeError('Утилита создала пустой файл')
                if suffix in ('.dump', '.pgdir'):
                    self.emit('Проверка оглавления бэкапа ' + database, 'stage')
                    self.run([self.tool('pg_restore'), '--list', stage], capture=True)
                elif suffix == '.sql.gz':
                    self.check_gzip(stage)
                self.check_cancel()
                os.replace(stage, target)
                created.append(str(target))
                self.report_artifact(target)
                self.emit('Готово: ' + str(target))
                if days:
                    self.cleanup(directory, safe, suffix, days, target)
            finally:
                if container is not None:
                    self.remove_archive_directory(container, directory)
                elif os.path.exists(stage):
                    os.unlink(stage)
        return created

    def dump_progress(self, database, jobs=1):
        try:
            names = json.loads(self.sql("SELECT COALESCE(json_agg(n.nspname||'.'||c.relname), '[]'::json) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind='r' AND c.relpersistence<>'t' AND n.nspname !~ '^pg_' "
                "AND n.nspname<>'information_schema' "
                "AND (c.relpersistence<>'u' OR NOT pg_is_in_recovery()) "
                "AND (NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass "
                "AND d.objid=c.oid AND d.deptype='e') OR EXISTS "
                "(SELECT 1 FROM pg_extension e WHERE c.oid=ANY(e.extconfig)));", database))
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError('Нет списка таблиц')
        except OperationCancelled:
            raise
        except (RuntimeError, ValueError, OSError):
            names = []  # Telemetry must not make an otherwise valid backup fail.
        return VerboseProgress(self, 'Экспорт данных · ' + database, len(names), set(names), workers=jobs)

    def cleanup(self, directory, safe, suffix, days, current):
        pattern = re.compile(re.escape(safe) + r'_\d{8}_\d{6}_\d{6}_[a-f0-9]{8}' + re.escape(suffix))
        cutoff = time.time() - days * 86400
        for path in directory.iterdir():
            self.check_cancel()
            if (path != current and pattern.fullmatch(path.name) and not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)() and
                    (path.is_file() or suffix == '.pgdir' and path.is_dir() and (path / 'toc.dat').is_file())):
                if path.stat().st_mtime < cutoff:
                    try:
                        if path.is_dir():
                            self.remove_archive_directory(path, directory)
                        else:
                            path.unlink()
                        self.emit('Удалён старый бэкап: ' + str(path))
                    except (OSError, ValueError) as exc:
                        self.emit('Бэкап создан, но очистка не выполнена: ' + str(exc))

    def check_gzip(self, path):
        self.emit('Проверка целостности gzip', 'stage')
        with gzip.open(path, 'rb') as stream:
            progress = ByteProgress(self, 'Проверка gzip', 'файла', Path(path).stat().st_size, stream.fileobj.tell)
            while stream.read(1024 * 1024):
                self.check_cancel()
                progress.feed()
        progress.finish()

    def archive_reader(self, backup):
        reader = self.tool('pg_restore')
        candidates = [reader] + [executable(path) for path in self.c.get('restore_candidates', [])]
        candidates = list(dict.fromkeys(candidates))
        for index, candidate in enumerate(candidates):
            try:
                toc = self.run([candidate, '--list', str(backup)], capture=True)
                reader = candidate
                self._archive_toc = toc
                break
            except RuntimeError as exc:
                # Only an unsupported archive version permits automatic fallback.
                if not re.search(r'(unsupported version|неподдерживаемая версия)\s*\(', str(exc), re.I):
                    raise
                if index == len(candidates) - 1:
                    raise RuntimeError(str(exc) + '\nВыберите более новый pg_restore или комплект «Автоматически». '
                                       'Целевая база не изменена.') from exc
        version = self.run([reader, '--version'], capture=True)
        match = re.search(r'\(PostgreSQL\)\s+(\d+)', version)
        if not match:
            raise RuntimeError('Не удалось определить версию pg_restore. Целевая база не изменена.')
        reader_major = int(match[1])
        source = re.search(r'^;\s+Dumped from database version:\s*(\d+)', toc, re.M)
        self.emit('Читатель архива: ' + version.strip() + ' · ' + reader)
        if source:
            self.emit('Версия исходного сервера: ' + source[1])
        return reader, reader_major, int(source[1]) if source else None

    def restore(self, database, path, ibcmd=False):
        jobs = 1 if ibcmd else self.workers('restore_jobs')
        target = self.target(database)
        backup = Path(path)
        directory_archive = backup.is_dir() and (backup / 'toc.dat').is_file() and (backup / 'toc.dat').stat().st_size > 0
        if (not backup.is_absolute() or
                not (directory_archive and not ibcmd or backup.is_file() and backup.stat().st_size > 0)):
            raise ValueError('Укажите существующий непустой файл или каталог pg_dump с toc.dat на компьютере выполнения')
        self.report_artifact(backup)
        if ibcmd:
            if backup.suffix.lower() != '.dt':
                raise ValueError('Для ibcmd выберите файл .dt')
            args = self.ibcmd_args(database, restore=True)
            self.emit('Восстановление DT · ' + database + ' ← ' + str(backup), 'stage')
            self.sql('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=' +
                     literal(database) + ' AND pid<>pg_backend_pid();')
            try:
                self.run(args + [str(backup)], database, fallback_encoding=ibcmd_output_encoding())
            except OperationCancelled as exc:
                raise OperationCancelled('Загрузка DT прервана. Прежние данные не возвращаются; '
                                         'возможно, потребуется повторное восстановление из DT.') from exc
            return database
        plain = not directory_archive and str(backup).lower().endswith(('.sql', '.sql.gz'))
        if plain and jobs > 1:
            raise ValueError('SQL / SQL.gz восстанавливается в 1 поток. Для параллельного восстановления нужен .dump или каталог. База не изменена')
        restore = None
        compatible = False
        if plain and str(backup).lower().endswith('.sql.gz'):
            self.check_gzip(backup)
        elif not plain:
            self.emit('Проверка оглавления архива pg_dump', 'stage')
            restore, reader_major, source_major = self.archive_reader(backup)
            target_major = int(target.get('server_version', 0)) // 10000
            if target_major and source_major and source_major > target_major:
                raise ValueError(f'Исходный сервер PostgreSQL {source_major} новее целевого {target_major}. '
                                 'Восстановление в старшую версию поддерживается, обратный перенос не выполняется. '
                                 'Целевая база не изменена.')
            if target_major and reader_major > target_major:
                if not source_major:
                    raise ValueError('В архиве не определена версия исходного сервера. '
                                     'Совместимость с целевым сервером не подтверждена; база не изменена.')
                if reader_major >= 17:
                    if reader_major != 17 or target_major < 14:
                        raise ValueError('Этот более новый pg_restore не поддерживается для данного целевого сервера. '
                                         'Целевая база не изменена.')
                    compatible = True
        if jobs > 1:
            if compatible:
                raise ValueError('Для этого архива требуется совместимый поток SQL, который не поддерживает параллельность. '
                                 'Выберите 1 поток или сделайте новый бэкап утилитой версии сервера. База не изменена')
            if not re.search(r'^;\s+Format:\s*(CUSTOM|DIRECTORY)\s*$', self._archive_toc, re.M | re.I):
                raise ValueError('Параллельное восстановление поддерживает только архив CUSTOM / DIRECTORY. База не изменена')
        # Both commands must be present before disconnecting users or dropping a DB.
        psql = self.tool('psql')
        create_sql = self.create_sql(database, target)
        archive_progress = None if plain else VerboseProgress(self, 'Восстановление · ' + database,
            len(re.findall(r'^\d+;', self._archive_toc, re.M)), generated=compatible, workers=jobs)
        stream = None
        dropped = False
        access_changed = False
        try:
            if compatible:
                stream = CompatibleSqlStream(self, [restore, '--verbose', '--exit-on-error', '--file=-', str(backup)],
                                             progress=archive_progress)
                self.emit('Совместимость с PostgreSQL ' + str(target_major) + ': исключён только SET transaction_timeout '
                          'из служебного заголовка. Архив не изменяется.')
            self.check_cancel()
            self.emit('Замена целевой базы ' + database, 'stage')
            access_changed = True
            self.sql('ALTER DATABASE ' + identifier(database) + ' ALLOW_CONNECTIONS false;')
            self.sql('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=' +
                     literal(database) + ' AND pid<>pg_backend_pid();')
            self.sql('DROP DATABASE ' + identifier(database) + ';')
            dropped = True
            self.sql(create_sql)
            if plain:
                opener = gzip.open if str(backup).lower().endswith('.gz') else open
                with opener(backup, 'rb') as stream:
                    compressed = str(backup).lower().endswith('.gz')
                    position = stream.fileobj.tell if compressed else stream.tell
                    progress = ByteProgress(self, 'Загрузка SQL · ' + database,
                                            'чтения SQL.gz' if compressed else 'подачи SQL', backup.stat().st_size, position)
                    self.run([psql, '-X', '-w', '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-f', '-'],
                             database, source=stream, progress=progress)
            elif compatible:
                self.run([psql, '-X', '-w', '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-f', '-'],
                         database, source=stream, progress=archive_progress)
            else:
                conn = "dbname='" + database.replace('\\', '\\\\').replace("'", "\\'") + "'"
                transaction = ['--single-transaction'] if jobs == 1 else ['--jobs=' + str(jobs)]
                self.emit(f'Потоки pg_restore: {jobs}' + (' · одна транзакция' if jobs == 1 else
                          ' · без общей транзакции; при ошибке возможны частичные данные'))
                self.run([restore, '-w', '--verbose', '--exit-on-error', *transaction,
                          '--dbname=' + conn, str(backup)], database, progress=archive_progress, own_tree=jobs > 1)
        except Exception as exc:
            if access_changed and not dropped:
                try:
                    self.sql('ALTER DATABASE ' + identifier(database) + ' ALLOW_CONNECTIONS true;', ignore_cancel=True)
                except Exception as cleanup:
                    self.emit('Не удалось вернуть подключения: ' + str(cleanup))
            if dropped:
                if isinstance(exc, OperationCancelled):
                    raise OperationCancelled('Восстановление прервано. Старая база уже удалена; '
                                             'прежние данные не возвращаются. Повторите восстановление из бэкапа.') from exc
                raise RuntimeError('Старая база уже удалена. Восстановление не завершено: ' + str(exc)) from exc
            raise
        finally:
            if stream is not None:
                stream.close()
        self.emit('Восстановление завершено: ' + database)
        return database


def execute(payload, event, cancel_event=None):
    mode = payload['mode']
    if mode not in MODES:
        raise ValueError('Неизвестная операция PostgreSQL')
    runner = Runner(payload['connection'], event, cancel_event, validate_connection=mode != 'ibcmd_probe')
    runner.check_cancel()
    if mode == 'ibcmd_probe':
        return runner.inspect_ibcmd()
    if mode == 'list':
        return runner.databases(fast=payload.get('fast', False))
    if mode == 'sizes':
        return runner.database_sizes(payload['databases'])
    if mode == 'create':
        return runner.create_database(payload['database'], payload.get('owner', ''))
    if mode == 'delete':
        return runner.delete_database(payload['database'])
    if mode == 'copy':
        return runner.backup_and_restore(payload['source_database'], payload['target_database'], payload['path'])
    databases = payload.get('databases', [])
    if not databases or len(databases) > 1000 or len(set(databases)) != len(databases):
        raise ValueError('Выберите базы без повторов')
    if mode == 'ibcmd_export':
        return runner.export_dt(databases)
    if mode in ('pg_dump', 'ibcmd_dump'):
        return runner.backup(databases, payload['path'], ibcmd=mode == 'ibcmd_dump')
    if len(databases) != 1:
        raise ValueError('Для восстановления выберите одну целевую базу')
    return runner.restore(databases[0], payload['path'], ibcmd=mode == 'ibcmd_restore')


def remote_main():
    payload = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
    cancel_event = threading.Event()
    def read_control():
        pending = b''
        while chunk := os.read(sys.stdin.fileno(), 1024):
            pending += chunk
            if b'cancel\n' in pending:
                cancel_event.set()
                return
        # A broken SSH input channel must also stop the current child command.
        cancel_event.set()
    threading.Thread(target=read_control, daemon=True).start()
    output_lock = threading.Lock()
    def send(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=True), flush=True)
    try:
        result = execute(payload, send, cancel_event)
        send({'kind': 'result', 'result': result})
    except OperationCancelled as exc:
        send({'kind': 'cancelled', 'text': str(exc)})
        sys.exit(2)
    except Exception as exc:
        send({'kind': 'error', 'text': redact(str(exc), payload.get('connection', {}))})
        sys.exit(1)


if __name__ == '__main__':
    remote_main()
