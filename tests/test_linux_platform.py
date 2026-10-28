"""Installer plans, SSH boundaries and owned-file cleanup. No real VM changes."""
import base64
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from install_workspace import cleanup_workspace, create_workspace
import linux_platform as linux
from linux_platform_runner import Runner, run_arguments, selected_debs, deb_component
from platform_install import install_properties
from postgres_runner import OperationCancelled
from test_platform_install import metadata
import test_service_ui
from installer_fixtures import deb_bytes, run_help


class PlanTests(unittest.TestCase):
    def test_msi_directory_never_receives_boolean_integrity_control_value(self):
        package = metadata()
        package['features'].append(dict(id='IntegrityControl', parent='', display='1', level='200'))
        package['directories'] = ['INTEGRITYCONTROL', 'SERVER']
        for features in (['FileVersion'], ['CSServer', 'IntegrityControl']):
            properties = install_properties(package, dict(features=features))
            self.assertNotIn('INTEGRITYCONTROL', properties)
            self.assertNotIn('SERVER', properties)
            self.assertIn('IntegrityControl' if 'IntegrityControl' in features else 'FileVersion', properties['ADDLOCAL'])

    def test_run_disables_unselected_defaults_and_includes_selected_language(self):
        help_text = run_help()
        args = run_arguments('/tmp/setup.run', dict(features=['server', 'server_admin'], language='RU', directory='/opt/custom'), help_text)
        self.assertEqual(args[args.index('--enable-components') + 1], 'server,server_admin,ru')
        self.assertEqual(set(args[args.index('--disable-components') + 1].split(',')), {'client_full', 'client_thin', 'ws', 'de'})
        self.assertEqual(args[-2:], ['--prefix', '/opt/custom'])
        with self.assertRaises(ValueError):
            run_arguments('setup.run', dict(features=['client_thin_fib']), help_text)

    def test_deb_uses_real_package_roles_skips_unselected_packages_and_checks_architecture(self):
        rows = [dict(path=name + '.deb', package='1c-enterprise-8.3.27.2342-' + name, architecture='amd64')
                for name in ('common', 'common-nls', 'server', 'server-nls', 'ws')]
        self.assertEqual(selected_debs(rows, dict(features=['server'], language='RU'), 'amd64'),
                         ['common.deb', 'common-nls.deb', 'server.deb', 'server-nls.deb'])
        self.assertEqual(selected_debs(rows, dict(features=['server'], language='EN'), 'amd64'), ['common.deb', 'server.deb'])
        for settings, arch in ((dict(features=['server']), 'arm64'), (dict(features=['client_thin']), 'amd64'),
                               (dict(features=['server'], directory='/opt/custom'), 'amd64')):
            with self.assertRaises(ValueError):
                selected_debs(rows, settings, arch)
        self.assertEqual(deb_component('1c-enterprise83-thin-client_8.3.27-2342_amd64.deb'), 'client_thin')

    def test_repository_deb_includes_server_and_web_dependencies_from_the_same_distribution(self):
        rows = [dict(path=name + '.deb', package='1c-enterprise83-' + name, architecture='amd64',
                     depends='1c-enterprise83-server (= 8.3.27-2342), 1c-enterprise83-ws (= 8.3.27-2342)' if name == 'crs' else '')
                for name in ('common', 'server', 'server-nls', 'ws', 'ws-nls', 'crs')]
        selected = selected_debs(rows, dict(features=['config_storage_server'], language='RU'), 'amd64')
        self.assertEqual(set(selected), {'common.deb', 'server.deb', 'server-nls.deb', 'ws.deb', 'ws-nls.deb', 'crs.deb'})

    def test_linux_archive_metadata_uses_real_files_and_does_not_invoke_installer(self):
        for kind in ('run', 'deb'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'linux.zip'
                with zipfile.ZipFile(archive, 'w') as output:
                    if kind == 'run':
                        output.writestr('setup-full-8.3.27.2342-x86_64.run', b'fixture, never executed')
                    else:
                        output.writestr('1c-enterprise83-common_8.3.27-2342_amd64.deb', deb_bytes('1c-enterprise83-common'))
                        output.writestr('1c-enterprise83-server_8.3.27-2342_amd64.deb', deb_bytes('1c-enterprise83-server', ('opt/1cv8/ragent', 'opt/1cv8/rac')))
                package = linux.linux_metadata(archive, dict(version='8.3.27.2342', architecture='64'), Mock(), threading.Event())
                self.assertEqual(package['format'], kind)
                if kind == 'deb':
                    self.assertIn('server', [row['id'] for row in package['features']])
                else:
                    self.assertFalse(package['capabilities_verified'])
                    self.assertEqual(package['features'], [])
                self.assertTrue(all(Path(path).exists() for path in package['files']))


class WorkspaceTests(unittest.TestCase):
    def test_success_removes_all_new_empty_parents_but_keeps_existing_download_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            created = downloads / '1C' / 'distributions'
            context = create_workspace(created, '8.3.27.2342')
            (Path(context['root']) / 'distribution.rar').write_bytes(b'own')
            cleanup_workspace(context)
            self.assertFalse((downloads / '1C').exists())
            self.assertTrue(downloads.is_dir())

    def test_preexisting_empty_version_and_download_folders_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / '8.3.27.2342'
            parent.mkdir()
            context = create_workspace(directory, '8.3.27.2342')
            cleanup_workspace(context)
            self.assertTrue(parent.is_dir())

    def test_unrelated_new_file_prevents_removing_a_created_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / '1C'
            context = create_workspace(folder, '8.3.27.2342')
            neighbor = folder / 'user-file.txt'
            neighbor.write_text('keep')
            cleanup_workspace(context)
            self.assertEqual(neighbor.read_text(), 'keep')
            self.assertFalse((folder / '8.3.27.2342').exists())

    def test_success_removes_only_owned_tree_and_preserves_neighbor_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            context = create_workspace(directory, '8.3.27.2342')
            sibling = Path(context['parent']) / 'existing.rar'
            sibling.write_bytes(b'keep')
            (Path(context['root']) / 'installer.rar').write_bytes(b'own')
            cleanup_workspace(context)
            self.assertFalse(Path(context['root']).exists())
            self.assertEqual(sibling.read_bytes(), b'keep')

    def test_wrong_parent_marker_or_reparse_point_never_starts_recursive_delete(self):
        for problem in ('parent', 'marker', 'reparse'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                context = create_workspace(directory, '8.3.27.2342')
                root = Path(context['root'])
                if problem == 'parent':
                    context = dict(context, parent=str(Path(directory) / 'outside'))
                if problem == 'marker':
                    (root / '.ka-install.json').write_text('{}')
                with patch('install_workspace.shutil.rmtree') as remove:
                    if problem == 'reparse':
                        native = Path.lstat
                        def info(path):
                            value = native(path)
                            return Mock(st_mode=value.st_mode, st_file_attributes=1024) if path == root else value
                        with patch.object(Path, 'lstat', info), self.assertRaises(ValueError):
                            cleanup_workspace(context)
                    else:
                        with self.assertRaises(ValueError):
                            cleanup_workspace(context)
                    remove.assert_not_called()
                self.assertTrue(root.is_dir())


class Channel:
    def __init__(self, output):
        self.output = output
    def exit_status_ready(self):
        return True
    def recv_ready(self):
        return bool(self.output)
    def recv_stderr_ready(self):
        return False
    def recv(self, size):
        part, self.output = self.output[:size], self.output[size:]
        return part
    def recv_exit_status(self):
        return 0
    def close(self):
        pass


class Sftp:
    def __init__(self):
        self.files = {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def mkdir(self, path, mode):
        pass
    def open(self, path, mode):
        target = io.StringIO()
        self.files[path] = target
        return target
    def put(self, source, destination, callback, confirm):
        data = Path(source).read_bytes()
        self.files[destination] = data
        callback(len(data), len(data))


class SshTests(unittest.TestCase):
    def test_native_crypto_reads_a_real_ssh_public_key(self):
        key = linux.paramiko.RSAKey.generate(2048)
        decoded = linux.paramiko.PKey.from_type_string(key.get_name(), key.asbytes())
        self.assertEqual(decoded.asbytes(), key.asbytes())
        self.assertFalse(decoded.can_sign())

    def test_unknown_host_is_not_trusted_automatically(self):
        key = Mock()
        key.get_name.return_value, key.get_base64.return_value, key.asbytes.return_value = 'ssh-ed25519', 'fixture', b'fixture'
        with self.assertRaises(linux.UnknownHostKey) as error:
            linux.RequireKnownHost().missing_host_key(Mock(), 'vm', key)
        self.assertTrue(error.exception.key['fingerprint'].startswith('SHA256:'))
        for values in (dict(host='vm -oProxyCommand=bad', user='root'), dict(host='vm', user='root;echo'), dict(host='vm', user='root', port='0')):
            with self.assertRaises(ValueError):
                linux.ssh_settings(values)

    def test_sftp_and_runner_payload_never_contain_ssh_or_windows_service_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / '1c-enterprise83-server_8.3.27-2342_amd64.deb'
            file.write_bytes(b'package fixture')
            package = dict(root=directory, path=directory, format='deb', architecture='64',
                           properties=dict(ProductVersion='8.3.27.2342'), files=[str(file)], features=[dict(id='server')])
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            sftp = Sftp()
            client.open_sftp.return_value = sftp
            messages = [dict(kind='stage', text='fixture'), dict(kind='result', result=dict(reboot=False, installed=True))]
            channel = Channel(('\n'.join(json.dumps(row) for row in messages) + '\n').encode())
            client.exec_command.return_value = (io.StringIO(), Mock(channel=channel), io.StringIO())
            def preflight(ssh, args, *rest, **kwargs):
                return 'ID=ubuntu\n' if args[0] == 'cat' else '0' if args[0] == 'id' else 'x86_64' if args[0] == 'uname' else 'Python 3.12.15' if args[-1] == '--version' else ''
            with patch('linux_platform.connect', return_value=client), patch('linux_platform.command', side_effect=preflight):
                result = linux.install(package, dict(features=['server'], language='RU', service=False,
                     service_password='windows-secret'), dict(host='vm', user='root', password='ssh-secret'), Mock(), threading.Event())
            self.assertTrue(result['installed'])
            command = client.exec_command.call_args.args[0]
            payload = json.loads(base64.b64decode(shlex.split(command)[-1]))
            self.assertEqual(payload['files'][0]['sha256'], hashlib.sha256(file.read_bytes()).hexdigest())
            self.assertNotIn('ssh-secret', str(payload))
            self.assertNotIn('windows-secret', str(payload))
            self.assertNotIn('ssh-secret', command)
            self.assertIn('/python-runtime/python/bin/python3.12', command)
            self.assertNotIn('apt-get install -y python3', str(sftp.files))
            self.assertIn(payload['root'] + '/python-runtime.tar.gz', sftp.files)
            self.assertTrue(payload['root'].startswith('/tmp/ka-1c-'))


class RemoteRunnerTests(unittest.TestCase):
    def test_missing_library_is_resolved_from_repository_and_rechecked(self):
        runner = Runner({}, Mock())
        calls, ldd_count = [], 0
        def run(args, **kwargs):
            nonlocal ldd_count
            calls.append(args)
            if args[0] == 'ldd':
                ldd_count += 1
                return 'libfixture.so.1 => not found' if ldd_count == 1 else 'libfixture.so.1 => /usr/lib/x86_64-linux-gnu/libfixture.so.1'
            if args[:2] == ['apt-file', 'search']:
                return 'libfixture1: /usr/lib/x86_64-linux-gnu/libfixture.so.1'
            return ''
        with patch.object(Path, 'is_file', return_value=True), patch.object(runner, 'run', side_effect=run):
            runner.verify_runtime(dict(features=['client_thin']), '8.3.27.2342', 'amd64')
        self.assertEqual(ldd_count, 2)
        self.assertEqual(calls[-2], ['apt-get', 'install', '-y', '--no-remove', 'libfixture1'])

    def test_deb_install_resolves_dependencies_without_removing_existing_packages_and_preserves_files_on_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'server.deb'
                path.write_bytes(b'fixture')
                payload = dict(root=str(root), token='a' * 32, format='deb', version='8.3.27.2342', architecture='64', cleanup=False,
                     files=[dict(name='server.deb', sha256=hashlib.sha256(b'fixture').hexdigest())], settings=dict(features=['server'], language='RU'))
                runner = Runner(payload, Mock())
                calls = []
                def run(args, **kwargs):
                    calls.append(args)
                    if args[0] == 'dpkg':
                        return 'amd64'
                    if args[0] == 'dpkg-deb':
                        return '1c-enterprise83-server\tamd64\t8.3.27-2342'
                    if args[0] == 'apt-cache':
                        return 'Candidate: 1.0'
                    if args[:2] == ['apt-get', 'install'] and failed:
                        raise RuntimeError('fixture package failure')
                    return ''
                native_read = Path.read_text
                def read(path, *args, **kwargs):
                    return 'ID=debian\n' if path.name == 'os-release' else native_read(path, *args, **kwargs)
                with patch('linux_platform_runner.validate_stage', return_value=root), \
                        patch('linux_platform_runner.os.geteuid', return_value=0, create=True), \
                        patch('linux_platform_runner.shutil.which', return_value='/bin/apt-get'), \
                        patch.object(Path, 'read_text', read), patch.object(runner, 'run', side_effect=run), \
                        patch.object(runner, 'verify_runtime'):
                    if failed:
                        with self.assertRaisesRegex(RuntimeError, 'package failure'):
                            runner.install()
                    else:
                        self.assertTrue(runner.install()['installed'])
                self.assertIn('--no-remove', next(args for args in calls if args[:2] == ['apt-get', 'install']))
                self.assertTrue(path.is_file())

    def test_command_streaming_reports_failure_and_cancellation_without_running_any_installer(self):
        events = []
        runner = Runner({}, events.append)
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            runner.run([sys.executable, '-c', 'print("fixture failure"); raise SystemExit(7)'])
        self.assertIn('fixture failure', str(events))
        runner.cancel.set()
        with patch('linux_platform_runner.subprocess.Popen') as start, self.assertRaises(Exception):
            runner.run(['never-run'])
        start.assert_not_called()


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_selection_and_search_never_start_download_and_choose_uses_original_row(self):
        panel = self.panel()
        panel.portal = Mock()
        panel.releases = [dict(version='8.3.27.2342', date='27.08.2026', url='fixture1'), dict(version='8.5.1.1529', date='12.10.2026', url='fixture2')]
        panel.render_releases()
        panel.version_search.set('8.5.1')
        self.assertEqual(panel.release_tree.get_children(), ('1',))
        with patch.object(panel, 'work') as work:
            panel.release_tree.selection_set('1')
            self.app.root.update()
            work.assert_not_called()
            panel.release_selected()
        operation = work.call_args.args[0]
        operation(Mock())
        panel.portal.distributions.assert_called_once_with(panel.releases[1])

    def test_windows_success_cleans_owned_downloads_while_failure_keeps_them(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                panel = self.panel()
                workspace = create_workspace(directory, '8.5.1.1529')
                package = dict(metadata(), workspace=workspace)
                panel.package_ready(package)
                with patch('platform_install_ui.install_msi', side_effect=RuntimeError('fixture failure') if failure else None,
                           return_value=dict(reboot=False)), patch('platform_install_ui.find_all_rac', return_value={}), \
                        patch('platform_install_ui.installed_agents', return_value={}), patch.object(panel, 'work') as work:
                    panel.execute()
                    operation, success = work.call_args.args
                    if failure:
                        with self.assertRaises(RuntimeError):
                            operation(Mock())
                        panel.finish('', 'fixture failure')
                    else:
                        success(operation(Mock()))
                self.assertEqual(Path(workspace['root']).exists(), failure)

    def test_linux_ui_has_remote_fields_filters_rpm_and_keeps_passwords_out_of_saved_settings(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        self.assertEqual(panel.ssh_frame.winfo_manager(), 'pack')
        self.assertEqual(panel.windows_frame.winfo_manager(), '')
        panel.ssh_password.set('fixture-ssh-secret')
        panel.distributions = [dict(os='linux', version='8.3.27.2342', title='DEB Linux', filename='deb64.zip', architecture='64'),
                               dict(os='linux', version='8.3.27.2342', title='RPM Linux', filename='rpm64.rpm.zip', architecture='64'),
                               dict(os='windows', version='8.3.27.2342', title='Windows', filename='windows64.rar', architecture='64')]
        panel.render_distributions()
        self.assertEqual(panel.distribution_tree.get_children(), ('0',))
        self.assertNotIn('fixture-ssh-secret', json.dumps(self.app._settings))

    def test_linux_install_waits_for_ssh_verification_then_uses_no_windows_passwords(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        panel.ssh_host.set('vm')
        panel.ssh_user.set('admin')
        panel.ssh_password.set('ssh-fixture')
        package = dict(metadata(), kind='linux', root='fixture', format='run', architecture='64', files=[],
                       features=[dict(id='server', parent='', display='1', level='200')])
        panel.package_ready(package)
        with patch.object(panel, 'check_ssh') as check, patch.object(panel, 'work') as work:
            panel.execute()
            check.assert_called_once_with(install_after=True)
            work.assert_not_called()
        panel._ssh_verified = panel.connection()
        panel.service_password.set('windows-fixture')
        panel.service.set(True)
        panel.service_port.set('4540')
        panel.service_debug.set(True)
        panel.linux_service_data.set('/data/1C')
        with patch('platform_install_ui.linux_platform.install', return_value=dict(reboot=False, installed=True)) as install, patch.object(panel, 'work') as work:
            panel.execute()
            operation, success = work.call_args.args
            success(operation(Mock()))
        self.assertNotIn('windows-fixture', str(install.call_args.args[1]))
        self.assertEqual(install.call_args.args[1]['linux_service_name'], 'srv1cv8_4541.service')
        self.assertEqual(install.call_args.args[1]['linux_service_data'], '/data/1C')
        self.assertTrue(install.call_args.args[1]['service_debug'])
        self.assertFalse(panel.running)

    def test_install_button_downloads_first_and_continues_with_settings_after_metadata(self):
        panel = self.panel()
        panel.portal = Mock()
        panel.distributions = [dict(url='fixture', version='8.5.1.1529', title='Windows full', filename='windows64full_8_5_1_1529.rar', architecture='64')]
        panel.render_distributions()
        panel.distribution_tree.selection_set('0')
        with patch.object(panel, 'work') as work:
            panel.execute()
            self.assertTrue(panel.running)
            success = work.call_args.args[1]
        with patch.object(panel, 'execute') as install:
            success(dict(metadata(), distribution_key=panel.distribution_key(panel.distributions[0])))
        install.assert_called_once()

    def test_bases_table_displays_only_names(self):
        self.app._switch_mode('bases')
        self.assertEqual(tuple(self.app.tree['displaycolumns']), ('base',))

    def test_compact_linux_form_keeps_actions_visible(self):
        panel = self.panel()
        panel.system.set('Linux · Ubuntu / Debian по SSH')
        panel.system_changed()
        self.app.root.deiconify()
        self.app.root.geometry('960x540')
        self.app.root.update()
        for widget in (panel.execute_button, panel.cancel_button, panel.clear_button):
            self.assertTrue(widget.winfo_viewable())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height())
