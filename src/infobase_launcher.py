"""Resolve a registered 1C database and launch its matching installed client."""
from pathlib import Path
import subprocess
import threading

from add_cluster_infobase import ClusterClient
from configuration_install import one_c_command, platform_client, designer_for_version
from infobase_address import infobase_address
from postgres_runner import OperationCancelled


def choose_open_mode(app,row):
    import tkinter as tk
    window=tk.Toplevel(app.root);window.title('Открыть информационную базу')
    window.transient(app.root);window.resizable(False,False);window.configure(bg=app.colors['panel'])
    frame=tk.Frame(window,bg=app.colors['panel'],padx=18,pady=15);frame.pack(fill=tk.BOTH,expand=True)
    app._label(frame,row['base'],12,'fg',True,wraplength=510,justify=tk.LEFT,anchor=tk.W).pack(fill=tk.X)
    app._label(frame,'В каком режиме открыть базу?',10,'muted',anchor=tk.W).pack(fill=tk.X,pady=(8,12))
    buttons=tk.Frame(frame,bg=app.colors['panel']);buttons.pack(fill=tk.X)
    result=[]
    def choose(mode):result.append(mode);window.destroy()
    for title,mode in (('Предприятие','ENTERPRISE'),('Конфигуратор','DESIGNER')):
        app._button(buttons,title,lambda value=mode:choose(value),primary=mode=='ENTERPRISE').pack(side=tk.LEFT,padx=(0,7))
    app._button(buttons,'Отмена',window.destroy).pack(side=tk.LEFT)
    window.bind('<Escape>',lambda event:window.destroy());window.protocol('WM_DELETE_WINDOW',window.destroy)
    window.grab_set();window.focus_set();window.wait_window()
    return result[0] if result else None


def prepare_client(row, target, versions, user='', password='', local=True, ras_port=1545,
                   event=lambda item: None, cancel=None, mode='ENTERPRISE'):
    if mode not in ('ENTERPRISE','DESIGNER'):raise ValueError('Выберите «Предприятие» или «Конфигуратор»')
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
    if mode=='DESIGNER':
        selected=dict(path=designer_for_version(cluster['platform_version'],versions=versions),
                      kind='конфигуратор',version=cluster['platform_version'])
    else:selected = platform_client(cluster['platform_version'], versions)
    if cancel.is_set():
        raise OperationCancelled('Открытие базы отменено до запуска клиента.')
    event(dict(kind='stage', text=('Найден Конфигуратор ' if mode=='DESIGNER' else 'Найден '+selected['kind']+' клиент ')+selected['version']+'\n'+address))
    # Cluster administrator credentials are never passed to the application.
    # 1C itself displays its standard infobase authentication dialog if needed.
    return dict(selected, address=address, cluster=cluster, base=base,mode=mode,
                args=[selected['path'], mode, '/IBConnectionString', address])


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
