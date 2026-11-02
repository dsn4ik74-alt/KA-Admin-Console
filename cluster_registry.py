"""Read-only 1C cluster registry snapshots. No password recovery or connections."""
from dataclasses import dataclass
from pathlib import Path
import os
import re
from uuid import UUID


MAX_BYTES = 16 * 1024 * 1024
DBMS = {'MSSQLServer', 'PostgreSQL', 'Oracle', 'OracleDatabase', 'IBMDB2'}
LABELS = ('UUID ИБ', 'Имя ИБ', 'Описание', 'Тип СУБД', 'Сервер СУБД', 'База в СУБД',
          'Пользователь СУБД', 'Пароль СУБД · зашифрован', 'Строка подключения',
          'Защищённое соединение', 'Блокировка начала сеансов', 'Блокировка регламентных заданий',
          'Выдача лицензий сервером 1С', 'Внешнее управление сеансами', 'Обязательное внешнее управление',
          'Профиль безопасности', 'Профиль безопасного режима', 'Внутренний идентификатор',
          'Резервирование рабочих процессов')
SESSION_LABELS = ('Блокировка включена', 'Начало блокировки', 'Конец блокировки', 'Сообщение',
                  'Код разрешения', 'Параметр блокировки')


def parse_braces(text):
    position = count = 0
    def value(depth=0):
        nonlocal position, count
        count += 1
        if depth > 64 or count > 200000:raise ValueError('Реестр слишком сложный для чтения')
        while position < len(text) and text[position].isspace():position += 1
        if position == len(text):raise ValueError('Реестр обрывается до конца записи')
        char = text[position];position += 1
        if char == '{':
            result = []
            while True:
                while position < len(text) and text[position].isspace():position += 1
                if position < len(text) and text[position] == '}':position += 1;return tuple(result)
                result.append(value(depth + 1))
                while position < len(text) and text[position].isspace():position += 1
                if position == len(text):raise ValueError('В реестре не закрыт вложенный блок')
                char = text[position];position += 1
                if char == '}':return tuple(result)
                if char != ',':raise ValueError('Нарушен разделитель полей реестра')
        if char == '"':
            result = []
            while position < len(text):
                char = text[position];position += 1
                if char == '"':
                    if position < len(text) and text[position] == '"':position += 1;result.append(char);continue
                    return ''.join(result)
                if char == '\x00':raise ValueError('Недопустимый символ в реестре')
                result.append(char)
            raise ValueError('В реестре не закрыта строка')
        if char in ',}':raise ValueError('В реестре пропущено значение')
        start = position - 1
        while position < len(text) and text[position] not in ',}':
            if text[position] in '{"\x00':raise ValueError('Нарушен формат значения реестра')
            position += 1
        token = text[start:position].strip()
        if not token:raise ValueError('В реестре пропущено значение')
        return token
    result = value()
    if text[position:].strip():raise ValueError('После реестра обнаружены посторонние данные')
    return result


def parameters(text):
    """Connection strings have their own quoting; semicolons may be in values."""
    parts, pending, quoted, index = [], [], '', 0
    while index < len(text):
        char = text[index];index += 1
        if char in ('"',"'"):
            if char == quoted:
                if index < len(text) and text[index] == char:index += 1;pending.append(char);continue
                quoted = ''
            elif not quoted and '=' in ''.join(pending) and not ''.join(pending).partition('=')[2].strip():quoted = char
            else:pending.append(char)
        elif char == ';' and not quoted:
            parts.append(''.join(pending));pending = []
        else:pending.append(char)
    if quoted:raise ValueError('В строке подключения не закрыты кавычки')
    parts.append(''.join(pending));result = {}
    for part in parts:
        key, separator, val = part.partition('=')
        if separator:result[key.strip().casefold()] = val.strip()
    return result


def boolean(value):
    return True if value == '1' else False if value == '0' else None


def flag_meaning(value, enabled='Включено', disabled='Выключено'):
    return enabled if value in ('1','Y','y') else disabled if value in ('0','N','n') else 'Неизвестное значение флага'


