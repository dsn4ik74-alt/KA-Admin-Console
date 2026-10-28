"""Installer boundaries; no MSI, apt, service or real database is executed."""
import ctypes
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import postgres_install as installer
import postgres_install_linux as linux
from postgres_install_catalog import releases, distributions, dependency_sections, PostgresPortal
from postgres_runner import OperationCancelled


def options(**changes):
    result=dict(data=r'C:\PG\data',prefix=r'C:\PG\program',name='ka5432',port='5432',password='test-secret',
        locale='C',listen='*',networks='192.168.20.0/24, ::1/128')
    result.update(changes);return result


def distribution(**changes):
    result=dict(os='windows',os_version='',architecture='x86_64',format='package',version='18.4-1.1C',
        title='PostgreSQL',url='https://releases.1c.ru/file?nick=AddCompPostgre&ver=18.4-1.1C')
    result.update(changes);return result


class CatalogTests(unittest.TestCase):
    def test_14_13_release_keeps_x64_and_arm64_deb_and_excludes_elbrus(self):
        source=(Path(__file__).parent/'fixtures'/'postgresql_14_13_distribution_links.html').read_text(encoding='utf-8')
        rows=distributions(source,dict(version='14.13-1.1C'),'https://releases.1c.ru/version_files')
        self.assertEqual([(r['os'],r['architecture']) for r in rows],[('windows','x86_64'),('linux-deb','x86_64'),('linux-deb','aarch64')])

    def test_legacy_its_linux_deb_link_is_not_hidden_and_rpm_addons_are_excluded(self):
        source=(Path(__file__).parent/'fixtures'/'postgresql_14_3_distribution_links.html').read_text(encoding='utf-8')
        rows=distributions(source,dict(version='14.3-1.1C'),'https://releases.1c.ru/version_files')
        self.assertEqual([(r['os'],r['architecture'],r['format']) for r in rows],[('windows','x86_64','package'),('linux-deb','x86_64','package')])
        self.assertTrue(rows[1]['filename'].endswith('_amd64_deb.tar.bz2'))
        self.assertEqual(rows[1]['os_version'],'')

    def test_legacy_release_notes_are_read_without_guessing_os_dependencies(self):
        source=(Path(__file__).parent/'fixtures'/'postgresql_14_3_distribution_links.html').read_text(encoding='utf-8')
        portal=PostgresPortal();release=dict(version='14.3-1.1C',url='https://releases.1c.ru/version_files?nick=AddCompPostgre&ver=14.3-1.1C')
        with patch.object(portal,'page',side_effect=[(release['url'],source),('https://releases.1c.ru/update','<p>Install DEB packages</p>')]) as page:
            rows=portal.postgres_distributions(release)
        self.assertIn('postgreUpdate_ru.htm',page.call_args.args[0])
        self.assertEqual(rows[1]['dependencies'],[])

    def test_versions_dates_and_os_specific_archives(self):
        source='<table><tr><td><a href="/version?nick=AddCompPostgre&ver=18.4-1.1C">18.4-1.1C</a></td><td>15.10.2026</td></tr></table>'
        rows=releases(source,'https://releases.1c.ru/project/AddCompPostgre')
        self.assertEqual(rows[0]['version'],'18.4-1.1C');self.assertIn('2026',rows[0]['date'])
        filenames=['postgresql_18.4_1_windows_x86_64_package.zip','postgresql_18.4_1_ubuntu_20.04_x86_64_install.tar.bz2',
                   'postgresql_18.4_1_debian_13.0_aarch64_package.tar.bz2','postgresql_18.4_1_debian_13.0_aarch64_addon.tar.bz2',
                   'postgresql_18.4_1_sources.tar.bz2']
        source=''.join('<a href="/file?nick=AddCompPostgre&amp;ver=18.4-1.1C&amp;path='+name+'">'+name+'</a>' for name in filenames)
        rows=distributions(source,dict(version='18.4-1.1C'),'https://releases.1c.ru/version')
        self.assertEqual([(r['os'],r['os_version'],r['architecture'],r['format']) for r in rows],
            [('windows','','x86_64','package'),('ubuntu','20.04','x86_64','binary'),('debian','13.0','aarch64','package')])

    def test_dependencies_are_package_names_not_arbitrary_shell(self):
        source='''<h3>Ubuntu 20.04 x86_64</h3><h3></h3><div class="shell-code">apt-get install -y libssl1.1 locales<br>
          rm -rf /<br>apt-get install libldap-2.4-2 /tmp/file.deb $(evil) *.deb</div>'''
        result=dependency_sections(source)
        self.assertEqual(result,[dict(title='Ubuntu 20.04 x86_64',packages=['libssl1.1','locales','libldap-2.4-2'])])

    def test_dependencies_match_exact_os_and_architecture(self):
        release=dict(version='18.4-1.1C',url='https://releases.1c.ru/version')
        source='<a href="/releasenotes">notes</a>'+''.join('<a href="/file?path=postgresql_18.4_1_'+os+'_install.tar.bz2">'+os+'</a>' for os in ('ubuntu_20.04_x86_64','ubuntu_20.04_aarch64','debian_9.13_x86_64'))
        notes='<h3>Ubuntu 20.04 x86_64</h3><pre>apt-get install libpython3.8</pre><h3>Ubuntu 20.04 aarch64</h3><pre>apt-get install libarm</pre><h3>Debian 9 x86_64</h3><pre>apt-get install libpython3.5</pre>'
        portal=PostgresPortal()
        with patch.object(portal,'page',side_effect=[(release['url'],source),('https://releases.1c.ru/releasenotes',notes)]):
            rows=portal.postgres_distributions(release)
        self.assertEqual([r['dependencies'] for r in rows],[['libpython3.8'],['libarm'],['libpython3.5']])

    def test_untrusted_links_and_wrong_projects_are_not_installers(self):
        source='<a href="https://evil.test/file?path=postgresql_windows_x64_package.zip">Windows</a><a href="/file?nick=Other&amp;path=postgresql_windows_x64_package.zip">Windows</a>'
        with self.assertRaises(RuntimeError):distributions(source,dict(version='18.4-1.1C'),'https://releases.1c.ru/version')


