"""Lifetime management for the temporary RAS processes owned by this app."""
import contextlib
import os
import socket
import subprocess
import tempfile
import threading
import time

START_TIMEOUT = 8


class SearchCancelled(RuntimeError):
    pass


@contextlib.contextmanager
def connect_with_fallback(manager, candidates, agent, probe, message=lambda text: None, warning=None):
    """Retry startup/read-only probes only. Never retry the caller's operation."""
    from ras_tools import tools_label, use_tools
    from postgres_runner import OperationCancelled
    errors = []
    ready = None
    for rac, ras, version in candidates:
        stack = contextlib.ExitStack()
        label = tools_label(ras, version)
        try:
            if manager.stop.is_set():
                raise SearchCancelled('Запрос 1С прерван')
            message('Проверка: ' + label)
            stack.enter_context(use_tools(ras))
            endpoint = stack.enter_context(manager.connect(ras, agent))
            clusters = probe(rac, endpoint)
        except (SearchCancelled, OperationCancelled):
            stack.close()
            raise
        except Exception as exc:
            stack.close()
            if manager.stop.is_set():
                raise SearchCancelled('Запрос 1С прерван') from exc
            detail = f'{label}: {exc}'
            errors.append(detail)
            # A failed read-only candidate is recoverable. Its native diagnostic
            # can contain "Ошибка"; callers must not count it as a failed task.
            (warning or message)('RAS недоступен; переход к следующему комплекту. ' + detail)
        else:
            ready = stack, rac, endpoint, version, clusters
            break
    if ready is None:
        raise RuntimeError('Не удалось подключиться к агенту 1С ' + agent + '.\n' + ('\n'.join(errors) if errors else
            'Нет доступных комплектов ras.exe/rac.exe. Обновите RAS в разделе «Установка 1С из ИТС».'))
    stack, rac, endpoint, version, clusters = ready
    with stack:
        message('Выбран ' + tools_label(ras, version))
        # Deliberately outside the retry handler: create/drop/update errors may
        # mean the server already changed state and must never be replayed.
        yield rac, endpoint, version, clusters


def decode_output(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp866", errors="replace")
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")


class LocalRasManager:
    def __init__(self, stop=None):
        self.stop = stop if stop is not None else threading.Event()
        self._lock = threading.Lock()
        self._processes = set()
        self._ports = set()
        self._closed = False

    def _check_cancelled(self):
        if self._closed or self.stop.is_set():
            raise SearchCancelled("Поиск остановлен")

    def _reserve_port(self):
        # Prevent this manager from allocating one port to concurrent launches.
        with self._lock:
            self._check_cancelled()
            for _ in range(100):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                if port not in self._ports:
                    self._ports.add(port)
                    return port
        raise RuntimeError("Не удалось выделить порт для локального RAS")

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                if process.poll() is None:
                    raise

    def close(self):
        """Stop only our processes; their owning workers perform wait/cleanup."""
        with self._lock:
            self._closed = True
            for process in self._processes:
                try:
                    self._terminate(process)
                except OSError:
                    # The owner will retry and report a cleanup failure.
                    pass

    def _wait_ready(self, process, port, output):
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            self._check_cancelled()
            code = process.poll()
            if code is not None:
                output.seek(0)
                detail = decode_output(output.read(4096)).strip()
                raise RuntimeError(f"Локальный RAS завершился (код {code}): {detail or 'без сообщения'}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            except OSError:
                self.stop.wait(0.1)
            else:
                # A port open alone does not guarantee the child is still alive.
                if process.poll() is None:
                    return
        raise RuntimeError(f"Локальный RAS не открыл порт {port} за {START_TIMEOUT} с")

    @contextlib.contextmanager
    def connect(self, ras_path, agent, env=None):
        port = self._reserve_port()
        process = None
        try:
            with tempfile.TemporaryFile() as output:
                try:
                    # Closing and launching are synchronized: no late orphan on exit.
                    with self._lock:
                        self._check_cancelled()
                        process = subprocess.Popen(
                            [str(ras_path), "cluster", f"--port={port}", agent],
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                            **({'env': env} if env is not None else {}),
                        )
                        self._processes.add(process)
                    self._wait_ready(process, port, output)
                    self._check_cancelled()
                    yield f"127.0.0.1:{port}"
                finally:
                    if process is not None:
                        try:
                            try:
                                self._terminate(process)
                            except OSError:
                                process.kill()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                        finally:
                            if process.poll() is not None:
                                with self._lock:
                                    self._processes.discard(process)
        finally:
            with self._lock:
                self._ports.discard(port)
