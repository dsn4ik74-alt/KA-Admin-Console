"""Serializable operations; the scheduler calls the same backends as the forms."""
from copy import deepcopy
from dataclasses import asdict
import os
from pathlib import Path, PureWindowsPath
import re
import sys

from postgres_runner import OperationCancelled


LABELS = {
 'db_backup': 'FULL-бэкап · MSSQL', 'db_restore': 'Восстановление · MSSQL',
 'db_copy': 'Бэкап + восстановление · MSSQL', 'pg_dump': 'Бэкап · PostgreSQL',
 'pg_restore': 'Восстановление · PostgreSQL', 'pg_copy': 'Бэкап + восстановление · PostgreSQL',
 'ibcmd_dump': 'Выгрузка DT', 'ibcmd_restore': 'Восстановление DT',
 'cluster_infobase': 'Добавление ИБ в кластер', 'cluster_drop': 'Удаление ИБ из кластера',
 'cluster_jobs': 'Блокировка регламентных заданий', 'jr_archive': 'Архивирование ЖР',
 'service_create': 'Создание службы', 'service_rename': 'Переименование службы',
 'service_delete': 'Удаление службы', 'accounts': 'Создание учётных записей',
 'sql_create': 'Создание базы MSSQL', 'sql_delete': 'Удаление базы MSSQL',
 'pg_create': 'Создание базы PostgreSQL', 'pg_delete': 'Удаление базы PostgreSQL',
 'command': 'Команда PowerShell',
 'platform_install': 'Установка платформы 1С из ИТС',
}


def bundle_paths(value, restore=False):
    """Onefile extraction paths change on each invocation; persist relative tokens."""
    if isinstance(value, dict):
        return {key: item if any(word in key.lower() for word in ('password', 'passwd', 'pwd', 'secret')) else
                bundle_paths(item, restore) for key, item in value.items()}
    if isinstance(value, list):
        return [bundle_paths(item, restore) for item in value]
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    if isinstance(value, str):
        # Favorites and old jobs may contain another process's onefile path.
        # Only rebase our vendor subtree; leave user executables/data untouched.
        windows = PureWindowsPath(value)
        parts = windows.parts if windows.is_absolute() else ()
        for index, part in enumerate(parts[:-1]):
            if re.fullmatch(r'_MEI[\w-]+', part, re.IGNORECASE) and parts[index + 1].casefold() == 'vendor':
                suffix = parts[index + 1:]
                if '..' in suffix or any(':' in item for item in suffix):
                    raise ValueError('Некорректный путь встроенного инструмента')
                value = '@ka-bundle/' + '/'.join(suffix)
                break
        if restore and value.startswith('@ka-bundle/'):
            path = (root / value[len('@ka-bundle/'):]).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError('Некорректный путь встроенного инструмента')
            return str(path)
        if not restore:
            try:
                relative = Path(value).relative_to(root)
                if relative.parts and relative.parts[0] == 'vendor':
                    return '@ka-bundle/' + relative.as_posix()
            except (ValueError, OSError):
                pass
    return value


def destructive(step):
    return step['action'] not in ('db_backup', 'pg_dump', 'ibcmd_dump')


def describe(step):
    p, action = step['parameters'], step['action']
    if action.startswith(('pg_', 'ibcmd_')):
        connection = p['connection']
        text = f"{connection['host']}:{connection.get('port', 5432)} / "
        text += ', '.join(p.get('databases', [])) or p.get('database', p.get('source_database', ''))
        if action == 'pg_copy':
            target = connection['target_connection']
            text += f" → {target['host']}:{target.get('port', 5432)} / {p['target_database']}"
        return text + (' · ' + p['path'] if p.get('path') else '')
    if action.startswith(('db_', 'sql_')):
        return p['connection']['server'] + ' / ' + p.get('database', p.get('source', '')) + (
            ' → ' + p['destination'] if action == 'db_copy' else '') + (' · ' + p['path'] if p.get('path') else '')
    if action.startswith('cluster_'):
        return f"{p['target']['server']}:{p['target']['port']} / " + (p['values']['name'] if action == 'cluster_infobase' else p['name'])
    if action == 'jr_archive':
        return p['source'] + ' → ' + p['destination']
    if action.startswith('service_'):
        return p['command']['name']
    if action == 'platform_install':
        return p['distribution']['version'] + (' → ' + p['connection'].get('host', '') if p['linux'] else ' · Windows')
    return os.environ.get('COMPUTERNAME', 'Локально · Windows')


