# -*- mode: python ; coding: utf-8 -*-


from pathlib import Path

pg_root = Path(SPECPATH) / 'vendor' / 'postgresql'
pg_data, pg_binaries = [], []
for file in pg_root.rglob('*'):
    if file.is_file():
        relative = file.parent.relative_to(Path(SPECPATH))
        (pg_binaries if file.suffix.lower() in ('.exe', '.dll') else pg_data).append((str(file), str(relative)))
for file in (Path(SPECPATH) / 'vendor' / '7zip').iterdir():
    if file.is_file():
        (pg_binaries if file.suffix.lower() in ('.exe', '.dll') else pg_data).append((str(file), 'vendor/7zip'))
for file in (Path(SPECPATH) / 'vendor' / 'ssh').rglob('*'):
    if file.is_file():
        pg_data.append((str(file), str(file.parent.relative_to(Path(SPECPATH)))))
for file in (Path(SPECPATH) / 'vendor' / 'python-linux').rglob('*'):
    if file.is_file():
        pg_data.append((str(file), str(file.parent.relative_to(Path(SPECPATH)))))
for file in (Path(SPECPATH) / 'vendor' / 'windirstat').rglob('*'):
    if file.is_file():
        # Keep the original executable and corresponding source bytes intact.
        pg_data.append((str(file), str(file.parent.relative_to(Path(SPECPATH)))))
for file in (Path(SPECPATH) / 'vendor' / '1c-ras').rglob('*'):
    if file.is_file():
        # Original official binaries and their dependency closure stay together.
        pg_data.append((str(file), str(file.parent.relative_to(Path(SPECPATH)))))

a = Analysis(
    ['1C Console.py'],
    pathex=[],
    binaries=pg_binaries,
    datas=[('create_users.ps1', '.'), ('archive_jr.ps1', '.'), ('postgres_runner.py', '.'), ('linux_platform_runner.py', '.'),
           ('ssh_job_runner.py','.'),('linux_disk_scan.py','.'),('linux_disk_delete.py','.'),('postgres_install_linux.py','.'),
           ('web_publication_linux.py','.'),('web_publication_windows.ps1','.'),
           ('postgres_access.py','.'),
           ('vendor/efd-format/LICENSE.txt','vendor/efd-format')] + pg_data,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Консоль администратора КА',
    icon=str(Path(SPECPATH) / 'assets' / 'admin_console.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    uac_admin=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
