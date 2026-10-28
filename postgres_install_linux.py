"""Install only the selected 1C PostgreSQL build and initialize one chosen path."""
from contextlib import contextmanager
import ipaddress
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time

from ssh_job_runner import execute,Cancelled


APT_NETWORK_OPTIONS=('-o','Acquire::Retries=3','-o','Acquire::http::Timeout=30',
                     '-o','Acquire::https::Timeout=30')


def settings(values):
    result=dict(values)
    path=Path(result.get('data',''))
    if not path.is_absolute() or path==Path('/') or '..' in path.parts or any(c in str(path) for c in '\x00\r\n'):
        raise ValueError('Укажите отдельный абсолютный каталог данных PostgreSQL')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}',result.get('name','')):raise ValueError('Имя экземпляра: латинские буквы, цифры и подчёркивания')
    port=int(result.get('port',5432))
    if not 1024<=port<=65535:raise ValueError('Порт PostgreSQL: 1024–65535')
    if not result.get('password') or any(c in result['password'] for c in '\x00\r\n'):raise ValueError('Задайте пароль postgres без переносов строк')
    if not re.fullmatch(r'[\w.@-]{1,80}',result.get('locale','')):raise ValueError('Некорректная локаль')
    networks=result.get('networks',[])
    if isinstance(networks,str):networks=networks.split(',')
    networks=[str(ipaddress.ip_network(v.strip(),strict=False)) for v in networks if v.strip()]
    if result.get('listen','*') not in ('*','localhost','127.0.0.1'):raise ValueError('Адреса прослушивания: *, localhost или 127.0.0.1')
    result.update(port=port,networks=networks)
    prefix=Path(result.get('prefix',''))
    for directory in (path,prefix):
        if not directory.is_absolute() or directory==Path('/') or '..' in directory.parts or any(c in str(directory) for c in '\x00\r\n'):
            raise ValueError('Укажите отдельные абсолютные каталоги данных и программы')
        if directory.resolve()!=directory.absolute():raise ValueError('Каталог проходит через ссылку: '+str(directory))
        if str(directory) in ('/etc','/usr','/var','/home','/tmp','/opt','/root','/var/lib','/var/lib/postgresql'):
            raise ValueError('Укажите отдельный подкаталог экземпляра')
    if path==prefix or prefix.is_relative_to(path):raise ValueError('Каталог программы находится внутри данных')
    return result


def compatible(row,release,machine):
    generic=row['os']=='linux-deb'
    if generic:
        if release.get('ID') not in ('ubuntu','debian') or row['format']!='package':raise ValueError('Комплект DEB поддерживается только на Ubuntu / Debian')
    elif row['os']!=release.get('ID'):raise ValueError('Дистрибутив PostgreSQL не соответствует ОС ВМ')
    expected=row['os_version'];actual=release.get('VERSION_ID','')
    if not generic and (not expected or (expected.split('.')[0]!=actual.split('.')[0] if row['os']=='debian' else expected!=actual)):
        raise ValueError('Версия ОС ВМ '+actual+' отличается от выбранного дистрибутива '+expected)
    arch={'amd64':'x86_64','x86_64':'x86_64','aarch64':'aarch64','arm64':'aarch64'}.get(machine)
    if arch!=row['architecture']:raise ValueError('Архитектура дистрибутива не совпадает с ВМ')


def extract(archive,destination):
    destination=Path(destination);destination.mkdir()
    with tarfile.open(archive,'r:*') as source:
        for item in source:
            # data_filter permits safe in-tree library symlinks used by the
            # official binary archives, rejecting devices and escaping links.
            source.extract(item,destination,filter='data')
    return destination


@contextmanager
def suppress_default(config=Path('/etc/postgresql-common/createcluster.conf')):
    config=Path(config)
    if config.is_symlink():raise ValueError('Конфигурация postgresql-common является ссылкой')
    old=config.read_bytes() if config.exists() else None
    config.parent.mkdir(parents=True,exist_ok=True)
    original=old.decode('utf-8') if old is not None else ''
    text=re.sub(r'^\s*create_main_cluster\s*=.*$', '',original,flags=re.M)+'\ncreate_main_cluster = false\n'
    config.write_text(text,encoding='utf-8')
    try:yield
    finally:
        # Restore another administrator's concurrent edits only when unchanged.
        if config.exists() and config.read_text(encoding='utf-8')==text:
            if old is None:config.unlink()
            else:config.write_bytes(old)


