"""Bounded-memory EFD template extraction into a new private directory.

Format reference: IngvarConsulting/efd_unpacker (MIT), domain/supply.py.
An EFD is raw DEFLATE containing a UTF-16LE catalog and sequential file bodies.
No installer or embedded executable is executed by this module.
"""
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import struct
import time
from uuid import uuid4
import zlib

from postgres_runner import OperationCancelled

CHUNK = 1024 * 1024
MAX_CONTENT = 64 * 1024**3
MAX_CATALOG = 8 * 1024**2


class EfdError(ValueError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise OperationCancelled('Извлечение шаблонов EFD прервано')


class Inflated:
    def __init__(self, source, cancel, event, total):
        self.source, self.cancel, self.event, self.total = source, cancel, event, total
        self.decoder = zlib.decompressobj(-15)
        self.pending = b''
        self.buffer = memoryview(b'')
        self.input = self.output = self.position = 0
        self.source_end = self.finished = False
        self.last = 0.

    def tell(self):
        return self.position

    def fill(self):
        while not self.buffer and not self.finished:
            check_cancel(self.cancel)
            if not self.pending and not self.source_end:
                self.pending = self.source.read(CHUNK)
                self.input += len(self.pending)
                self.source_end = not self.pending
            try:
                block = self.decoder.decompress(self.pending, CHUNK)
            except zlib.error as exc:
                raise EfdError('Повреждён поток EFD: ' + str(exc)) from None
            self.pending = self.decoder.unconsumed_tail
            self.output += len(block)
            if self.output > MAX_CONTENT + MAX_CATALOG or (self.output > 128 * 1024**2 and self.output > 200 * max(self.input, 1)):
                raise EfdError('Превышен допустимый объём распаковки EFD')
            self.buffer = memoryview(block)
            if self.decoder.eof:
                if self.decoder.unused_data or self.source.read(1):
                    raise EfdError('Лишние данные после конца EFD')
                self.finished = True
            elif self.source_end and not block and not self.pending:
                raise EfdError('Поток EFD обрезан; шаблоны не подтверждены')
            now = time.monotonic()
            if now - self.last >= .5:
                self.event(dict(kind='progress', percent=min(99, self.input / max(self.total, 1) * 100),
                                phase='Извлечение EFD', unit='извлечения'))
                self.last = now

    def read(self, amount):
        if not 0 <= amount <= CHUNK:
            raise EfdError('Недопустимый размер порции EFD')
        check_cancel(self.cancel)
        result = bytearray()
        while len(result) < amount:
            self.fill()
            if not self.buffer:
                break
            take = min(amount - len(result), len(self.buffer))
            result.extend(self.buffer[:take])
            self.buffer = self.buffer[take:]
        self.position += len(result)
        return bytes(result)


def relative_parts(name):
    path = PureWindowsPath(name)
    if not name or path.drive or path.root or '..' in path.parts:
        raise EfdError('Недопустимый путь в EFD: ' + repr(name))
    parts = path.parts
    for part in parts:
        if (len(part) > 255 or part.rstrip(' .') != part or re.search(r'[<>:"|?*\x00-\x1f]', part) or
                re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?', part)):
            raise EfdError('Недопустимое имя файла EFD: ' + repr(name))
    if not parts:
        raise EfdError('Пустое имя файла EFD')
    return parts


def catalog(source):
    """Validate the complete directory before creating output files."""
    def exact(amount):
        if amount > CHUNK or source.tell() + amount > MAX_CATALOG:
            raise EfdError('Слишком большое оглавление EFD')
        data = source.read(amount)
        if len(data) != amount:
            raise EfdError('Оглавление EFD обрезано')
        return data
    def integer():
        return struct.unpack('<I', exact(4))[0]
    def text():
        length = integer()
        if length > 32768:
            raise EfdError('Слишком длинная строка оглавления EFD')
        try:
            return exact(length * 2).decode('utf-16-le')
        except UnicodeError:
            raise EfdError('Повреждённая строка оглавления EFD') from None
    if integer() != 1:
        raise EfdError('Неподдерживаемая версия формата EFD')
    languages = integer()
    if languages > 100:
        raise EfdError('Недопустимое число описаний поставки EFD')
    for _ in range(languages):
        integer()
        for _ in range(4):text()
    count = integer()
    if not 0 < count <= 100000:
        raise EfdError('Недопустимое число файлов в EFD')
    entries, files, directories, total = [], set(), set(), 0
    for _ in range(count):
        integer()
        parts = relative_parts(text())
        exact(8)  # FILETIME is not required to create/use a template.
        integer()
        size = integer()  # UNSIGNED: ERP can contain files larger than 2 GB.
        key = tuple(part.casefold() for part in parts)
        if key in files or key in directories or any(key[:i] in files for i in range(1, len(key))):
            raise EfdError('Конфликт имён файлов в EFD: ' + '/'.join(parts))
        files.add(key)
        directories.update(key[:i] for i in range(1, len(key)))
        total += size
        if total > MAX_CONTENT:
            raise EfdError('Превышен допустимый объём файлов EFD')
        entries.append((parts, size))
    return entries


def extract_stream(source, destination, event, cancel, total):
    reader = Inflated(source, cancel, event, total)
    entries = catalog(reader)
    selected = {parts for parts, _ in entries if Path(parts[-1]).suffix.casefold() in ('.mft', '.cf', '.dt')}
    if not selected or not any(parts[-1].casefold().endswith('.mft') for parts in selected):
        raise EfdError('В EFD не найден манифест шаблонов CF/DT')
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    root = root.resolve()
    needed = sum(size for parts, size in entries if parts in selected)
    if shutil.disk_usage(root).free < needed + 64 * 1024**2:
        raise EfdError('Недостаточно места для шаблонов EFD: нужно не менее ' + str((needed + 1024**2 - 1) // 1024**2) + ' МБ')
    event(dict(kind='stage', text='Потоковое извлечение CF/DT из EFD'))
    written = 0
    for parts, size in entries:
        check_cancel(cancel)
        target = root.joinpath(*parts)
        temporary = None
        output = None
        try:
            if parts in selected:
                parent = root
                for part in parts[:-1]:
                    parent = parent / part
                    parent.mkdir(exist_ok=True)
                    info = parent.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                            getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
                        raise EfdError('Папка шаблона EFD содержит ссылку')
                if target.exists() or target.is_symlink():
                    raise EfdError('Файл шаблона уже существует: ' + str(target))
                temporary = target.with_name('.efd-' + uuid4().hex + '.part')
                output = temporary.open('xb')
            left = size
            while left:
                block = reader.read(min(CHUNK, left))
                if not block:
                    raise EfdError('Данные файла EFD обрезаны: ' + '/'.join(parts))
                if output is not None:output.write(block)
                left -= len(block)
            if output is not None:
                output.close()
                output = None
                temporary.replace(target)
                temporary = None
                written += 1
        finally:
            if output is not None:output.close()
            if temporary is not None:temporary.unlink(missing_ok=True)
    if reader.read(1):
        raise EfdError('Размер данных EFD не совпадает с оглавлением')
    check_cancel(cancel)
    event(dict(kind='progress', percent=100, phase='Извлечение EFD', unit='извлечения'))
    return dict(files=written, bytes=needed, path=str(root))


def extract_efd(path, destination, event, cancel):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise EfdError('Не найден файл поставки EFD')
    with path.open('rb') as source:
        return extract_stream(source, destination, event, cancel, path.stat().st_size)
