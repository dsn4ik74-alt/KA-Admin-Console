"""Read-only Ring licensing commands and bounded native process execution.

This module is also copied to the isolated Linux worker. It never activates,
updates, exports or removes a license and explicitly disables statistics.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time


NAME = re.compile(r'(?<![\w-])(\d{5,30}-\d{5,30})(?:\.lic)?(?![\w-])', re.I)
ACTIONS = ('list', 'info', 'validate')
LIMIT = 2 * 1024 * 1024


class CheckCancelled(RuntimeError):
    pass


def clean(value, title):
    value = str(value or '').strip()
    if any(c in value for c in '\r\n\x00') or len(value) > 4096:
        raise ValueError('Недопустимые символы: ' + title)
    return value


def validate_payload(values):
    p = dict(values)
    if p.get('action') not in ('list', 'check'):
        raise ValueError('Выберите список или проверку лицензий')
    p['path'] = clean(p.get('path'), 'каталог лицензий')
    names = p.get('names', [])
    if not isinstance(names, list) or len(names) > 1000:
        raise ValueError('За одну проверку допускается не более 1000 лицензий')
    if any(not isinstance(n, str) or not NAME.fullmatch(n) or n.lower().endswith('.lic') for n in names):
        raise ValueError('Выберите лицензию из полученного списка')
    p['names'] = list(dict.fromkeys(names))
    return p


def license_arguments(action, path='', name=''):
    if action not in ACTIONS:
        raise ValueError('Разрешены только list, info и validate')
    args = ['license', action, '--send-statistics', 'false']
    if action != 'list':
        if not NAME.fullmatch(name) or name.lower().endswith('.lic'):
            raise ValueError('Требуется полное имя лицензии из списка ring')
        args += ['--name', name]
    path = clean(path, 'каталог лицензий')
    if path:
        args += ['--path', path]
    return args


def parse_names(text):
    return list(dict.fromkeys(m[1] for m in NAME.finditer(text)))


def decode(data):
    for encoding in ('utf-8-sig', 'cp866', 'cp1251'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode('utf-8', errors='replace')


def native(args, cancel, timeout=90, environment=None):
    """Capture a bounded response; stop only this command's own descendants."""
    if cancel.is_set():
        raise CheckCancelled('Проверка лицензий прервана')
    env = dict(os.environ)
    # Do not inherit arbitrary JVM options from unrelated applications.
    for key in ('JAVA_TOOL_OPTIONS', '_JAVA_OPTIONS', 'JDK_JAVA_OPTIONS', 'CLASSPATH'):
        env.pop(key, None)
    env.update(environment or {})
    process = subprocess.Popen([str(a) for a in args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        start_new_session=os.name != 'nt', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    tree = None
    if os.name == 'nt':
        from postgres_runner import ProcessTree
        try:
            tree = ProcessTree(process)
        except Exception:
            process.kill(); process.wait(); process.stdout.close()
            raise
    chunks = queue.Queue(maxsize=40)
    stopped = threading.Event()
    def read():
        try:
            while not stopped.is_set():
                chunk = process.stdout.read(16384)
                if not chunk:
                    break
                while not stopped.is_set():
                    try:
                        chunks.put(chunk, timeout=.1); break
                    except queue.Full:
                        continue
        finally:
            while not stopped.is_set():
                try:
                    chunks.put(None, timeout=.1); break
                except queue.Full:
                    continue
    reader = threading.Thread(target=read, daemon=True); reader.start()
    data = bytearray(); deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel.is_set():
                raise CheckCancelled('Проверка лицензий прервана')
            if time.monotonic() >= deadline:
                raise RuntimeError('ring не завершил команду за ' + str(timeout) + ' с. Проверьте Java, права и доступ к оборудованию.')
            try:
                item = chunks.get(timeout=.1)
            except queue.Empty:
                continue
            if item is None:
                break
            data.extend(item)
            if len(data) > LIMIT:
                raise RuntimeError('Вывод ring превышает 2 МБ; сузьте каталог проверки')
        return dict(code=process.wait(timeout=max(.1, deadline-time.monotonic())), text=decode(data).replace('\r\r\n','\n').replace('\r\n','\n').strip())
    finally:
        stopped.set()
        if tree:
            tree.close()
        elif process.poll() is None:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(); reader.join(timeout=3); process.stdout.close()


def java_prefix(root, java):
    root, java = Path(root).resolve(), Path(java).resolve()
    if not java.is_file() or not (root / 'lib/boot').is_dir():
        raise ValueError('Не найден комплект ring / Java. Укажите каталог установленного ring и Java.')
    return [str(java), '-Dfile.encoding=UTF-8', '-Dsun.stdout.encoding=UTF-8', '-Dsun.stderr.encoding=UTF-8', '-Duser.language=ru', '-Duser.country=RU',
        '-De1c.ring.location=' + str(root), '-p', str(root/'lib/boot'), '--add-modules=ALL-MODULE-PATH',
        '--add-opens', 'java.base/java.lang=com.e1c.ring.cli.boot.jpms',
        'com.e1c.ring.cli.boot.RingBootstrap']


@contextmanager
def bundled_command(root):
    root = Path(root).resolve()
    module = next((root/'license-tools').glob('com._1c.license.activator.ring-*.jar'), None)
    if module is None:
        raise ValueError('Во встроенном комплекте отсутствует модуль License Tools')
    with tempfile.TemporaryDirectory(prefix='ka-ring-') as temporary:
        registry = Path(temporary)/'ring-commands.cfg'
        registry.write_text('license:\n- file: ' + json.dumps(str(module), ensure_ascii=True) +
            '\n  arch: x86_64\n  version: 0.15.2\n', encoding='utf-8')
        registry.chmod(0o600)
        user_registry=Path(temporary)/'user-commands.cfg'
        user_registry.write_text('{}\n',encoding='utf-8');user_registry.chmod(0o600)
        yield (java_prefix(root/'ring', root/('java/bin/java.exe' if os.name=='nt' else 'java/bin/java')),
               dict(E1C_RING_COMMANDS=str(registry),E1C_RING_COMMANDS_USER=str(user_registry)))


def summary_row(name, info, check=None):
    fields = {}
    for line in info.get('text', '').splitlines():
        key, separator, value = line.partition(':')
        if separator and value.strip():
            fields[key.strip().casefold()] = value.strip()
    def field(*names):
        return next((fields[n] for n in names if n in fields), '—')
    # Keep the complete output: labels differ between License Tools versions.
    return dict(name=name, product=field('product', 'product name', 'description', 'продукт', 'наименование продукта', 'описание'),
        owner=field('company', 'компания', 'организация', 'наименование организации'),
        seats=field('license count', 'licenses count', 'number of licenses', 'количество лицензий', 'число лицензий'),
        expires=field('expiration date', 'valid until', 'срок действия', 'дата окончания действия'),
        status=('Ошибка чтения' if info['code'] else 'Не проверена') if check is None else
               ('Привязка подтверждена' if check['code']==0 else 'Проверка не пройдена'),
        info=info['text'], info_code=info['code'], validation='' if check is None else check['text'],
        validation_code=None if check is None else check['code'], checked_at=datetime.now().astimezone().isoformat(timespec='seconds'))


def inspect(prefix, values, event, cancel, environment=None, invoke=native):
    p = validate_payload(values)
    names = p['names']
    listing = ''
    def command(action, name=''):
        if cancel.is_set():
            raise CheckCancelled('Проверка лицензий прервана')
        event(dict(kind='stage', text='ring license ' + action + (' · '+name if name else '')))
        result = invoke([*prefix, *license_arguments(action, p['path'], name)], cancel, environment=environment)
        if result['text']:
            event(dict(kind='message' if result['code']==0 else 'warning', text=result['text']))
        return result
    if not names:
        result = command('list'); listing = result['text']
        if result['code']:
            raise RuntimeError('ring license list: код ' + str(result['code']) + '. ' + result['text'][-3000:])
        names = parse_names(listing)
        if len(names)>1000:
            raise ValueError('Найдено более 1000 лицензий. Укажите отдельный каталог.')
        if not names and listing:
            event(dict(kind='warning',text='Имена лицензий в выводе ring не распознаны либо список пуст. Полный ответ приведён в журнале.'))
    rows=[]
    for index, name in enumerate(names):
        info=command('info',name)
        check=command('validate',name) if p['action']=='check' else None
        row=summary_row(name,info,check);rows.append(row)
        event(dict(kind='license_row',row=row))
        event(dict(kind='progress',percent=(index+1)/len(names)*100,phase='Проверка лицензий',unit='лицензий'))
    return dict(rows=rows,listing=listing,checked=p['action']=='check',
                settings_error=any(row['info_code'] or row['validation_code'] not in (None,0) for row in rows))
