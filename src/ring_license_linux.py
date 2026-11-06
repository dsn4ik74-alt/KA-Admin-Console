"""Read-only license checks on the selected Linux machine, never on the client."""
from pathlib import Path, PurePosixPath
import tarfile
import platform
import stat

from ring_license import CheckCancelled, bundled_command, inspect, native, validate_payload
from ssh_job_runner import Cancelled


def unpack_bundle(archive,bundle,event,cancel):
    bundle.mkdir(mode=0o700)
    with tarfile.open(archive,'r:gz') as source:
        total=0
        for item in source:
            if cancel.is_set():raise Cancelled('Проверка лицензий прервана')
            name=PurePosixPath(item.name)
            if name.is_absolute() or '..' in name.parts or not item.isfile():
                raise ValueError('Небезопасный путь в комплекте ring')
            total+=item.size
            if total>1024**3:raise ValueError('Комплект ring превышает 1 ГБ')
            target=bundle/str(name);target.parent.mkdir(parents=True,exist_ok=True)
            with source.extractfile(item) as incoming,target.open('xb') as output:
                while chunk:=incoming.read(1024*1024):
                    if cancel.is_set():raise Cancelled('Проверка лицензий прервана')
                    output.write(chunk)
            mode=item.mode&0o755
            # Old Windows-built bundles lost POSIX execute bits. Repair only
            # the official JRE entry points, without granting extra access to LICs.
            if (len(name.parts)==3 and name.parts[:2]==('java','bin')) or name.as_posix()=='java/lib/jspawnhelper':
                mode|=stat.S_IXUSR
            target.chmod(mode)


def check_java(java,event,cancel,invoke=native):
    event(dict(kind='stage',text='Проверка запуска встроенной Java на Linux'))
    try:
        result=invoke([str(java),'-version'],cancel,timeout=30)
    except PermissionError:
        executable=bool(java.stat().st_mode&stat.S_IXUSR)
        detail='Право исполнения файла установлено. Проверьте noexec у файловой системы, ACL и политики безопасности Linux.' if executable else 'У файла Java отсутствует право исполнения.'
        raise RuntimeError('Linux запретил запуск встроенной Java: '+str(java)+'. '+detail+' Проверка лицензий ещё не начиналась.') from None
    if result['code']:
        raise RuntimeError('Не удалось запустить встроенную Java: '+result['text'][-3000:])
    event(dict(kind='message',text=result['text']))


def run(payload, event, cancel):
    p=validate_payload(payload)
    if platform.machine().lower() not in ('x86_64','amd64'):
        raise ValueError('Встроенный Linux-комплект ring предназначен для x86-64')
    root=Path(payload['_root']).resolve()
    if p['path'] and not Path(p['path']).is_dir():
        raise ValueError('Каталог лицензий на Linux не существует или недоступен выбранному пользователю')
    # ssh_job_runner already verifies SHA256 of uploaded payloads.
    archive=root/'payload-0.tar.gz'
    if not archive.is_file():
        raise ValueError('Не передан встроенный Linux-комплект ring')
    bundle=root/'ring-bundle'
    event(dict(kind='stage',text='Распаковка временного ring и Java на Linux'))
    try:
        unpack_bundle(archive,bundle,event,cancel)
        check_java(bundle/'java/bin/java',event,cancel)
        with bundled_command(bundle) as (prefix,environment):
            return inspect(prefix,p,event,cancel,environment)
    except CheckCancelled as exc:
        raise Cancelled(str(exc)) from None
