"""UI-side elapsed time and a bounded estimate from observed progress only."""
from collections import deque
import math
import re
import time


PERCENT = re.compile(r'^\s*(\d{1,3}(?:[.,]\d+)?)\s*(?:percent|проц(?:\.|ент(?:ов|а)?)?|%)', re.I | re.M)


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


class ProgressState:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.start()

    def start(self):
        self.started = self.clock()
        self.complete_task = False
        self.measured = False
        self.stage('')

    def stage(self, phase):
        self.phase, self.unit, self.estimated, self.percent = phase, 'этапа', False, None
        self.samples = deque([(self.clock(), 0.0)], maxlen=10)

    def update(self, percent, unit='этапа', estimated=False, phase=''):
        percent = float(percent)
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            return
        if (phase and phase != self.phase) or (self.percent is not None and percent < self.percent):
            self.stage(phase or self.phase)
        if self.percent is None and percent > 5:
            self.samples.clear()
            self.samples.append((self.clock(), percent))
        self.percent, self.unit, self.estimated = percent, unit, estimated
        self.measured = True
        if percent > self.samples[-1][1]:
            self.samples.append((self.clock(), percent))

    def remaining(self):
        if self.percent is None or not 0 < self.percent < 100 or len(self.samples) < 2:
            return None
        first, last = self.samples[0], self.samples[-1]
        elapsed, change = last[0] - first[0], last[1] - first[1]
        gap = self.clock() - last[0]
        if elapsed < 3 or change <= 0 or gap > max(30, min(300, 3 * elapsed / (len(self.samples) - 1))):
            return None
        seconds = (100 - last[1]) * elapsed / change - gap
        return seconds if seconds > 0 else None

    def text(self):
        elapsed = 'Прошло ' + duration(self.clock() - self.started)
        if self.percent is None:
            return 'Осталось: неизвестно\n' + elapsed
        number = f'{self.percent:.1f}'.rstrip('0').rstrip('.').replace('.', ',')
        prefix = '≈' if self.estimated else ''
        eta = self.remaining()
        return f'{prefix}{number}% {self.unit}\n' + ('Осталось ≈' + duration(eta) if eta is not None else 'Время уточняется') + ' · ' + elapsed

    def finished_text(self, success):
        elapsed = 'Прошло ' + duration(self.clock() - self.started)
        if not success:
            return 'Не завершено\n' + elapsed
        return ('100% · завершено\n' if self.complete_task or self.measured else '') + elapsed