class ValidationTests(unittest.TestCase):
    def test_custom_instance_and_networks_validated_before_any_mutation(self):
        result=installer.validate(options(port='5544',data=r'D:\Custom Data\cluster'))
        self.assertEqual(result['port'],5544);self.assertEqual(result['networks'],['192.168.20.0/24','::1/128'])
        for change in (dict(port='x'),dict(port=0),dict(name='../main'),dict(data='relative'),dict(data='C:\\'),
                       dict(data=r'\\server\share\data'),dict(password=''),dict(password='bad;secret'),dict(networks='bad'),
                       dict(data=r'C:\PG',prefix=r'C:\PG\program')):
            with self.subTest(change=change),self.assertRaises(ValueError):installer.validate(options(**change))

    def test_linux_paths_and_native_package_compatibility(self):
        result=installer.validate(options(data='/data/pg',prefix='/opt/pg',password='pass;allowed'),linux=True)
        self.assertEqual(result['data'],'/data/pg')
        for path in ('/','/var/lib/postgresql','/data/../root','relative'):
            with self.assertRaises(ValueError):installer.validate(options(data=path,prefix='/opt/pg'),linux=True)
        linux.compatible(distribution(os='debian',os_version='9.13'),dict(ID='debian',VERSION_ID='9'),'x86_64')
        for change,release,machine in (({},dict(ID='ubuntu',VERSION_ID='22.04'),'x86_64'),
            ({'os':'ubuntu','os_version':'20.04'},dict(ID='ubuntu',VERSION_ID='22.04'),'x86_64'),
            ({'os':'debian','os_version':'13.0'},dict(ID='debian',VERSION_ID='13'),'aarch64')):
            with self.assertRaises(ValueError):linux.compatible(distribution(**change),release,machine)

    def test_generic_deb_is_checked_on_ubuntu_debian_and_still_rejects_other_platforms(self):
        row=distribution(os='linux-deb',os_version='')
        for system,version in (('ubuntu','20.04'),('ubuntu','22.04'),('debian','11')):
            linux.compatible(row,dict(ID=system,VERSION_ID=version),'x86_64')
        for release,machine in ((dict(ID='centos',VERSION_ID='7'),'x86_64'),(dict(ID='ubuntu',VERSION_ID='20.04'),'aarch64')):
            with self.assertRaises(ValueError):linux.compatible(row,release,machine)
        with self.assertRaises(ValueError):linux.compatible(dict(row,format='binary'),dict(ID='ubuntu',VERSION_ID='20.04'),'x86_64')

    def test_windows_properties_set_data_before_init_and_suppress_extra_apps(self):
        config=installer.validate(options(data=r'E:\Company\Data',prefix=r'D:\Apps\Postgres'))
        props=installer.windows_properties(config)
        self.assertEqual(props['CUSTOMDATADIR'],r'E:\Company\Data')
        self.assertEqual(props['DATADIR'],'E:\\Company\\Data\\');self.assertEqual(props['DOSERVICE'],'1')
        self.assertEqual(props['DOINITDB'],'1');self.assertEqual(props['SERVICEID'],'ka5432')
        self.assertEqual(props['RUNSTACKBUILDER'],'0');self.assertIn('SUPERPASSWORD',props['MsiHiddenProperties'])