def pg_literal(value):return "'"+str(value).replace("'","''")+"'"


def version_greater(left,right):
    result=subprocess.run(['dpkg','--compare-versions',left,'gt',right],capture_output=True,text=True)
    if result.returncode not in (0,1):raise ValueError('Не удалось сравнить версии пакетов: '+left+' / '+right)
    return result.returncode==0


def select_packages(packages,row,event):
    """Keep newer installed dependencies instead of forcing archive versions."""
    major='.'.join(row['version'].split('-')[0].split('.')[:2]) if int(row['version'].split('.')[0])<10 else row['version'].split('.')[0]
    server_name='postgresql-'+major;selected=[];server=False
    architecture={'x86_64':'amd64','aarch64':'arm64'}[row['architecture']]
    for package in packages:
        fields=subprocess.check_output(['dpkg-deb','-f',str(package),'Package','Version','Architecture'],text=True)
        info=dict(line.split(': ',1) for line in fields.splitlines() if ': ' in line)
        name=info.get('Package','');version=info.get('Version','');arch=info.get('Architecture','')
        if not re.fullmatch(r'(?:postgresql(?:-client)?(?:-common|-common-dev|-[0-9.]+)|lib(?:pq5|ecpg6|ecpg-compat3|pgtypes3))',name):raise ValueError('Неожиданный пакет в дистрибутиве: '+name)
        if arch not in ('all',architecture):raise ValueError('Архитектура пакета '+name+' ('+arch+') не соответствует ВМ ('+architecture+')')
        installed=subprocess.run(['dpkg-query','-W','-f=${Status}|${Version}',name+(':'+arch if name.startswith('lib') else '')],capture_output=True,text=True)
        current=installed.stdout.strip().split('|')[-1] if installed.returncode==0 and installed.stdout.startswith('install ok installed|') else ''
        if name==server_name:
            server=True
            if version.split(':')[-1].casefold()!=row['version'].casefold():raise ValueError('Версия DEB отличается от выбранной в ИТС')
            if current and current!=version:raise ValueError('Другая сборка PostgreSQL этой версии уже установлена. Автоматическая замена действующего сервера не выполняется.')
        if current and (current==version or version_greater(current,version)):
            event(dict(kind='message',text='Сохраняется установленный пакет '+name+' '+current+'; пакет '+version+' из архива не переустанавливается.'))
            continue
        selected.append(str(package))
    if not server:raise ValueError('В комплекте нет серверного пакета выбранной версии')
    return selected


def check_apt_plan(output):
    for line in output.splitlines():
        if line.startswith('Remv '):raise ValueError('План APT требует удаления пакетов. Установка остановлена до изменений: '+line)
        match=re.match(r'^Inst\s+(\S+)\s+\[([^\]]+)\]\s+\(([^\s)]+)',line)
        if match and version_greater(match[2],match[3]):
            raise ValueError('План APT требует понижения '+match[1]+' с '+match[2]+' до '+match[3]+'. Установка остановлена до изменений; проверьте зависимости выбранного дистрибутива.')


def acquire_apt(args,event,cancel,env):
    """Retry downloads in APT itself; never repeat a dpkg transaction."""
    problems=[];dns=set();network=False;failed=False
    def inspect(text):
        nonlocal network,failed
        for line in text.splitlines():
            lower=line.casefold()
            if any(term in lower for term in ('temporary failure resolving','could not resolve',"couldn't resolve")):
                matches=re.findall(r"(?:resolving|resolve)\s+['\"]?([\w.:-]+)",line,re.I)
                dns.update(matches or ['адрес репозитория'])
            if any(term in lower for term in ('could not connect',"couldn't connect",'connection timed out',
                                              'connection failed','network is unreachable','connection refused','failed to connect')):
                network=True
            if line.startswith(('Err:','E:')) or 'failed to fetch' in lower or 'some index files failed to download' in lower:
                failed=True;problems.append(line.strip());del problems[:-4]
    def report(item):
        if item.get('kind')=='message':inspect(item.get('text',''))
        event(item)
    cause=''
    try:
        output=execute(['apt-get',*APT_NETWORK_OPTIONS,*args],report,cancel,env=env)
        # Older APT may return zero after failing to refresh some repositories.
        inspect(output)
        if not failed:return output
    except Cancelled:raise
    except RuntimeError as exc:
        cause=str(exc);inspect(cause)
    action='обновить списки пакетов' if args==['update'] else 'загрузить зависимости PostgreSQL'
    if dns:
        detail='DNS на ВМ не разрешает адрес репозитория: '+', '.join(sorted(dns))+'. Проверьте DNS и доступ к репозиторию на ВМ, затем повторите установку.'
    elif network:
        detail='Репозиторий недоступен с Linux-ВМ. Проверьте сеть, прокси и доступ к настроенным репозиториям, затем повторите установку.'
    else:
        detail='Причина APT: '+('\n'.join(problems) or cause)[-700:]+'. Полный вывод — в журнале операций.'
    raise RuntimeError('Не удалось '+action+' на Linux. '+detail+' Установка пакетов и создание экземпляра не начаты.') from None


