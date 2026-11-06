"""Read DEB control records and packaged binaries without executing packages."""
import io
from pathlib import Path, PurePosixPath
import re
import tarfile


def ar_members(source):
    if source.read(8) != b'!<arch>\n':
        raise ValueError('Файл не является DEB: отсутствует заголовок ar')
    while header := source.read(60):
        if len(header) != 60 or header[58:60] != b'`\n':
            raise ValueError('Повреждён заголовок DEB')
        length = int(header[48:58])
        offset = source.tell()
        yield header[:16].decode('ascii').strip().rstrip('/'), offset, length
        source.seek(offset + length + length % 2)


def read_deb(path, cancel):
    fields, binaries = {}, set()
    targets = {'ragent', 'rac', 'ras', '1cv8', '1cv8c', 'crserver'}
    with Path(path).open('rb') as source:
        for name, offset, length in ar_members(source):
            if cancel.is_set():
                from postgres_runner import OperationCancelled
                raise OperationCancelled('Чтение состава DEB прервано')
            if name.startswith('control.tar'):
                with tarfile.open(fileobj=io.BytesIO(source.read(length)), mode='r:*') as archive:
                    control = next((item for item in archive if PurePosixPath(item.name).name == 'control' and item.isfile()), None)
                    if control is None:
                        raise ValueError('В DEB нет файла control')
                    text = archive.extractfile(control).read().decode('utf-8')
                    for line in text.splitlines():
                        if line and not line[0].isspace() and ':' in line:
                            key, value = line.split(':', 1)
                            fields[key] = value.strip()
            elif name.startswith('data.tar') and not fields.get('Package', '').endswith('-nls'):
                with tarfile.open(fileobj=source, mode='r|*') as archive:
                    for item in archive:
                        if cancel.is_set():
                            from postgres_runner import OperationCancelled
                            raise OperationCancelled('Чтение состава DEB прервано')
                        basename = PurePosixPath(item.name).name
                        if basename in targets and (item.isfile() or item.issym()):
                            binaries.add(basename)
    if not all(fields.get(key) for key in ('Package', 'Version', 'Architecture')):
        raise ValueError('В DEB отсутствуют имя пакета, версия или архитектура')
    return dict(path=str(path), package=fields['Package'], version=fields['Version'], architecture=fields['Architecture'],
                depends=fields.get('Depends', ''), binaries=sorted(binaries))


def inspect_debs(paths, version, cancel):
    from linux_platform_runner import deb_component
    rows = [read_deb(path, cancel) for path in paths]
    for row in rows:
        if re.sub(r'\D', '', row['version']) != re.sub(r'\D', '', version):
            raise ValueError('Версия DEB отличается от выбранного релиза: ' + row['version'])
    features, bundles = set(), {}
    binary_roles = {'ragent': 'server', 'rac': 'server_admin', '1cv8': 'client_full',
                    '1cv8c': 'client_thin', 'crserver': 'config_storage_server'}
    for row in rows:
        role = deb_component(row['package'])
        if role and role != 'common':
            features.add(role)
        for binary in row['binaries']:
            feature = binary_roles.get(binary)
            if not feature:
                continue
            features.add(feature)
            if feature != role:
                bundles[feature] = dict(package=row['package'], with_feature=role if role != 'common' else '')
    return dict(packages=rows, features=sorted(features), bundled_features=bundles,
                capabilities_verified=True, capabilities_source='DEB: control и список файлов пакетов',
                directory_option=None, languages=[], hasp_supported=False,
                ras_features=sorted({deb_component(row['package']) for row in rows if 'ras' in row['binaries']}))
