"""Read-only discovery in data directories of RUNNING local ragent services."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import ipaddress
import ntpath
import os
from pathlib import Path
import re
import socket
import threading

from cluster_registry import read_registry
from service_management import windows_arguments


def normalized_ip(text):
    try:
        value=ipaddress.ip_address(text.split('%',1)[0]);return value.ipv4_mapped or value if value.version==6 else value
    except ValueError:return None


def local_addresses():
    """Query adapter addresses without a DNS request or a child process."""
    if os.name!='nt':return set()
    class Address(ctypes.Structure):pass
    class Adapter(ctypes.Structure):pass
    class SocketAddress(ctypes.Structure):_fields_=[('address',ctypes.c_void_p),('length',ctypes.c_int)]
    Address._fields_=[('alignment',ctypes.c_ulonglong),('next',ctypes.POINTER(Address)),('socket',SocketAddress)]
    Adapter._fields_=[('alignment',ctypes.c_ulonglong),('next',ctypes.POINTER(Adapter)),
                     ('name',ctypes.c_char_p),('unicast',ctypes.POINTER(Address))]
    query=ctypes.WinDLL('iphlpapi').GetAdaptersAddresses
    query.argtypes=[wintypes.ULONG,wintypes.ULONG,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(wintypes.ULONG)]
    query.restype=wintypes.ULONG;size=wintypes.ULONG(16384)
    for attempt in range(3):
        if size.value>1024*1024:raise OSError('Слишком большой список сетевых интерфейсов')
        buffer=ctypes.create_string_buffer(size.value);code=query(0,14,None,buffer,ctypes.byref(size))
        if code==111:continue
        if code==232:return set()
        if code:raise ctypes.WinError(code)
        result=set();adapter=ctypes.cast(buffer,ctypes.POINTER(Adapter));seen=set()
        while adapter and ctypes.addressof(adapter.contents) not in seen:
            seen.add(ctypes.addressof(adapter.contents));address=adapter.contents.unicast;visited=set()
            while address and ctypes.addressof(address.contents) not in visited:
                visited.add(ctypes.addressof(address.contents));current=address.contents.socket
                if current.address and 4<=current.length<=128:
                    raw=ctypes.string_at(current.address,current.length);family=int.from_bytes(raw[:2],'little')
                    if family==socket.AF_INET and len(raw)>=8:result.add(normalized_ip(socket.inet_ntop(family,raw[4:8])))
                    elif family==socket.AF_INET6 and len(raw)>=24:result.add(normalized_ip(socket.inet_ntop(family,raw[8:24])))
                address=address.contents.next
            adapter=adapter.contents.next
        return result-{None}
    raise OSError('Не удалось получить адреса сетевых интерфейсов')


def is_local_host(host):
    if str(host).strip()=='.':return True
    host=str(host).strip().strip('[]').rstrip('.').casefold()
    names={'.','localhost',os.environ.get('COMPUTERNAME','').casefold(),socket.gethostname().casefold()}
    if os.name=='nt':
        # Primary DNS name, without invoking DNS for this machine's name.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Services\Tcpip\Parameters',0,winreg.KEY_READ) as key:
                name=winreg.QueryValueEx(key,'Hostname')[0];domain=winreg.QueryValueEx(key,'Domain')[0]
                names.add(name.casefold())
                names.add((name+'.'+domain if domain else name).casefold())
        except OSError:pass
    if host and host in names:return True
    address=normalized_ip(host)
    if address and address.is_loopback:return True
    addresses=local_addresses()
    if address:return address in addresses
    if not host:return False
    # An alias may point to this PC. Bound DNS latency; never query remote services.
    answer=[];ready=threading.Event()
    def resolve():
        try:answer.extend(normalized_ip(item[4][0]) for item in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM))
        except OSError:pass
        finally:ready.set()
    threading.Thread(target=resolve,daemon=True).start()
    if not ready.wait(2):return False
    resolved={value for value in answer if value is not None}
    return bool(resolved) and all(value in addresses or value.is_loopback for value in resolved)


def running_services():
    """Only ENUMERATE/KEY_READ rights. No start, stop, or configuration commands."""
    if os.name!='nt':return []
    import winreg
    class Status(ctypes.Structure):
        _fields_=[(key,wintypes.DWORD) for key in ('type','state','controls','exit','specific','checkpoint','wait','pid','flags')]
    class Entry(ctypes.Structure):
        _fields_=[('name',wintypes.LPWSTR),('display',wintypes.LPWSTR),('status',Status)]
    dll=ctypes.WinDLL('advapi32',use_last_error=True)
    dll.OpenSCManagerW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD];dll.OpenSCManagerW.restype=wintypes.HANDLE
    dll.CloseServiceHandle.argtypes=[wintypes.HANDLE];dll.CloseServiceHandle.restype=wintypes.BOOL
    dll.EnumServicesStatusExW.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                      wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(wintypes.DWORD),wintypes.LPCWSTR]
    dll.EnumServicesStatusExW.restype=wintypes.BOOL
    manager=dll.OpenSCManagerW(None,None,4)
    if not manager:raise ctypes.WinError(ctypes.get_last_error())
    result=[];resume=wintypes.DWORD()
    try:
        for page in range(64):
            buffer=ctypes.create_string_buffer(256*1024);needed,count=wintypes.DWORD(),wintypes.DWORD();previous=resume.value
            ok=dll.EnumServicesStatusExW(manager,0,0x30,1,buffer,len(buffer),ctypes.byref(needed),ctypes.byref(count),ctypes.byref(resume),None)
            code=0 if ok else ctypes.get_last_error()
            if code not in (0,234):raise ctypes.WinError(code)
            entries=ctypes.cast(buffer,ctypes.POINTER(Entry))
            for index in range(count.value):
                item=entries[index]
                if item.status.state!=4:continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Services'+'\\'+item.name,0,winreg.KEY_READ) as key:
                        image,kind=winreg.QueryValueEx(key,'ImagePath')
                    if kind==winreg.REG_EXPAND_SZ:
                        # Service accounts can have a different user profile.
                        # Do not substitute the console user's profile into -d.
                        user_variables={'USERPROFILE','APPDATA','LOCALAPPDATA','HOMEPATH','HOMEDRIVE','USERNAME','TEMP','TMP'}
                        image=re.sub(r'%([^%]+)%',lambda match:match[0] if match[1].upper() in user_variables else os.environ.get(match[1],match[0]),image)
                    result.append(dict(name=item.name,display=item.display,state=4,image=image))
                except OSError:continue
            if ok:return result
            if resume.value==previous:raise OSError('Не удалось продолжить чтение списка служб')
        raise OSError('Слишком большой список служб')
    finally:dll.CloseServiceHandle(manager)


def service_parameters(service):
    if service.get('state')!=4:return None
    image=service.get('image','').strip()
    # 1C service command lines also occur with a single trailing backslash
    # inside a quoted -d path. Keep it a directory separator, not an escaped quote.
    image=re.sub(r'((?<!\S)[-/]d\s+"[^"]*?)(\\+)"(?=\s|$)',lambda match:match[1]+match[2]*2+'"',image,flags=re.I)
    # Some manually registered services have an unquoted executable with spaces.
    if not image.startswith('"'):
        match=re.match(r'((?:[A-Za-z]:\\|\\\\)[^"\r\n]*?\\ragent(?:\.exe)?)(?=\s|$)',image,re.I)
        if match and not re.search(r'\.(?:exe|com|bat|cmd)\s',match[1],re.I):image='"'+match[1]+'"'+image[match.end():]
    arguments=windows_arguments(image)
    if not arguments or ntpath.basename(arguments[0]).casefold() not in ('ragent','ragent.exe'):return None
    options={}
    for index,arg in enumerate(arguments[1:],1):
        key=arg.casefold().lstrip('-/')
        if key in ('port','regport','d'):
            if index+1==len(arguments):raise ValueError('Неполный параметр службы 1С')
            if key in options:raise ValueError('Повторный параметр службы 1С')
            options[key]=arguments[index+1]
    port=int(options.get('port','1540'));registration=int(options.get('regport','1541'))
    if not 1<=port<=65535 or not 1<=registration<=65535:raise ValueError('Некорректный порт службы')
    directory=options.get('d','')
    return dict(service=service['name'],port=port,registration=registration,directory=directory)


@dataclass(frozen=True,repr=False)
class LocalRegistry:
    snapshot: object
    service: str
    agent_port: int


def find_local_registry(row):
    if not is_local_host(row.get('server','')):return False,[],''
    agent=int(row['port']) if str(row.get('port','')).isdigit() else None
    cluster_port=int(row['cluster_port']) if str(row.get('cluster_port','')).isdigit() else None
    if not agent and not cluster_port:return True,[],'Нет порта сервера 1С. Обновите список ИБ.'
    found=[];seen=set();issues=[];matched=False
    for service in running_services():
        try:options=service_parameters(service)
        except (ValueError,OSError):continue
        if not options or (options['port']!=agent if agent else options['registration']!=cluster_port):continue
        matched=True
        if not options['directory']:
            issues.append(options['service']+': в параметрах службы отсутствует путь -d');continue
        if re.search(r'%[^%]+%',options['directory']):
            issues.append(options['service']+': путь -d содержит нераскрытые переменные окружения службы');continue
        if not ntpath.isabs(options['directory']):
            issues.append(options['service']+': в параметре -d службы требуется полный путь');continue
        root=Path(options['directory']);port=cluster_port or options['registration']
        for path in (root/('reg_'+str(port))/'1CV8Clst.lst',root/'1CV8Clst.lst'):
            key=os.path.normcase(str(path.absolute()))
            if key in seen:continue
            seen.add(key)
            try:snapshot=read_registry(path)
            except FileNotFoundError:continue
            except (OSError,ValueError) as exc:issues.append(options['service']+': '+str(exc));continue
            if snapshot.cluster_id!=row['cluster_id'] or row['infobase_id'] not in snapshot.bases:continue
            if cluster_port and snapshot.port!=cluster_port:continue
            found.append(LocalRegistry(snapshot,options['service'],options['port']))
    message='Найдено несколько реестров. Выберите нужную запущенную службу.' if len(found)>1 else ''
    if not found:
        message=('Файл выбранной ИБ не найден в каталоге -d запущенной службы. ' if matched else
                 'Запущенная служба ragent с нужным портом не найдена. ')
        message+='Выберите файл вручную.'
        if issues:message+=' '+issues[0]
    return True,found,message
