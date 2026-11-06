"""Standard-library worker copied into an owned directory on a Linux host."""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import threading
import time
import re


class Cancelled(RuntimeError):
    pass


def execute(args, event, cancel, data=None, critical=False, env=None):
    if cancel.is_set():raise Cancelled('Операция отменена до следующего действия')
    process=subprocess.Popen([str(v) for v in args],stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,start_new_session=True)
    if data is not None:
        process.stdin.write(data.encode() if isinstance(data,str) else data);process.stdin.close()
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    output=bytearray();pending=b''
    def report(item):
        try:event(item)
        except (BrokenPipeError,ConnectionResetError):
            if not critical:raise
    try:
        while selector.get_map():
            if cancel.is_set() and not critical:
                import signal
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                raise Cancelled('Операция прервана')
            for key,_ in selector.select(.2):
                chunk=os.read(key.fileobj.fileno(),32768)
                if not chunk:selector.unregister(key.fileobj);continue
                output.extend(chunk);del output[:-131072]
                pending+=chunk
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1)
                    report(dict(kind='message',text=line.decode('utf-8',errors='replace')))
                if len(pending)>65536:
                    report(dict(kind='message',text=pending[:65536].decode('utf-8',errors='replace')));pending=pending[65536:]
        if pending:report(dict(kind='message',text=pending.decode('utf-8',errors='replace')))
        code=process.wait()
        if code:raise RuntimeError(Path(str(args[0])).name+': код '+str(code)+'. '+output.decode('utf-8',errors='replace')[-3000:])
        return output.decode('utf-8',errors='replace')
    finally:
        selector.close()
        if process.poll() is None:
            import signal
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()
        process.stdout.close()


def cleanup(root,token):
    root=Path(root)
    if not re.fullmatch(r'/tmp/ka-job-[0-9a-f]{32}',str(root)) or root.name!='ka-job-'+token:
        raise ValueError('Каталог SSH-задания не принадлежит консоли')
    if root.is_symlink() or root.resolve()!=root or (root/'.ka-owned').is_symlink() or (root/'.ka-owned').read_text()!=token:
        raise ValueError('Метка SSH-задания не совпадает')
    shutil.rmtree(root)


def read_request(stream,token):
    prefix=token+':request:'
    for _ in range(4):
        line=stream.readline(4*1024*1024)
        if not line:break
        if line.startswith(prefix):return json.loads(line[len(prefix):])
    raise ValueError('Не получены параметры SSH-задания')


def main():
    root=Path(__file__).resolve().parent
    token=(root/'.ka-owned').read_text()
    if not re.fullmatch('[0-9a-f]{32}',token):raise ValueError('Некорректная метка SSH-задания')
    payload=read_request(sys.stdin,token)
    payload['_root']=str(root)
    secrets=payload.pop('_secrets',[])
    cancel=threading.Event()
    def emit(item):
        # Redact values before serializing, so escaping cannot bypass masking.
        def clean(value):
            if isinstance(value,str):
                for secret in sorted((v for v in secrets if v),key=len,reverse=True):value=value.replace(secret,'[скрыто]')
            elif isinstance(value,dict):value={k:clean(v) for k,v in value.items()}
            elif isinstance(value,list):value=[clean(v) for v in value]
            return value
        print(json.dumps(clean(item),ensure_ascii=True),flush=True)
    def watch():
        for line in sys.stdin:
            if line.strip()==token+':cancel':cancel.set()
    threading.Thread(target=watch,daemon=True).start()
    try:
        import hashlib
        for file in payload.pop('_files',[]):
            path=root/file['name']
            if path.is_symlink() or path.resolve().parent!=root:raise ValueError('Неверный файл SSH-задания')
            with path.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
            if digest!=file['sha256']:raise ValueError('Повреждён переданный дистрибутив')
        sys.path.insert(0,str(root));sys.modules['ssh_job_runner']=sys.modules[__name__]
        import task
        emit(dict(kind='stage',text='SSH: среда выполнения готова'))
        result=task.run(payload,emit,cancel)
        emit(dict(kind='result',result=result))
    except Cancelled as exc:emit(dict(kind='cancelled',text=str(exc)))
    except Exception as exc:emit(dict(kind='error',text=str(exc)));raise SystemExit(1)
    finally:
        try:cleanup(root,token)
        except Exception as exc:emit(dict(kind='warning',text='Не удалены временные файлы SSH: '+str(root)+': '+str(exc)))


if __name__=='__main__':main()
