"""Ubuntu/Debian Apache administration worker; no dependencies beyond Python."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

from ssh_job_runner import execute, Cancelled

VH = re.compile(r'<VirtualHost\s+([^>]+)>(.*?)</VirtualHost\s*>', re.I | re.S)


def read(path):
    path = Path(path)
    with path.open('rb') as source:
        raw = source.read(2*1024**2+1)
    if len(raw) > 2*1024**2:
        raise ValueError('Конфигурация превышает 2 МБ')
    return raw


def write(path, content):
    path = Path(path)
    if path.is_symlink():raise ValueError('Нельзя заменять файл-ссылку')
    temporary = path.with_name('.ka-config-'+uuid4().hex)
    try:
        with temporary.open('xb') as stream:stream.write(content)
        if path.exists():
            shutil.copystat(path, temporary)
            info = path.stat();os.chown(temporary,info.st_uid,info.st_gid)
        else:temporary.chmod(0o644)
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def backup(path, event):
    path = Path(path)
    if path.exists():
        target = path.with_name(path.name+'.ka-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6]+'.bak')
        shutil.copy2(path,target)
        event(dict(kind='message',text='Резервная копия: '+str(target)))


def site_data(path):
    path=Path(path).resolve()
    text=read(path).decode('utf-8-sig')
    rows=[]
    for index, match in enumerate(VH.finditer(text)):
        body=match[2]
        name=re.search(r'^\s*ServerName\s+([^\s#]+)',body,re.I|re.M)
        folder=re.search(r'^\s*DocumentRoot\s+"?([^"\r\n]+)',body,re.I|re.M)
        rows.append(dict(id=str(path)+'#'+str(index),name=name[1] if name else path.stem,
                         bindings=match[1].strip(),path=folder[1].strip() if folder else '',config_path=str(path),index=index,
                         etag=hashlib.sha256(read(path)).hexdigest(),
                         state='Включён' if any(p.resolve()==path for p in Path('/etc/apache2/sites-enabled').glob('*.conf')) else 'Выключен'))
    return rows


def selected(p):
    path,sep,index=p.get('site','').rpartition('#')
    if not sep or not index.isdigit() or not path.startswith('/'):
        raise ValueError('Выберите VirtualHost из списка сайтов')
    rows=site_data(path)
    if int(index)>=len(rows):raise ValueError('Список VirtualHost изменился. Обновите его.')
    text=read(path).decode('utf-8-sig')
    if p.get('site_etag') and hashlib.sha256(read(path)).hexdigest()!=p['site_etag']:
        raise ValueError('Конфигурация сайта изменилась. Обновите список сайтов перед действием.')
    return Path(path),text,list(VH.finditer(text))[int(index)]


def inspect(p):
    sites=[]
    for path in sorted(Path('/etc/apache2/sites-available').glob('*.conf'))[:500]:
        sites.extend(site_data(path))
    platforms=[]
    for tool in sorted(Path('/opt/1cv8').glob('*/*/webinst')):
        platforms.append(dict(version=tool.parent.name,bin=str(tool.parent)))
    publications=[]
    for site in sites:
        _,text,match=selected(dict(site=site['id']))
        for marker in re.finditer(r'# KA-1C ([\w-]+) BEGIN\n(.*?)# KA-1C \1 END',match[2],re.S):
            directory=re.search(r'ManagedApplicationDescriptor\s+"([^"]+)/default\.vrd"',marker[2],re.I)
            publications.append(dict(site=site['id'],name=marker[1],path=directory[1] if directory else '',pool=''))
        known={row['name'] for row in publications if row['site']==site['id']}
        for alias in re.finditer(r'^\s*Alias\s+"?(/[^"\s]+)"?\s+"([^"\r\n]+)"',match[2],re.I|re.M):
            name=alias[1].strip('/')
            if name not in known:
                publications.append(dict(site=site['id'],name=name,path=alias[2].rstrip('/'),pool=''))
    return dict(sites=sites,publications=publications,pools=[],certificates=[],platforms=platforms)


def main(p,event,cancel):
    action=p['action']
    def run(args):return execute(args,event,cancel)
    def configtest():run(['apache2ctl','configtest'])
    def reload():
        if subprocess.run(['systemctl','is-active','--quiet','apache2'],stdout=subprocess.DEVNULL).returncode==0:
            run(['systemctl','reload','apache2'])
        else:event(dict(kind='warning',text='Apache остановлен. Конфигурация проверена, запустите службу отдельно.'))
    if action=='inspect':return inspect(p)
    if action=='install':
        run(['apt-get','update']);run(['apt-get','install','-y','apache2'])
        return dict(text='Apache установлен. Обновите список сайтов.')
    if action=='server_restart':
        configtest();run(['systemctl','restart','apache2'])
        return dict(text='Служба Apache перезапущена.')
    if action=='site_create':
        name=p.get('site_name','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',name):raise ValueError('Имя сайта: латинские буквы, цифры, _ и -')
        port=int(p.get('port',80))
        if not 1<=port<=65535:raise ValueError('Некорректный порт сайта')
        folder=Path(p['site_path'])
        if not folder.is_absolute() or any(c in str(folder) for c in '"\r\n'):raise ValueError('Укажите абсолютный каталог сайта')
        path=Path('/etc/apache2/sites-available')/(name+'.conf')
        if path.exists():raise ValueError('Сайт уже существует')
        folder.mkdir(parents=True,exist_ok=True)
        hostname=p.get('hostname') or name
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',hostname):raise ValueError('Некорректное доменное имя сайта')
        text=f'<VirtualHost *:{port}>\n  ServerName {hostname}\n  DocumentRoot "{folder}"\n</VirtualHost>\n'
        if port not in (80,443):
            raise ValueError('Для нестандартного порта сначала добавьте Listen в ports.conf через редактор конфигурации')
        write(path,text.encode())
        try:run(['a2ensite',path.name]);configtest();reload()
        except Exception:
            subprocess.run(['a2dissite',path.name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            path.unlink(missing_ok=True);raise
        return dict(text='Сайт создан: '+name)
    if action in ('read_config','save_config'):
        path=Path(p['config_path'])
        if not path.is_absolute() or not path.is_file():raise ValueError('Файл конфигурации не найден на Linux')
        path=path.resolve()
        raw=read(path)
        digest=hashlib.sha256(raw).hexdigest()
        if action=='read_config':return dict(text=raw.decode('utf-8-sig'),etag=digest,path=str(path))
        if digest!=p['etag']:raise ValueError('Файл изменился на сервере. Прочитайте его заново.')
        text=p['text']
        if path.suffix.lower() in ('.vrd','.config'):
            if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():raise ValueError('DTD не поддерживается')
            try:ET.fromstring(text)
            except ET.ParseError:raise ValueError('Некорректный XML') from None
        backup(path,event)
        try:write(path,text.encode('utf-8'));configtest();reload()
        except Exception:write(path,raw);raise
        return dict(text='Конфигурация сохранена и проверена.')
    path,text,match=selected(p)
    if action in ('site_start','site_stop','site_delete'):
        if path.parent!=Path('/etc/apache2/sites-available'):raise ValueError('Сайт расположен вне sites-available')
        backup(path,event)
        run(['a2ensite' if action=='site_start' else 'a2dissite',path.name])
        if action=='site_delete':path.unlink()
        configtest()
        if action=='site_start':run(['systemctl','start','apache2'])
        reload()
        return dict(text='Действие с сайтом завершено; файлы сайта не удалялись.')
    if action not in ('publish','unpublish'):raise ValueError('Для Apache используйте сайты и редактор конфигураций; пулы относятся к IIS')
    alias=p['alias']
    if not re.fullmatch(r'[\w-]{1,80}',alias):raise ValueError('Некорректное имя публикации')
    marker=re.compile(r'\n?# KA-1C '+re.escape(alias)+r' BEGIN\n.*?# KA-1C '+re.escape(alias)+r' END\n?',re.S)
    existing=marker.search(match[2])
    if action=='unpublish' and not existing:raise ValueError('Эта публикация не создана консолью. Отредактируйте конфигурацию сайта вручную.')
    if action=='publish' and existing and not p.get('replace'):raise ValueError('Публикация уже существует. Включите замену.')
    fragment=''
    vrd_path=None;previous_vrd=None
    if action=='publish':
        directory=Path(p['directory'])
        if not directory.is_absolute() or directory==Path('/') or '"' in str(directory):raise ValueError('Некорректный каталог публикации')
        directory.mkdir(parents=True,exist_ok=True)
        if directory.is_symlink():raise ValueError('Каталог публикации — ссылка')
        vrd_path=directory/'default.vrd'
        if vrd_path.exists():
            if not p.get('replace'):raise ValueError('В каталоге уже есть default.vrd. Включите замену или выберите другой каталог.')
            previous_vrd=read(vrd_path);backup(vrd_path,event)
        tool=Path(p['platform'])/'webinst'
        if not tool.is_file():raise ValueError('Не найден webinst выбранной платформы на Linux')
        expected=(tool.parent/'wsap24.so').resolve()
        sources={path.resolve() for folder in ('mods-enabled','sites-enabled','conf-enabled') for path in Path('/etc/apache2',folder).glob('*') if path.is_file()}
        sources.add(path.resolve())
        for source in sources:
            configured=read(source).decode('utf-8-sig')
            if source==path.resolve():configured=marker.sub('',configured)
            for loaded in re.finditer(r'^\s*LoadModule\s+\S+\s+"?([^"\r\n]*wsap24\.so)"?',configured,re.I|re.M):
                if Path(loaded[1].strip()).resolve()!=expected:
                    raise ValueError('В Apache уже загружен модуль другой версии 1С: '+loaded[1]+'. Используйте совместимую платформу или отдельный экземпляр Apache.')
        stage=Path(p['_root'])/'publication.conf';stage.write_text('',encoding='utf-8')
        descriptor=Path(p['_root'])/'template.vrd';descriptor.write_text(p['vrd'],encoding='utf-8')
        backup(path,event)
        try:
            run([str(tool),'-publish','-apache24','-wsdir',alias,'-dir',str(directory),'-connstr',p['connection'],
                 '-descriptor',str(descriptor),'-confpath',str(stage)])
            fragment=stage.read_text(encoding='utf-8-sig')
            if 'ManagedApplicationDescriptor' not in fragment or 'Alias' not in fragment:
                raise RuntimeError('webinst не создал описание Apache')
            fragment='# KA-1C '+alias+' BEGIN\n'+fragment.strip()+'\n# KA-1C '+alias+' END\n'
            body=marker.sub('\n',match[2])+'\n'+fragment
            updated=text[:match.start()]+f'<VirtualHost {match[1]}>'+body+'</VirtualHost>'+text[match.end():]
            write(path,updated.encode());configtest()
        except Exception:
            write(path,text.encode())
            if previous_vrd is not None:write(vrd_path,previous_vrd)
            elif vrd_path and vrd_path.exists():vrd_path.unlink()
            raise
    else:
        backup(path,event)
        body=marker.sub('\n',match[2])
        updated=text[:match.start()]+f'<VirtualHost {match[1]}>'+body+'</VirtualHost>'+text[match.end():]
        try:write(path,updated.encode());configtest()
        except Exception:write(path,text.encode());raise
    reload()
    return dict(text=('Опубликовано: /' if action=='publish' else 'Публикация снята: /')+alias)


run = main
