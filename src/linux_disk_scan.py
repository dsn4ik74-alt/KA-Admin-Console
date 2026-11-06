"""Read-only Linux disk walk. No links are followed and no files are changed."""
import heapq
import os
import selectors
import signal
import stat
import subprocess
import time

from ssh_job_runner import Cancelled


def file_identity(info):
    return dict(device=getattr(info,'st_dev',0),inode=getattr(info,'st_ino',0),mode=info.st_mode,
                mtime_ns=getattr(info,'st_mtime_ns',0),size=getattr(info,'st_size',0))


def quick_scan(path,cross,event,cancel):
    """GNU du walks descendants in C; Python handles only immediate children."""
    args=['du','--all','--max-depth=1','--block-size=1','--null','--no-dereference']
    if not cross:args.append('--one-file-system')
    process=subprocess.Popen([*args,'--',path],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,start_new_session=True,env=dict(os.environ,LC_ALL='C.UTF-8'))
    poll=selectors.DefaultSelector();poll.register(process.stdout,selectors.EVENT_READ,'output');poll.register(process.stderr,selectors.EVENT_READ,'error')
    rows=[];pending=b'';errors=bytearray();total=None;other=0;limited=False;last=time.monotonic()
    try:
        while poll.get_map():
            if cancel.is_set():raise Cancelled('Анализ диска прерван')
            for key,_ in poll.select(.2):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk:poll.unregister(key.fileobj);continue
                if key.data=='error':errors.extend(chunk);del errors[:-65536];continue
                pending+=chunk
                while b'\0' in pending:
                    line,pending=pending.split(b'\0',1)
                    amount,separator,name=line.partition(b'\t')
                    if not separator or not amount.isdigit():raise RuntimeError('Не удалось прочитать результат GNU du')
                    size=int(amount);name=os.fsdecode(name)
                    if name==path:total=size;continue
                    if len(rows)>=5000:other+=size;limited=True;continue
                    try:
                        info=os.lstat(name);is_directory=stat.S_ISDIR(info.st_mode);identity=file_identity(info)
                    except OSError:is_directory=False;identity=None
                    rows.append(dict(path=name,name=name.rsplit('/',1)[-1],directory=is_directory,size=size,apparent=None,files=None,identity=identity))
                if len(pending)>65536:raise RuntimeError('Слишком длинный ответ GNU du')
            if time.monotonic()-last>=1:
                event(dict(kind='stage',text='Быстрый обзор Linux: размеров получено '+str(len(rows))))
                last=time.monotonic()
        code=process.wait()
        if cancel.is_set():raise Cancelled('Анализ диска прерван')
        detail=bytes(errors).decode('utf-8',errors='replace').strip()
        if pending or total is None or code not in (0,1):
            raise RuntimeError('GNU du не завершил обзор. Выберите полный анализ. '+detail[-1200:])
        if detail:event(dict(kind='warning',text=detail[-4000:]))
        if limited:
            rows.append(dict(path=path,name='Остальные элементы',directory=False,size=other,apparent=None,files=None))
            event(dict(kind='warning',text='Первые 5000 элементов показаны отдельно; остальные сгруппированы.'))
        disk=os.statvfs(path)
        return dict(path=path,mode='quick',children=sorted(rows,key=lambda r:r['size'],reverse=True),largest=[],extensions=[],
                    size=total,files=None,skipped=None,partial=bool(code),capacity=disk.f_blocks*disk.f_frsize,available=disk.f_bavail*disk.f_frsize)
    finally:
        poll.close()
        if process.poll() is None:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()
        process.stdout.close();process.stderr.close()


