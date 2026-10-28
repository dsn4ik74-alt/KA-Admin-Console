import io
import tarfile


def run_help(features='client_full client_thin server server_admin ws ru de', directory='--prefix'):
    return ('1C:Enterprise 8.3.27.2342\n--enable-components <enable-components> Components\n'
            '  Default: server,server_admin,ru\n  Allowed: ' + features + '\n'
            '--disable-components <disable-components> Components\n' +
            (directory + ' <directory> Installation directory\n' if directory else ''))


def deb_bytes(package, files=(), version='8.3.27-2342'):
    def tar_bytes(items):
        target = io.BytesIO()
        with tarfile.open(fileobj=target, mode='w:gz') as archive:
            for name, content in items:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return target.getvalue()
    control = f'Package: {package}\nVersion: {version}\nArchitecture: amd64\n'
    members = [('debian-binary', b'2.0\n'), ('control.tar.gz', tar_bytes([('./control', control.encode())])),
               ('data.tar.gz', tar_bytes([(name, b'fixture') for name in files]))]
    result = b'!<arch>\n'
    for name, data in members:
        header = f'{name+"/":<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n'.encode()
        result += header + data + (b'\n' if len(data) % 2 else b'')
    return result