def install_apt_packages(packages,event,cancel,env):
    event(dict(kind='stage',text='Обновление списков APT; до 3 повторов загрузки при сбое сети',cancellable=True))
    acquire_apt(['update'],event,cancel,env)
    simulation=['apt-get','-s','--no-remove','install',*packages]
    event(dict(kind='stage',text='Проверка плана установки зависимостей',cancellable=True))
    check_apt_plan(execute(simulation,event,cancel,env=env))
    event(dict(kind='stage',text='Загрузка зависимостей; до 3 повторов при сбое сети',cancellable=True))
    acquire_apt(['-y','--download-only','--no-remove','install',*packages],event,cancel,env)
    # A different administrator may have changed packages during the download.
    check_apt_plan(execute(simulation,event,cancel,env=env))
    local_debs=any(str(package).casefold().endswith('.deb') for package in packages)
    # APT 2.0 must process local file acquisition even after --download-only.
    # --no-download shuts that queue down before absolute filenames are set,
    # leading to "Pathname to install is not absolute" in dpkg preconfiguration.
    # Allow normal acquisition for local DEBs, reusing the dependency cache.
    acquisition=['-o','APT::Get::Download=true',*APT_NETWORK_OPTIONS] if local_debs else ['--no-download']
    event(dict(kind='stage',text='Установка DEB из архива и загруженных зависимостей; стандартный main отключён' if local_debs
               else 'Установка загруженных зависимостей; стандартный main отключён',cancellable=False))
    with suppress_default():
        execute(['apt-get','-y',*acquisition,'--no-remove','-o','Dpkg::Options::=--force-confold',
                 'install',*packages],event,cancel,env=env,critical=True)


def install_debs(selected,event,cancel,env):
    install_apt_packages([*selected,'locales'],event,cancel,env)


def service_filename(name):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}',name):
        raise ValueError('Имя службы: латинские буквы, цифры и подчёркивания, без .service')
    return name+'.service'


def service_target(name):
    filename=service_filename(name)
    for directory in ('/etc/systemd/system','/run/systemd/system','/usr/lib/systemd/system','/lib/systemd/system',
                      '/run/systemd/transient','/run/systemd/generator','/run/systemd/generator.early','/run/systemd/generator.late'):
        candidate=Path(directory)/filename
        if candidate.exists() or candidate.is_symlink():
            raise ValueError('Служба уже существует: '+str(candidate)+'. Укажите другое имя; существующий файл не изменён.')
    return Path('/etc/systemd/system')/filename


def unit_quote(value,exec_argument=False):
    text=str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('\t','\\t')
    if exec_argument:text=text.replace('$','$$')
    return '"'+text+'"'


def service_content(config,row,binary):
    header='[Unit]\nDescription=PostgreSQL 1C '+row['version']+'\nAfter=network.target\n'
    mounts='RequiresMountsFor='+unit_quote(config['data'])+'\n'
    if row['format']=='package':
        major='.'.join(row['version'].split('-')[0].split('.')[:2]) if int(row['version'].split('.')[0])<10 else row['version'].split('.')[0]
        cluster=major+'-'+config['name']
        # Keep the Debian/Ubuntu wrapper: it locates the configuration, creates
        # runtime directories, drops privileges and preserves PostgreSQL logging.
        command='/usr/bin/pg_ctlcluster --skip-systemctl-redirect '
        args=major+' '+config['name']
        return (header+mounts+'PartOf=postgresql.service\nBefore=postgresql.service\n'
                'Conflicts=postgresql@'+cluster+'.service\nAfter=postgresql@'+cluster+'.service\n[Service]\nType=forking\n'
                'ExecStart='+command+args+' start\nExecStop='+command+'-m fast '+args+' stop\n'
                'ExecReload='+command+args+' reload\nPIDFile=/run/postgresql/'+cluster+'.pid\n'
                'TimeoutStartSec=0\nTimeoutStopSec=0\n[Install]\nWantedBy=multi-user.target\n')
    return (header+mounts+'[Service]\nType=simple\nUser=postgres\nGroup=postgres\n'
            'ExecStart='+unit_quote(binary/'postgres',True)+' -D '+unit_quote(config['data'],True)+'\n'
            'ExecReload=/bin/kill -HUP $MAINPID\nKillMode=mixed\nKillSignal=SIGINT\nTimeoutStopSec=0\n'
            'Restart=on-failure\n[Install]\nWantedBy=multi-user.target\n')


