"""Explicit, identity-checked deletion below the analyzed Linux directory."""
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import stat

from ssh_job_runner import Cancelled


PROTECTED={'/','/bin','/boot','/dev','/etc','/home','/lib','/lib64','/proc','/root','/run','/sbin','/sys','/tmp','/usr','/var'}


def validate_paths(root,path):
    if not all(isinstance(value,str) and value.startswith('/') and '\x00' not in value for value in (root,path)):
        raise ValueError('Для удаления нужны абсолютные пути Linux')
    base=PurePosixPath('/'+root.lstrip('/'));target=PurePosixPath('/'+path.lstrip('/'))
    if '..' in base.parts or '..' in target.parts or base==target or base not in target.parents:
        raise ValueError('Удалять можно только выбранный объект внутри проанализированного каталога')
    if str(target) in PROTECTED:raise ValueError('Удаление системного корневого каталога запрещено')
    return str(base),str(target)


def reject_mounts(path):
    # Bind mounts can share st_dev with their parent; use mountinfo as well.
    for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
        fields=line.split()
        if len(fields)<5:continue
        mounted=re.sub(r'\\([0-7]{3})',lambda match:chr(int(match[1],8)),fields[4])
        if mounted==path or mounted.startswith(path+'/'):
            raise ValueError('В выбранном каталоге есть точка монтирования: '+mounted+'. Удаление остановлено.')


def open_parent(path):
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    descriptor=os.open('/',flags)
    try:
        for part in PurePosixPath(path).parent.parts[1:]:
            child=os.open(part,flags,dir_fd=descriptor);os.close(descriptor);descriptor=child
        return descriptor
    except BaseException:
        os.close(descriptor);raise


def run(payload,event,cancel):
    root,path=validate_paths(payload.get('root'),payload.get('path'))
    if cancel.is_set():raise Cancelled('Удаление отменено до изменений')
    expected=payload.get('identity')
    if not isinstance(expected,dict) or set(expected)!={'device','inode','mode','mtime_ns','size'}:
        raise ValueError('Нет данных для проверки объекта. Обновите анализ перед удалением.')
    parent=open_parent(path)
    try:
        name=PurePosixPath(path).name
        info=os.stat(name,dir_fd=parent,follow_symlinks=False)
        actual=dict(device=info.st_dev,inode=info.st_ino,mode=info.st_mode,mtime_ns=info.st_mtime_ns,size=info.st_size)
        if actual!=expected:raise ValueError('Объект изменился после анализа. Обновите список перед удалением.')
        directory=stat.S_ISDIR(info.st_mode)
        if directory:
            if not payload.get('recursive'):raise ValueError('Нужно подтвердить удаление каталога с содержимым')
            if not shutil.rmtree.avoids_symlink_attacks:raise RuntimeError('Среда не поддерживает безопасное удаление каталога')
            reject_mounts(path)
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError('Удаление устройств, сокетов и служебных объектов не поддерживается')
        if cancel.is_set():raise Cancelled('Удаление отменено до изменений')
        event(dict(kind='stage',text='Удаление: '+path,cancellable=False))
        if cancel.is_set():raise Cancelled('Удаление отменено до изменений')
        if directory:shutil.rmtree(name,dir_fd=parent)
        else:os.unlink(name,dir_fd=parent)
        return dict(deleted=path,directory=directory)
    finally:os.close(parent)
