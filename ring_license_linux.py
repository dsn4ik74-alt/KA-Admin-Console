"""Read-only license checks on the selected Linux machine, never on the client."""
from pathlib import Path, PurePosixPath
import tarfile
import platform

from ring_license import CheckCancelled, bundled_command, inspect, validate_payload
from ssh_job_runner import Cancelled


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
    bundle=root/'ring-bundle';bundle.mkdir(mode=0o700)
    event(dict(kind='stage',text='Распаковка временного ring и Java на Linux'))
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
            target.chmod(item.mode&0o755)
    try:
        with bundled_command(bundle) as (prefix,environment):
            return inspect(prefix,p,event,cancel,environment)
    except CheckCancelled as exc:
        raise Cancelled(str(exc)) from None
