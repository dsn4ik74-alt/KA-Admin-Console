"""HTTPS session and release catalog for the official 1C update portal."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import http.cookiejar
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from postgres_runner import OperationCancelled

PORTAL = 'https://releases.1c.ru'
PROJECTS = {'8.3': 'Platform83', '8.5': 'Platform85'}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self, tag=None):
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk(tag)

    def text(self):
        return ' '.join(' '.join(child.text() if isinstance(child, Node) else child for child in self.children).split())


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in ('input', 'meta', 'link', 'br', 'hr', 'img', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'col', 'track'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def trusted_url(url):
    value = urllib.parse.urlsplit(url)
    host = (value.hostname or '').casefold()
    if value.scheme != 'https' or value.username or value.password or not (host == '1c.ru' or host.endswith('.1c.ru')):
        raise ValueError('Портал вернул адрес вне HTTPS-доменов 1c.ru')
    return url


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Portal:
    def __init__(self, cancel=None):
        self.cancel = cancel or threading.Event()
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(TrustedRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [('User-Agent', 'KA-Admin-Console/20.22 (Windows)')]

    def check_cancel(self):
        if self.cancel.is_set():
            raise OperationCancelled('Запрос ИТС прерван')

    def open(self, url, data=None):
        self.check_cancel()
        trusted_url(url)
        try:
            return self.opener.open(url, data=data, timeout=30)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'ИТС: HTTP {exc.code}. Проверьте вход и доступ к дистрибутиву.') from None
        except urllib.error.URLError:
            raise RuntimeError('Не удалось подключиться к ИТС. Проверьте сеть, прокси и сертификаты Windows.') from None

    def page(self, url, data=None):
        with self.open(url, data) as response:
            source = response.read(12 * 1024 * 1024 + 1)
            if len(source) > 12 * 1024 * 1024:
                raise RuntimeError('Страница ИТС превышает допустимый размер')
            return response.geturl(), source.decode('utf-8-sig', errors='replace')

    def login(self, username, password):
        if not username.strip() or not password:
            raise ValueError('Введите логин и пароль ИТС')
        url, source = self.page(PORTAL + '/project/Platform85')
        if urllib.parse.urlsplit(url).hostname == 'login.1c.ru':
            document = Document(source).root
            form = next((node for node in document.walk('form')
                         if any(item.attrs.get('type') == 'password' for item in node.walk('input'))), None)
            if form is None:
                raise RuntimeError('Форма входа ИТС изменилась. Не удалось найти поля авторизации.')
            inputs = {}
            for node in form.walk('input'):
                if not node.attrs.get('name') or 'disabled' in node.attrs:
                    continue
                checkbox = node.attrs.get('type') in ('checkbox', 'radio')
                if checkbox and 'checked' not in node.attrs:
                    continue
                inputs[node.attrs['name']] = node.attrs.get('value', 'on' if checkbox else '')
            user_key = next((node.attrs['name'] for node in form.walk('input')
                             if node.attrs.get('name') in ('username', 'login')), 'username')
            password_key = next(node.attrs['name'] for node in form.walk('input') if node.attrs.get('type') == 'password')
            inputs.update({user_key: username.strip(), password_key: password})
            action = trusted_url(urllib.parse.urljoin(url, form.attrs.get('action', '')))
            if urllib.parse.urlsplit(action).hostname != 'login.1c.ru':
                raise RuntimeError('Неожиданный адрес формы входа ИТС')
            url, source = self.page(action, urllib.parse.urlencode(inputs).encode('utf-8'))
        self.last_login_host = urllib.parse.urlsplit(url).hostname
        if urllib.parse.urlsplit(url).hostname == 'login.1c.ru' or 'name="password"' in source:
            document = Document(source).root
            errors = [node.text() for node in document.walk()
                      if node.attrs.get('id') in ('error', 'loginError', 'errorMessage', 'loginErrorMessage')
                      or 'alert-danger' in node.attrs.get('class', '').split()
                      or ('text-error' in node.attrs.get('class', '').split() and 'display: none' not in node.attrs.get('style', ''))]
            detail = next((text for text in errors if text and len(text) < 600), '')
            if detail:
                detail = detail.replace(password, '[скрыто]').replace(username, '[пользователь]')
            raise RuntimeError('Вход в ИТС не выполнен. Проверьте логин/пароль; возможна дополнительная проверка на портале.' + (' ' + detail if detail else ''))
        if urllib.parse.urlsplit(url).hostname != 'releases.1c.ru':
            url, source = self.page(PORTAL + '/project/Platform85')
        return True

    def releases(self, branch):
        project = PROJECTS[branch]
        url, source = self.page(PORTAL + '/project/' + project + '?allUpdates=true')
        if urllib.parse.urlsplit(url).hostname != 'releases.1c.ru':
            raise RuntimeError('Сеанс ИТС завершён. Войдите повторно.')
        rows, seen = [], set()
        document = Document(source).root
        for row in document.walk('tr'):
            if any(child is not row for child in row.walk('tr')):
                continue
            for link in row.walk('a'):
                version = re.search(r'\b8\.[35]\.\d+\.\d+\b', link.text())
                href = link.attrs.get('href', '')
                if not version or not any(part in href for part in ('/version/', '/version_files')):
                    continue
                version = version.group()
                if version in seen:
                    continue
                date = re.search(r'\b\d{2}\.\d{2}\.(?:\d{4}|\d{2})\b', row.text())
                seen.add(version)
                published = datetime.strptime(date.group(), '%d.%m.%Y' if len(date.group()) == 10 else '%d.%m.%y').strftime('%d.%m.%Y') if date else ''
                rows.append(dict(version=version, date=published, url=trusted_url(urllib.parse.urljoin(url, href))))
        if not rows:
            raise RuntimeError('Нет доступных релизов. Проверьте права ИТС; структура страницы могла измениться.')
        return rows

    def distributions(self, release, operating_system=None):
        url, source = self.page(release['url'])
        document = Document(source).root
        rows, seen = [], set()
        for link in document.walk('a'):
            href, title = link.attrs.get('href', ''), link.text()
            if not any(part in href for part in ('/template', '/version_file?')) or not title or href in seen:
                continue
            filename = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get('path', [''])[0].replace('\\', '/').split('/')[-1]
            lowered = filename.casefold()
            system = 'windows' if lowered.startswith(('windows', 'setuptc')) else 'linux' if (
                'linux' in title.casefold() or any(part in lowered for part in ('.deb', '.rpm', 'linux', 'setup-full', 'setup-thin'))) else ''
            if not system or operating_system and system != operating_system:
                continue
            seen.add(href)
            prefix = re.split(r'[_-]8[_.]', lowered, maxsplit=1)[0]
            architecture = 'ARM64' if 'arm' in prefix or 'aarch' in prefix else '64' if '64' in prefix or re.search(r'\.(?:deb|rpm)64\.', lowered) else '32'
            rows.append(dict(title=title, url=trusted_url(urllib.parse.urljoin(url, href)), version=release['version'],
                             architecture=architecture,
                             os=system, filename=filename))
        if not rows:
            raise RuntimeError('Список дистрибутивов не найден или недоступен для этой учётной записи ИТС.')
        rows.sort(key=lambda row: (row['os'] != 'windows', row['architecture'] != '64',
                                  'windows64full_' not in row['url'] if row['os'] == 'windows' else not row['filename'].startswith('linux64'),
                                  'with_' in row['url']))
        return rows

    def download_link(self, distribution):
        url, source = self.page(distribution['url'])
        links = [urllib.parse.urljoin(url, link.attrs.get('href', '')) for link in Document(source).root.walk('a')]
        for link in links:
            if '/getFile' in link or '/file/get/' in link or urllib.parse.urlsplit(link).path.lower().endswith(('.rar', '.zip', '.7z', '.exe', '.tar.gz', '.tgz', '.tar.xz', '.tar.bz2', '.tbz2', '.run')):
                return trusted_url(link)
        raise RuntimeError('Ссылка скачивания не найдена. Проверьте доступ к выбранному дистрибутиву.')

    def download(self, distribution, destination, event):
        url = self.download_link(distribution)
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        with self.open(url) as response:
            content_type = response.headers.get('Content-Type', '').casefold()
            if 'text/html' in content_type:
                raise RuntimeError('Вместо архива ИТС вернул страницу. Проверьте авторизацию и доступ.')
            disposition = response.headers.get('Content-Disposition', '')
            match = re.search(r'filename\*?=(?:UTF-8\x27\x27)?"?([^";]+)', disposition, re.I)
            filename = urllib.parse.unquote(match.group(1)) if match else Path(urllib.parse.urlsplit(response.geturl()).path).name
            filename = filename.replace('\\', '/').split('/')[-1]
            if not filename or re.search(r'[<>:"|?*\x00-\x1f]', filename) or not filename.lower().endswith(('.zip', '.rar', '.7z', '.exe', '.tar.gz', '.tgz', '.tar.xz', '.tar.bz2', '.tbz2', '.run')):
                raise RuntimeError('ИТС вернул неизвестное имя дистрибутива')
            path = folder / filename
            temporary = folder / (filename + '.' + uuid4().hex[:12] + '.part')
            total = int(response.headers.get('Content-Length', '0'))
            done = 0
            try:
                with temporary.open('xb') as target:
                    while True:
                        self.check_cancel()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        done += len(chunk)
                        if total:
                            event(dict(kind='progress', percent=min(100, done / total * 100), phase='Скачивание', unit='скачивания'))
                self.check_cancel()
                if total and done != total:
                    raise RuntimeError('Дистрибутив скачан не полностью')
                temporary.replace(path)
                event(dict(kind='artifact', path=str(path), size=done))
                return str(path)
            finally:
                temporary.unlink(missing_ok=True)
