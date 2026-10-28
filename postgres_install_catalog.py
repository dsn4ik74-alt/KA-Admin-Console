"""PostgreSQL with 1C patches: releases and exact OS-specific distributions."""
import re
import shlex
from urllib.parse import parse_qs, urljoin, urlsplit

from configuration_catalog import leaf_rows,published_date
from its_portal import Document,Portal,PORTAL,trusted_url

PROJECT='AddCompPostgre'


def releases(source,url):
    result=[];seen=set()
    for row in leaf_rows(Document(source).root):
        for link in row.walk('a'):
            address=urljoin(url,link.attrs.get('href',''));parts=urlsplit(address);query=parse_qs(parts.query)
            version=query.get('ver',[''])[0]
            if parts.hostname!='releases.1c.ru' or query.get('nick',[''])[0]!=PROJECT or not re.fullmatch(r'\d[\w.-]{1,45}',version):continue
            if version in seen:continue
            seen.add(version);result.append(dict(version=version,date=published_date(row.text()),url=trusted_url(address)))
    if not result:raise RuntimeError('ИТС: не найден список версий PostgreSQL с патчами 1С')
    return result


def distributions(source,release,url):
    result=[];seen=set()
    for link in Document(source).root.walk('a'):
        address=urljoin(url,link.attrs.get('href',''));parts=urlsplit(address);query=parse_qs(parts.query)
        if parts.hostname!='releases.1c.ru' or query.get('nick',[PROJECT])[0]!=PROJECT or query.get('ver',[release['version']])[0]!=release['version']:continue
        filename=query.get('path',[''])[0].replace('\\','/').rsplit('/',1)[-1]
        lower=filename.casefold();title=link.text();description=lower+' '+title.casefold()
        if not lower.endswith(('.zip','.tar.bz2','.tar.gz','.tgz','.tar.xz','.exe')) or any(v in lower for v in ('_sources','_addon')):continue
        # Some legacy cards put Elbrus DEBs beside Ubuntu/Debian packages.
        # They cannot run on the x64/ARM64 Linux hosts supported by the console.
        if any(v in description for v in ('e2k','elbrus','эльбрус')):continue
        system=next((name for name in ('windows','ubuntu','debian') if name in description),None)
        # Older 1C releases (e.g. 14.3) publish one generic Linux DEB bundle,
        # rather than separate archives labelled Ubuntu / Debian. The actual
        # package dependencies must be checked on the VM, not guessed here.
        if not system and 'linux' in description and re.search(r'(?:\bdeb\b|_deb(?:\.|_))',description):system='linux-deb'
        if not system or address in seen:continue
        seen.add(address)
        match=re.search(system+r'[_\s]+(\d+(?:\.\d+)?)',lower+' '+title.casefold())
        architecture='aarch64' if any(v in lower for v in ('aarch64','arm64')) else 'x86_64' if any(v in lower for v in ('x86_64','amd64','x64','64bit','-64','_64')) or '64-bit' in title else 'i386'
        result.append(dict(title=title,url=trusted_url(address),version=release['version'],filename=filename,os=system,
            os_version=match[1] if match else '',architecture=architecture,format='binary' if '_install.' in lower else 'package'))
    if not result:raise RuntimeError('В этой версии нет доступных дистрибутивов для Windows, Ubuntu или Debian')
    return result


def dependency_sections(source):
    heading='';result=[]
    for node in Document(source).root.walk():
        if (node.tag=='button' or node.tag in ('h2','h3','h4')) and node.text():heading=node.text()
        if node.tag!='pre' and 'shell-code' not in node.attrs.get('class','').split():continue
        def raw(n):return '\n' if n.tag=='br' else ''.join(raw(c) if hasattr(c,'children') else c for c in n.children)
        packages=[]
        for line in raw(node).replace('\\\n',' ').splitlines():
            if not re.match(r'\s*apt-get\s+install\b',line):continue
            for word in shlex.split(line)[2:]:
                if re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,100}',word):packages.append(word)
        if packages:result.append(dict(title=heading,packages=list(dict.fromkeys(packages))))
    return result


class PostgresPortal(Portal):
    def postgres_releases(self):
        url,source=self.page(PORTAL+'/project/'+PROJECT+'?allUpdates=true')
        return releases(source,url)

    def postgres_distributions(self,release):
        url,source=self.page(release['url'])
        rows=distributions(source,release,url)
        notes=next((urljoin(url,n.attrs.get('href','')) for n in Document(source).root.walk('a')
                    if any(name in n.attrs.get('href','').casefold() for name in ('releasenotes','postgreupdate_ru.htm'))),None)
        sections=[]
        if notes:
            _,text=self.page(trusted_url(notes));sections=dependency_sections(text)
        for row in rows:
            row['dependencies']=[]
            if row['os']=='windows':continue
            os_version=row['os_version'];os_name=row['os']
            for section in sections:
                title=section['title'].casefold()
                version=re.search(os_name+r'\s+(\d+(?:\.\d+)?)',title)
                if version and (version[1].split('.')[0]==os_version.split('.')[0] if os_name=='debian' else version[1]==os_version):
                    if ('aarch64' in title)==(row['architecture']=='aarch64'):
                        row['dependencies']=section['packages'];break
            row['notes_url']=notes or release['url']
        return rows
