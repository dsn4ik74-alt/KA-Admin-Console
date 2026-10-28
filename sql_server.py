"""SQL Server backup/restore plans and a dependency-free .NET SqlClient bridge."""
from dataclasses import dataclass
from datetime import datetime
import ntpath
import posixpath
import re
from uuid import uuid4

from powershell_jobs import run_script
from postgres_runner import OperationCancelled


SQL_BRIDGE = r'''
param($request)
Add-Type -AssemblyName System.Data
Add-Type -ReferencedAssemblies System.Data -TypeDefinition @'
using System;
using System.Data.SqlClient;
using System.Threading;
public static class SqlConsoleMessages {
    private static readonly object Gate = new object();
    private static SqlCommand current;
    private static bool cancelled;
    public static void SetCommand(SqlCommand command) {
        lock (Gate) {
            if (command != null && cancelled) throw new OperationCanceledException("Запрос прерван пользователем");
            current = command;
        }
    }
    public static void Attach(SqlConnection connection) {
        ThreadPool.QueueUserWorkItem(_ => {
            string line;
            while ((line = Console.In.ReadLine()) != null) {
                if (line != "cancel") continue;
                lock (Gate) {
                    cancelled = true;
                    if (current != null) {
                        try { current.Cancel(); } catch (Exception) { }
                    }
                }
                break;
            }
        });
        connection.InfoMessage += (sender, args) => {
            string text = args.Message.Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
            Console.WriteLine("{\"kind\":\"message\",\"text\":\"" + text + "\"}");
        };
    }
}
'@
function Send($event) {
    [Console]::Out.WriteLine(($event | ConvertTo-Json -Compress -Depth 12))
    [Console]::Out.Flush()
}
$builder=New-Object System.Data.SqlClient.SqlConnectionStringBuilder
$builder['Data Source']=[string]$request.connection.server
$builder['Initial Catalog']='master'
$builder['Integrated Security']=[bool]$request.connection.windows_auth
$builder['Encrypt']=[bool]$request.connection.encrypt
$builder['TrustServerCertificate']=[bool]$request.connection.trust_certificate
$builder['Connect Timeout']=15
$builder['ConnectRetryCount']=0
$builder['Pooling']=$false
$builder['Application Name']='1C Console'
$builder['Persist Security Info']=$false
if (-not $request.connection.windows_auth) {
    $builder['User ID']=[string]$request.connection.username
    $builder['Password']=[string]$request.connection.password
}
$connection=New-Object System.Data.SqlClient.SqlConnection($builder.ConnectionString)
[SqlConsoleMessages]::Attach($connection)
$results=New-Object 'System.Collections.Generic.List[object]'
try {
    $connection.Open()
    foreach ($step in $request.steps) {
        if ($step.label) { Send @{kind='stage';text=[string]$step.label} }
        $command=$connection.CreateCommand()
        $command.CommandText=[string]$step.sql
        $command.CommandTimeout=[int]$step.timeout
        try {
            [SqlConsoleMessages]::SetCommand($command)
            if ($step.query) {
                $reader=$command.ExecuteReader()
                $rows=New-Object 'System.Collections.Generic.List[object]'
                try {
                    while ($reader.Read()) {
                        $row=@{}
                        for ($i=0;$i -lt $reader.FieldCount;$i++) {
                            $value=$reader.GetValue($i)
                            if ($value -is [DBNull]) { $value=$null }
                            if ($value -is [datetime]) { $value=$value.ToString('yyyy-MM-dd HH:mm:ss') }
                            $row[$reader.GetName($i)]=$value
                        }
                        $rows.Add($row)
                    }
                } finally { $reader.Dispose() }
                $results.Add(@($rows.ToArray()))
            } else {
                [void]$command.ExecuteNonQuery()
                $results.Add(@())
            }
        } finally { [SqlConsoleMessages]::SetCommand($null); $command.Dispose() }
    }
    Send @{kind='done';results=@($results.ToArray())}
} finally { $connection.Dispose() }
'''


def literal(value):
    return "N'" + str(value).replace("'", "''") + "'"


