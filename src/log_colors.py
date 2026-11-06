"""Incremental severity coloring shared by operation journals."""
import re


COLORS = {'info': '#75ed91', 'warning': '#ffda6a', 'error': '#ff7878'}
ERROR = re.compile(r'(?i)(?<!\w)(?:error|fatal|panic|failed|ошибка|ошибки|сбой)(?!\w)|'
                   r'недостаточно прав|доступ запрещ[её]н|permission denied|access is denied|'
                   r'подключение не установлено|соединение отклонено|connection refused')
WARNING = re.compile(r'(?i)(?<!\w)(?:warnings?|предупреждени\w*|внимание)(?!\w)')
LEVEL = re.compile(r'(?i)(?:severity|level|уровень)\s*[:=]?\s*(\d+)')


def severity(line):
    if ERROR.search(line):
        return 'error'
    if WARNING.search(line):
        return 'warning'
    match = LEVEL.search(line)
    if match and int(match[1]) >= 11:
        return 'error'
    return 'info'


def configure_log(widget):
    widget.configure(bg='#0b0f0c', fg=COLORS['info'], selectbackground='#20462d', selectforeground='#c9ffd6')
    for name, color in COLORS.items():
        widget.tag_configure(name, foreground=color)


def insert_log(widget, text, level=None):
    """Insert each run in one Tcl call; never rescan the existing large journal."""
    runs = []
    previous, parts = None, []
    for line in str(text).splitlines(keepends=True):
        current = level or severity(line)
        if current == previous:
            parts.append(line)
        else:
            if parts:
                runs.extend((''.join(parts), (previous,)))
            parts = [line]
            previous = current
    if parts:
        runs.extend((''.join(parts), (previous,)))
    if runs:
        widget.insert('end', *runs)
