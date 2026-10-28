"""Pinned Linux interpreters bundled with the Windows console for SSH jobs."""
import hashlib
import json
from pathlib import Path
import sys


def runtime_for(machine):
    arch = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(machine.strip())
    if not arch:
        raise ValueError('Встроенный Python для Linux поддерживает x64 и ARM64. Архитектура ВМ: ' + machine.strip())
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'vendor' / 'python-linux'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    entry = manifest[arch]
    archive = root / entry['file']
    if archive.parent.resolve() != root.resolve() or not archive.is_file():
        raise RuntimeError('В сборке отсутствует переносимый Python Linux ' + arch)
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != entry['sha256']:
        raise RuntimeError('Повреждён встроенный архив Python Linux ' + arch)
    return dict(entry, path=str(archive), arch=arch)
