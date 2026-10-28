"""Delete only the private download tree created for a particular installation."""
import json
import re
from pathlib import Path
import shutil
import stat
from uuid import uuid4


def create_workspace(folder, version):
    if not re.fullmatch(r'8\.[35]\.\d+\.\d+', version):
        raise ValueError('Некорректная версия платформы')
    return _create_private_workspace(Path(folder).resolve() / version)


def create_configuration_workspace(folder, project, version, inherited_parents=()):
    if not re.fullmatch(r'[\w.-]{1,100}', project) or project in ('.', '..'):
        raise ValueError('Некорректное имя конфигурации')
    if not re.fullmatch(r'\d+(?:\.\d+){1,5}', version):
        raise ValueError('Некорректная версия конфигурации')
    return _create_private_workspace(Path(folder).resolve() / 'configurations' / project / version, inherited_parents)


def _create_private_workspace(parent, inherited_parents=()):
    for directory in inherited_parents:
        path = Path(directory)
        if not path.is_absolute() or path == Path(path.anchor) or not parent.is_relative_to(path):
            raise ValueError('Некорректная родительская папка временной установки')
    missing, candidate = [], parent
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    created_parents = []
    for candidate in reversed(missing):
        try:
            candidate.mkdir()
            created_parents.append(str(candidate))
        except FileExistsError:
            if not candidate.is_dir():
                raise
    token = uuid4().hex
    root = parent / ('ka-install-' + token)
    root.mkdir()
    context = dict(root=str(root), parent=str(parent.resolve()), token=token,
                   created_parents=list(dict.fromkeys([*inherited_parents, *created_parents])))
    (root / '.ka-install.json').write_text(json.dumps(context), encoding='utf-8')
    return context


def cleanup_workspace(context):
    root, parent = Path(context['root']), Path(context['parent'])
    token = context['token']
    if not root.exists():
        return
    if not root.is_absolute() or root.name != 'ka-install-' + token or root.resolve() != root.absolute() or root.resolve().parent != parent.resolve():
        raise ValueError('Отказ от очистки: каталог не принадлежит этой установке')
    marker = root / '.ka-install.json'
    if json.loads(marker.read_text(encoding='utf-8')) != context:
        raise ValueError('Отказ от очистки: метка каталога установки не совпадает')
    created_parents = [Path(value) for value in context.get('created_parents', ())]
    for directory in created_parents:
        if not directory.is_absolute() or directory == Path(directory.anchor) or not parent.is_relative_to(directory):
            raise ValueError('Отказ от очистки: неверная родительская папка установки')
    # Verify every resolved target before recursive deletion on Windows. Refuse
    # junctions/symlinks instead of traversing a path outside the private tree.
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('Отказ от очистки: в каталоге есть ссылка или точка подключения')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Отказ от очистки: путь находится вне каталога установки')
        if path.is_dir():
            pending.extend(path.iterdir())
    shutil.rmtree(root)
    # Only empty parents actually created by this operation are removable.
    # Existing Downloads folders and neighboring files are preserved.
    for directory in sorted(created_parents, key=lambda value: len(value.parts), reverse=True):
        try:
            info = directory.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024) or directory.resolve() != directory.absolute()):
                break
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # Another download or a user file may now be in this directory.
            break