def validate_step(step):
    if not isinstance(step, dict) or step.get('action') not in LABELS:
        raise ValueError('Неизвестное действие последовательности')
    if not isinstance(step.get('title'), str) or not step['title'].strip():
        raise ValueError('Укажите название действия')
    p = step.get('parameters')
    if not isinstance(p, dict):
        raise ValueError('Не заполнены параметры действия')
    action = step['action']
    if action in ('pg_dump', 'pg_restore', 'pg_copy', 'ibcmd_dump', 'ibcmd_restore', 'pg_create', 'pg_delete'):
        from postgres_runner import Runner, database_name
        runner = Runner(p['connection'], lambda event: None)
        names = p.get('databases', [p.get('source_database'), p.get('target_database')]) if action not in ('pg_create', 'pg_delete') else [p.get('database')]
        if not names or any(not name for name in names):
            raise ValueError('Выберите базу данных')
        for name in names:
            database_name(name)
        if action not in ('pg_create', 'pg_delete') and not p.get('path', '').strip():
            raise ValueError('Укажите папку или файл бэкапа')
        if action in ('pg_dump', 'pg_copy') and runner.workers('dump_jobs') > 1 and p['connection'].get('format') != 'directory':
            raise ValueError('Параллельный бэкап требует формат «Каталог .pgdir»')
        if action.startswith('ibcmd') and not p.get('exclusive_confirmed'):
            raise ValueError('Для DT подтвердите остановку доступа 1С к базе')
    elif action.startswith(('db_', 'sql_')):
        from sql_server import identifier
        if not p['connection'].get('server'):
            raise ValueError('Укажите SQL Server')
        for key in ('database', 'source', 'destination'):
            if key in p:
                identifier(p[key])
        if not any(p.get(key) for key in ('database', 'source')):
            raise ValueError('Выберите базу данных')
        if action in ('db_backup', 'db_restore', 'db_copy') and not p.get('path', '').strip():
            raise ValueError('Укажите папку или файл бэкапа')
        if 'verify' in p and not isinstance(p['verify'],bool):
            raise ValueError('Параметр проверки бэкапа должен быть флажком')
    elif action.startswith('cluster_'):
        from uuid import UUID
        UUID(p['cluster'])
        if not p['target'].get('server'):
            raise ValueError('Выберите сервер 1С')
        if action == 'cluster_infobase':
            from add_cluster_infobase import validate_parameters
            validate_parameters(p['values'])
        else:
            UUID(p['infobase'])
    elif action.startswith('service_'):
        command = p.get('command', {})
        if command.get('action') != action.removeprefix('service_') or not command.get('name'):
            raise ValueError('Проверьте команду службы')
    elif action == 'jr_archive':
        if not p.get('source') or not p.get('destination') or not 1 <= int(p['months']) <= 1200:
            raise ValueError('Укажите исходную папку, папку архивов и срок хранения ЖР')
    elif action == 'accounts' and not p.get('names', '').strip():
        raise ValueError('Введите ФИО сотрудников')
    elif action == 'command' and not p.get('code', '').strip():
        raise ValueError('Введите команду PowerShell')
    elif action == 'platform_install':
        if not p.get('distribution') or not p.get('its_user') or not p.get('its_password'):
            raise ValueError('Выберите дистрибутив и укажите учётные данные ИТС')
        if not p.get('settings', {}).get('features'):
            raise ValueError('Выберите компоненты установки')
        if not Path(p.get('folder', '')).is_absolute():
            raise ValueError('Укажите локальную папку загрузки дистрибутивов')