def identifier(value):
    if not value or len(value.encode("utf-16-le")) > 256 or any(ord(char) < 32 for char in value):
        raise ValueError("Некорректное имя базы или логического файла")
    return '[' + value.replace(']', ']]') + ']'


def sql_path(value):
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value) or len(value.encode("utf-16-le")) > 520:
        raise ValueError("Укажите путь на сервере SQL (до 260 символов), без переводов строк")
    if not (ntpath.isabs(value) and ntpath.splitdrive(value)[0] or value.startswith('/')):
        raise ValueError("Нужен полный путь сервера или UNC-путь")
    return value


def step(sql, label="", query=False, timeout=30):
    return {"sql": sql, "label": label, "query": query, "timeout": timeout}


def metadata_row(row, expected, source):
    """Canonicalize SQL field casing; never guess absent restore metadata."""
    columns = {str(key).casefold(): value for key, value in row.items()}
    missing = [name for name in expected if name.casefold() not in columns]
    if missing:
        raise ValueError(f"{source}: SQL Server не вернул поля {', '.join(missing)}. "
                         f"Получены: {', '.join(map(str, row)) or '(нет полей)'}")
    return {name: columns.get(name.casefold()) for name in expected}


def query(connection, steps, on_event=lambda event: None):
    if not connection.get("server", "").strip():
        raise ValueError("Укажите SQL Server: сервер, сервер\\экземпляр или сервер,порт")
    if not connection.get("windows_auth") and not connection.get("username", "").strip():
        raise ValueError("Укажите логин SQL Server")
    result = run_script(SQL_BRIDGE, {"connection": connection, "steps": steps}, on_event,
                        secrets=(connection.get("password", ""),))
    return result["results"]


def list_databases(connection, on_event=lambda event: None):
    results = query(connection, [step("""WITH file_sizes AS (
    SELECT database_id, type, SUM(CONVERT(bigint,size)*8192) AS size_bytes
    FROM sys.master_files WHERE type IN (0,1) GROUP BY database_id, type
)
SELECT d.name, d.state_desc, d.recovery_model_desc, d.user_access_desc,
    f.size_bytes AS data_bytes, l.size_bytes AS log_bytes, f.size_bytes+l.size_bytes AS total_bytes
FROM sys.databases AS d
LEFT JOIN file_sizes AS f ON f.database_id=d.database_id AND f.type=0
LEFT JOIN file_sizes AS l ON l.database_id=d.database_id AND l.type=1
WHERE d.database_id>4 AND d.state=0 AND d.is_read_only=0
    AND d.source_database_id IS NULL ORDER BY d.name""", query=True),
                   step("SELECT CONVERT(nvarchar(128),SERVERPROPERTY('MachineName')) AS machine, "
                        "CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')) AS server", query=True)], on_event)
    return results[0], results[1][0]


def create_database(connection, database, on_event=lambda event: None):
    # CREATE itself rejects an existing database; never drop or replace it.
    sql = 'CREATE DATABASE ' + identifier(database) + ';'
    query(connection, [step(sql, 'Создание базы ' + database, timeout=0)], on_event)
    return database


def delete_database(connection, database, on_event=lambda event: None):
    db = identifier(database)
    if database.casefold() in ('master', 'model', 'msdb', 'tempdb'):
        raise ValueError('Удаление системной базы SQL Server запрещено')
    rows = query(connection, [step('SELECT database_id, user_access_desc FROM sys.databases WHERE name=' +
                                   literal(database) + ';', query=True)], on_event)[0]
    if len(rows) != 1 or int(rows[0]['database_id']) <= 4:
        raise ValueError('База не найдена или является системной')
    access = rows[0]['user_access_desc']
    if access not in ('MULTI_USER', 'SINGLE_USER', 'RESTRICTED_USER'):
        raise ValueError('Не определён режим доступа к базе')
    dbid = int(rows[0]['database_id'])
    cleanup = (f'IF EXISTS (SELECT 1 FROM sys.databases WHERE name={literal(database)} '
               f'AND database_id={dbid} AND user_access_desc<>{literal(access)}) '
               f'ALTER DATABASE {db} SET {access} WITH ROLLBACK IMMEDIATE;')
    sql = f"""USE [master];
IF DB_ID({literal(database)}) IS NULL OR DB_ID({literal(database)}) <> {dbid} OR DB_ID({literal(database)}) <= 4
    THROW 50000, N'База изменилась после проверки. Повторите выбор базы', 1;
BEGIN TRY
    ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE {db};
END TRY
BEGIN CATCH
    {cleanup}
    THROW;
END CATCH;"""
    try:
        query(connection, [step(sql, 'Удаление базы ' + database, timeout=0)], on_event)
    except Exception:
        # A cancelled SqlCommand may skip SQL's CATCH. Cleanup uses a fresh
        # connection without the cancellation event and only the original DB id.
        try:
            query(connection, [step(cleanup, timeout=30)], lambda event: on_event(event))
        except Exception as exc:
            on_event(dict(kind='message', text='Не удалось вернуть режим доступа: ' + str(exc)))
        raise
    return database