class WindowsInstallerTests(unittest.TestCase):
    def test_related_msi_would_not_silently_upgrade_existing_server(self):
        native=Mock();native.dll.MsiQueryProductStateW.side_effect=[-1,5]
        def related(upgrade,reserved,index,buffer):buffer.value='{11111111-1111-1111-1111-111111111111}';return 0
        native.dll.MsiEnumRelatedProductsW.side_effect=related
        self.assertTrue(installer.registered_product(native,dict(ProductCode='{new}',UpgradeCode='{family}')))
        native.dll.MsiQueryProductStateW.side_effect=None;native.dll.MsiQueryProductStateW.return_value=-1
        native.dll.MsiEnumRelatedProductsW.side_effect=None;native.dll.MsiEnumRelatedProductsW.return_value=259
        self.assertFalse(installer.registered_product(native,dict(ProductCode='{new}',UpgradeCode='{family}')))

    def test_real_server_msi_chosen_instead_of_ui_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'postgres-int.msi').touch();(root/'postgres.msi').touch()
            native=Mock()
            def query(path,sql,columns):
                if 'Property' in sql:
                    props=dict(ProductName='PostgreSQL18.4-1.1C(x64)' if '-int' in str(path) else 'PostgreSQL MUI Wrapper',
                        Manufacturer='1C-Soft',ProductCode='{fixture}',CUSTOMDATADIR=' ',DOINITDB='1',DOSERVICE='1',SERVICEID='default',SUPERPASSWORD=' ',LISTENPORT='5432')
                    return [dict(key=k,value=v) for k,v in props.items()]
                if 'Feature' in sql:return [dict(id=v,level='0' if v=='server' else '1') for v in ('base','server','datadir')]
                return [dict(id=v) for v in ('BASEDIR','DATADIR')]
            native.query.side_effect=query
            result=installer.windows_metadata(root,'18.4-1.1C',native)
            self.assertTrue(result['path'].endswith('postgres-int.msi'))
            with self.assertRaises(ValueError):installer.windows_metadata(root,'17.10-2.1C',native)

    def test_native_msi_enables_server_and_keeps_password_out_of_process_argv(self):
        native=Mock();native.dll.MsiSetInternalUI.return_value=2;native.dll.MsiSetExternalUIRecord.return_value=0
        native.dll.MsiInstallProductW.return_value=0;native.create_transform.return_value=r'C:\fixture\server.mst'
        metadata=dict(path=r'C:\fixture\postgres-int.msi',features=[dict(id='server',level='0')],properties={})
        with patch.object(installer,'Msi',return_value=native),patch.object(installer,'windows_preflight'),patch.object(installer.subprocess,'run') as spawn:
            installer.install_windows_msi(metadata,installer.validate(options()),Mock(),threading.Event())
        spawn.assert_not_called()
        native.create_transform.assert_called_once_with(metadata['path'],["UPDATE `Feature` SET `Level`=1 WHERE `Feature`='server'"])
        args=native.dll.MsiInstallProductW.call_args.args
        self.assertIn('CUSTOMDATADIR="C:\\PG\\data"',args[1]);self.assertIn('TRANSFORMSSECURE="1"',args[1])

    def test_cancel_before_native_msi(self):
        cancel=threading.Event();cancel.set()
        with patch.object(installer,'Msi') as native,self.assertRaises(OperationCancelled):installer.install_windows_msi({},options(),Mock(),cancel)
        native.assert_not_called()


