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
for filename in ('windows-x64.zip','linux-x64.tar.gz'):
    pg_data.append((str(Path(SPECPATH)/'vendor'/'ring'/filename),'vendor/ring'))

a = Analysis(
    ['src/1C Console.py'],
    pathex=[str(Path(SPECPATH) / 'src')],
    binaries=pg_binaries,
    datas=[('src/create_users.ps1', '.'), ('src/archive_jr.ps1', '.'), ('src/postgres_runner.py', '.'), ('src/linux_platform_runner.py', '.'),
           ('src/ssh_job_runner.py','.'),('src/linux_disk_scan.py','.'),('src/linux_disk_delete.py','.'),('src/postgres_install_linux.py','.'),
           ('src/web_publication_linux.py','.'),('src/web_publication_windows.ps1','.'),
           ('src/postgres_access.py','.'),
           ('src/ring_license.py','.'),('src/ring_license_linux.py','.'),('src/ring_license_windows.ps1','.'),
           ('src/app_update_windows.ps1','.'),
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
