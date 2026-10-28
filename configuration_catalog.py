"""Official Russian configuration catalog; no platform or third-party projects."""
from datetime import datetime
import re
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from its_portal import Document, Portal, PORTAL, trusted_url
from configuration_requirements import requirements, table_minimums

VERSION = re.compile(r'(?<!\d)\d+(?:\.\d+){1,5}(?!\d)')


def published_date(text):
    match = re.search(r'\b\d{2}\.\d{2}\.(?:\d{4}|\d{2})\b', text)
    if match:
        try:
            return datetime.strptime(match[0], '%d.%m.%Y' if len(match[0]) == 10 else '%d.%m.%y').strftime('%d.%m.%Y')
        except ValueError:
            pass
    return ''


def leaf_rows(document):
    return [row for row in document.walk('tr') if not any(child is not row for child in row.walk('tr'))]


def russian_projects(source, url=PORTAL + '/total'):
    rows = leaf_rows(Document(source).root)
    groups = {row.attrs['group'] for row in rows if row.attrs.get('group') and
              'типовые конфигурации' in row.text().casefold() and 'для россии' in row.text().casefold()}
    if not groups:
        raise RuntimeError('В каталоге ИТС не найден раздел «Типовые конфигурации фирмы 1С для России». Другие разделы не загружаются.')
    result, seen = [], set()
    for row in rows:
        if row.attrs.get('parent-group') not in groups:
            continue
        for link in row.walk('a'):
            address = urlsplit(urljoin(url, link.attrs.get('href', '')))
            if address.hostname != 'releases.1c.ru' or address.fragment or not re.fullmatch(r'/project/[\w.-]+', address.path):
                continue
            nick = address.path.rsplit('/', 1)[1]
            if nick in seen or not link.text():
                continue
            seen.add(nick)
            actual = next((n.text() for n in row.walk('td') if 'actualVersionColumn' in n.attrs.get('class', '').split()), '')
            version = VERSION.search(actual)
            date = next((n.text() for n in row.walk('td') if 'releaseDate' in n.attrs.get('class', '').split()), '')
            result.append(dict(nick=nick, title=link.text(), version=version[0] if version else '',
                               date=published_date(date), url=trusted_url(urljoin(url, address.path))))
    if not result:
        raise RuntimeError('В российском разделе ИТС не найдено доступных конфигураций. Проверьте права учётной записи.')
    return result


def project_releases(source, project, url):
    result, seen = [], set()
    document = Document(source).root
    minimums = table_minimums(document)
    for row in leaf_rows(document):
        for link in row.walk('a'):
            address = urljoin(url, link.attrs.get('href', ''))
            parts = urlsplit(address)
            match = VERSION.fullmatch(link.text().strip())
            if not match or parts.hostname != 'releases.1c.ru' or parts.path not in ('/version_files', '/version') and not parts.path.startswith('/version/'):
                continue
            nick = parse_qs(parts.query).get('nick', [project['nick']])[0]
            if nick != project['nick'] or match[0] in seen:
                continue
            seen.add(match[0])
            minimum = minimums.get(id(row), '')
            result.append(dict(version=match[0], date=published_date(row.text()), url=trusted_url(address), project=dict(project),
                               platform_minimum=minimum or 'Не указана',
                               platform_requirements=dict(minimum=minimum, source=trusted_url(address), known=bool(minimum))))
    if not result:
        raise RuntimeError('Версии конфигурации не найдены. Проверьте доступ ИТС к выбранному продукту.')
    return result


def configuration_distributions(source, release, url):
    result, seen = [], set()
    for link in Document(source).root.walk('a'):
        href, title = link.attrs.get('href', ''), link.text()
        if not title or not any(part in href for part in ('/template', '/version_file?')):
            continue
        address = urljoin(url, href)
        parts = urlsplit(address)
        if parts.hostname != 'releases.1c.ru' or address in seen:
            continue
        seen.add(address)
        filename = parse_qs(parts.query).get('path', [''])[0].replace('\\', '/').rsplit('/', 1)[-1]
        # Release pages also link ReadMe.txt, news.htm and update instructions
        # through /template. They are not installable distributions.
        if filename and not filename.casefold().endswith(('.zip', '.rar', '.7z', '.exe')):
            continue
        if not filename and not any(word in title.casefold() for word in ('дистрибутив', 'установка')):
            continue
        lowered = title.casefold()
        kind = ('Обновление' if 'обновлен' in lowered else 'Технологический' if 'технологическ' in lowered else
                'Полная установка' if any(word in lowered for word in ('полной конфигурации', 'полный', 'полная')) else 'Дистрибутив')
        result.append(dict(title=title, kind=kind, filename=filename, url=trusted_url(address),
                           version=release['version'], project=dict(release['project']),
                           platform_requirements=dict(release.get('platform_requirements', {}))))
    if not result:
        raise RuntimeError('Дистрибутивы конфигурации не найдены. Проверьте доступ к выбранной версии.')
    return sorted(result, key=lambda r: (r['kind'] != 'Полная установка', r['kind'] == 'Обновление', r['title']))


class ConfigurationPortal(Portal):
    def projects(self):
        url, source = self.page(PORTAL + '/total')
        return russian_projects(source, url)

    def configuration_releases(self, project):
        nick = project.get('nick', '')
        if not re.fullmatch(r'[\w.-]+', nick):
            raise ValueError('Выберите конфигурацию из каталога ИТС.')
        url, source = self.page(PORTAL + '/project/' + quote(nick) + '?allUpdates=true')
        return project_releases(source, project, url)

    def configuration_details(self, release):
        self.check_cancel()
        cache = getattr(self, '_configuration_details', {})
        key = (release['project']['nick'], release['version'], release['url'])
        if key in cache:
            from copy import deepcopy
            return deepcopy(cache[key])
        url, source = self.page(release['url'])
        address = urlsplit(url)
        query = parse_qs(address.query)
        if address.hostname != 'releases.1c.ru':
            raise RuntimeError('ИТС: требуется повторный вход для чтения выбранного релиза.')
        if (address.path.startswith('/project/') or
                query.get('nick', [release['project']['nick']])[0] != release['project']['nick'] or
                query.get('ver', [release['version']])[0] != release['version']):
            raise RuntimeError('ИТС вернул страницу другого релиза. Обновите список версий.')
        info = requirements(source, url, release.get('platform_requirements', {}).get('minimum', ''))
        row = dict(release, platform_requirements=info, platform_minimum=info['minimum'] or 'Не указана')
        result = dict(release=row, distributions=configuration_distributions(source, row, url))
        cache[key] = result
        self._configuration_details = cache
        from copy import deepcopy
        return deepcopy(result)

    def configuration_distributions(self, release):
        return self.configuration_details(release)['distributions']
