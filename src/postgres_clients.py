"""Find the PostgreSQL client sets included with source or the frozen EXE."""
from pathlib import Path
from app_paths import resource_root


def bundled_tools(root=None):
    base = Path(root) if root is not None else resource_root() / 'vendor' / 'postgresql'
    result = {}
    if not base.is_dir():
        return result
    for directory in base.iterdir():
        if directory.is_dir() and directory.name.isdigit():
            tools = {name: str(directory / 'bin' / (name + '.exe')) for name in ('psql', 'pg_dump', 'pg_restore')}
            if all(Path(path).is_file() for path in tools.values()):
                result[int(directory.name)] = tools
    return result


def server_major(rows):
    if not rows:
        return None
    try:
        version = int(rows[0]['server_version'])
    except (KeyError, ValueError, TypeError):
        return None
    return version // 10000 if version >= 100000 else None
