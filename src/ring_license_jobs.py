"""Portable license checks locally, by WinRM + SMB, or by Linux SSH."""
from contextlib import contextmanager
import hashlib
from app_paths import resource_root
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import re
from uuid import uuid4
import zipfile

from file_transfer import endpoint, smb
from postgres_runner import OperationCancelled
from ring_license import CheckCancelled, bundled_command, inspect, summary_row, validate_payload
from windows_remote import run_windows
from ssh_jobs import run_script


def bundle_path(system):
    name={'local':'windows-x64.zip','windows':'windows-x64.zip','linux':'linux-x64.tar.gz'}[system]
    path=resource_root()/'vendor/ring'/name
    if not path.is_file():raise ValueError('Во встроенном комплекте отсутствует '+name+'. Используйте полную сборку приложения.')
    return path


def windows_code():
    return (Path(__file__).resolve().parent/'ring_license_windows.ps1').read_text(encoding='utf-8-sig')


def validate_windows(connection):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',connection.get('host','')):
        raise ValueError('Укажите имя или IPv4-адрес Windows-сервера без порта')
    if not connection.get('user') or not connection.get('password'):
        raise ValueError('Введите логин и пароль Windows-сервера')
    try:port=int(connection.get('port',5985))
    except (ValueError,TypeError):raise ValueError('Некорректный порт WinRM') from None
    if not 1<=port<=65535:raise ValueError('Порт WinRM должен быть от 1 до 65535')
    return dict(connection,port=port)


@contextmanager
def local_bundle():
    with tempfile.TemporaryDirectory(prefix='ka-ring-bundle-') as temporary:
        root=Path(temporary)
        with zipfile.ZipFile(bundle_path('local')) as archive:
            for item in archive.infolist():
                name=PureWindowsPath(item.filename)
                if name.is_absolute() or name.drive or '..' in name.parts or ':' in item.filename:
                    raise ValueError('Небезопасный путь встроенного ring')
            archive.extractall(root)
        yield root


def probe_windows(connection,event):
    return run_windows(windows_code(),dict(validate_windows(connection),remote=True),dict(action='probe'),event)


def remote_windows(values,connection,event,cancel):
    connection=validate_windows(connection)
    archive=bundle_path('windows')
    with archive.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    token=uuid4().hex;root='';completed=False
    code=windows_code()
    try:
        event(dict(kind='stage',text='Подготовка временного ring на Windows-сервере'))
        result=run_windows(code,connection,dict(action='prepare',token=token),event)
        root=result['root']
        if not PureWindowsPath(root).is_absolute() or PureWindowsPath(root).name!='ka-ring-'+token:
            raise ValueError('Windows вернул неожиданный временный каталог ring')
        # SMB uses the same explicit login. Existing connections are not removed.
        destination=endpoint(dict(kind='windows',host=connection['host'],user=connection['user'],password=connection['password'],
                                  path=str(PureWindowsPath(root)/'ring.zip')))
        event(dict(kind='stage',text='Передача временного ring и Java на Windows · SMB'))
        with smb(destination),archive.open('rb') as incoming,open(destination['path'],'xb') as output:
            size=archive.stat().st_size;done=0
            while chunk:=incoming.read(1024*1024):
                if cancel.is_set():raise OperationCancelled('Передача ring прервана')
                output.write(chunk);done+=len(chunk)
                event(dict(kind='progress',percent=done/size*100,phase='Передача ring на Windows',unit='передачи'))
        payload=dict(values,root=root,token=token,sha256=digest)
        def receive(item):
            if item.get('kind')=='license_raw_row':
                row=item['row'];item=dict(kind='license_row',row=summary_row(row['name'],row['info'],row.get('check')))
            event(item)
        receive.cancel_event=cancel
        raw=run_windows(code,connection,payload,receive);completed=True
        rows=[summary_row(row['name'],row['info'],row.get('check')) for row in raw.get('rows',[])]
        return dict(rows=rows,listing=raw.get('listing',''),checked=raw.get('checked',False),
                    settings_error=any(row['info_code'] or row['validation_code'] not in (None,0) for row in rows))
    finally:
        if root and not completed:
            try:
                # Cancel stops the check, but must not skip cleanup of our toolkit.
                run_windows(code,connection,dict(action='cleanup',root=root,token=token),lambda item:None)
            except Exception:
                event(dict(kind='warning',text='Не подтверждено удаление временного комплекта на Windows: '+root+'. Лицензии не изменялись.'))


def operate(system,values,connection,event,cancel,elevate=False):
    values=validate_payload(values)
    try:
        if system=='local':
            if values['path'] and not Path(values['path']).is_dir():
                raise ValueError('Каталог лицензий на этом компьютере не существует или недоступен')
            event(dict(kind='stage',text='Подготовка встроенного ring и Java · этот компьютер'))
            with local_bundle() as root,bundled_command(root) as (prefix,environment):
                return inspect(prefix,values,event,cancel,environment)
        if system=='windows':
            return remote_windows(values,dict(connection,remote=True),event,cancel)
        if system=='linux':
            return run_script('ring_license_linux',connection,values,event,cancel,files=(bundle_path('linux'),),elevate=elevate)
        raise ValueError('Выберите компьютер проверки лицензий')
    except CheckCancelled as exc:
        raise OperationCancelled(str(exc)) from None
    except OperationCancelled:
        raise OperationCancelled('Проверка лицензий прервана. Активация и изменение лицензий не выполнялись.') from None
