"""Publication metadata, service rules and narrowly scoped Apache edits."""
import re
import ipaddress
from search_query import matches as full_text_match
from urllib.parse import quote
import xml.etree.ElementTree as ET

NS='http://v8.1c.ru/8.2/virtual-resource-system'
ET.register_namespace('',NS)


def parse(raw):
    if isinstance(raw,str):raw=raw.encode('utf-8')
    markup=raw.replace(b'\x00',b'').upper()
    if len(raw)>2*1024**2 or b'<!DOCTYPE' in markup or b'<!ENTITY' in markup:
        raise ValueError('Недопустимый или слишком большой default.vrd')
    try:root=ET.fromstring(raw,parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except ET.ParseError:raise ValueError('Некорректный XML в default.vrd') from None
    if root.tag!='{'+NS+'}point':raise ValueError('Файл не является описанием публикации 1С')
    return root


def flag(node,name,default=False):
    return default if node is None or node.get(name) is None else node.get(name).casefold() in ('true','1')


def connection_info(value):
    pairs={match[1].casefold():match[2].replace('""','"') if match[2] is not None else match[3].strip()
           for match in re.finditer(r'(?:^|;)\s*([\w-]+)\s*=\s*(?:"((?:[^"]|"")*)"|([^;]*))',value)}
    # Return only endpoint information, never passwords/users from an ib string.
    if 'file' in pairs:return dict(connection='File="'+pairs['file'].replace('"','""')+'";',file_path=pairs['file'],base='',cluster='',base_type='Файловая')
    if 'srvr' in pairs:
        return dict(connection='Srvr="'+pairs['srvr'].replace('"','""')+'";Ref="'+pairs.get('ref','').replace('"','""')+'";',
                    cluster=pairs['srvr'],base=pairs.get('ref',''),file_path='',base_type='Серверная')
    return dict(connection='',cluster='',base='',file_path='',base_type='Не определён')


def metadata(raw):
    root=parse(raw);rows=[]
    def add(key,kind,name,node,attr,default,parent=True,address=''):
        enabled=flag(node,attr,default)
        rows.append(dict(id=key,kind=kind,name=name,address=address,enabled=enabled,
                         effective=bool(enabled and parent),explicit=node is not None and attr in node.attrib))
    ws=root.find('{'+NS+'}ws');hs=root.find('{'+NS+'}httpServices')
    ws_enabled=ws is not None and flag(ws,'enable',True)
    add('ws/enabled','WS','Разрешить Web-сервисы',ws,'enable',False if ws is None else True)
    add('ws/default','WS','Сервисы без явного разрешения',ws,'pointEnableCommon',True,ws_enabled)
    add('ws/extensions','WS','Сервисы расширений по умолчанию',ws,'publishExtensionsByDefault',False,ws_enabled)
    if ws is not None:
        for index,node in enumerate(ws.findall('{'+NS+'}point')):
            name=node.get('name','');alias=node.get('alias') or name
            add('ws/point/'+str(index),'WS',name,node,'enable',flag(ws,'pointEnableCommon',True),ws_enabled,'ws/'+quote(alias,safe=''))
    add('hs/default','HS','HTTP-сервисы по умолчанию',hs,'publishByDefault',hs is not None)
    add('hs/extensions','HS','HTTP-сервисы расширений по умолчанию',hs,'publishExtensionsByDefault',False)
    if hs is not None:
        for index,node in enumerate(hs.findall('{'+NS+'}service')):
            url=node.get('rootUrl','')
            add('hs/service/'+str(index),'HS',node.get('name',''),node,'enable',False,True,'hs/'+quote(url,safe='') if url else '')
    return dict(connection_info(root.get('ib','')),base_url=root.get('base',''),web_enabled=flag(root,'enable',True),
                services=rows,services_active=any(row['effective'] for row in rows if row['id'] not in ('ws/enabled',)))


def update_services(raw,changes):
    root=parse(raw);known={row['id'] for row in metadata(raw)['services']}
    if not isinstance(changes,list) or not changes or len(changes)>10000:raise ValueError('Не выбраны настройки WS/HS')
    seen=set()
    for change in changes:
        key=change.get('id');enabled=change.get('enabled')
        if key not in known or key in seen or type(enabled) is not bool:raise ValueError('Настройки сервисов изменились. Обновите свойства.')
        seen.add(key)
        kind,field,*index=key.split('/')
        tag='ws' if kind=='ws' else 'httpServices'
        parent=root.find('{'+NS+'}'+tag)
        if parent is None:parent=ET.SubElement(root,'{'+NS+'}'+tag)
        attr={'enabled':'enable','default':'pointEnableCommon' if kind=='ws' else 'publishByDefault','extensions':'publishExtensionsByDefault'}.get(field)
        node=parent
        if attr is None:
            node=parent.findall('{'+NS+'}'+('point' if kind=='ws' else 'service'))[int(index[0])];attr='enable'
        node.set(attr,str(enabled).lower())
    return b'<?xml version="1.0" encoding="UTF-8"?>\n'+ET.tostring(root,encoding='utf-8')+b'\n'


def state(value):
    text=str(value or '').strip().casefold()
    if text in ('started','running','включён','включен','включено','active'):return 'Включено','on'
    if text in ('stopped','disabled','выключен','выключено','выключён','отключено','inactive'):return 'Отключено','off'
    return ({'starting':'Запускается','stopping':'Останавливается'}.get(text,str(value or 'Не определено')),'unknown')


def publication_urls(site,alias,host):
    result=[]
    for endpoint in site.get('endpoints',[]):
        protocol=endpoint.get('protocol','http')
        if protocol not in ('http','https'):continue
        hostname=endpoint.get('host') or host or endpoint.get('ip') or ''
        local=hostname.casefold().rstrip('.') in ('localhost','localhost.localdomain','*','0.0.0.0','::','[::]','+')
        try:local=local or ipaddress.ip_address(hostname.strip('[]')).is_loopback
        except ValueError:pass
        if local:hostname=host
        hostname=hostname.strip('[]')
        if not hostname or any(c in hostname for c in '/\\?#@\r\n\x00'):continue
        if ':' in hostname:hostname='['+hostname+']'
        try:port=int(endpoint.get('port') or (443 if protocol=='https' else 80))
        except (ValueError,TypeError):continue
        if not 1<=port<=65535:continue
        suffix='' if port==(443 if protocol=='https' else 80) else ':'+str(port)
        url=protocol+'://'+hostname+suffix+'/'+quote(alias.strip('/'),safe='/')+'/'
        if not alias.strip('/'):url=url[:-1]
        if url not in result:result.append(url)
    return result


ALIASES=re.compile(r'^\s*Alias\s+"?(/[^"\s]+)"?\s+"?([^"\r\n]+?)"?\s*$',re.I|re.M)
DIRECTORIES=re.compile(r'<Directory\s+"?([^">]+)"?\s*>(.*?)</Directory\s*>',re.I|re.S)


def apache_publications(body):
    rows=[]
    for match in ALIASES.finditer(body):
        name,directory=match[1].strip('/'),match[2].strip().rstrip('/\\')
        descriptor='';module=''
        for block in DIRECTORIES.finditer(body):
            if block[1].strip().strip('"').rstrip('/\\')==directory:
                found=re.search(r'ManagedApplicationDescriptor\s+"?([^"\r\n]+)',block[2],re.I)
                if found:descriptor=found[1].strip()
        loaded=re.findall(r'^\s*LoadModule\s+\S+\s+"?([^"\r\n]*wsap(?:24|22)\.(?:so|dll))',body,re.I|re.M)
        if loaded:module=loaded[-1].strip()
        rows.append(dict(name=name,path=directory,vrd_path=descriptor or directory+'/default.vrd',module_path=module,entry_type='application' if descriptor else 'virtual_directory'))
    return rows


def remove_apache_publication(body,alias,directory):
    matches=list(ALIASES.finditer(body))
    target=next((match for match in matches if match[1].strip('/')==alias),None)
    if target is None:raise ValueError('Alias публикации не найден. Обновите список.')
    if target[2].strip().rstrip('/\\')!=directory.rstrip('/\\'):raise ValueError('Каталог Alias изменился. Обновите список.')
    if any(match is not target and match[2].strip().rstrip('/\\')==directory.rstrip('/\\') for match in matches):
        raise ValueError('Каталог используется другим Alias. Измените конфигурацию сайта вручную.')
    spans=[target.span()]
    for block in DIRECTORIES.finditer(body):
        if block[1].strip().strip('"').rstrip('/\\')==directory.rstrip('/\\') and re.search(r'ManagedApplicationDescriptor\s',block[2],re.I):
            spans.append(block.span())
    for start,end in sorted(spans,reverse=True):body=body[:start]+body[end:]
    return body
