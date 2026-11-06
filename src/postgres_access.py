"""Validated, ordered PostgreSQL access rules for newly created instances."""
import ipaddress
import os
from pathlib import Path
import re


def rules(values):
    if values is None:return None
    if not isinstance(values,list) or len(values)>200:raise ValueError('Допускается не более 200 правил доступа')
    result=[]
    for index,value in enumerate(values,1):
        if not isinstance(value,dict):raise ValueError('Некорректное правило доступа №'+str(index))
        row={key:str(value.get(key,default)).strip() for key,default in (
            ('action','allow'),('database_mode','all'),('database',''),('user_mode','all'),('user',''),
            ('address',''),('connection','host'),('auth','auto'))}
        if row['action'] not in ('allow','reject') or row['connection'] not in ('host','hostssl','hostnossl'):
            raise ValueError('Некорректный способ подключения в правиле №'+str(index))
        if row['auth'] not in ('auto','scram-sha-256','md5'):raise ValueError('Некорректная проверка пароля в правиле №'+str(index))
        for field in ('database','user'):
            modes=('all','name','replication') if field=='database' else ('all','name')
            if row[field+'_mode'] not in modes:raise ValueError('Некорректный выбор базы или пользователя')
            if row[field+'_mode']=='name' and not re.fullmatch(r'[\w .\-$]{1,128}',row[field]):
                raise ValueError('Укажите имя '+('базы' if field=='database' else 'пользователя')+' без кавычек, переносов и специальных символов')
        try:row['address']=str(ipaddress.ip_network(row['address'],strict=False))
        except ValueError:raise ValueError('Правило №'+str(index)+': укажите IP или подсеть, например 192.168.20.10 или 192.168.20.0/24') from None
        result.append(row)
    return result


def method(version):
    # PostgreSQL 10-13 Windows installers can initialize an MD5 password;
    # HBA's md5 also negotiates SCRAM when the role already has a SCRAM secret.
    return 'scram-sha-256' if int(str(version).split('.')[0])>=14 else 'md5'


def rule_line(row,version):
    row=rules([row])[0]
    auth='reject' if row['action']=='reject' else method(version) if row['auth']=='auto' else row['auth']
    if auth=='scram-sha-256' and int(str(version).split('.')[0])<10:
        raise ValueError('SCRAM требует PostgreSQL 10 или новее; выберите автоматический способ или MD5')
    database='"'+row['database']+'"' if row['database_mode']=='name' else row['database_mode'] if row['database_mode']=='replication' else 'all'
    user='"'+row['user']+'"' if row['user_mode']=='name' else 'all'
    return ' '.join((row['connection'],database,user,row['address'],auth))


def legacy_rules(networks):
    if isinstance(networks,str):networks=networks.split(',')
    return rules([dict(address=str(network).strip()) for network in networks if str(network).strip()])


def configuration(values,version,linux=False):
    custom=rules(values.get('access_rules'))
    rows=legacy_rules(values.get('networks',[])) if custom is None else custom
    auth=method(version)
    text='# KA access rules; first matching rule wins.\n'
    if linux:text+='local all postgres peer\nlocal all all peer\nlocal replication all peer\n'
    # Keep a local administrative login for initial verification and recovery.
    text+='host all postgres 127.0.0.1/32 '+auth+'\nhost all postgres ::1/128 '+auth+'\n'
    if custom is None:
        text+='host all all 127.0.0.1/32 '+auth+'\nhost all all ::1/128 '+auth+'\n'
    return text+'\n'.join(rule_line(row,version) for row in rows)+'\n'


def write_configuration(path,values,version,linux=False):
    path=Path(path)
    text=configuration(values,version,linux)
    if path.is_symlink() or not path.is_file():raise ValueError('pg_hba.conf отсутствует или является ссылкой')
    original=path.read_bytes();backup=path.with_name(path.name+'.ka-before-access.bak')
    # Preserve the installer's file, but replace its host rules: appending can
    # leave a broader default rule ahead of the restrictions selected in the UI.
    descriptor=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(original)
    try:path.write_text(text,encoding='utf-8')
    except Exception:path.write_bytes(original);raise
    return backup