def run(payload,event,cancel):
    path=payload.get('path','/')
    if not isinstance(path,str) or not path.startswith('/') or '\x00' in path:raise ValueError('Укажите абсолютный каталог Linux')
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):raise ValueError('Путь должен быть каталогом, а не ссылкой')
    if cancel.is_set():raise Cancelled('Анализ диска прерван')
    device=info.st_dev;cross=bool(payload.get('cross_filesystems',False))
    path=path.rstrip('/') or '/'
    if payload.get('mode')=='quick':return quick_scan(path,cross,event,cancel)
    totals={};extensions={};largest=[];largest_ids={};hardlinks=set();warnings=[];skipped=0;files=0;last=time.monotonic();limited=False
    visited={(info.st_dev,info.st_ino)}
    overflow='\x00other'
    def error(name,exc):
        nonlocal skipped
        skipped+=1
        if len(warnings)<30:warnings.append(name+': '+str(exc))
    stack=[(path,None)];checked=0
    def progress(directory):
        nonlocal last
        if time.monotonic()-last>=1:
            event(dict(kind='stage',text='Анализ Linux: файлов '+str(files)+', каталог '+directory))
            last=time.monotonic()
    while stack:
        if cancel.is_set():raise Cancelled('Анализ диска прерван')
        directory,group=stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel.is_set():raise Cancelled('Анализ диска прерван')
                    checked+=1
                    if checked%512==0:progress(directory)
                    child=entry.path;key=group or child
                    if group is None and len(totals)>=5000:
                        key=overflow;limited=True
                    try:
                        st=entry.stat(follow_symlinks=False)
                        row=totals.get(key)
                        if row is None:row=totals[key]=dict(path=path if key==overflow else key,
                            name='Остальные элементы' if key==overflow else entry.name if group is None else key.rsplit('/',1)[-1],
                            directory=False if key==overflow else stat.S_ISDIR(st.st_mode) if group is None else True,size=0,apparent=0,files=0,
                            identity=None if key==overflow else file_identity(st))
                        if stat.S_ISLNK(st.st_mode):continue
                        if stat.S_ISDIR(st.st_mode):
                            identity=(st.st_dev,st.st_ino)
                            if identity in visited:error(child,'каталог уже учтён; повторное подключение пропущено')
                            elif cross or st.st_dev==device:visited.add(identity);stack.append((child,key))
                            else:error(child,'другая файловая система; включите соответствующий флажок для её анализа')
                            continue
                        if not stat.S_ISREG(st.st_mode):continue
                        if st.st_nlink>1:
                            identity=(st.st_dev,st.st_ino)
                            if identity in hardlinks:continue
                            hardlinks.add(identity)
                        size=getattr(st,'st_blocks',0)*512
                        row['size']+=size;row['apparent']+=st.st_size;row['files']+=1;files+=1
                        stem,separator,extension=entry.name.rpartition('.')
                        suffix='.'+extension.casefold() if stem and separator and extension else '(без расширения)'
                        if suffix not in extensions and len(extensions)>=2000:suffix='(прочие)'
                        ext=extensions.setdefault(suffix,dict(extension=suffix,size=0,files=0));ext['size']+=size;ext['files']+=1
                        item=(size,child,st.st_size)
                        if len(largest)<1000:heapq.heappush(largest,item);largest_ids[child]=file_identity(st)
                        elif item>largest[0]:
                            previous=heapq.heapreplace(largest,item);largest_ids.pop(previous[1],None);largest_ids[child]=file_identity(st)
                    except OSError as exc:error(child,exc)
        except OSError as exc:error(directory,exc)
        progress(directory)
    disk=os.statvfs(path)
    if limited:event(dict(kind='warning',text='Показаны первые 5000 элементов каталога; остальные сгруппированы. Итоговый размер включает все прочитанные файлы.'))
    for text in warnings:event(dict(kind='warning',text=text))
    return dict(path=path,children=sorted(totals.values(),key=lambda r:r['size'],reverse=True),files=files,skipped=skipped,
                largest=[dict(size=size,path=name,apparent=apparent,identity=largest_ids[name]) for size,name,apparent in sorted(largest,reverse=True)],
                extensions=sorted(extensions.values(),key=lambda r:r['size'],reverse=True),
                size=sum(r['size'] for r in totals.values()),capacity=disk.f_blocks*disk.f_frsize,available=disk.f_bavail*disk.f_frsize)