def capture(panel, action=None):
    action = action or getattr(panel, 'mode', getattr(panel, '_page_kind', ''))
    if action not in LABELS:
        raise ValueError('Для этого окна нет фоновой операции. Можно добавить свою команду PowerShell в планировщике.')
    endpoint = ''
    if action.startswith(('pg_', 'ibcmd_')):
        connection = panel.connection()
        manual = panel.manual_database.get().strip() if action.startswith('ibcmd') else ''
        names = [manual] if manual else panel.selected()
        if action in ('pg_create', 'pg_delete'):
            if len(names) != 1:
                raise ValueError('Выберите одну базу; для создания её имя задаётся в планировщике')
            p = dict(connection=connection, database=names[0])
        else:
            p = dict(connection=connection, databases=names, path=panel.path.get().strip())
        if action.startswith('ibcmd'):
            p['exclusive_confirmed'] = panel.exclusive.get()
        if action == 'pg_copy':
            selection = panel.target_tree.selection()
            if len(names) != 1 or len(selection) != 1:
                raise ValueError('Выберите по одной базе источника и получателя')
            connection['target_connection'] = panel.target_connection()
            p.update(source_database=names[0], target_database=panel.target_tree.set(selection[0], 'name'))
            p.pop('databases', None)
        endpoint = f"{connection['host']}:{connection['port']} / " + ', '.join(names)
        if action == 'pg_copy':
            target = connection['target_connection']
            endpoint += f" → {target['host']}:{target['port']} / {p['target_database']}"
    elif action.startswith(('db_', 'sql_')):
        p = dict(connection=panel.connection(), database=panel.selected(),
                 path=panel.server_path.get().strip(), compression=panel.compression.get(), copy_only=panel.copy_only.get())
        if action == 'db_copy':
            selection = panel.target_tree.selection()
            if len(selection) != 1:
                raise ValueError('Выберите базу получателя')
            p.update(source=p.pop('database'), destination=panel.target_tree.set(selection[0], 'name'))
        if action in ('db_restore', 'db_copy'):
            p['verify'] = panel.verify_restore.get()
        endpoint = p['connection']['server'] + ' / ' + p.get('database', p.get('source', ''))
        if action == 'db_copy':
            endpoint += ' → ' + p['destination']
    elif action.startswith('cluster_'):
        cluster, target = panel.selected_cluster(), panel.target()
        if not cluster or not target:
            raise ValueError('Выберите сервер и кластер 1С')
        p = dict(target=target, cluster=cluster['cluster'], user=panel.app.user_var.get(), password=panel.app.pwd_var.get(),
                 local=panel.app.local_ras_var.get(), ras_port=panel.app._settings.get('ras_ports', {}).get(
                     str(target['server']) + ':' + str(target['port']), 1545),
                 ib_user=panel.ib_user.get(), ib_password=panel.ib_password.get())
        if action == 'cluster_infobase':
            p['values'] = panel.parameters()
            name = p['values']['name']
        else:
            base = panel.selected_base()
            if not base:
                raise ValueError('Выберите ИБ в кластере')
            p.update(infobase=base['infobase'], name=base['name'], denied=panel.deny_jobs.get())
            name = base['name']
        endpoint = f"{target['server']}:{target['port']} / {name}"
    elif action == 'jr_archive':
        p = {key: getattr(panel, key).get() for key in ('source', 'destination', 'base_name', 'months', 'delete')}
        endpoint = p['source']
    elif action.startswith('service_'):
        panel._capture_draft()
        panel._refresh()
        if panel._command is None:
            raise ValueError('Исправьте параметры команды службы')
        p = dict(command=asdict(panel._command))
        endpoint = p['command']['name']
    elif action == 'platform_install':
        distribution = panel.selected_distribution()
        if not distribution or not panel.metadata or panel.prepared_key != panel.distribution_key(distribution):
            raise ValueError('Сначала выберите и подготовьте дистрибутив: состав компонентов должен быть проверен')
        if panel.is_linux() and panel.needs_inspection():
            raise ValueError('Проверьте состав Linux-дистрибутива перед добавлением в расписание')
        p = dict(distribution=dict(distribution), settings=panel.settings(),
                 folder=panel.folder.get().strip(), its_user=panel.username.get(), its_password=panel.password.get(),
                 linux=panel.is_linux(), connection=panel.connection() if panel.is_linux() else {})
        endpoint = distribution['version'] + (' → ' + p['connection'].get('host', '') if p['linux'] else ' · Windows')
    else:
        p = dict(names=panel.names.get('1.0', 'end-1c'), email_domain=panel.domain.get())
        endpoint = os.environ.get('COMPUTERNAME', 'Windows')
    result = dict(action=action, title=LABELS[action], summary=endpoint, parameters=bundle_paths(p))
    validate_step(result)
    result['summary'] = describe(result)
    return result


def secret_values(value):
    """Redact all password fields, including nested DB and service settings."""
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in key.lower() for word in ('password', 'passwd', 'pwd', 'secret')) and isinstance(item, str) and item:
                result.append(item)
            else:
                result.extend(secret_values(item))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str) and item.casefold() in ('password=', '--password', '--database-password') and index+1 < len(value):
                result.append(str(value[index+1]))
            result.extend(secret_values(item))
    return result


