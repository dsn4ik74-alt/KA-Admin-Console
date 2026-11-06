"""Separate per-operation journals; seven-day retention and no credentials."""
import io
from pathlib import Path
import re
import threading
import time

RETENTION = 7 * 24 * 60 * 60
IDENTIFIER = re.compile(r'[0-9a-f]{32}')


class OperationLogs:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self.error = ''
        self._lock = threading.RLock()
        self._active = set()
        self._memory = {}
        self._last_prune = 0

    def writer(self, identifier, secrets=()):
        if not IDENTIFIER.fullmatch(identifier):raise ValueError('Некорректный номер журнала')
        with self._lock:self._active.add(identifier)
        return LogWriter(self, identifier, secrets)

    def prune(self, clear=False, keep=()):
        with self._lock:
            keep = self._active | set(keep)
            cutoff = time.time()-RETENTION
            if self.directory:
                if not self.directory.is_dir():return
                for path in self.directory.glob('*.log'):
                    if not IDENTIFIER.fullmatch(path.stem) or path.stem in keep:continue
                    if clear or path.stat().st_mtime < cutoff:path.unlink(missing_ok=True)
            else:
                for identifier,(at,text) in list(self._memory.items()):
                    if identifier not in keep and (clear or at<cutoff):self._memory.pop(identifier,None)
            self._last_prune = time.time()

    def read(self, identifier, offset=0, size=256*1024):
        if not IDENTIFIER.fullmatch(identifier):raise ValueError('Некорректный номер журнала')
        offset = max(0,int(offset))
        with self._lock:
            if self.directory:
                path = self.directory/(identifier+'.log')
                if path.is_symlink() or not path.is_file():return 'Журнал не сохранён. Для прежних версий доступны только итоги операции.',0,False
                if identifier not in self._active and path.stat().st_mtime < time.time()-RETENTION:
                    return 'Срок хранения журнала истёк (7 дней).',0,False
                stream = path.open(encoding='utf-8',errors='replace')
            else:
                at,text = self._memory.get(identifier,(0,''))
                if not text:return 'Журнал не сохранён.',0,False
                if identifier not in self._active and at < time.time()-RETENTION:return 'Срок хранения журнала истёк (7 дней).',0,False
                stream = io.StringIO(text)
            with stream:
                stream.seek(offset)
                text = stream.read(size)
                next_offset = stream.tell()
                more = bool(stream.read(1))
            return text,next_offset,more


class LogWriter:
    def __init__(self, store, identifier, secrets):
        self.store,self.identifier = store,identifier
        self.secrets = sorted({value for value in secrets if isinstance(value,str) and value},key=len,reverse=True)
        self.stream = None
        self.closed = False
        self._last_flush = 0
        self._lock = threading.RLock()

    def hide(self, values):
        with self._lock:
            self.secrets=sorted(set(self.secrets)|{value for value in values if isinstance(value,str) and value},key=len,reverse=True)

    def write(self, text):
        with self._lock:
            if self.closed:return
            try:
                if time.time()-self.store._last_prune>600:self.store.prune()
                if self.stream is None:
                    if self.store.directory:
                        self.store.directory.mkdir(parents=True,exist_ok=True)
                        self.stream = (self.store.directory/(self.identifier+'.log')).open('x',encoding='utf-8',errors='replace')
                    else:self.stream=io.StringIO()
                text = str(text)
                for secret in self.secrets:text=text.replace(secret,'••••')
                self.stream.write(time.strftime('%Y-%m-%d %H:%M:%S')+'  '+text.rstrip('\n')+'\n')
                if self.store.directory and time.monotonic()-self._last_flush>1:
                    self.stream.flush();self._last_flush=time.monotonic()
                if not self.store.directory:
                    with self.store._lock:self.store._memory[self.identifier]=(time.time(),self.stream.getvalue())
            except OSError as exc:
                self.store.error='Не удалось сохранить журнал: '+str(exc)

    def close(self):
        with self._lock:
            if self.closed:return
            self.closed=True
            try:
                if self.stream:self.stream.close()
            except OSError as exc:self.store.error='Не удалось закрыть журнал: '+str(exc)
            finally:
                self.secrets=[]
                with self.store._lock:self.store._active.discard(self.identifier)