class LinuxInstallerTests(unittest.TestCase):
    def test_default_main_suppressed_before_apt_and_original_restored_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'postgresql-common'/'createcluster.conf'
            config.parent.mkdir();original=b'# original\ncreate_main_cluster = true\n';config.write_bytes(original)
            with self.assertRaises(RuntimeError):
                with linux.suppress_default(config):
                    text=config.read_text();self.assertIn('create_main_cluster = false',text)
                    self.assertNotIn('= true',text);raise RuntimeError('apt failed')
            self.assertEqual(config.read_bytes(),original)
            config.unlink()
            with linux.suppress_default(config):self.assertTrue(config.exists())
            self.assertFalse(config.exists())

    def test_concurrent_admin_edit_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'createcluster.conf';config.write_text('# original')
            with linux.suppress_default(config):config.write_text('# changed by admin')
            self.assertEqual(config.read_text(),'# changed by admin')

    def test_tar_refuses_escape_and_supports_safe_library_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=root/'safe.tar.bz2'
            with tarfile.open(archive,'w:bz2') as target:
                item=tarfile.TarInfo('bin/postgres');item.size=3;target.addfile(item,io.BytesIO(b'bin'))
            linux.extract(archive,root/'safe');self.assertEqual((root/'safe/bin/postgres').read_bytes(),b'bin')
            link=tarfile.TarInfo('safe/lib/libpq.so');link.type=tarfile.SYMTYPE;link.linkname='libpq.so.5'
            self.assertEqual(tarfile.data_filter(link,str(root)).linkname,'libpq.so.5')
            link.linkname='../../../outside'
            with self.assertRaises(tarfile.FilterError):tarfile.data_filter(link,str(root))
            archive=root/'evil.tar'
            with tarfile.open(archive,'w') as target:
                item=tarfile.TarInfo('../escaped');item.size=3;target.addfile(item,io.BytesIO(b'bad'))
            with self.assertRaises(tarfile.FilterError):linux.extract(archive,root/'bad')
            self.assertFalse((root/'escaped').exists())

    def test_deb_install_uses_selected_cluster_data_and_no_default_main(self):
        self.install_with_custom_service('package')

    def test_binary_install_creates_the_same_user_named_service_file(self):
        self.install_with_custom_service('binary')

    def install_with_custom_service(self,package_format):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);job=root/'job';job.mkdir();(job/'payload-0.tar.bz2').touch()
            data=root/'custom-data';program=root/'program';commands=[];policy=root/'createcluster.conf'
            cfg=dict(options(data=str(data),prefix=str(program)),port=5544,networks=['192.168.20.0/24'])
            real_path=Path
            def path(value):
                text=str(value)
                return root/text.lstrip('/') if text.startswith(('/etc/','/usr/')) else real_path(value)
            release=path('/etc/os-release');release.parent.mkdir();release.write_text('ID=debian\nVERSION_ID="13"')
            unit=path('/etc/systemd/system');unit.mkdir(parents=True)
            def unpack(archive,dest):
                dest.mkdir()
                if package_format=='package':(dest/'postgresql-18_18.4-1.1C_amd64.deb').touch()
                else:
                    (dest/'bin').mkdir();(dest/'bin/postgres').touch()
                return dest
            def execute(args,event,cancel,**kwargs):
                commands.append(args)
                if args[0]=='apt-get':
                    if kwargs.get('critical'):self.assertIn('create_main_cluster = false',policy.read_text())
                    else:self.assertFalse(policy.exists())
                if args[0]=='pg_createcluster':
                    data.mkdir(exist_ok=True);conf=path('/etc/postgresql/18/ka5432');conf.mkdir(parents=True)
                    (conf/'postgresql.conf').touch();(conf/'pg_hba.conf').touch()
                if args[0]=='runuser':(data/'postgresql.conf').touch();(data/'pg_hba.conf').touch()
                if args==['systemctl','start','ka5432.service']:
                    self.assertTrue((unit/'ka5432.service').is_file());self.assertFalse((unit/'ka5432.service').is_symlink())
                if str(args[0]).endswith('psql'):return str(data)+'\t18.4\n'
                return ''
            def check_output(args,**kw):
                return 'Package: postgresql-18\nVersion: 18.4-1.1C\nArchitecture: amd64\n' if args[0]=='dpkg-deb' else 'C\n'
            owner=SimpleNamespace(pw_uid=1,pw_gid=1)
            original_suppress=linux.suppress_default
            with patch.object(linux,'Path',side_effect=path),patch.object(linux,'settings',return_value=cfg),\
                 patch.object(linux,'compatible'),patch.object(linux,'extract',side_effect=unpack),\
                 patch.object(linux,'suppress_default',side_effect=lambda:original_suppress(policy)),\
                 patch.object(linux,'execute',side_effect=execute),patch.object(linux.os,'geteuid',return_value=0,create=True),\
                 patch.object(linux.os,'uname',return_value=SimpleNamespace(machine='x86_64'),create=True),\
                 patch.object(linux.os,'chown',create=True),patch.object(linux.os,'fchown',create=True),patch.object(linux.os,'fchmod',create=True),\
                 patch.dict(sys.modules,pwd=SimpleNamespace(getpwnam=lambda name:owner)),patch.object(linux.socket,'socket'),\
                 patch.object(linux.subprocess,'check_output',side_effect=check_output),\
                 patch.object(linux.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='')):
                result=linux.install_instance(dict(settings=cfg,distribution=distribution(os='debian',os_version='13.0',format=package_format,dependencies=['libicu76']),_root=str(job)),Mock(),threading.Event())
            creates=[args for args in commands if args[0]=='pg_createcluster']
            if package_format=='package':
                self.assertEqual(len(creates),1);self.assertEqual(creates[0][1:3],['18','ka5432'])
                self.assertIn('--datadir='+str(data),creates[0]);self.assertIn('--port=5544',creates[0])
                self.assertIn('--start-conf=manual',creates[0])
                hba=path('/etc/postgresql/18/ka5432')/'pg_hba.conf'
            else:self.assertEqual(creates,[]);hba=data/'pg_hba.conf'
            self.assertEqual(result['data'],str(data));self.assertFalse(policy.exists())
            self.assertIn('192.168.20.0/24 scram-sha-256',hba.read_text())
            self.assertEqual(result['service'],'ka5432.service');self.assertEqual(result['unit_file'],str(unit/'ka5432.service'))
            self.assertEqual([args for args in commands if args[0]=='systemctl'],
                             [['systemctl','daemon-reload'],['systemctl','enable','ka5432.service'],['systemctl','start','ka5432.service']])
            content=(unit/'ka5432.service').read_text()
            self.assertIn('WantedBy=multi-user.target',content)
            if package_format=='package':self.assertIn('ExecStart=/usr/bin/pg_ctlcluster --skip-systemctl-redirect 18 ka5432 start',content)
            else:self.assertIn('User=postgres',content);self.assertIn('-D ',content)


class OrchestrationTests(unittest.TestCase):
    def test_download_workspace_removed_after_success_and_failure(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as directory:
                portal=Mock();event=Mock()
                def download(row,destination,event):
                    path=Path(destination)/'distribution.zip';path.write_bytes(b'archive');return str(path)
                portal.download.side_effect=download
                expected=dict(data=r'C:\PG\data',port=5432,service='ka5432')
                with patch.object(installer,'windows_preflight'),patch.object(installer,'install_windows',side_effect=RuntimeError('fixture failure') if fail else None,return_value=expected):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'fixture failure'):installer.install(portal,distribution(),options(),directory,event,threading.Event())
                    else:self.assertEqual(installer.install(portal,distribution(),options(),directory,event,threading.Event()),expected)
                self.assertEqual(list(Path(directory).iterdir()),[])

    def test_existing_instance_stops_before_download(self):
        portal=Mock()
        with tempfile.TemporaryDirectory() as directory,patch.object(installer,'windows_preflight',side_effect=ValueError('Каталог уже занят')),self.assertRaises(ValueError):
            installer.install(portal,distribution(),options(),directory,Mock(),threading.Event())
        portal.download.assert_not_called()


if __name__=='__main__':unittest.main()
