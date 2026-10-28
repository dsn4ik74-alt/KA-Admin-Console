"""Linux unit files preserve the exact chosen name and existing service files."""
from pathlib import Path,PurePosixPath
import tempfile
import unittest
from unittest.mock import patch

import postgres_install_linux as linux


class ServiceTests(unittest.TestCase):
    def test_service_name_is_only_the_user_name_plus_suffix(self):
        for name in ('postgresql_5432','CustomPG','db_5544'):
            self.assertEqual(linux.service_filename(name),name+'.service')
        for name in ('../postgresql','db.service','db\nExecStart=bad','a/b',''):
            with self.assertRaises(ValueError):linux.service_filename(name)

    def test_deb_unit_controls_exact_cluster_without_redirecting_to_template(self):
        content=linux.service_content(dict(name='postgresql_5432',data='/srv/pg 14'),dict(format='package',version='14.13-2.1C'),PurePosixPath('/usr/lib/postgresql/14/bin'))
        self.assertIn('ExecStart=/usr/bin/pg_ctlcluster --skip-systemctl-redirect 14 postgresql_5432 start\n',content)
        self.assertIn('ExecStop=/usr/bin/pg_ctlcluster --skip-systemctl-redirect -m fast 14 postgresql_5432 stop\n',content)
        self.assertIn('PIDFile=/run/postgresql/14-postgresql_5432.pid\n',content)
        self.assertIn('Conflicts=postgresql@14-postgresql_5432.service\n',content)
        self.assertNotIn('ExecStart=-',content)
        self.assertIn('RequiresMountsFor="/srv/pg 14"',content)

    def test_binary_unit_escapes_paths_and_stops_postgres_cleanly(self):
        content=linux.service_content(dict(name='MyPG',data='/srv/PG $data%'),dict(format='binary',version='18.4-1.1C'),PurePosixPath('/opt/PG $bin%/bin'))
        self.assertIn('RequiresMountsFor="/srv/PG $data%%"',content)
        self.assertIn('ExecStart="/opt/PG $$bin%%/bin/postgres" -D "/srv/PG $$data%%"',content)
        self.assertIn('User=postgres\n',content);self.assertIn('KillSignal=SIGINT\n',content)
        self.assertNotIn('postgresql@',content)

    def test_service_is_a_real_file_in_etc_and_never_overwrites_an_existing_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def path(value):return root/str(value).lstrip('/')
            target=path('/etc/systemd/system')/'MyPG.service';target.parent.mkdir(parents=True)
            config=dict(name='MyPG',data='/srv/pg');row=dict(format='package',version='14.13-2.1C')
            with patch.object(linux,'Path',side_effect=path):
                actual=linux.create_service(config,row,PurePosixPath('/usr/lib/postgresql/14/bin'))
                self.assertEqual(actual,target);self.assertTrue(target.is_file());self.assertFalse(target.is_symlink())
                original=target.read_bytes()
                with self.assertRaisesRegex(ValueError,'уже существует'):linux.create_service(config,row,PurePosixPath('/usr/lib/postgresql/14/bin'))
                self.assertEqual(target.read_bytes(),original)

    def test_a_vendor_or_runtime_service_cannot_be_shadowed_in_etc(self):
        for location in ('/usr/lib/systemd/system','/lib/systemd/system','/run/systemd/system','/run/systemd/transient','/run/systemd/generator'):
            with self.subTest(location=location),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                def path(value):return root/str(value).lstrip('/')
                existing=path(location)/'MyPG.service';existing.parent.mkdir(parents=True);existing.write_bytes(b'other service')
                with patch.object(linux,'Path',side_effect=path),self.assertRaisesRegex(ValueError,'существует'):linux.service_target('MyPG')
                self.assertEqual(existing.read_bytes(),b'other service')
                self.assertFalse((path('/etc/systemd/system')/'MyPG.service').exists())


if __name__=='__main__':unittest.main()
