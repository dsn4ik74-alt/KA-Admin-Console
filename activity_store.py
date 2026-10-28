"""Per-user activity store; favorite credentials use DPAPI, logs exclude secrets."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import os
import threading
import time
from uuid import uuid4
from favorite_secrets import protect, unprotect
from operation_logs import OperationLogs


PROFILE_FIELDS = {
    'sql': {'server', 'username', 'windows_auth', 'encrypt', 'trust_certificate'},
    'postgres': {'execution', 'host', 'port', 'username', 'sslmode', 'ssh_host', 'ssh_port',
                 'ssh_user', 'ssh_key', 'ssh_use_key', 'dt_keep_remote', 'client_set', 'data_path', 'ib_user', 'manual_database',
                 'target_host', 'target_port', 'target_user', 'target_ssl', 'same_server'},
    'cluster': {'server', 'port', 'version', 'cluster', 'username', 'ib_user', 'db_server', 'dbms', 'db_user'},
    'ssh': {'host', 'port', 'user', 'use_key', 'key'},
}
SECRET_FIELDS = {'sql': {'password'}, 'postgres': {'password', 'ib_password', 'target_password', 'ssh_password'},
                 'cluster': {'password', 'ib_password', 'db_password'}, 'ssh': {'password', 'sudo_password'}}
PROFILE_PAGES = {
    'sql': {'db_backup', 'db_restore', 'db_copy'},
    'postgres': {'pg_dump', 'pg_restore', 'pg_copy', 'ibcmd_dump', 'ibcmd_restore'},
    'cluster': {'cluster_infobase'},
    'ssh': {'linux_disk'},
}


def clean_profile(profile):
    kind = profile.get('kind')
    if kind not in PROFILE_FIELDS or profile.get('page') not in PROFILE_PAGES[kind]:
        raise ValueError('Неизвестный тип подключения')
    values = {key: value for key, value in profile.get('values', {}).items()
              if key in PROFILE_FIELDS[kind] and isinstance(value, (str, bool, int))}
    for key, value in list(values.items()):
        if key in ('same_server', 'ssh_use_key', 'dt_keep_remote', 'use_key'):
            values[key] = value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')
        elif key not in ('windows_auth', 'encrypt', 'trust_certificate'):
            values[key] = str(value)
    if kind == 'postgres' and values.get('execution') == 'SSH · Windows':
        values['execution'] = 'Локально · Windows'
    if kind == 'postgres' and 'ssh_use_key' not in values:
        # Profiles saved before password authentication used SSH keys exclusively.
        values['ssh_use_key'] = str(values.get('execution', '')).startswith('SSH')
    if kind == 'ssh':
        values.setdefault('port', '22')
        values.setdefault('use_key', False)
        if not values['use_key']:
            values['key'] = ''
    return dict(kind=kind, page=profile['page'], values=values)


def context_key(profile, role='source'):
    profile = clean_profile(profile)
    values = dict(profile['values'])
    if profile['kind'] == 'postgres':
        if role == 'target' and not values.get('same_server', False):
            for key, target in (('host', 'target_host'), ('port', 'target_port'),
                                ('username', 'target_user'), ('sslmode', 'target_ssl')):
                values[key] = values.get(target, '')
        values = {key: values.get(key, '') for key in
                  ('execution', 'host', 'port', 'username', 'sslmode', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_key')}
        if not str(values.get('execution', '')).startswith('SSH'):
            for key in ('ssh_host', 'ssh_port', 'ssh_user', 'ssh_key'):
                values[key] = ''
    elif profile['kind'] == 'cluster':
        values = {key: value for key, value in values.items()
                  if key in ('server', 'port', 'version', 'cluster', 'username')}
    return json.dumps([profile['kind'], values], ensure_ascii=False, sort_keys=True)


class ActivityStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.logs = OperationLogs(self.path.with_suffix('.logs') if self.path else None)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._closing = False
        self._worker = None
        self._dirty = False
        self.error = ''
        self.revision = 0
        self.data = dict(favorites=[], recent=[], history=[], connections=[])
        if self.path and self.path.is_file():
            try:
                if self.path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('Файл истории превышает 8 МиБ')
                stored = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(stored, dict):
                    raise ValueError('Ожидался объект избранного и истории')
                skipped = 0
                for section, limit in (('favorites', 100), ('recent', 100), ('history', 500), ('connections', 100)):
                    items = stored.get(section, [])
                    if not isinstance(items, list):
                        skipped += 1
                        continue
                    for item in items[:limit]:
                        try:
                            if not isinstance(item, dict):
                                raise ValueError('Повреждённая запись')
                            if section in ('favorites', 'connections'):
                                row = dict(id=str(item['id']), name=str(item['name'])[:100], **clean_profile(item))
                                envelope = item.get('credentials')
                                if isinstance(envelope, dict) and envelope.get('scheme') == 'dpapi-user-v1' and isinstance(envelope.get('data'), str):
                                    row['credentials'] = dict(scheme=envelope['scheme'], data=envelope['data'])
                            elif section == 'recent':
                                profile = clean_profile(item['profile'])
                                role = 'target' if item.get('role') == 'target' else 'source'
                                row = dict(profile=profile, context=context_key(profile, role),
                                           database=str(item['database']), role=role, at=str(item.get('at', '')))
                            else:
                                row = self._history_fields(item)
                                if row.get('status') == 'Выполняется':
                                    row['status'] = 'Не завершено'
                            self.data[section].append(row)
                        except (ValueError, KeyError, TypeError, AttributeError):
                            skipped += 1
                if skipped:
                    self.error = f'Пропущены повреждённые записи избранного и истории: {skipped}. Остальные записи загружены.'
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.error = 'Не удалось прочитать избранное и историю: ' + str(exc)

    def snapshot(self, section, limit=None):
        with self._lock:
            rows = self.data[section]
            return deepcopy(rows if limit is None else rows[:limit])

    def _changed(self):
        self.revision += 1
        if self.path and not self._closing:
            self._dirty = True
            if self._worker is None:
                self._worker = threading.Thread(target=self._write_loop, daemon=True)
                self._worker.start()
            self._wake.set()

    def _write_loop(self):
        while True:
            awakened=self._wake.wait(timeout=600)
            self._wake.clear()
            if not awakened:
                try:self.logs.prune();self.prune_history()
                except OSError as exc:self.error='Не удалось очистить старые журналы: '+str(exc)
            with self._lock:
                if not self._dirty:
                    if self._closing:
                        return
                    continue
                payload = json.dumps(self.data, ensure_ascii=False, indent=2)
                self._dirty = False
            temporary = self.path.with_name(self.path.name + '.' + uuid4().hex + '.tmp')
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open('w', encoding='utf-8') as file:
                    file.write(payload)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
                self.error = ''
            except OSError as exc:
                self.error = 'Не удалось сохранить избранное и историю: ' + str(exc)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                if self._closing and not self._dirty:
                    return
                if self._dirty:
                    self._wake.set()

    def close(self, wait=False):
        with self._lock:
            self._closing = True
            self._wake.set()
        if wait and self._worker:
            self._worker.join(timeout=5)

    def save_favorite(self, name, profile, favorite_id=None):
        incoming = profile
        profile = clean_profile(incoming)
        secrets = {key: value for key, value in incoming.get('values', {}).items()
                   if key in SECRET_FIELDS[profile['kind']] and isinstance(value, str)}
        # Encrypt before changing the saved profile. Fail without a plaintext fallback.
        credentials = protect(secrets) if secrets else deepcopy(incoming.get('credentials'))
        name = name.strip()[:100]
        if not name:
            raise ValueError('Введите название подключения')
        with self._lock:
            existing = next((item for item in self.data['favorites']
                             if item['name'] == name and item['kind'] == profile['kind'] and item['page'] == profile['page']), None)
            identifier = favorite_id or (existing['id'] if existing else uuid4().hex)
            if credentials is None:
                previous = next((item for item in self.data['favorites'] if item['id'] == identifier), None)
                credentials = deepcopy(previous.get('credentials')) if previous else None
            self.data['favorites'] = [item for item in self.data['favorites'] if item['id'] != identifier]
            row = dict(id=identifier, name=name, **profile)
            if isinstance(credentials, dict):
                row['credentials'] = credentials
            self.data['favorites'].insert(0, row)
            del self.data['favorites'][100:]
            self._changed()
            return identifier

    def credentials(self, favorite):
        envelope = favorite.get('credentials')
        if not envelope:
            return {}
        values = unprotect(envelope)
        return {key: value for key, value in values.items() if key in SECRET_FIELDS[favorite['kind']]}

    def remove_favorite(self, identifier):
        with self._lock:
            self.data['favorites'] = [item for item in self.data['favorites'] if item['id'] != identifier]
            self._changed()

    def save_connection(self, profile):
        """Remember a successful endpoint; replace credentials for the same login."""
        cleaned = clean_profile(profile)
        if cleaned['kind'] not in ('sql', 'postgres', 'ssh'):
            raise ValueError('Поддерживаются подключения SQL Server, PostgreSQL и SSH')
        values = cleaned['values']
        keys = (('server', 'windows_auth', 'username') if cleaned['kind'] == 'sql' else
                ('host', 'port', 'user', 'use_key', 'key') if cleaned['kind'] == 'ssh' else
                ('execution', 'host', 'port', 'username', 'ssh_host', 'ssh_port', 'ssh_user', 'ssh_key'))
        context = [cleaned['kind'], [values.get(key, '') for key in keys]]
        identifier = json.dumps(context, ensure_ascii=False)
        secrets = {key: value for key, value in profile['values'].items()
                   if key in SECRET_FIELDS[cleaned['kind']] and isinstance(value, str)}
        with self._lock:
            if cleaned['kind'] == 'ssh':
                previous = next((row for row in self.data['connections'] if row['id'] == identifier), None)
                if previous:
                    # PostgreSQL SSH forms have no sudo field. Reusing them
                    # must not erase a separately saved sudo password.
                    secrets = {**self.credentials(previous), **secrets}
            credentials = protect(secrets)
            self.data['connections'] = [row for row in self.data['connections'] if row['id'] != identifier]
            self.data['connections'].insert(0, dict(id=identifier, name=str(values.get('server', values.get('host', ''))),
                                                   **cleaned, credentials=credentials))
            del self.data['connections'][100:]
            self._changed()
        return identifier

    def remove_connection(self, identifier):
        with self._lock:
            previous = len(self.data['connections'])
            self.data['connections'] = [row for row in self.data['connections'] if row['id'] != identifier]
            if len(self.data['connections']) != previous:
                self._changed()
                return True
            return False

    def remember(self, profile, database, role='source'):
        if not database:
            return
        profile = clean_profile(profile)
        key = context_key(profile, role)
        with self._lock:
            if (self.data['recent'] and (self.data['recent'][0]['context'], self.data['recent'][0]['database'],
                                         self.data['recent'][0]['role']) == (key, database, role)):
                return
            self.data['recent'] = [row for row in self.data['recent']
                                   if (row['context'], row['database'], row['role']) != (key, database, role)]
            self.data['recent'].insert(0, dict(context=key, profile=profile, database=str(database), role=role,
                                              at=datetime.now().astimezone().isoformat(timespec='seconds')))
            del self.data['recent'][100:]
            self._changed()

    @staticmethod
    def _history_fields(row):
        result = {key: row[key] for key in ('id', 'started', 'operation', 'server', 'database', 'target',
                  'executor', 'status', 'duration', 'artifacts', 'path') if key in row}
        for key in ('id', 'started', 'operation', 'server', 'database', 'target', 'executor', 'status', 'path'):
            if key in result:
                result[key] = str(result[key])[:8192]
        result.setdefault('id', uuid4().hex)
        result.setdefault('status', 'Неизвестно')
        try:
            elapsed = float(result.get('duration', 0))
            result['duration'] = max(0, elapsed) if math.isfinite(elapsed) else 0
        except (TypeError, ValueError, OverflowError):
            result['duration'] = 0
        if not isinstance(result.get('artifacts', []), list):
            result['artifacts'] = []
        result['artifacts'] = [dict(path=str(item['path']), size=item.get('size') if isinstance(item.get('size'), int) else None)
                               for item in result.get('artifacts', []) if isinstance(item, dict) and item.get('path')]
        return result

    def begin(self, context):
        self.prune_history()
        with self._lock:
            identifier = uuid4().hex
            row = self._history_fields(context)
            row.update(id=identifier, started=datetime.now().astimezone().isoformat(timespec='seconds'),
                       status='Выполняется', duration=0)
            self.data['history'].insert(0, row)
            del self.data['history'][500:]
            self._changed()
            return identifier, time.monotonic()

    def artifact(self, token, event):
        with self._lock:
            row = next((row for row in self.data['history'] if row['id'] == token[0]), None)
            if row and event.get('path'):
                artifacts = row.setdefault('artifacts', [])
                previous = next((item.get('size') for item in artifacts if item['path'] == event['path']), None)
                artifacts[:] = [item for item in artifacts if item['path'] != event['path']]
                size = event.get('size') if event.get('size') is not None else previous
                artifacts.append(dict(path=str(event['path']), size=size if isinstance(size, int) and size >= 0 else None))
                self._changed()

    def end(self, token, status):
        with self._lock:
            row = next((row for row in self.data['history'] if row['id'] == token[0]), None)
            if row:
                row.update(status=status, duration=max(0, time.monotonic() - token[1]))
                self._changed()

    def clear_history(self):
        with self._lock:
            keep=[row for row in self.data['history'] if row['status']=='Выполняется']
            self.logs.prune(clear=True,keep=[row['id'] for row in keep])
            self.data['history']=keep
            self._changed()

    def prune_history(self):
        cutoff=time.time()-7*24*60*60
        def expired(row):
            if row.get('status')=='Выполняется':return False
            try:return datetime.fromisoformat(row['started']).timestamp()+float(row.get('duration',0))<cutoff
            except (KeyError,ValueError,TypeError,OverflowError):return False
        with self._lock:
            rows=[row for row in self.data['history'] if not expired(row)]
            if len(rows)!=len(self.data['history']):self.data['history']=rows;self._changed()
