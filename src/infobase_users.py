"""Version-matched, isolated external connections for 1C user administration."""
from copy import deepcopy
from pathlib import Path
import re
import threading
from uuid import UUID

from configuration_install import _versioned_client
from powershell_jobs import run_script
from postgres_runner import OperationCancelled
from publication_config import connection_info
from console_core import collect_bases, discover_local_lst, find_all_rac, read_lst
from local_ras import LocalRasManager


def target_from_row(row):
    host = row.get('cluster_host') or row.get('server', '')
    port = str(row.get('cluster_port', ''))
    if not host or not port.isdigit() or not 0 < int(port) <= 65535:
        raise ValueError('Обновите список ИБ: нужен фактический адрес кластера, например сервер:1541.')
    name = str(row.get('base') or row.get('name') or '').strip()
    host = str(host).strip()
    if ':' in host and not host.startswith('['):host = '[' + host + ']'
    return dict(name=name, address='Srvr=' + quoted(host + ':' + port) + ';Ref=' + quoted(name) + ';',
                version=row.get('cluster_version') or row.get('version', ''))


def quoted(value):
    value = str(value)
    if any(c in value for c in '\x00\r\n'):raise ValueError('В параметре соединения есть недопустимый символ.')
    return '"' + value.replace('"', '""') + '"'


def discover_infobases(items, versions, user, password, local, ports, event):
    """Use the same inventory and current-user sources as the ИБ workspace."""
    cancel = getattr(event, '__dict__', {}).get('cancel_event') or threading.Event()
    items, versions = [dict(item) for item in items], dict(versions)
    errors = []
    if not items:
        report = discover_local_lst()
        errors.extend(report.errors)
        for path in report.current_paths:
            try:
                servers = read_lst(path)
                items.extend(dict(record, server=server) for server, records in servers.items() for record in records)
            except (OSError, UnicodeError) as exc:errors.append(str(path) + ': ' + str(exc))
    if cancel.is_set():raise OperationCancelled('Получение списка ИБ прервано.')
    if not items:raise ValueError('В списках appsrvrs.lst текущего пользователя не найдены серверы 1С. Добавьте сервер в консоль 1С.')
    if not versions:versions = find_all_rac()
    if not local and not versions:raise ValueError('Не найден rac.exe. Установите компоненты администрирования 1С.')
    def notify(stage, done, total):
        event(dict(kind='stage', text=stage + (' · ' + str(done) + ' / ' + str(total) if total else '')))
    manager = LocalRasManager(cancel) if local else None
    try:
        found, failures = collect_bases(items, versions, user, password, notify, cancel,
                                        ras_ports=ports, local_ras=manager)
    finally:
        if manager:manager.close()
    if cancel.is_set():raise OperationCancelled('Получение списка ИБ прервано. Предыдущий список сохранён.')
    return dict(bases=found, errors=errors + failures, items=items, versions=versions)


def resolve_inventory_target(row, versions, user, password, local, ports, event):
    from add_cluster_infobase import ClusterClient
    from infobase_address import infobase_address
    cancel = getattr(event, '__dict__', {}).get('cancel_event') or threading.Event()
    agent = dict(server=row['server'], port=row['port'], version=row.get('cluster_version') or row.get('version', ''))
    client = ClusterClient(agent, versions, user, password, local,
        ports.get(str(agent['server']) + ':' + str(agent['port']), 1545), event, cancel)
    context = client.launch_context(row['cluster_id'], row['infobase_id'], row['base'])
    return validate_target(dict(name=row['base'], version=context['cluster']['platform_version'],
        address=infobase_address(context['cluster'], context['base']['name'], agent['server'])))


