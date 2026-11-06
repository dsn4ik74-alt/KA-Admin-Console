"""Verified GitHub releases and streaming binary patches for the frozen app."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPO = 'dsn4ik74-alt/KA-Admin-Console'
API = 'https://api.github.com/repos/' + REPO + '/releases/latest'
EXE_NAME = 'Консоль администратора КА.exe'
LIMIT = 2 * 1024**3
BLOCK = 1024 * 1024


class UpdateCancelled(Exception):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}', value):
        raise ValueError('Неизвестный формат версии обновления')
    parts = tuple(map(int, value.split('.')))
    return parts + (0,) * (4-len(parts))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checked(cancel):
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled('Обновление отменено')


def asset_info(item, target):
    name = item.get('name', '')
    url = item.get('browser_download_url', '')
    expected = 'https://github.com/' + REPO + '/releases/download/v' + target + '/' + name
    checksum = item.get('digest', '')
    if url != expected or not re.fullmatch(r'sha256:[0-9a-f]{64}', checksum):
        raise ValueError('У файла обновления нет подтверждённой контрольной суммы GitHub')
    size = item.get('size')
    if type(size) is not int or not 0 < size <= LIMIT:
        raise ValueError('Некорректный размер обновления')
    return dict(name=name, url=url, sha256=checksum[7:], size=size)


def parse_release(data, current):
    target = str(data.get('tag_name', '')).removeprefix('v')
    if data.get('draft') or data.get('prerelease') or version(target) <= version(current):
        return None
    assets = data.get('assets', [])
    full_name = 'KA-Admin-Console-' + target + '.zip'
    patch_name = 'KA-Admin-Console-' + current + '-to-' + target + '.kaupdate'
    full = next((row for row in assets if row.get('name') == full_name), None)
    patch = next((row for row in assets if row.get('name') == patch_name), None)
    if full is None:
        raise ValueError('В релизе нет установочного ZIP для Windows')
    return dict(version=target, notes=str(data.get('body', ''))[:24000],
                full=asset_info(full, target), patch=asset_info(patch, target) if patch else None)


def latest(current):
    request = urllib.request.Request(API, headers={'Accept':'application/vnd.github+json', 'User-Agent':'KA-Admin-Console'})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise ValueError('Ответ GitHub превышает допустимый размер')
        return parse_release(json.loads(raw), current)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise ValueError('GitHub временно ограничил запросы. Повторите проверку позже.') from exc
        raise ValueError('GitHub: ошибка HTTP ' + str(exc.code)) from exc


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
            raise ValueError('Неожиданный адрес загрузки обновления')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(asset, destination, progress, cancel=None):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Временный файл обновления уже существует')
    total = 0; checksum = hashlib.sha256(); last = 0
    try:
        request = urllib.request.Request(asset['url'], headers={'User-Agent':'KA-Admin-Console'})
        with urllib.request.build_opener(ReleaseRedirect()).open(request, timeout=30) as response, destination.open('xb') as output:
            while True:
                checked(cancel)
                data = response.read(BLOCK)
                if not data:break
                total += len(data)
                if total > asset['size']:raise ValueError('Размер загрузки не совпадает с релизом')
                output.write(data);checksum.update(data)
                if time.monotonic()-last > .15:
                    progress(total,asset['size']);last=time.monotonic()
        checked(cancel)
        if total != asset['size'] or checksum.hexdigest() != asset['sha256']:
            raise ValueError('Контрольная сумма скачанного обновления не совпала')
        progress(total,total)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def chunks(data):
    """Content boundaries found in C: offsets may shift without changing chunks."""
    offset = 0
    while offset < len(data):
        boundary = data.find(b'\x00\x00', offset+32768, min(len(data), offset+262144))
        end = min(len(data), offset+262144) if boundary < 0 else boundary+2
        yield offset, end-offset
        offset = end


def build_patch(source, target, destination, from_version, to_version):
    version(from_version);version(to_version)
    source,target,destination=map(Path,(source,target,destination))
    if not source.stat().st_size or not target.stat().st_size:raise ValueError('Empty executable')
    manifest=dict(schema=1,from_version=from_version,to_version=to_version,
                  source_sha256=digest(source),target_sha256=digest(target),source_size=source.stat().st_size,
                  target_size=target.stat().st_size,operations=[])
    with source.open('rb') as old, target.open('rb') as new, tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        with mmap.mmap(old.fileno(),0,access=mmap.ACCESS_READ) as before, mmap.mmap(new.fileno(),0,access=mmap.ACCESS_READ) as after:
            index={hashlib.sha256(before[pos:pos+size]).digest():(pos,size) for pos,size in chunks(before)}
            payload=Path(folder)/'blocks.bin';offset=0
            with payload.open('wb') as output:
                for pos,size in chunks(after):
                    data=after[pos:pos+size];match=index.get(hashlib.sha256(data).digest())
                    if match and match[1]==size:operation=['copy',*match]
                    else:
                        operation=['data',offset,size];output.write(data);offset+=size
                    previous=manifest['operations'][-1] if manifest['operations'] else None
                    if previous and previous[0]==operation[0] and previous[1]+previous[2]==operation[1]:previous[2]+=size
                    else:manifest['operations'].append(operation)
            with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
                archive.writestr('patch.json',json.dumps(manifest,separators=(',',':')))
                archive.write(payload,'blocks.bin')
    return manifest


def apply_patch(source, patch, destination, current, target, progress=lambda done,total:None, cancel=None):
    source,patch,destination=map(Path,(source,patch,destination))
    if destination.exists():raise ValueError('Файл результата уже существует')
    try:
        with zipfile.ZipFile(patch) as archive:
            if sorted(archive.namelist()) != ['blocks.bin','patch.json'] or archive.getinfo('patch.json').file_size>4*1024*1024:
                raise ValueError('Неверный формат патча')
            manifest=json.loads(archive.read('patch.json'))
            if manifest.get('schema')!=1 or manifest.get('from_version')!=current or manifest.get('to_version')!=target:
                raise ValueError('Патч предназначен для другой версии')
            size=manifest.get('target_size');old_size=manifest.get('source_size')
            if type(size) is not int or not 0<size<=LIMIT or type(old_size) is not int or old_size!=source.stat().st_size:
                raise ValueError('Некорректный размер EXE в патче')
            if digest(source)!=manifest.get('source_sha256'):
                raise ValueError('Установленный EXE отличается от исходной версии патча. Выберите полное обновление.')
            data_size=archive.getinfo('blocks.bin').file_size
            operations=manifest.get('operations',[])
            if not isinstance(operations,list) or len(operations)>100000 or data_size>size:
                raise ValueError('Некорректный список блоков')
            done=position=0;checksum=hashlib.sha256();last=0
            with source.open('rb') as before, archive.open('blocks.bin') as data, destination.open('xb') as output:
                for operation in operations:
                    checked(cancel)
                    if not isinstance(operation,list) or len(operation)!=3:raise ValueError('Неверный блок патча')
                    kind,start,length=operation
                    if type(start) is not int or type(length) is not int or start<0 or length<=0 or done+length>size:
                        raise ValueError('Некорректные границы блока')
                    if kind=='copy' and start+length<=old_size:before.seek(start);stream=before
                    elif kind=='data' and start==position and start+length<=data_size:stream=data;position+=length
                    else:raise ValueError('Недопустимый источник блока')
                    remaining=length
                    while remaining:
                        checked(cancel);block=stream.read(min(BLOCK,remaining))
                        if not block:raise ValueError('Патч обрезан')
                        output.write(block);checksum.update(block);done+=len(block);remaining-=len(block)
                        if time.monotonic()-last>.15:progress(done,size);last=time.monotonic()
                output.flush();os.fsync(output.fileno())
            checked(cancel)
            if done!=size or position!=data_size or checksum.hexdigest()!=manifest.get('target_sha256'):
                raise ValueError('Проверка собранного EXE не пройдена')
            progress(done,size)
            return checksum.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def unpack_full(package,destination,progress,cancel=None):
    destination=Path(destination)
    if destination.exists():raise ValueError('Файл результата уже существует')
    try:
        with zipfile.ZipFile(package) as archive:
            rows=archive.infolist()
            if len(rows)!=1 or rows[0].filename!=EXE_NAME or not 0<rows[0].file_size<=LIMIT:
                raise ValueError('ZIP должен содержать один EXE приложения')
            done=0;checksum=hashlib.sha256()
            with archive.open(rows[0]) as source,destination.open('xb') as output:
                while True:
                    checked(cancel);data=source.read(BLOCK)
                    if not data:break
                    output.write(data);checksum.update(data);done+=len(data);progress(done,rows[0].file_size)
                output.flush();os.fsync(output.fileno())
        return checksum.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def stage_update(release,asset,executable,current,progress,cancel=None):
    folder=Path(tempfile.mkdtemp(prefix='ka-update-'+uuid.uuid4().hex+'-'))
    try:
        if shutil.disk_usage(folder).free < Path(executable).stat().st_size*2+asset['size']+50*1024**2:
            raise ValueError('Недостаточно места во временной папке для сборки нового EXE')
        (folder/'ka-update.marker').write_text('KA_UPDATE_1',encoding='ascii')
        package=folder/'download';new=folder/EXE_NAME
        download(asset,package,lambda done,total:progress('Скачивание',done,total),cancel)
        callback=lambda done,total:progress('Сборка и проверка EXE',done,total)
        if asset['name'].endswith('.kaupdate'):
            checksum=apply_patch(executable,package,new,current,release['version'],callback,cancel)
        else:checksum=unpack_full(package,new,callback,cancel)
        with new.open('rb') as stream:
            if stream.read(2)!=b'MZ':raise ValueError('Обновление не содержит Windows EXE')
        package.unlink()
        return dict(folder=str(folder),executable=str(new),sha256=checksum,version=release['version'])
    except BaseException:
        shutil.rmtree(folder)
        raise


def cleanup(stage):
    if not stage:return
    folder=Path(stage['folder'])
    if not folder.exists():return
    if folder.name.startswith('ka-update-') and (folder/'ka-update.marker').read_text(encoding='ascii')=='KA_UPDATE_1':
        shutil.rmtree(folder)


def launch_helper(stage,executable,resume_scheduler=False):
    folder=Path(stage['folder']);config=folder/'update.json'
    target=Path(executable).resolve()
    if target.suffix.lower()!='.exe' or Path(stage['executable']).parent!=folder:
        raise ValueError('Неверный путь обновляемого приложения')
    if shutil.disk_usage(target.parent).free<Path(stage['executable']).stat().st_size+20*1024**2:
        raise ValueError('Недостаточно места в папке приложения')
    payload=dict(stage,target=str(target),pid=os.getpid(),source_sha256=digest(target),resume_scheduler=resume_scheduler)
    config.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    script=folder/'apply.ps1'
    shutil.copyfile(Path(__file__).with_name('app_update_windows.ps1'),script)
    system=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    return subprocess.Popen([str(system),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                            '-WindowStyle','Hidden','-File',str(script),'-Config',str(config)],
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