def execute_step(step, event):
    validate_step(step)
    p = bundle_paths(deepcopy(step['parameters']), restore=True)
    action = step['action']
    cancel = event.cancel_event
    if cancel.is_set():
        raise OperationCancelled('Последовательность прервана')
    if action.startswith(('pg_', 'ibcmd_')):
        from postgres_jobs import run_job
        modes = {'pg_copy': 'copy', 'pg_create': 'create', 'pg_delete': 'delete'}
        p.pop('exclusive_confirmed', None)
        return run_job(dict(p, mode=modes.get(action, action)), event)
    if action.startswith(('db_', 'sql_')):
        from sql_server import (backup_and_restore, backup_plan, create_database, delete_database,
                                inspect_restore, query, report_backup_artifact, step as sql_step,
                                literal, identifier,restore_events,restore_cleanup)
        connection = p['connection']
        if action == 'db_copy':
            return backup_and_restore(connection, p['source'], p['destination'], p['path'], event,
                                      compression=p['compression'], copy_only=p['copy_only'], verify=p.get('verify',True))
        database = p['database']
        if action == 'db_backup':
            path, commands = backup_plan(database, p['path'], compression=p['compression'], copy_only=p['copy_only'])
            result = query(connection, commands, event)
            report_backup_artifact(connection, path, event, result)
            return dict(backup_path=path)
        if action == 'db_restore':
            plan = inspect_restore(connection, database, p['path'], event, verify=p.get('verify',True))
            if cancel.is_set():
                raise OperationCancelled('Восстановление отменено до изменения базы')
            try:
                emit,state=restore_events(event)
                query(connection, [sql_step(plan.sql, 'Восстановление FULL', timeout=0)], emit)
            except OperationCancelled:
                cleanup = restore_cleanup(database,state['restored'])
                query(connection, [sql_step(cleanup)], lambda item: event(item))
                raise
            return dict(database=database, backup_path=p['path'])
        return (create_database if action == 'sql_create' else delete_database)(connection, database, event)
    if action.startswith('cluster_'):
        from add_cluster_infobase import ClusterClient
        from console_core import find_all_rac
        client = ClusterClient(p['target'], find_all_rac(), p.get('user', ''), p.get('password', ''),
                               p.get('local', True), p.get('ras_port', 1545), event, cancel,
                               ib_user=p.get('ib_user', ''), ib_password=p.get('ib_password', ''))
        if action == 'cluster_infobase':
            result = client.create(p['cluster'], p['values'])
            if result.get('settings_error'):
                raise RuntimeError(result['settings_error'] + '\nИБ уже добавлена. Повтор создания не выполняется.')
            return result
        if action == 'cluster_drop':
            return client.drop(p['cluster'], p['infobase'], p['name'])
        return client.apply_scheduled_jobs(p['cluster'], p['infobase'], p['name'], p['denied'])
    if action == 'jr_archive':
        from archive_jr import archive_logs
        return archive_logs(p['source'], p['destination'], p['base_name'], p['months'], p['delete'], event)
    if action.startswith('service_'):
        from service_management import ServiceCommand, run_service_command
        result = run_service_command(ServiceCommand(**p['command']))
        event(dict(kind='message', text=result))
        return result
    if action == 'accounts':
        from account_creation import run_account_batch
        rows = {}
        def receive(row):
            rows[row['line']] = dict(row)
            event(dict(kind='message', text=row.get('full_name', '') + ': ' + row.get('message', '')))
            if row.get('status') in ('error', 'created_no_rdp', 'duplicate'):
                event(dict(kind='error', text='Ошибка создания УЗ: ' + row.get('message', '')))
        run_account_batch(p['names'], p['email_domain'], receive)
        # The worker stores this result encrypted, never in the public journal.
        return dict(account_rows=list(rows.values()))
    if action == 'platform_install':
        from its_portal import Portal
        from install_workspace import create_workspace, cleanup_workspace
        from platform_install import Msi, extract_distribution, install_msi, install_properties
        import linux_platform
        workspace = create_workspace(p['folder'], p['distribution']['version'])
        try:
            portal = Portal(cancel)
            portal.login(p['its_user'], p['its_password'])
            event(dict(kind='stage', text='Скачивание выбранного дистрибутива 1С'))
            path = portal.download(p['distribution'], workspace['root'], event)
            if p['linux']:
                metadata = linux_platform.linux_metadata(path, p['distribution'], event, cancel)
                connection = p['connection']
                # Authentication/trust must already have been checked in the form.
                if metadata.get('format') == 'run' and metadata.get('capabilities_verified') is False:
                    inspected = linux_platform.inspect_distribution(metadata, connection, event, cancel)
                    capabilities = dict(inspected['capabilities'])
                    features = capabilities.pop('features')
                    metadata.update(capabilities, remote_prepared=inspected['remote_prepared'])
                    metadata['features'] = [dict(id=key, title=linux_platform.FEATURES[key], parent='', display='1', level='200') for key in features]
                settings = linux_platform.installation_settings(p['settings'])
                linux_platform.validate_settings(metadata, settings)
                return linux_platform.install(metadata, settings, connection, event, cancel, cleanup=True)
            msi = extract_distribution(path, event, cancel)
            metadata = Msi().metadata(msi, p['distribution']['version'])
            install_properties(metadata, p['settings'])
            return install_msi(metadata, p['settings'], event, cancel)
        finally:
            cleanup_workspace(workspace)
    from powershell_jobs import run_script
    code = p['code']
    result = run_script(code, {}, event, require_done=False)
    return result
