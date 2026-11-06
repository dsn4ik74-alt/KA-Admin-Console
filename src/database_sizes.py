"""Read sizes separately so directory scans cannot delay the database picker."""
import threading
import time

from postgres_jobs import run_job
from postgres_runner import OperationCancelled
from sql_server import format_size


class DatabaseSizes:
    def __init__(self, app):
        self.app = app
        self.jobs = {}
        self.slots = threading.BoundedSemaphore(2)
        self.threads = set()
        self.lock = threading.Lock()

    def cancel(self, panel=None, role=None):
        for key, job in list(self.jobs.items()):
            if (panel is None or key[0] is panel) and (role is None or key[1] == role):
                job['cancel'].set()
                self.jobs.pop(key, None)

    def refresh(self, panel):
        started = False
        roles = ['source']
        if hasattr(panel, 'target_tree') and not panel.same_server.get():
            roles.append('target')
        for role in roles:
            target = role == 'target'
            current = panel.target_key() if target else panel.connection_key()
            connected = panel.target_connected if target else panel.connected
            rows = panel.target_databases if target else panel.databases
            names = [row['name'] for row in rows if row.get('sizes_pending')]
            if not names or connected != current:
                continue
            key = (panel, role)
            if key in self.jobs:
                continue
            settings = panel.connection()
            if target:
                settings.update(panel.target_connection())
            job = dict(cancel=threading.Event(), context=current)
            self.jobs[key] = job
            started = True
            thread = threading.Thread(target=self.worker, args=(key, job, settings, names), daemon=True)
            with self.lock:
                self.threads.add(thread)
            thread.start()
        if started:
            panel.append('Список баз готов. Размеры уточняются в фоне; выбор баз и запуск операций уже доступны.')

    def worker(self, key, job, settings, names):
        acquired = False
        result, error = {}, ''
        try:
            while not job['cancel'].is_set():
                if self.slots.acquire(timeout=.1):
                    acquired = True
                    break
            if not acquired or job['cancel'].is_set():
                return
            def receive(event):
                if event.get('kind') == 'database_sizes':
                    self.app._post(self.apply, key, job, event['rows'])
            receive.cancel_event = job['cancel']
            result = run_job(dict(mode='sizes', connection=settings, databases=names), receive)
        except OperationCancelled:
            pass
        except Exception as exc:
            error = str(exc)
        finally:
            if acquired:
                self.slots.release()
            self.app._post(self.finished, key, job, result, error)
            with self.lock:
                self.threads.discard(threading.current_thread())

    def valid(self, key, job):
        if self.jobs.get(key) is not job or job['cancel'].is_set():
            return False
        panel, role = key
        if not panel.winfo_exists():
            return False
        current = panel.target_key() if role == 'target' else panel.connection_key()
        connected = panel.target_connected if role == 'target' else panel.connected
        return current == connected == job['context']

    def apply(self, key, job, rows):
        if not self.valid(key, job):
            return
        panel, role = key
        sizes = {(row['name'], row.get('database_oid')): row.get('size_bytes') for row in rows}
        roles = [role]
        if role == 'source' and hasattr(panel, 'same_server') and panel.same_server.get():
            roles.append('target')
        for side in roles:
            source = panel.databases if side == 'source' else panel.target_databases
            updated = {}
            for row in source:
                identity = (row['name'], row.get('database_oid'))
                if identity in sizes:
                    row.update(size_bytes=sizes[identity], sizes_pending=False)
                    updated[row['name']] = sizes[identity]
            tree = panel.tree if side == 'source' else panel.target_tree
            sorter = panel.sorter if side == 'source' else panel.target_sorter
            # Keep row IDs, focus and scroll position while the user works.
            for item, values in (sorter.values or {}).items():
                if values.get('name') in updated:
                    values['size'] = updated[values['name']]
                    tree.set(item, 'size', format_size(values['size']))
            if sorter.column == 'size' and updated:
                position = tree.yview()[0]
                sorter.apply()
                tree.yview_moveto(position)

    def finished(self, key, job, result, error):
        if self.valid(key, job) and (error or result.get('unavailable')):
            key[0].append('ПРЕДУПРЕЖДЕНИЕ: часть размеров БД не получена. Список баз доступен; размеры можно обновить позднее.')
        if self.jobs.get(key) is job:
            self.jobs.pop(key, None)

    def close(self):
        self.cancel()
        deadline = time.monotonic() + .8
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