def create_service(config,row,binary):
    target=service_target(config['name'])
    with target.open('x',encoding='utf-8') as stream:stream.write(service_content(config,row,binary))
    target.chmod(0o644)
    return target


def run(payload,event,cancel):
    # Serialize changes to postgresql-common on this VM, including its default
    # cluster policy. Other console tasks and disk scans remain independent.
    import fcntl
    with open('/run/lock/ka-postgresql-install.lock','a') as lock:
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if cancel.is_set():raise Cancelled('Ожидание установки PostgreSQL отменено')
                time.sleep(.2)
        return install_instance(payload,event,cancel)


def install_instance(payload,event,cancel):
    import pwd
    config=settings(payload['settings']);row=payload['distribution'];root=Path(payload['_root'])
    release={}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key,value=line.split('=',1);release[key]=value.strip('"\'')
    compatible(row,release,os.uname().machine)
    if os.geteuid()!=0:raise ValueError('Для установки PostgreSQL нужны root или sudo')
    data=Path(config['data'])
    if data.is_symlink() or (data.exists() and (not data.is_dir() or any(data.iterdir()))):raise ValueError('Каталог данных уже занят. Существующий экземпляр не изменён.')
    for directory in (data,Path(config['prefix'])):
        if directory==root or directory.is_relative_to(root):raise ValueError('Рабочий экземпляр нельзя создавать во временном каталоге установки')
    major='.'.join(row['version'].split('-')[0].split('.')[:2]) if int(row['version'].split('.')[0])<10 else row['version'].split('.')[0]
    unit_name=service_filename(config['name'])
    service_target(config['name'])
    if (Path('/etc/postgresql')/major/config['name']).exists():raise ValueError('Экземпляр с таким именем уже существует')
    with socket.socket() as check:
        try:check.bind(('0.0.0.0',config['port']))
        except OSError:raise ValueError('Порт PostgreSQL уже занят: '+str(config['port'])) from None
    archive=next(root.glob('payload-*'))
    unpacked=extract(archive,root/'unpacked')
    env=dict(os.environ,DEBIAN_FRONTEND='noninteractive',LC_ALL='C.UTF-8')
    run_cmd=lambda args,**kw:execute(args,event,cancel,**kw)
    event(dict(kind='stage',text='Проверка пакетов и зависимостей PostgreSQL'))
    if row['os']=='linux-deb':event(dict(kind='message',text='Универсальный DEB-комплект ИТС: совместимость с этой ОС определяется архитектурой и зависимостями пакетов до их установки.'))
    if row['format']=='package':
        packages=[p for p in unpacked.rglob('*.deb') if not re.search(r'(?:-dev|-doc|-dbgsym)_',p.name)]
        if not packages:raise ValueError('В архиве не найдены пакеты DEB')
        selected=select_packages(packages,row,event)
        install_debs(selected,event,cancel,env)
        binary=Path('/usr/lib/postgresql')/major/'bin'
    else:
        dependencies=row.get('dependencies',[])
        if not dependencies:raise ValueError('В карточке релиза ИТС не найдены зависимости для этой ОС. Установка остановлена до изменения системы.')
        if any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,100}',p) for p in dependencies):raise ValueError('Некорректное имя зависимости')
        prefix=Path(config['prefix'])
        if not prefix.is_absolute() or prefix==Path('/') or prefix.exists():raise ValueError('Укажите новый отдельный каталог программы PostgreSQL')
        bins=[p for p in unpacked.rglob('bin/postgres') if p.is_file()]
        if len(bins)!=1:raise ValueError('Не найден однозначный каталог бинарного дистрибутива PostgreSQL')
        install_apt_packages([*dependencies,'locales'],event,cancel,env)
        shutil.copytree(bins[0].parent.parent,prefix,symlinks=True);binary=prefix/'bin'
    if cancel.is_set():raise Cancelled('Пакеты установлены; создание экземпляра отменено')
    # A newly installed dependency may itself provide a unit with this name.
    service_target(config['name'])
    try:owner=pwd.getpwnam('postgres')
    except KeyError:
        run_cmd(['useradd','--system','--user-group','--home-dir','/var/lib/postgresql','--shell','/bin/bash','postgres'])
        owner=pwd.getpwnam('postgres')
    # Generate only the selected locale, rather than changing the system locale.
    known=subprocess.check_output(['locale','-a'],text=True).casefold().replace('-','')
    if config['locale'].casefold().replace('-','') not in known:
        if '.' not in config['locale']:raise ValueError('Локаль отсутствует в системе: '+config['locale'])
        language,encoding=config['locale'].split('.',1)
        run_cmd(['localedef','-i',language,'-f',encoding,config['locale']])
    # Recheck after a potentially long apt transaction, before changing ownership.
    if data.resolve()!=data.absolute() or (data.exists() and (not data.is_dir() or any(data.iterdir()))):raise ValueError('Каталог данных изменился во время установки. Создание экземпляра остановлено.')
    data.parent.mkdir(parents=True,exist_ok=True)
    if not data.exists():data.mkdir(mode=0o700)
    os.chown(data,owner.pw_uid,owner.pw_gid);data.chmod(0o700)
    fd,pwfile=tempfile.mkstemp(prefix='ka-postgres-password-')
    try:
        os.fchmod(fd,0o600);os.fchown(fd,owner.pw_uid,owner.pw_gid)
        with os.fdopen(fd,'w') as stream:stream.write(config['password']+'\n')
        auth='scram-sha-256' if int(major.split('.')[0])>=10 else 'md5'
        event(dict(kind='stage',text='Создание единственного экземпляра: '+str(data)))
        if row['format']=='package':
            run_cmd(['pg_createcluster',major,config['name'],'--datadir='+str(data),'--port='+str(config['port']),
                     '--locale='+config['locale'],'--start-conf=manual','--','--auth-local=peer','--auth-host='+auth,'--pwfile='+pwfile])
            conf=Path('/etc/postgresql')/major/config['name']/'postgresql.conf';hba=conf.parent/'pg_hba.conf'
        else:
            run_cmd(['runuser','-u','postgres','--',str(binary/'initdb'),'-D',str(data),'--locale='+config['locale'],
                     '--encoding=UTF8','--auth-local=peer','--auth-host='+auth,'--pwfile='+pwfile])
            conf=data/'postgresql.conf';hba=data/'pg_hba.conf'
        with conf.open('a') as stream:stream.write('\n# KA installation\nport = '+str(config['port'])+'\nlisten_addresses = '+pg_literal(config['listen'])+'\n')
        with hba.open('a') as stream:
            for network in config['networks']:stream.write('host all all '+network+' '+auth+'\n')
        target=create_service(config,row,binary)
        event(dict(kind='message',text='Служба '+unit_name+'; файл: '+str(target)))
        run_cmd(['systemctl','daemon-reload']);run_cmd(['systemctl','enable',unit_name]);run_cmd(['systemctl','start',unit_name])
        verify_env=dict(os.environ,PGPASSWORD=config['password'],PGCONNECT_TIMEOUT='10',PGCLIENTENCODING='UTF8')
        deadline=time.monotonic()+40
        while True:
            ready=subprocess.run([str(binary/'pg_isready'),'-h','127.0.0.1','-p',str(config['port']),'-t','2'],capture_output=True)
            if ready.returncode==0:break
            if cancel.is_set():raise Cancelled('Служба создана; проверка готовности прервана. Проверьте экземпляр перед повтором.')
            if time.monotonic()>deadline:raise RuntimeError('Служба создана, но PostgreSQL не подтвердил готовность на выбранном порту')
            time.sleep(.4)
        output=run_cmd([str(binary/'psql'),'-X','-w','-h','127.0.0.1','-p',str(config['port']),'-U','postgres','-d','postgres','-At','-c',
                        "SELECT current_setting('data_directory') || E'\\t' || current_setting('server_version');"],env=verify_env)
        actual,_,version=output.strip().partition('\t')
        if actual!=str(data) or not version.startswith(row['version'].split('-')[0]):raise RuntimeError('Установка завершилась, но каталог данных или версия сервера не подтверждены')
        return dict(service=unit_name,unit_file=str(target),data=str(data),port=config['port'],version=row['version'])
    finally:
        try:os.unlink(pwfile)
        except FileNotFoundError:pass
