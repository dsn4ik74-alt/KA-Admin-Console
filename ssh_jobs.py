"""Isolated, password-safe transport for installer and disk-analysis workers."""
import hashlib
import json
from pathlib import Path
import shlex
import time
from uuid import uuid4

from linux_platform import command
from linux_python_runtime import runtime_for
from postgres_runner import OperationCancelled
from ssh_connection import connect


def run_script(module,values,payload,event,cancel,files=(),elevate=False):
    if module not in ('postgres_install_linux','linux_disk_scan','linux_disk_delete','web_publication_linux'):raise ValueError('Неизвестный исполнитель SSH')
    if any(c in (values.get('sudo_password') or values.get('password','')) for c in '\r\n\x00'):raise ValueError('Пароль SSH / sudo не должен содержать переносы строк')
    token=uuid4().hex;root='/tmp/ka-job-'+token
    with connect(values) as client:
        prefix=[];password=''
        if elevate and command(client,['id','-u'],cancel=cancel).strip()!='0':
            password=values.get('sudo_password') or (values.get('password','') if not values.get('use_key') else '')
            prefix=['sudo','-S','-p','','--']
            command(client,['sudo','-S','-p','','-v'],password,cancel=cancel)
        machine=command(client,['uname','-m'],cancel=cancel).strip()
        runtime=runtime_for(machine)
        event(dict(kind='stage',text='Подготовка встроенного Python на Linux'))
        owned=False;started=False;completed=False;channel=None;stdin=None
        try:
            with client.open_sftp() as sftp:
                sftp.mkdir(root,mode=0o700);owned=True
                with sftp.open(root+'/.ka-owned','w') as stream:stream.write(token)
                source_root=Path(__file__).resolve().parent
                last_progress=[0.]
                def progress(done,total):
                    if cancel.is_set():raise OperationCancelled('Передача файлов прервана')
                    now=time.monotonic()
                    if done==total or now-last_progress[0]>.3:
                        event(dict(kind='progress',percent=done/max(1,total)*100,phase='Передача на Linux',unit='передачи'));last_progress[0]=now
                sftp.put(runtime['path'],root+'/runtime.tar.gz',callback=progress,confirm=True)
                for source,name in ((source_root/'ssh_job_runner.py','runner.py'),(source_root/(module+'.py'),'task.py')):
                    sftp.put(str(source),root+'/'+name,confirm=True)
                manifests=[]
                for index,source in enumerate(files):
                    path=Path(source)
                    name='payload-'+str(index)+''.join(path.suffixes)
                    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    sftp.put(str(path),root+'/'+name,callback=progress,confirm=True)
                    manifests.append(dict(name=name,sha256=digest))
            check=runtime['sha256']+'  '+root+'/runtime.tar.gz'
            bootstrap="printf '%s\\n' "+shlex.quote(check)+' | sha256sum -c - && mkdir -m 700 '+root+'/python && tar -xzf '+root+'/runtime.tar.gz -C '+root+'/python --no-same-owner'
            command(client,['sh','-c',bootstrap],cancel=cancel)
            interpreter=root+'/python/python/bin/python3.12'
            command(client,[interpreter,'-I','--version'],cancel=cancel)
            secrets=[values.get('password',''),password,payload.get('password',''),payload.get('settings',{}).get('password','')]
            request=dict(payload,_root=root,_files=manifests,_secrets=secrets)
            stdin,stdout,stderr=client.exec_command(shlex.join([*prefix,interpreter,'-I','-u',root+'/runner.py']),timeout=30)
            started=True
            if prefix:stdin.write(password+'\n')
            # sudo may consume the first password line, or leave it unread when
            # credentials are cached / NOPASSWD. A per-job frame lets the worker
            # accept both cases without mistaking a password for JSON.
            stdin.write(token+':request:'+json.dumps(request,ensure_ascii=True)+'\n');stdin.flush()
            channel=stdout.channel;pending=b'';result=None;errors=[];reported_error=False;sent=False;cancelled='';ready=False;deadline=time.monotonic()+120
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if not ready and time.monotonic()>deadline:raise RuntimeError('SSH-исполнитель не запустился за 120 с. Проверьте sudo и свободное место на ВМ.')
                if cancel.is_set() and not sent:
                    stdin.write(token+':cancel\n');stdin.flush();sent=True
                if channel.recv_ready():
                    pending+=channel.recv(65536)
                    if len(pending)>16*1024*1024:raise RuntimeError('Ответ SSH превышает допустимый размер')
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        try:item=json.loads(line);ready=True
                        except ValueError:item=dict(kind='message',text=line.decode('utf-8',errors='replace'))
                        if item.get('kind')=='result':result=item['result']
                        elif item.get('kind')=='cancelled':cancelled=item['text']
                        elif item.get('kind')=='error':errors.append(item['text']);reported_error=True
                        else:event(item)
                if channel.recv_stderr_ready():errors.append(channel.recv_stderr(32768).decode('utf-8',errors='replace'))
                if not client.get_transport().is_active():raise RuntimeError('Связь SSH потеряна. Проверьте результат операции на ВМ перед повтором.')
                time.sleep(.04)
            code=channel.recv_exit_status();completed=True
            if cancelled:raise OperationCancelled(cancelled)
            if errors or code or result is None:
                text='\n'.join(errors)[-4000:]
                for secret in secrets:
                    if secret:text=text.replace(secret,'[скрыто]')
                prefix='Операция на Linux завершилась с ошибкой. ' if reported_error else 'SSH: выполнение не подтверждено. '
                raise RuntimeError(prefix+text)
            return result
        finally:
            if stdin is not None:stdin.close()
            if channel is not None:channel.close()
            if started and not completed:event(dict(kind='warning',text='SSH-задание не подтвердило завершение. Проверьте операцию и временные файлы на ВМ: '+root))
            # The remote worker cleans after completion. Preflight/upload failures
            # are cleaned via SFTP, which unlinks links instead of following them.
            if owned and not started:
                try:
                    import stat
                    with client.open_sftp() as sftp:
                        with sftp.open(root+'/.ka-owned') as marker:
                            value=marker.read()
                            if isinstance(value,bytes):value=value.decode()
                            if value!=token:raise ValueError('Метка SSH-каталога не совпадает')
                        def remove(path):
                            if not (path==root or path.startswith(root+'/')):raise ValueError('Путь вне SSH-задания')
                            for entry in sftp.listdir_attr(path):
                                if entry.filename in ('.','..') or '/' in entry.filename:raise ValueError('Неверное имя SSH-файла')
                                child=path+'/'+entry.filename
                                if stat.S_ISDIR(entry.st_mode):remove(child)
                                else:sftp.remove(child)
                            sftp.rmdir(path)
                        remove(root)
                except Exception as exc:event(dict(kind='warning',text='Не удалены временные файлы: '+root+': '+str(exc)))