def validate_target(target):
    # Rebuild the endpoint, excluding credentials and arbitrary connection flags.
    info = connection_info(str(target.get('address', '')))
    if info.get('file_path'):
        address = 'File=' + quoted(info['file_path']) + ';'
    elif info.get('cluster') and info.get('base'):
        address = 'Srvr=' + quoted(info['cluster']) + ';Ref=' + quoted(info['base']) + ';'
    else:raise ValueError('Укажите Srvr="сервер:1541";Ref="база"; или File="путь";.')
    version = str(target.get('version', '')).strip()
    if not re.fullmatch(r'8\.[35]\.\d+\.\d+', version):
        raise ValueError('Укажите точную версию платформы базы, например 8.3.27.1859.')
    return dict(name=str(target.get('name') or info.get('base') or info.get('file_path')), address=address, version=version)


def connector_path(version, versions=None):
    path = _versioned_client(version, 'comcntr.dll', versions=versions)
    if not path:
        raise ValueError('Не найден comcntr.dll версии ' + version + '. Установите компонент «Внешнее соединение» '
                         'этой платформы на компьютер консоли либо откройте Конфигуратор.')
    return path


def validate_change(change):
    action = change.get('action')
    if action not in ('create', 'update', 'delete'):raise ValueError('Неизвестная операция с пользователем.')
    result = dict(action=action, mode=change.get('mode', 'bsp'))
    if result['mode'] not in ('bsp', 'platform'):raise ValueError('Выберите механизм управления пользователями.')
    if action != 'create':
        result['id'] = str(UUID(str(change.get('id', ''))))
        if not re.fullmatch(r'[0-9a-f]{64}', str(change.get('etag', ''))):
            raise ValueError('Обновите список пользователей перед изменением.')
        result['etag'] = change['etag']
    if action != 'delete':
        for key in ('name', 'full_name'):
            result[key] = str(change.get(key, '')).strip()
            if not result[key] or len(result[key]) > 100 or any(c in result[key] for c in '\r\n\x00'):
                raise ValueError('Укажите логин и полное имя пользователя (до 100 символов).')
        for key in ('standard_auth', 'os_auth', 'show_in_list'):
            if type(change.get(key)) is not bool:raise ValueError('Некорректные параметры аутентификации.')
            result[key] = change[key]
        result['os_user'] = str(change.get('os_user', '')).strip()
        if '\x00' in result['os_user'] or '\n' in result['os_user']:raise ValueError('Некорректный пользователь ОС.')
        if result['os_auth'] and not result['os_user']:raise ValueError('Укажите пользователя ОС: ДОМЕН\\логин.')
        if action == 'create' and not result['standard_auth'] and not result['os_auth']:
            raise ValueError('Разрешите хотя бы один способ входа нового пользователя.')
        if change.get('set_password'):
            password = str(change.get('password', ''))
            if not password or '\x00' in password:raise ValueError('Укажите новый пароль.')
            result.update(set_password=True, password=password)
        elif action == 'create' and result['standard_auth']:raise ValueError('Укажите пароль нового пользователя.')
        else:result['set_password'] = False
        for key in ('roles', 'groups'):
            items = change.get(key, [])
            if not isinstance(items, list) or len(items) > 10000:raise ValueError('Некорректный список прав.')
            result[key] = list(dict.fromkeys(str(item) for item in items))
        if result['mode'] == 'bsp':
            result['groups'] = [str(UUID(item)) for item in result['groups']]
            if action == 'create' and not result['groups']:raise ValueError('Выберите хотя бы одну группу доступа.')
            result['group_names'] = [str(item) for item in change.get('group_names', [])]
            if result['group_names'] and len(result['group_names']) != len(result['groups']):
                raise ValueError('Некорректное сопоставление групп доступа.')
        elif action == 'create' and not result['roles']:raise ValueError('Выберите хотя бы одну роль платформы.')
    return result