def property_meaning(record,index,value,child=None):
    """Explain only verified fields. Retain all original codes in the value column."""
    if index >= 9 and not record.known_layout:return 'Назначение поля для этой схемы не подтверждено'
    if index == 8 and child is not None:
        key=str(child).casefold()
        if key == 'schjobdn':return flag_meaning(value,'Задания запрещены · сохранённая строка','Задания разрешены · сохранённая строка')
        if key == 'sqlyoffs':return {'0':'Без смещения дат SQL Server','2000':'Смещение дат SQL Server на 2000 лет'}.get(value,'Неизвестное смещение дат')
        if key in ('dbpwd','pwd','password','srvrpwd','susrpwd'):return 'Пароль скрыт'
        known={'db':'Имя базы в СУБД','dbms':'Тип СУБД','dbsrvr':'Сервер СУБД','dbuid':'Пользователь СУБД',
                'srvr':'Адрес кластера 1С','ref':'Имя ИБ в кластере','locale':'Язык и страна','descr':'Описание'}.get(key,'Параметр сохранённой строки подключения')
        return 'Назначение числового параметра не подтверждено' if known=='Параметр сохранённой строки подключения' and str(value).isdigit() else known
    if index == 10 and child is not None:
        if child == 0:return flag_meaning(value,'Новые сеансы запрещены','Новые сеансы разрешены')
        if child in (1,2):
            if value in ('','00010101000000'):return 'Дата не задана'
            if re.fullmatch(r'\d{14}',str(value)):
                from datetime import datetime
                try:return datetime.strptime(value,'%Y%m%d%H%M%S').strftime('%d.%m.%Y %H:%M:%S')
                except ValueError:pass
            return 'Дата в исходном формате реестра'
        return {3:'Сообщение при блокировке входа',4:'Код разрешения входа при блокировке',
                5:'Параметр блокировки'}.get(child,'Назначение дополнительного поля не подтверждено')
    if index == 9:return {'0':'Защищённое соединение выключено','1':'Защита при установке соединения',
                           '2':'Защита в течение всего сеанса'}.get(value,'Неизвестный уровень защиты')
    if index == 11:return flag_meaning(value,'Блокировка включена · задания запрещены','Блокировка выключена · задания разрешены')
    if index == 12:return flag_meaning(value,'Выдача лицензий разрешена','Выдача лицензий запрещена')
    if index == 14:return flag_meaning(value,'Внешнее управление обязательно','Внешнее управление необязательно')
    if index == 18:return flag_meaning(value,'Резервирование включено','Резервирование выключено')
    if index == 17:return 'Внутренний номер записи · это не флаг 0/1'
    if index >= len(LABELS):return 'Назначение дополнительного поля в этой версии не подтверждено'
    if value == '':return 'Не задано'
    return {0:'Уникальный идентификатор ИБ',1:'Имя регистрации в кластере',2:'Описание ИБ',3:'Тип СУБД',
            4:'Адрес сервера СУБД',5:'Имя базы на сервере СУБД',6:'Учётная запись подключения 1С к СУБД',
            7:'Исходный пароль не восстанавливается',8:'Сохранённые параметры · могут отличаться от текущих',
            10:'Блокировка входа новых сеансов',13:'Адрес внешнего управления сеансами',
            15:'Имя профиля безопасности',16:'Имя профиля безопасного режима'}.get(index,'')


@dataclass(frozen=True, repr=False)
class RegistryBase:
    uuid: str
    values: tuple

    @property
    def known_layout(self):
        return 18 <= len(self.values) <= 26 and isinstance(self.values[10], tuple) and len(self.values[10]) in (6, 7)

    @property
    def jobs_denied(self):
        # Verified common 8.3 layout. SchJobDn in the stored connection string
        # is historical and can disagree with the separate registry flag.
        if self.known_layout:
            return boolean(self.values[11])
        return None

    def properties(self, show_codes=False):
        for index, value in enumerate(self.values):
            label = LABELS[index] if index < len(LABELS) else 'Дополнительное поле'
            if index >= 9 and not self.known_layout:label = 'Поле неизвестной схемы'
            if index == 7:value = 'Зашифрован · исходный пароль не восстановлен' if value else 'Пустое значение'
            elif index == 8:
                try:value = tuple((key,'••••' if key in ('dbpwd','pwd','password','srvrpwd','susrpwd') else val)
                                  for key, val in parameters(value).items())
                except ValueError:value = 'Не удалось разобрать строку подключения'
            elif index == 10 and isinstance(value, tuple):
                value = tuple((SESSION_LABELS[i] if self.known_layout and i < len(SESSION_LABELS) else 'Дополнительное поле',
                               '••••' if not show_codes and i in (4, 5) and val else val) for i, val in enumerate(value))
            yield index, label, value


@dataclass(frozen=True, repr=False)
class RegistrySnapshot:
    cluster_id: str
    cluster_name: str
    host: str
    port: int
    bases: dict
    path: str
    mtime: float


def parse_registry(text, path='', mtime=0):
    tree = parse_braces(text.lstrip('\ufeff'))
    try:
        header, block = tree[1], tree[2]
        cluster = str(UUID(header[0]));port = int(header[2])
        if not 1 <= port <= 65535 or not isinstance(header[3],str) or not isinstance(header[1],str):raise ValueError()
        if int(block[0]) != len(block) - 1:raise ValueError()
        bases = {}
        for record in block[1:]:
            if not isinstance(record,tuple) or len(record) < 12 or record[3] not in DBMS:raise ValueError()
            if any(not isinstance(val,str) for val in record[:10]):raise ValueError()
            uuid = str(UUID(record[0]))
            if uuid in bases:raise ValueError()
            bases[uuid] = RegistryBase(uuid,record)
        return RegistrySnapshot(cluster,header[1],header[3],port,bases,str(path),mtime)
    except (ValueError,TypeError,IndexError,AttributeError):
        raise ValueError('Структура реестра кластера не распознана или файл записан не полностью. Обновите файл; неизвестная схема не интерпретируется автоматически.') from None


def open_registry(path):
    if os.name != 'nt':return open(path,'rb')
    # Let the running cluster replace/rename its own file while we read it.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    handle=kernel.CreateFileW(str(path),0x80000000,7,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except Exception:
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle);raise
    return os.fdopen(descriptor,'rb')


def read_registry(path):
    path=Path(path)
    for attempt in range(2):
        with open_registry(path) as stream:
            before=os.fstat(stream.fileno());raw=stream.read(MAX_BYTES+1);after=os.fstat(stream.fileno())
        if len(raw)>MAX_BYTES:raise ValueError('Реестр превышает 16 МБ')
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:continue
        for encoding in (('utf-16',) if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else ('utf-8-sig','cp1251')):
            try:text=raw.decode(encoding);break
            except UnicodeError:continue
        else:raise ValueError('Кодировка реестра не распознана')
        return parse_registry(text,path,after.st_mtime)
    raise ValueError('Реестр менялся во время чтения. Повторите обновление.')
