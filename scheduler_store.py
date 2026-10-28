"""Persistent schedules and journals shared by the UI and a headless executor."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from favorite_secrets import protect, unprotect


def next_time(rule, after, inclusive=False):
    """Local calendar schedules; stored instants are UTC epoch seconds."""
    start = datetime.strptime(rule['start'], '%Y-%m-%d %H:%M')
    boundary = after if inclusive else after + .001
    kind = rule['kind']
    if kind == 'once':
        return start.timestamp() if start.timestamp() >= boundary else None
    if kind == 'interval':
        seconds = int(rule.get('minutes', 60)) * 60
        if not 1 <= seconds // 60 <= 525600:
            raise ValueError('Интервал: от 1 до 525600 минут')
        first = start.timestamp()
        return first + max(0, math.ceil((boundary - first) / seconds)) * seconds
    if kind not in ('daily', 'weekly'):
        raise ValueError('Неизвестный вид расписания')
    days = rule.get('days', list(range(7))) if kind == 'weekly' else list(range(7))
    if not days or any(type(day) is not int or not 0 <= day < 7 for day in days):
        raise ValueError('Выберите дни недели')
    current = datetime.fromtimestamp(boundary)
    date = max(start.date(), current.date())
    for offset in range(8):
        candidate = datetime.combine(date + timedelta(days=offset), start.time())
        if candidate.weekday() in days and candidate.timestamp() >= boundary:
            return candidate.timestamp()
    raise ValueError('Не удалось рассчитать следующий запуск')


SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL,rule TEXT NOT NULL,
 payload TEXT NOT NULL,next_at REAL,last_started REAL,last_finished REAL,status TEXT NOT NULL,
 errors INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,catch_up INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL,name TEXT NOT NULL,started REAL NOT NULL,finished REAL,
 status TEXT NOT NULL,errors INTEGER NOT NULL DEFAULT 0,warnings INTEGER NOT NULL DEFAULT 0,
 cancel INTEGER NOT NULL DEFAULT 0,scheduled_at REAL,owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS steps(
 run_id TEXT NOT NULL,number INTEGER NOT NULL,title TEXT NOT NULL,started REAL,finished REAL,
 status TEXT NOT NULL,errors INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(run_id,number));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,step INTEGER NOT NULL,at REAL NOT NULL,
 level TEXT NOT NULL,text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started DESC);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS results(run_id TEXT NOT NULL,step INTEGER NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(run_id,step));
'''


class SchedulerStore:
    def __init__(self, path, seal=protect, unseal=unprotect):
        self.path = Path(path)
        self.seal, self.unseal = seal, unseal

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('PRAGMA foreign_keys=ON')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    def save(self, name, rule, steps, enabled=True, job_id=None, catch_up=False, revision=None):
        from scheduler_actions import bundle_paths, validate_step
        steps = bundle_paths(steps)
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError('Название последовательности: от 1 до 120 символов')
        if not 1 <= len(steps) <= 100:
            raise ValueError('Добавьте от 1 до 100 действий')
        for step in steps:
            validate_step(step)
        next_at = next_time(rule, time.time(), inclusive=True)
        if enabled and next_at is None:
            raise ValueError('Время однократного запуска уже прошло')
        payload = json.dumps(self.seal({'json': json.dumps(steps, ensure_ascii=False)}))
        job_id = job_id or uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT revision FROM jobs WHERE id=?', (job_id,)).fetchone()
            if old and revision is not None and old['revision'] != revision:
                raise ValueError('Последовательность изменена в другом окне. Обновите её перед сохранением')
            if db.execute("SELECT 1 FROM runs WHERE job_id=? AND status='Выполняется'", (job_id,)).fetchone():
                raise ValueError('Нельзя менять выполняемую последовательность')
            if old:
                db.execute('UPDATE jobs SET name=?,enabled=?,rule=?,payload=?,next_at=?,catch_up=?,revision=revision+1 WHERE id=?',
                           (name, bool(enabled), json.dumps(rule), payload, next_at, bool(catch_up), job_id))
            else:
                db.execute('INSERT INTO jobs(id,name,enabled,rule,payload,next_at,status,catch_up) VALUES(?,?,?,?,?,?,?,?)',
                           (job_id, name, bool(enabled), json.dumps(rule), payload, next_at, 'Не запускалась', bool(catch_up)))
        return job_id

    def jobs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,name,enabled,rule,next_at,last_started,last_finished,status,errors,revision,catch_up FROM jobs ORDER BY name COLLATE NOCASE')]

    def load(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Последовательность больше не найдена')
        result = dict(row)
        result['steps'] = json.loads(self.unseal(json.loads(result.pop('payload')))['json'])
        result['rule'] = json.loads(result['rule'])
        return result

    def delete(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM runs WHERE job_id=? AND status='Выполняется'", (job_id,)).fetchone():
                raise ValueError('Сначала остановите запущенную последовательность')
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            db.execute('DELETE FROM requests WHERE job_id=?', (job_id,))

    def enable(self, job_id, value, revision=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT rule,revision FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Последовательность больше не найдена')
            if revision is not None and row['revision'] != revision:
                raise ValueError('Последовательность изменена в другом окне. Обновите её перед включением')
            rule = json.loads(row['rule'])
            now = time.time()
            following = next_time(rule, now, inclusive=True) if value else None
            if value and following is None:
                raise ValueError('Сохранённое время однократного запуска: ' + rule['start'] +
                                 '. Сейчас: ' + datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M') +
                                 '. Задайте будущее время, установите «Включена» и нажмите «Сохранить расписание».')
            db.execute('UPDATE jobs SET enabled=?,next_at=?,revision=revision+1 WHERE id=?', (bool(value), following, job_id))
            return dict(id=job_id, enabled=bool(value), next_at=following, revision=row['revision']+1)

    def request(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
                raise ValueError('Последовательность не найдена')
            if db.execute('SELECT 1 FROM requests WHERE job_id=?', (job_id,)).fetchone() or db.execute(
                    "SELECT 1 FROM runs WHERE job_id=? AND status='Выполняется'", (job_id,)).fetchone():
                raise ValueError('Эта последовательность уже запущена или ожидает запуска')
            db.execute('INSERT INTO requests VALUES(?,?,?)', (uuid4().hex, job_id, time.time()))

    def claim(self, owner, now=None):
        self.prune_due()
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # One chain at a time avoids overlapping restore/create operations.
            if db.execute("SELECT 1 FROM runs WHERE status='Выполняется'").fetchone():
                return None
            request = db.execute('SELECT * FROM requests ORDER BY created LIMIT 1').fetchone()
            job = db.execute('SELECT * FROM jobs WHERE id=?', (request['job_id'],)).fetchone() if request else db.execute(
                'SELECT * FROM jobs WHERE enabled=1 AND next_at<=? ORDER BY next_at LIMIT 1', (now,)).fetchone()
            if job is None:
                if request:
                    db.execute('DELETE FROM requests WHERE id=?', (request['id'],))
                return None
            following = next_time(json.loads(job['rule']), now)
            if request:
                db.execute('DELETE FROM requests WHERE id=?', (request['id'],))
                following = job['next_at']
            elif not job['catch_up'] and now - job['next_at'] > 300:
                db.execute('UPDATE jobs SET next_at=?,enabled=?,status=? WHERE id=?',
                           (following, following is not None, 'Пропущено время запуска', job['id']))
                run_id = uuid4().hex
                db.execute('INSERT INTO runs(id,job_id,name,started,finished,status,scheduled_at,owner) VALUES(?,?,?,?,?,?,?,?)',
                           (run_id, job['id'], job['name'], now, now, 'Пропущено', job['next_at'], owner))
                db.execute('INSERT INTO events(run_id,step,at,level,text) VALUES(?,?,?,?,?)',
                           (run_id, 0, now, 'warning', 'Пропущенный запуск не выполнялся: задержка более 5 минут. Включите «Запускать пропущенное», если это требуется.'))
                return None
            run_id = uuid4().hex
            db.execute('INSERT INTO runs(id,job_id,name,started,status,scheduled_at,owner) VALUES(?,?,?,?,?,?,?)',
                       (run_id, job['id'], job['name'], now, 'Выполняется', job['next_at'] if not request else None, owner))
            db.execute('UPDATE jobs SET next_at=?,enabled=?,last_started=?,last_finished=NULL,status=?,errors=0 WHERE id=?',
                       (following, bool(job['enabled']) and following is not None, now, 'Выполняется', job['id']))
            return dict(id=run_id, job_id=job['id'], name=job['name'], payload=job['payload'])

    def add_steps(self, run_id, steps):
        with self.db() as db:
            db.executemany('INSERT INTO steps VALUES(?,?,?,NULL,NULL,?,0)',
                           [(run_id, i, step['title'], 'Ожидание') for i, step in enumerate(steps, 1)])

    def step_state(self, run_id, number, status):
        with self.db() as db:
            column = 'started' if status == 'Выполняется' else 'finished'
            db.execute(f'UPDATE steps SET status=?,{column}=?,errors=CASE WHEN ?=\'Ошибка\' THEN MAX(errors,1) ELSE errors END WHERE run_id=? AND number=?',
                       (status, time.time(), status, run_id, number))

    def append(self, rows):
        if not rows:
            return
        with self.db() as db:
            db.executemany('INSERT INTO events(run_id,step,at,level,text) VALUES(?,?,?,?,?)', rows)
            for run_id, number, at, level, text in rows:
                if level in ('error', 'warning'):
                    field = 'errors' if level == 'error' else 'warnings'
                    db.execute(f'UPDATE runs SET {field}={field}+1 WHERE id=?', (run_id,))
                    if level == 'error':
                        db.execute('UPDATE steps SET errors=errors+1 WHERE run_id=? AND number=?', (run_id, number))

    def finish(self, run_id, status):
        now = time.time()
        with self.db() as db:
            row = db.execute('SELECT job_id,errors FROM runs WHERE id=?', (run_id,)).fetchone()
            db.execute('UPDATE runs SET status=?,finished=? WHERE id=?', (status, now, run_id))
            db.execute("UPDATE steps SET status=?,finished=? WHERE run_id=? AND status='Ожидание'", ('Пропущено', now, run_id))
            if row:
                db.execute('UPDATE jobs SET status=?,last_finished=?,errors=? WHERE id=?', (status, now, row['errors'], row['job_id']))
                job = db.execute('SELECT rule,next_at FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
                if job and job['next_at'] is not None and job['next_at'] <= now:
                    following = next_time(json.loads(job['rule']), now)
                    db.execute('UPDATE jobs SET next_at=?,enabled=CASE WHEN ? IS NULL THEN 0 ELSE enabled END WHERE id=?',
                               (following, following, row['job_id']))

    def interrupted(self):
        with self.db() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM runs WHERE status='Выполняется'")]
        for run_id in ids:
            self.append([(run_id, 0, time.time(), 'error', 'Исполнитель был остановлен без подтверждения результата. Проверьте состояние баз. Автоматический повтор отключён.')])
            with self.db() as db:
                db.execute("UPDATE steps SET status='Не завершено',finished=? WHERE run_id=? AND status='Выполняется'", (time.time(), run_id))
            self.finish(run_id, 'Не завершено')
            with self.db() as db:
                db.execute('UPDATE jobs SET enabled=0,next_at=NULL WHERE id=(SELECT job_id FROM runs WHERE id=?)', (run_id,))

    def cancel(self, run_id):
        with self.db() as db:
            db.execute("UPDATE runs SET cancel=1 WHERE id=? AND status='Выполняется'", (run_id,))

    def cancelled(self, run_id):
        with self.db() as db:
            row = db.execute('SELECT cancel FROM runs WHERE id=?', (run_id,)).fetchone()
            return bool(row and row[0])

    def history(self, limit=300):
        self.prune_due()
        with self.db() as db:
            runs = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY started DESC LIMIT ?', (limit,))]
            steps = [dict(row) for row in db.execute('SELECT steps.* FROM steps JOIN (SELECT id FROM runs ORDER BY started DESC LIMIT ?) r ON r.id=steps.run_id ORDER BY number', (limit,))]
            return runs, steps

    def prune_history(self, clear=False):
        cutoff=time.time()-7*24*60*60
        with self.db() as db:
            where='finished IS NOT NULL'+('' if clear else ' AND finished<?')
            args=() if clear else (cutoff,)
            ids=[row[0] for row in db.execute('SELECT id FROM runs WHERE '+where,args)]
            for identifier in ids:
                for table in ('events','steps','results'):db.execute('DELETE FROM '+table+' WHERE run_id=?',(identifier,))
                db.execute('DELETE FROM runs WHERE id=?',(identifier,))
        self._last_prune=time.time()

    def prune_due(self):
        if time.time()-getattr(self,'_last_prune',0)>600:self.prune_history()

    def journal(self, run_id, after=0, number=None, limit=500):
        with self.db() as db:
            where, args = 'run_id=? AND id>?', [run_id, after]
            if number is not None:
                where += ' AND step=?'
                args.append(number)
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE '+where+' ORDER BY id LIMIT ?', (*args, limit))]

    def result(self, run_id, number, value=None, artifacts=None):
        with self.db() as db:
            if artifacts is not None:
                content = json.dumps(dict(result=value, artifacts=artifacts), ensure_ascii=False, default=str)
                encrypted = json.dumps(self.seal({'json': content}))
                db.execute('INSERT OR REPLACE INTO results VALUES(?,?,?)', (run_id, number, encrypted))
                return
            row = db.execute('SELECT payload FROM results WHERE run_id=? AND step=?', (run_id, number)).fetchone()
            return json.loads(self.unseal(json.loads(row[0]))['json']) if row else None

    def meta(self, key, value=None):
        with self.db() as db:
            if value is not None:
                db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def health(self):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
            active = db.execute("SELECT name FROM runs WHERE status='Выполняется' ORDER BY started DESC LIMIT 1").fetchone()
            return (json.loads(row[0]) if row else {}), active[0] if active else ''
