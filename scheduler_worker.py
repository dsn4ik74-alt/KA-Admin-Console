"""Headless scheduler. No Tk, interactive prompts, clipboard or automatic retries."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from log_colors import severity
from postgres_runner import OperationCancelled
from scheduler_actions import execute_step, secret_values, validate_step


@contextmanager
def executor_lock(path):
    path = Path(str(path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    locked = False
    try:
        if not path.stat().st_size:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                pass
        yield locked
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class Journal:
    def __init__(self, store, run_id, secrets, cancel):
        store.prune_history()
        self.store, self.run_id = store, run_id
        self.secrets = sorted(set(secrets), key=len, reverse=True)
        self.cancel_event = cancel
        self.number = 0
        self.rows = []
        self.lock = threading.Lock()
        self.errors = 0
        self.artifacts = []
        self.last_progress = 0

    def clean(self, text):
        text = str(text)
        for value in self.secrets:
            text = text.replace(value, '[скрыто]')
        return text

    def __call__(self, event):
        kind = event.get('kind', 'message')
        if kind == 'progress':
            # Progress can arrive for every block; retain at most one per 5 s.
            now = time.monotonic()
            if now - self.last_progress < 5:
                return
            self.last_progress = now
        text = event.get('text', '')
        if kind == 'artifact':
            self.artifacts.append(dict(path=event.get('path', ''), size=event.get('size')))
            text = 'Файл: ' + str(event.get('path', '')) + ' · размер: ' + str(event.get('size', 'не определён'))
        if not text:
            return
        with self.lock:
            for line in self.clean(text).splitlines():
                level = 'error' if kind in ('error', 'fatal') else 'warning' if kind == 'warning' else severity(line)
                if level == 'error':
                    self.errors += 1
                self.rows.append((self.run_id, self.number, time.time(), level, line[:32000]))
            if len(self.rows) >= 100:
                self._flush()

    def _flush(self):
        self.store.append(self.rows)
        self.rows.clear()

    def flush(self):
        with self.lock:
            self._flush()


def substitute(value, previous):
    if isinstance(value, dict):
        return {key: substitute(item, previous) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, previous) for item in value]
    if value == '${previous.backup_path}':
        if not previous.get('backup_path'):
            raise ValueError('Предыдущий шаг не вернул один файл/каталог бэкапа')
        return previous['backup_path']
    return value


def run_chain(store, run, dispatch=execute_step, shutdown=None):
    cancel, done = threading.Event(), threading.Event()
    journal = Journal(store, run['id'], [], cancel)
    def watch():
        last = 0
        while not done.wait(.5):
            try:
                if store.cancelled(run['id']):
                    cancel.set()
                if time.monotonic() - last > 5:
                    store.meta('heartbeat', dict(at=time.time(), pid=os.getpid(), run_id=run['id']))
                    last = time.monotonic()
                journal.flush()
            except Exception:
                # Main thread will surface any persistent storage error.
                pass
    watcher = threading.Thread(target=watch, daemon=True)
    status, previous = 'Успешно', {}
    current = 0
    try:
        steps = json.loads(store.unseal(json.loads(run['payload']))['json'])
        journal.secrets = sorted(set(secret_values(steps)), key=len, reverse=True)
        if not 1 <= len(steps) <= 100:
            raise ValueError('Некорректное число действий последовательности')
        for step in steps:
            validate_step(step)
        store.add_steps(run['id'], steps)
        watcher.start()
        for number, original in enumerate(steps, 1):
            current = journal.number = number
            if cancel.is_set() or store.cancelled(run['id']):
                raise OperationCancelled('Последовательность прервана до следующего шага')
            step = substitute(original, previous)
            before = journal.errors
            journal.artifacts = []
            store.step_state(run['id'], number, 'Выполняется')
            journal(dict(kind='stage', text=f"Шаг {number}/{len(steps)}: {step['title']}\n{step.get('summary', '')}"))
            journal.flush()
            result = dispatch(step, journal)
            # Cancellation reported after a completed step still prevents the next.
            store.result(run['id'], number, result, journal.artifacts)
            if journal.errors > before:
                raise RuntimeError('Шаг сообщил об ошибках. Последовательность остановлена; автоматический повтор не выполняется.')
            store.step_state(run['id'], number, 'Успешно')
            previous = dict(result) if isinstance(result, dict) else {}
            if isinstance(result, list) and len(result) == 1 and isinstance(result[0], str):
                previous['backup_path'] = result[0]
            journal(dict(kind='message', text='Шаг завершён успешно'))
            journal.flush()
    except OperationCancelled as exc:
        status = 'Прервано'
        journal(dict(kind='warning', text=str(exc)))
        if current:
            store.step_state(run['id'], current, status)
    except Exception as exc:
        status = 'Ошибка'
        journal(dict(kind='error', text=str(exc)))
        if current:
            store.step_state(run['id'], current, status)
    finally:
        done.set()
        if watcher.ident is not None:
            watcher.join(timeout=3)
        journal.flush()
        store.finish(run['id'], status)
    return status


def serve(path, stop=None, dispatch=execute_step, once=False):
    from scheduler_store import SchedulerStore
    store = SchedulerStore(path)
    stop = stop or threading.Event()
    store.initialize()
    with executor_lock(path) as acquired:
        if not acquired:
            return 0
        owner = uuid4().hex
        store.meta('stop', False)
        store.interrupted()
        last_heartbeat = 0
        try:
            while not stop.is_set():
                if time.monotonic() - last_heartbeat >= 5:
                    store.meta('heartbeat', dict(at=time.time(), pid=os.getpid(), run_id=''))
                    last_heartbeat = time.monotonic()
                if store.meta('stop'):
                    break
                run = store.claim(owner)
                if run:
                    run_chain(store, run, dispatch)
                if once:
                    break
                stop.wait(1)
        finally:
            store.meta('heartbeat', dict(at=0, pid=os.getpid(), run_id=''))
    return 0


def main(path):
    try:
        return serve(path)
    except Exception as exc:
        # A windowed executable must never show a crash dialog in Session 0.
        try:
            from scheduler_store import SchedulerStore
            store = SchedulerStore(path)
            store.initialize()
            store.meta('worker_error', dict(at=time.time(), text=str(exc)))
        except Exception:
            pass
        return 1