def backup_filename(database, now=None):
    identifier(database)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', database).rstrip(' .')[:80] or "Database"
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:[.]|$)', safe, re.I):
        safe = '_' + safe
    date = (now or datetime.now()).strftime('%Y%m%d_%H%M%S_%f')
    return f"{safe}_FULL_{date}_{uuid4().hex[:8]}.bak"


def format_size(value):
    """Display allocated file bytes in Russian units using powers of 1024."""
    if value is None:
        return '—'
    size = float(value)
    if size < 0:
        return '—'
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ', 'ЭБ'):
        if size < 1024 or unit == 'ЭБ':
            number = f'{size:.2f}'.rstrip('0').rstrip('.').replace('.', ',')
            return f'{number} {unit}'
        size /= 1024


def join_path(folder, filename):
    return posixpath.join(folder, filename) if folder.startswith('/') else ntpath.join(folder, filename)


def backup_plan(database, folder, *, compression=True, copy_only=True, now=None, verify=True):
    target = sql_path(join_path(sql_path(folder), backup_filename(database, now)))
    options = ["COPY_ONLY"] if copy_only else []
    if compression:
        options.append("COMPRESSION")
    options += ["CHECKSUM", "NOFORMAT", "NOINIT", "STATS = 1", "NAME = " + literal(database + " — FULL")]
    sql = f"BACKUP DATABASE {identifier(database)} TO DISK = {literal(target)} WITH " + ', '.join(options) + ';'
    commands = [step(sql, "Полный бэкап", timeout=0)]
    if verify:
        commands.append(step(f"RESTORE VERIFYONLY FROM DISK = {literal(target)} WITH CHECKSUM, STATS = 1;", "Проверка бэкапа", timeout=0))
    commands.append(step(f"BEGIN TRY RESTORE HEADERONLY FROM DISK = {literal(target)}; END TRY "
                         "BEGIN CATCH SELECT CAST(NULL AS bigint) AS CompressedBackupSize; END CATCH;",
                         query=True, timeout=0))
    return target, commands


def report_backup_artifact(connection, path, on_event, results=None):
    size = None
    if isinstance(results, list) and results and isinstance(results[-1], list) and results[-1]:
        header = {str(key).casefold(): value for key, value in results[-1][-1].items()}
        size = header.get('compressedbackupsize') or header.get('backupsize')
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError, OverflowError):
        size = None
    on_event(dict(kind='artifact', text='', path=path, size=size))


@dataclass(frozen=True)
class RestorePlan:
    database: str
    path: str
    position: int
    source_database: str
    backup_date: str
    moves: tuple
    logical_names: tuple
    sql: str
    backup_size: int | None = None


def _short_name(name, suffix):
    while len((name + suffix).encode('utf-16-le')) > 256:
        name = name[:-1]
    return name + suffix


RESTORE_LOG_STAGE = 'Получатель: SIMPLE и сжатие файлов журнала с целью 1024 МБ суммарно'


def restore_events(on_event):
    state={'restored':False}
    def emit(event):
        if event.get('kind') in ('message','log') and RESTORE_LOG_STAGE in event.get('text',''):
            state['restored']=True
        on_event(event)
    cancel=getattr(on_event,'__dict__',{}).get('cancel_event')
    if cancel is not None:emit.cancel_event=cancel
    return emit,state