def operate(target, credentials, action, event, *, versions=None, change=None, expected_mode=None):
    target = validate_target(target)
    if action not in ('inspect', 'change'):raise ValueError('Неизвестная операция.')
    data = dict(target=target, credentials=dict(user=str(credentials.get('user', '')),
        password=str(credentials.get('password', ''))), action=action)
    quoted(data['credentials']['user']);quoted(data['credentials']['password'])
    if action == 'change':data['change'] = validate_change(change or {})
    if expected_mode:data['expected_mode'] = expected_mode
    data['connector'] = connector_path(target['version'], versions)
    import pefile
    with pefile.PE(data['connector'], fast_load=True) as binary:
        bitness = 64 if binary.FILE_HEADER.Machine == 0x8664 else 32 if binary.FILE_HEADER.Machine == 0x14c else 0
    if not bitness:raise ValueError('Неподдерживаемая архитектура COM-соединителя.')
    code = Path(__file__).with_name('infobase_users_windows.ps1').read_text(encoding='utf-8-sig')
    secrets = (data['credentials']['password'], (change or {}).get('password', ''))
    result = run_script(code, data, event, secrets=secrets, bitness=bitness,
        cancellation_message='Операция прервана. Изменение пользователя могло завершиться на сервере; '
                             'обновите список пользователей перед повтором.')
    return result['result']


def batch_create(plans, credentials, change, event, *, versions=None, runner=operate):
    """Preflight every target before the first write; never retry a mutation."""
    change = validate_change(dict(change, action='create'))
    plans = [dict(target=validate_target(plan['target']), mode=plan['mode']) for plan in plans]
    if not plans:raise ValueError('Выберите базы для создания пользователя.')
    cancel = getattr(event, '__dict__', {}).get('cancel_event') or threading.Event()
    inventories = []
    for plan in plans:
        if cancel.is_set():raise OperationCancelled('Отменено до изменения следующей базы.')
        target = plan['target']
        event(dict(kind='stage', text='Проверка базы ' + target['name']))
        snapshot = runner(target, credentials, 'inspect', event, versions=versions)
        if snapshot['mode'] != plan['mode'] or plan['mode'] != change['mode']:
            raise ValueError(target['name'] + ': механизм прав отличается. Создавайте пользователей отдельно для каждого механизма.')
        if any(u['name'].casefold() == change['name'].casefold() for u in snapshot['users']):
            raise ValueError(target['name'] + ': пользователь уже существует. Ни одна база не изменена.')
        # Cross-base group UUIDs differ; match names only when each match is unique.
        assigned = []
        for name in change.get('group_names', []):
            matches = [g['id'] for g in snapshot.get('groups', []) if g['name'] == name]
            if len(matches) != 1:raise ValueError(target['name'] + ': группа не найдена однозначно: ' + name)
            assigned.extend(matches)
        local = deepcopy(change)
        if assigned:local['groups'] = assigned
        available = {r['name'] for r in snapshot.get('roles', [])} if local['mode'] == 'platform' else {g['id'] for g in snapshot.get('groups', [])}
        required = local['roles'] if local['mode'] == 'platform' else local['groups']
        if any(item not in available for item in required):
            raise ValueError(target['name'] + ': выбранные права отсутствуют. Ни одна база не изменена.')
        inventories.append((target, local))
    results = []
    for target, local in inventories:
        if cancel.is_set():raise OperationCancelled('Отменено. Проверьте результаты уже обработанных баз.')
        event(dict(kind='stage', text='Создание пользователя: ' + target['name']))
        try:
            result = runner(target, credentials, 'change', event, versions=versions, change=local, expected_mode=local['mode'])
            results.append(dict(target=target, result=result, status='Успешно'))
            event(dict(kind='message', text=target['name'] + ': пользователь создан и проверен.'))
        except OperationCancelled:raise
        except Exception as exc:
            message = str(exc)
            for secret in (credentials.get('password'), change.get('password')):
                if secret:message = message.replace(secret, '[скрыто]')
            event(dict(kind='error', text=target['name'] + ': ' + message))
            raise RuntimeError('Последовательность остановлена. Успешно обработано баз: ' + str(len(results)) +
                               '. В текущей базе результат нужно проверить перед повтором.\n' + message) from exc
    return results
