"""Validated publication plans for IIS/Apache and protected config editing."""
from pathlib import Path, PureWindowsPath, PurePosixPath
import re
import xml.etree.ElementTree as ET

from windows_remote import run_windows
from ssh_jobs import run_script

NS = 'http://v8.1c.ru/8.2/virtual-resource-system'
ET.register_namespace('', NS)


def descriptor(alias, connection, web=True, services=True, http=True, odata=False):
    if not re.fullmatch(r'[\w-]{1,80}', alias, re.UNICODE):
        raise ValueError('Имя публикации: буквы, цифры, _ и -, до 80 символов')
    if not connection.strip() or any(c in connection for c in '\r\n\x00'):
        raise ValueError('Укажите строку подключения ИБ без переносов строк')
    server=bool(re.search(r'(?i)(?:^|;)\s*Srvr\s*=',connection))
    if ((not server and not re.search(r'(?i)(?:^|;)\s*File\s*=',connection))
            or (server and not re.search(r'(?i)(?:^|;)\s*Ref\s*=',connection))):
        raise ValueError('Строка ИБ: Srvr="сервер:1541";Ref="база"; либо File="каталог";')
    root = ET.Element('{'+NS+'}point', dict(base='/'+alias, ib=connection, enable=str(bool(web)).lower()))
    ET.SubElement(root, '{'+NS+'}ws', dict(publishByDefault=str(bool(services)).lower()))
    ET.SubElement(root, '{'+NS+'}httpServices', dict(publishByDefault=str(bool(http)).lower()))
    ET.SubElement(root, '{'+NS+'}standardOData', dict(enable=str(bool(odata)).lower()))
    ET.indent(root, space='  ')
    return '<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(root, encoding='unicode')+'\n'


def validate(values):
    p = dict(values)
    if p.get('os') not in ('windows', 'linux') or p.get('server') not in ('iis', 'apache'):
        raise ValueError('Выберите Windows / Linux и IIS / Apache')
    if p['os'] == 'linux' and p['server'] == 'iis':
        raise ValueError('IIS работает только на Windows. Для Linux выберите Apache 2.4.')
    actions = {'inspect','publish','unpublish','read_config','save_config','site_create','site_start','site_stop','site_delete',
               'pool_create','pool_start','pool_stop','pool_recycle','pool_delete','binding_add','binding_delete','install','server_restart'}
    if p.get('action') not in actions:
        raise ValueError('Неизвестная операция веб-сервера')
    if p['action'] in ('publish','unpublish','site_start','site_stop','site_delete','binding_add','binding_delete') and not p.get('site'):
        raise ValueError('Сначала подключитесь и выберите конкретный сайт')
    if p['action'].startswith('pool_') and not p.get('pool'):
        raise ValueError('Выберите или укажите имя пула IIS')
    if p['action'] in ('binding_add','binding_delete','site_create'):
        try:port=int(p.get('port',0))
        except (ValueError,TypeError):raise ValueError('Некорректный порт сайта') from None
        if not 1<=port<=65535:raise ValueError('Порт сайта: от 1 до 65535')
    if p['action'] == 'publish':
        if not p.get('site') or not p.get('directory') or not p.get('platform'):
            raise ValueError('Выберите сайт, каталог публикации и каталог bin платформы на веб-сервере')
        path_type=PureWindowsPath if p['os']=='windows' else PurePosixPath
        for name in ('directory','platform'):
            path=path_type(p[name])
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Укажите полный путь на веб-сервере: '+name)
        descriptor(p.get('alias',''), p.get('connection',''))
        validate_xml(p.get('vrd',''))
        root = ET.fromstring(p['vrd'])
        if root.tag != '{'+NS+'}point' or root.get('base') != '/'+p['alias'] or root.get('ib') != p['connection']:
            raise ValueError('В default.vrd значения base и ib должны совпадать с параметрами публикации')
    if p['action'] == 'save_config':
        if not p.get('etag'):
            raise ValueError('Сначала прочитайте файл конфигурации с сервера')
        if len(p.get('text','').encode('utf-8')) > 2*1024**2:
            raise ValueError('Конфигурация превышает 2 МБ')
        if p.get('config_kind') in ('vrd','web','iis'):
            validate_xml(p['text'])
    for name in ('site','alias','directory','platform','main_conf','config_path','pool','hostname','site_path','certificate'):
        if any(c in str(p.get(name,'')) for c in '\r\n\x00'):
            raise ValueError('Недопустимые символы: '+name)
    return p


def validate_xml(text):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('DTD и внешние сущности в конфигурациях не поддерживаются')
    try:
        ET.fromstring(text)
    except ET.ParseError:
        raise ValueError('Некорректный XML: проверьте закрывающие теги и кавычки') from None


def operate(values, connection, event, cancel):
    p = validate(values)
    def safe_event(item):
        if item.get('text'):
            item=dict(item,text=re.sub(r'(?i)(Pwd\s*=\s*)(?:"[^"]*"|[^;\r\n]*)',r'\1[скрыто]',str(item['text'])))
        event(item)
    safe_event.cancel_event=cancel
    if p['os'] == 'linux':
        return run_script('web_publication_linux', connection, p, safe_event, cancel, elevate=True)
    code = (Path(__file__).resolve().parent / 'web_publication_windows.ps1').read_text(encoding='utf-8-sig')
    return run_windows(code, connection, p, safe_event)
