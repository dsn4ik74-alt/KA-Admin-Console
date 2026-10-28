"""Download a completed DT into the console VM, then remove only its remote staging files."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time

from postgres_runner import OperationCancelled, Runner


def download_dt(client, result, folder, event, cancel=None, retention_days=0):
    parent = Path(folder)
    if not parent.is_absolute():
        raise ValueError('Укажите абсолютную локальную папку DT на компьютере консоли')
    root, remote = PurePosixPath(result['workspace']), PurePosixPath(result['path'])
    if (not root.is_absolute() or not re.fullmatch(r'ka-ibcmd-[a-z0-9_]+', root.name) or
            '..' in root.parts or '..' in remote.parts or remote.parent.parent != root or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', remote.parent.name) or remote.parent.name.rstrip('. ') != remote.parent.name or
            re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', remote.parent.name) or
            not remote.name.endswith('.dt') or re.search(r'[<>:"/\\|?*\x00-\x1f]', remote.name) or
            remote.name.rstrip('. ') != remote.name):
        raise ValueError('Некорректный путь временного DT на Linux')
    size, expected = result['size'], result['sha256']
    if type(size) is not int or size <= 0 or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('Некорректные размер или контрольная сумма DT')
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise OperationCancelled('Скачивание DT прервано пользователем')
    check_cancel()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / remote.parent.name
    if (destination.is_symlink() or getattr(destination, 'is_junction', lambda: False)() or
            destination.resolve().parent != parent.resolve()):
        raise ValueError('Локальная папка базы не должна быть ссылкой за пределы выбранной папки')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / remote.name
    if target.exists():
        raise FileExistsError('Файл уже существует и не будет заменён: ' + str(target))
    stage = None
    try:
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(15)
            for path in (root, remote.parent):
                if not stat.S_ISDIR(sftp.lstat(str(path)).st_mode):
                    raise ValueError('Временный каталог DT заменён ссылкой или файлом')
            attrs = sftp.lstat(str(remote))
            if not stat.S_ISREG(attrs.st_mode) or attrs.st_size != size:
                raise ValueError('Временный DT изменён или заменён ссылкой')
            event(dict(kind='stage', text='Скачивание DT на компьютер консоли → ' + str(target)))
            event(dict(kind='progress', percent=0, phase='Скачивание DT', unit='файла', estimated=False))
            digest = hashlib.sha256()
            transferred, last_report = 0, time.monotonic()
            fd, stage = tempfile.mkstemp(prefix='.ka_dt_', suffix='.part', dir=destination)
            with os.fdopen(fd, 'wb') as output:
                class Writer:
                    def write(self, chunk):
                        nonlocal transferred, last_report
                        check_cancel()
                        if transferred + len(chunk) > size:
                            raise ValueError('Размер DT изменился во время скачивания')
                        written = output.write(chunk)
                        digest.update(chunk)
                        transferred += written
                        now = time.monotonic()
                        if now - last_report >= .2:
                            event(dict(kind='progress', percent=min(99.9, transferred * 100 / size),
                                       phase='Скачивание DT', unit='файла', estimated=False))
                            last_report = now
                        return written
                sftp.getfo(str(remote), Writer(), prefetch=True, max_concurrent_prefetch_requests=16)
                check_cancel()
                if transferred != size or digest.hexdigest() != expected:
                    raise ValueError('Размер или SHA256 скачанного DT не совпадает с исходным файлом')
                output.flush()
                os.fsync(output.fileno())
            check_cancel()
            # On Windows rename refuses to overwrite an existing destination.
            if os.name == 'nt':
                os.rename(stage, target)
            else:
                os.link(stage, target)
                os.unlink(stage)
            stage = None
            event(dict(kind='artifact', text='', path=str(target), size=size))
            event(dict(kind='progress', percent=100, phase='Скачивание DT', unit='файла', estimated=False))
            event(dict(kind='message', text='DT сохранён на компьютере консоли, размер и SHA256 проверены: ' + str(target)))
            try:
                # Never delete recursively: only this file and its two empty private directories.
                sftp.remove(str(remote))
                sftp.rmdir(str(remote.parent))
                sftp.rmdir(str(root))
            except Exception as exc:
                event(dict(kind='message', text='ПРЕДУПРЕЖДЕНИЕ: локальный DT готов, но временные файлы Linux не удалены: ' + str(root) + ' · ' + str(exc)))
        if retention_days:
            try:
                Runner({}, event, cancel, validate_connection=False).cleanup(destination, remote.parent.name, '.dt', retention_days, target)
            except Exception as exc:
                event(dict(kind='message', text='ПРЕДУПРЕЖДЕНИЕ: DT сохранён, очистка старых локальных копий не завершена: ' + str(exc)))
        return [str(target)]
    except Exception as exc:
        message = str(exc) + '\nDT на Linux сохранён для повторного скачивания: ' + str(remote)
        if isinstance(exc, OperationCancelled):
            raise OperationCancelled(message) from exc
        raise RuntimeError(message) from exc
    finally:
        if stage is not None:
            Path(stage).unlink(missing_ok=True)
