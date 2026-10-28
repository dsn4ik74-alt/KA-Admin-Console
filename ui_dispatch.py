"""Bounded worker-to-Tk delivery; workers never call Tk directly."""
from collections import deque
import threading


class JobEvents:
    def __init__(self, post, consume, limit=4096, byte_limit=4 * 1024 * 1024):
        self.post, self.consume = post, consume
        self.limit, self.byte_limit = limit, byte_limit
        self._items = deque()
        self._lock = threading.Lock()
        self._scheduled = self._closed = False
        self._size = self._omitted = 0

    def emit(self, event):
        event = dict(event)
        size = len(str(event.get('text', '')).encode('utf-8', errors='replace'))
        with self._lock:
            if self._closed:
                return
            # Keep only the newest consecutive measurement of the same phase.
            if (event.get('kind') == 'progress' and self._items
                    and self._items[-1][0] == 'event'
                    and self._items[-1][1].get('kind') == 'progress'
                    and self._items[-1][1].get('phase') == event.get('phase')):
                self._size -= self._items.pop()[2]
            self._items.append(('event', event, size))
            self._size += size
            while len(self._items) > self.limit or self._size > self.byte_limit:
                # Prefer retaining stages; surviving events keep their order.
                index = next((i for i, item in enumerate(self._items)
                              if item[0] == 'event' and item[1].get('kind') != 'stage'), None)
                if index is None:
                    index = 0
                item = self._items[index]
                del self._items[index]
                self._size -= item[2]
                self._omitted += item[1].get('kind') != 'progress'
            self._schedule()

    def complete(self, callback, *args):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._items.append(('complete', (callback, args), 0))
            self._schedule()

    def _schedule(self):
        if not self._scheduled:
            self._scheduled = True
            self.post(self.flush)

    def flush(self):
        batch, terminal, size = [], None, 0
        with self._lock:
            if self._omitted:
                batch.append({'kind': 'log', 'text':
                              f'Ранние строки скрыты из журнала: {self._omitted}. Ниже — последние сообщения.'})
                self._omitted = 0
            while self._items and len(batch) < 128 and size < 65536:
                kind, value, length = self._items.popleft()
                self._size -= length
                if kind == 'complete':
                    terminal = value
                    break
                batch.append(value)
                size += length
        try:
            if batch:
                self.consume(batch)
        finally:
            try:
                if terminal is not None:
                    callback, args = terminal
                    callback(*args)
            finally:
                with self._lock:
                    self._scheduled = False
                    if self._items:
                        self._schedule()
