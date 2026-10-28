"""Resolve a registered 1C database and launch its matching installed client."""
from pathlib import Path
import subprocess
import threading

from add_cluster_infobase import ClusterClient
from configuration_install import one_c_command, platform_client
from infobase_address import infobase_address
from postgres_runner import OperationCancelled


def prepare_client(row, target, versions, user='', password='', local=True, ras_port=1545,
                   event=lambda item: None, cancel=None):
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise OperationCancelled('Открытие базы отменено до подключения к серверу.')
    if not row.get('cluster_id') or not row.get('infobase_id') or not row.get('base'):
        raise ValueError('У выбранной базы нет идентификатора кластера или ИБ. Обновите список ИБ.')
    if str(row.get('server', '')).casefold() != str(target.get('server', '')).casefold() or str(row.get('port')) != str(target.get('port')):
        raise ValueError('Сервер выбранной базы изменился. Обновите список ИБ.')
    event(dict(kind='stage', text='Определение адреса и версии базы: ' + row['base']))
    client = ClusterClient(target, versions, user, password, local, ras_port, event, cancel)
    context = client.launch_context(row['cluster_id'], row['infobase_id'], row['base'])
    cluster, base = context['cluster'], context['base']
    address = infobase_address(cluster, base['name'], target['server'])
    if not address:
        raise ValueError('Сервер 1С не вернул порт выбранного кластера. Обновите список ИБ. Для открытия нужен порт кластера, например 1541 или 4541.')
    selected = platform_client(cluster['platform_version'], versions)
    if cancel.is_set():
        raise OperationCancelled('Открытие базы отменено до запуска клиента.')
    event(dict(kind='stage', text='Найден ' + selected['kind'] + ' клиент ' + selected['version'] + '\n' + address))
    # Cluster administrator credentials are never passed to the application.
    # 1C itself displays its standard infobase authentication dialog if needed.
    return dict(selected, address=address, cluster=cluster, base=base,
                args=[selected['path'], 'ENTERPRISE', '/IBConnectionString', address])


def start_client(plan, cancel):
    if cancel.is_set():
        raise OperationCancelled('Открытие базы отменено до запуска клиента.')
    executable = Path(plan['path'])
    if not executable.is_file():
        raise ValueError('Файл клиента 1С больше не найден: ' + str(executable))
    process = subprocess.Popen(one_c_command(plan['args']), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               cwd=str(executable.parent), shell=False)
    # Do not wait for the application window to close or terminate it when the
    # administrator console closes. This is an independently running GUI client.
    return dict(plan, process=process, pid=process.pid)