def restore_cleanup(database, restored=False, database_id=None):
    match = f'name={literal(database)} AND user_access=1'
    if database_id is not None:match+=f' AND database_id={int(database_id)}'
    recovery = (f"BEGIN TRY ALTER DATABASE {identifier(database)} SET RECOVERY FULL; END TRY "
                "BEGIN CATCH DECLARE @RecoveryCleanup nvarchar(2048)=ERROR_MESSAGE(); "
                "RAISERROR(N'Не удалось вернуть FULL: %s',10,1,@RecoveryCleanup) WITH NOWAIT; END CATCH;\n") if restored else ''
    return (f'IF EXISTS (SELECT 1 FROM sys.databases WHERE {match}) BEGIN\n'+recovery+
            f'ALTER DATABASE {identifier(database)} SET MULTI_USER WITH ROLLBACK IMMEDIATE; END;')


def restore_plan(database, path, header, files, target_files, all_files=()):
    header = normalize_header(header)
    files = [normalize_backup_file(row) for row in files]
    target_files = [metadata_row(row, ('name', 'type', 'physical_name', 'file_id'), 'sys.master_files') for row in target_files]
    all_files = [metadata_row(row, ('database_name', 'physical_name'), 'sys.master_files') for row in all_files]
    db = identifier(database)
    path = sql_path(path)
    if int(header.get("BackupType", 0)) != 1 or header.get("IsDamaged"):
        raise ValueError("Выберите исправный полный бэкап базы (BackupType = 1)")
    position = int(header["Position"])
    if position < 1:
        raise ValueError("Некорректный номер набора бэкапа")
    present = [file for file in files if file.get("IsPresent", True)]
    if not present or any(file["Type"] not in ('D', 'L') for file in present):
        raise ValueError("Поддерживаются полные бэкапы с файлами данных D и журнала L; FILESTREAM и другие типы не поддерживаются")
    if any(file["type"] not in (0, 1) for file in target_files):
        raise ValueError("Целевая база содержит FILESTREAM или другие неподдерживаемые типы файлов")
    counts, moves, renames, log_names = {}, [], [], []
    nonce = uuid4().hex[:12]
    destinations = set()
    for index, file in enumerate(sorted(present, key=lambda item: int(item["FileID"]))):
        kind = file["Type"]
        counts[kind] = counts.get(kind, 0) + 1
        number = counts[kind]
        targets = sorted([item for item in target_files if item['type'] == (0 if kind == 'D' else 1)],
                         key=lambda item: item['file_id'])
        if not targets:
            raise ValueError("Не найдены текущие пути файлов целевой базы")
        if number <= len(targets):
            physical = targets[number - 1]['physical_name']
        else:
            original = targets[0]['physical_name']
            folder = posixpath.dirname(original) if original.startswith('/') else ntpath.dirname(original)
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', database)[:60]
            physical = join_path(folder, f"{safe}_{kind.lower()}{number}_{nonce}" + ('.ndf' if kind == 'D' else '.ldf'))
        normalized = physical.replace('/', '\\').casefold()
        if normalized in destinations or any(item['database_name'] != database and
                item['physical_name'].replace('/', '\\').casefold() == normalized for item in all_files):
            raise ValueError("Путь восстановления совпадает с файлом другой базы или другим файлом плана")
        destinations.add(normalized)
        sql_path(physical)
        old = file['LogicalName']
        identifier(old)
        suffix = ('' if number == 1 else f'_data{number}') if kind == 'D' else ('_log' if number == 1 else f'_log{number}')
        new = _short_name(database, suffix)
        temp = f"__console_restore_{nonce}_{index}"
        moves.append((old, physical))
        renames.append((old, temp, new))
        if kind == 'L':log_names.append(new)
    if set(counts) != {'D', 'L'}:
        raise ValueError("В бэкапе должны присутствовать файлы данных и журнала")
    if len({item[2].casefold() for item in renames}) != len(renames):
        raise ValueError("Совпадают новые логические имена")
    move_sql = ', '.join(f"MOVE {literal(old)} TO {literal(physical)}" for old, physical in moves)
    checksum = ', CHECKSUM' if header.get('HasBackupChecksums') else ''
    expected_files = ' OR '.join(
        f"(file_id={int(file['file_id'])} AND type={int(file['type'])} AND physical_name={literal(file['physical_name'])})"
        for file in target_files)
    destination_sql = ', '.join(literal(physical) for old, physical in moves)
    sql = f"""USE [master];
DECLARE @ConsoleChangedAccess bit=0;
DECLARE @ConsoleChangedRecovery bit=0;
BEGIN TRY
    IF DB_ID({literal(database)}) IS NULL THROW 50000, N'Целевая база больше не существует', 1;
    IF DB_ID({literal(database)}) <= 4 THROW 50000, N'Восстановление системной базы здесь запрещено', 1;
    IF (SELECT COUNT(*) FROM sys.master_files WHERE database_id=DB_ID({literal(database)})) <> {len(target_files)}
        OR EXISTS (SELECT 1 FROM sys.master_files WHERE database_id=DB_ID({literal(database)}) AND NOT ({expected_files}))
        THROW 50000, N'Файлы целевой базы изменились после проверки. Повторите подготовку восстановления', 1;
    IF EXISTS (SELECT 1 FROM sys.master_files WHERE physical_name IN ({destination_sql}) AND database_id<>DB_ID({literal(database)}))
        THROW 50000, N'Путь восстановления занят другой базой', 1;
    SET @ConsoleChangedAccess=1;
    ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    RESTORE DATABASE {db} FROM DISK = {literal(path)}
        WITH FILE = {position}, {move_sql}, REPLACE, RECOVERY, STATS = 1{checksum};
    ALTER DATABASE {db} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
"""
    for old, temp, new in renames:
        sql += f"    ALTER DATABASE {db} MODIFY FILE (NAME = {literal(old)}, NEWNAME = {literal(temp)});\n"
    for old, temp, new in renames:
        sql += f"    ALTER DATABASE {db} MODIFY FILE (NAME = {literal(temp)}, NEWNAME = {literal(new)});\n"
    sql += f"""    SET @ConsoleChangedRecovery=1;
    RAISERROR({literal(RESTORE_LOG_STAGE)}, 10, 1) WITH NOWAIT;
    ALTER DATABASE {db} SET RECOVERY SIMPLE;
    USE {db};
    CHECKPOINT;
"""
    for index,name in enumerate(log_names):
        size = max(1,1024//len(log_names)+(index < 1024%len(log_names)))
        sql += f"    DBCC SHRINKFILE ({literal(name)}, {size}) WITH NO_INFOMSGS;\n"
    sql += f"""    DECLARE @ConsoleLogMB decimal(20,2)=(SELECT SUM(CONVERT(bigint,size))/128.0 FROM sys.database_files WHERE type=1);
    DECLARE @ConsoleLogText nvarchar(40)=CONVERT(nvarchar(40),@ConsoleLogMB);
    RAISERROR(N'Журнал получателя после сжатия: %s МБ', 10, 1, @ConsoleLogText) WITH NOWAIT;
    IF @ConsoleLogMB>1024
        RAISERROR(N'ПРЕДУПРЕЖДЕНИЕ: журнал остался больше 1024 МБ; активные VLF или минимальный размер ограничивают сжатие', 10, 1) WITH NOWAIT;
    USE [master];
    ALTER DATABASE {db} SET RECOVERY FULL;
    SET @ConsoleChangedRecovery=0;
    RAISERROR(N'Получатель: модель FULL. Для новой цепочки журнала нужен полный или дифференциальный бэкап получателя', 10, 1) WITH NOWAIT;
    ALTER DATABASE {db} SET MULTI_USER WITH ROLLBACK IMMEDIATE;
END TRY
BEGIN CATCH
    BEGIN TRY
        IF @ConsoleChangedRecovery=1 AND EXISTS (SELECT 1 FROM sys.databases WHERE name={literal(database)} AND state=0)
            ALTER DATABASE {db} SET RECOVERY FULL;
    END TRY
    BEGIN CATCH
        DECLARE @RecoveryError nvarchar(2048)=ERROR_MESSAGE();
        RAISERROR(N'Не удалось вернуть FULL: %s', 10, 1, @RecoveryError) WITH NOWAIT;
    END CATCH;
    BEGIN TRY
        IF @ConsoleChangedAccess=1 AND EXISTS (SELECT 1 FROM sys.databases WHERE name={literal(database)} AND state=0)
            ALTER DATABASE {db} SET MULTI_USER WITH ROLLBACK IMMEDIATE;
    END TRY
    BEGIN CATCH
        DECLARE @CleanupError nvarchar(2048)=ERROR_MESSAGE();
        RAISERROR(N'Не удалось вернуть MULTI_USER: %s', 10, 1, @CleanupError) WITH NOWAIT;
    END CATCH;
    THROW;
END CATCH;"""
    return RestorePlan(database, path, position, header.get('DatabaseName', ''),
                       header.get('BackupFinishDate', ''), tuple(moves), tuple(item[2] for item in renames), sql,
                       header.get('CompressedBackupSize') or header.get('BackupSize'))


def inspect_restore(connection, database, path, on_event=lambda event: None, *, verify=True):
    identifier(database)
    path = sql_path(path)
    results = query(connection, [step(f"RESTORE HEADERONLY FROM DISK = {literal(path)};", "Чтение заголовка бэкапа", query=True),
                     step(f"SELECT name,type,physical_name,file_id FROM sys.master_files WHERE database_id=DB_ID({literal(database)});", query=True),
                     step("SELECT DB_NAME(database_id) AS database_name,physical_name FROM sys.master_files;", query=True)], on_event)
    headers = [normalize_header(row) for row in results[0]]
    headers = [row for row in headers if row.get('BackupType') == 1 and not row.get('IsDamaged')]
    if not headers:
        raise ValueError("В файле нет исправного полного бэкапа. Дифференциальные и журнальные копии здесь не восстанавливаются")
    header = max(headers, key=lambda row: int(row['Position']))
    position = int(header['Position'])
    checksum = ', CHECKSUM' if header.get('HasBackupChecksums') else ''
    commands = [step(f"RESTORE FILELISTONLY FROM DISK = {literal(path)} WITH FILE = {position};", query=True)]
    if verify:
        commands.append(step(f"RESTORE VERIFYONLY FROM DISK = {literal(path)} WITH FILE = {position}{checksum}, STATS = 1;",
                             "Проверка полного бэкапа перед восстановлением", timeout=0))
    else:
        on_event(dict(kind='message', text='Проверка VERIFYONLY перед восстановлением отключена пользователем.'))
    files = query(connection, commands, on_event)[0]
    plan = restore_plan(database, path, header, files, results[1], results[2])
    on_event(dict(kind='artifact', text='', path=path, size=plan.backup_size))
    return plan


def backup_and_restore(connection, source, destination, folder, on_event=lambda event: None,
                       *, compression=True, copy_only=True, verify=True):
    """Copy one database to another on the same SQL Server, retaining the FULL."""
    identifier(source)
    identifier(destination)
    if source.casefold() == destination.casefold():
        raise ValueError('Источник и целевая база должны различаться')
    if any(name.casefold() in ('master', 'model', 'msdb', 'tempdb') for name in (source, destination)):
        raise ValueError('Системные базы не участвуют в бэкапе + восстановлении')
    rows = query(connection, [step('SELECT name,database_id,state,is_read_only,source_database_id FROM sys.databases '
                                 f'WHERE name IN ({literal(source)},{literal(destination)});', query=True)], on_event)[0]
    if (len(rows) != 2 or len({int(row['database_id']) for row in rows}) != 2 or
            any(int(row['database_id']) <= 4 or int(row['state']) != 0 or row['is_read_only'] or
                row['source_database_id'] is not None for row in rows)):
        raise ValueError('Нужны две разные доступные пользовательские базы ONLINE без снимков и режима только чтения')
    # Resolve names through SQL's collation rather than Python's spelling rules.
    ids = query(connection, [step(f'SELECT DB_ID({literal(source)}) AS source_id, '
                                 f'DB_ID({literal(destination)}) AS target_id;', query=True)], on_event)[0]
    if len(ids) != 1 or ids[0]['source_id'] == ids[0]['target_id'] or not ids[0]['source_id'] or not ids[0]['target_id']:
        raise ValueError('Источник и получатель не определены как две разные базы')
    source_id, target_id = int(ids[0]['source_id']), int(ids[0]['target_id'])
    if {source_id, target_id} != {int(row['database_id']) for row in rows}:
        raise ValueError('Список баз изменился во время проверки. Обновите подключение')
    guard = (f'IF ISNULL(DB_ID({literal(source)}),0)<>{source_id} OR '
             f'ISNULL(DB_ID({literal(destination)}),0)<>{target_id} '
             "THROW 50000, N'Источник или целевая база изменились после проверки', 1;\n")
    # Verify once during restore preparation rather than on both sides of the chain.
    path, steps = backup_plan(source, folder, compression=compression, copy_only=copy_only, verify=False)
    steps[0]['sql'] = guard + steps[0]['sql']
    cancel = getattr(on_event, '__dict__', {}).get('cancel_event')
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise OperationCancelled('Бэкап + восстановление прервано')
    on_event(dict(kind='stage', text=f'1/2 · FULL-бэкап {source} → {path}'))
    result = query(connection, steps, on_event)  # Failure/cancellation never reaches RESTORE.
    report_backup_artifact(connection, path, on_event, result)
    on_event(dict(kind='message', text='FULL-бэкап создан: ' + path))
    try:
        check_cancel()
        plan = inspect_restore(connection, destination, path, on_event, verify=verify)
        if plan.source_database.casefold() != source.casefold():
            raise ValueError('База в созданном бэкапе не совпадает с выбранным источником')
        check_cancel()
        on_event(dict(kind='message', text='Файлы целевой базы:\n' + '\n'.join(f'{old} → {new}' for old, new in plan.moves)))
        restore_event,state=restore_events(on_event)
        try:
            query(connection, [step(guard + plan.sql, '2/2 · Восстановление в ' + destination, timeout=0)], restore_event)
        except Exception:
            # Cancelled commands may skip T-SQL CATCH. Only touch the original target.
            cleanup = restore_cleanup(destination,state['restored'],target_id)
            try:
                query(connection, [step(cleanup, timeout=30)], lambda item: on_event(item))
            except Exception as exc:
                on_event(dict(kind='message', text='Не удалось вернуть MULTI_USER: ' + str(exc)))
            raise
    except Exception as exc:
        cls = OperationCancelled if isinstance(exc, OperationCancelled) else RuntimeError
        raise cls('Бэкап сохранён: ' + path + '\nВосстановление не завершено: ' + str(exc)) from exc
    return dict(backup_path=path, source=source, destination=destination)


def normalize_header(row):
    values = {str(key).casefold(): value for key, value in row.items()}
    result = metadata_row(row, ('BackupType', 'Position'), 'RESTORE HEADERONLY')
    for name in ('IsDamaged', 'HasBackupChecksums', 'DatabaseName', 'BackupFinishDate', 'CompressedBackupSize', 'BackupSize'):
        result[name] = values.get(name.casefold())
    for name in ('CompressedBackupSize', 'BackupSize'):
        try:
            result[name] = int(result[name]) if result[name] is not None else None
        except (TypeError, ValueError, OverflowError):
            result[name] = None
    return result


def normalize_backup_file(row):
    result = metadata_row(row, ('LogicalName', 'Type', 'FileID'), 'RESTORE FILELISTONLY')
    values = {str(key).casefold(): value for key, value in row.items()}
    result['IsPresent'] = values.get('ispresent', True)
    try:
        result['FileID'] = int(result['FileID'])
    except (TypeError, ValueError):
        raise ValueError('RESTORE FILELISTONLY: некорректный FileID') from None
    if result['FileID'] < 1:
        raise ValueError('RESTORE FILELISTONLY: FileID должен быть положительным')
    return result
