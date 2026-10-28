"""Synthetic EFDs: bounded reads, catalog validation, cancellation and fallback."""
import io
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

import configuration_efd as efd
import configuration_install as setup
from postgres_runner import OperationCancelled
from test_configuration_install import MFT, PROJECT
from test_configuration_install import DISTRIBUTION
import test_service_ui


def integer(value):return struct.pack('<I', value)
def wide(text):return integer(len(text.encode('utf-16-le')) // 2) + text.encode('utf-16-le')


def directory(entries):
    result = integer(1) + integer(1) + integer(0)
    result += wide('ru') + wide('Конфигурация тест') + wide('1С') + wide('ReadMe.htm') + integer(len(entries))
    for name, size in entries:
        result += integer(0) + wide(name) + struct.pack('<qII', 0, 0, size)
    return result


def bundle(entries):
    raw = directory([(name, len(data)) for name,data in entries]) + b''.join(data for _,data in entries)
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(raw) + compressor.flush()


def templates():
    prefix = '1c/HRMCorp/3_1_38_123/'
    return [(prefix+'1cv8.mft', MFT.encode('utf-8-sig')), (prefix+'1cv8.cf', b'clean-cf-fixture'),
            (prefix+'1Cv8.dt', b'demo-dt-fixture'), (prefix+'ReadMe.htm', b'excluded documentation'),
            (prefix+'helper.exe', b'never executed')]


class EfdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def extract(self, data, cancel=None, event=None, name='templates'):
        return efd.extract_stream(io.BytesIO(data), self.root/name, event or Mock(), cancel or threading.Event(), len(data))

    def test_full_template_variants_are_preserved_without_executables_or_documentation(self):
        events=[]
        result=self.extract(bundle(templates()),event=events.append)
        rows=setup.templates_in(result['path'],PROJECT['version'])
        self.assertEqual([row['variant'] for row in rows],['Пустая','Демонстрационная'])
        self.assertEqual([Path(row['path']).read_bytes() for row in rows],[b'clean-cf-fixture',b'demo-dt-fixture'])
        self.assertEqual(result['files'],3)
        self.assertEqual(list((self.root/'templates').rglob('*.exe')),[])
        self.assertEqual(events[-1]['percent'],100)

    def test_large_body_is_read_in_bounded_portions_and_crosses_chunk_boundaries(self):
        data=bundle([('1c/test/1cv8.mft',b'fixture'),('1c/test/1cv8.cf',b'A'*(3*efd.CHUNK+17))])
        class Guarded(io.BytesIO):
            def read(inner, amount=-1):
                self.assertTrue(0<=amount<=efd.CHUNK)
                return super().read(amount)
        result=efd.extract_stream(Guarded(data),self.root/'templates',Mock(),threading.Event(),len(data))
        self.assertEqual((Path(result['path'])/'1c/test/1cv8.cf').stat().st_size,3*efd.CHUNK+17)

    def test_missing_optional_multilingual_variant_does_not_hide_clean_and_demo(self):
        manifest = MFT + '\n[Config3]\nCatalog=Мультиязычная поставка\nSource=1cv8en.cf\n'
        entries = templates()
        entries[0] = (entries[0][0], manifest.encode('utf-8-sig'))
        result = self.extract(bundle(entries))
        events = []
        rows = setup.installed_templates(result['path'], PROJECT['version'], events.append)
        self.assertEqual([row['demo'] for row in rows], [False, True])
        self.assertTrue(all(Path(row['path']).is_file() for row in rows))
        self.assertEqual(len(events), 1)
        self.assertIn('ПРЕДУПРЕЖДЕНИЕ', events[0]['text'])
        self.assertIn('1cv8en.cf', events[0]['text'])

    def test_no_available_variants_still_fails_with_exact_missing_filename(self):
        manifest = 'Version=3.3.4.9\n[Config1]\nSource=missing.cf\n'
        result = self.extract(bundle([('1cv8.mft', manifest.encode('utf-8'))]))
        with self.assertRaisesRegex(ValueError, 'missing.cf'):
            setup.installed_templates(result['path'], '3.3.4.9', Mock())

    def test_valid_variant_does_not_bypass_unsafe_optional_source(self):
        manifest = MFT + '\n[Config3]\nSource=..\\outside.cf\n'
        entries = templates()
        entries[0] = (entries[0][0], manifest.encode('utf-8-sig'))
        result = self.extract(bundle(entries))
        with self.assertRaisesRegex(ValueError, 'некорректный путь'):
            setup.templates_in(result['path'])

    def test_catalog_uses_unsigned_file_sizes_and_rejects_excessive_total_before_writing(self):
        self.assertEqual(efd.catalog(io.BytesIO(directory([('big.cf',3*1024**3)])))[0][1],3*1024**3)
        with self.assertRaisesRegex(efd.EfdError,'объём'):
            efd.catalog(io.BytesIO(directory([(str(i)+'.cf',2**32-1) for i in range(18)])))

    def test_unsafe_paths_aliases_reserved_devices_and_file_directory_collisions_are_rejected(self):
        for name in ('../escape.cf','/root.cf','C:\\root.cf','\\\\host\\share\\a.cf','a.cf:stream','nul.cf','dir./a.cf','foo\\..\\a.cf'):
            with self.subTest(name=name),self.assertRaises(efd.EfdError):
                efd.catalog(io.BytesIO(directory([(name,1)])))
        for names in (['a.cf','A.cf'],['a','a/b.cf'],['a/b.cf','a'],['a/./b.cf','a/b.cf']):
            with self.subTest(names=names),self.assertRaisesRegex(efd.EfdError,'Конфликт'):
                efd.catalog(io.BytesIO(directory([(name,1) for name in names])))
        self.assertEqual(list(self.root.iterdir()),[])

    def test_invalid_header_metadata_truncation_and_deflate_truncation_never_report_success(self):
        for data in (integer(2),integer(1)+integer(101),integer(1)+integer(0)+integer(100001),directory([('a.cf',1)])[:-2]):
            with self.subTest(data=data[:12]),self.assertRaises(efd.EfdError):efd.catalog(io.BytesIO(data))
        compressed=bundle(templates())
        with self.assertRaises(efd.EfdError):self.extract(compressed[:-3])
        with self.assertRaisesRegex(efd.EfdError,'Лишние данные'):self.extract(compressed+b'extra',name='extra')

    def test_body_length_and_trailing_unpacked_content_are_checked(self):
        for extra in (b'',b'XY'):
            raw=directory([('test.mft',1)])+extra
            comp=zlib.compressobj(wbits=-15);data=comp.compress(raw)+comp.flush()
            with self.subTest(extra=extra),self.assertRaises(efd.EfdError):self.extract(data,name='case'+str(len(extra)))

    def test_cancel_during_a_large_file_removes_partial_file(self):
        cancel=threading.Event()
        data=bundle([('test.mft',b'a'),('test.cf',b'x'*(5*efd.CHUNK))])
        class Interrupted(io.BytesIO):
            calls=0
            def read(inner,amount=-1):
                inner.calls+=1
                if inner.calls>=2:cancel.set()
                return super().read(min(amount,128))
        with self.assertRaises(OperationCancelled):
            efd.extract_stream(Interrupted(data),self.root/'templates',Mock(),cancel,len(data))
        self.assertEqual(list(self.root.rglob('*.part')),[])
        self.assertFalse((self.root/'templates/test.cf').exists())

    def test_low_disk_space_and_missing_manifest_fail_before_writing_template_files(self):
        with patch.object(efd.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(efd.EfdError,'места'):self.extract(bundle(templates()))
        self.assertEqual(list((self.root/'templates').iterdir()),[])
        with self.assertRaisesRegex(efd.EfdError,'манифест'):self.extract(bundle([('alone.cf',b'cf')]),name='alone')
        self.assertFalse((self.root/'alone').exists())

    def test_existing_output_is_not_overwritten_and_initial_cancel_creates_nothing(self):
        target=self.root/'templates';target.mkdir();(target/'keep').write_bytes(b'user')
        with self.assertRaises(FileExistsError):self.extract(bundle(templates()))
        self.assertEqual((target/'keep').read_bytes(),b'user')
        cancel=threading.Event();cancel.set()
        with self.assertRaises(OperationCancelled):self.extract(bundle(templates()),cancel,name='cancelled')
        self.assertFalse((self.root/'cancelled').exists())


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.unpacked=self.root/'unpacked';self.unpacked.mkdir()
        (self.unpacked/'setup.exe').write_bytes(b'fixture-not-executable')
        self.efd=self.unpacked/'1cv8.efd';self.efd.write_bytes(bundle(templates()))
        self.destination=self.root/'templates'
        self.config=self.root/'1cestart.cfg'

    def execute(self,method='auto',native=None,cancel=None):
        with patch.object(setup,'launcher_config_path',return_value=self.config),patch.object(setup,'run_native',side_effect=native) as run:
            result=setup.prepare_templates(self.unpacked,self.destination,PROJECT['version'],Mock(),cancel or threading.Event(),method)
        return result,run

    def test_empty_setup_with_zero_exit_falls_back_and_restores_profile(self):
        original='DefaultVersion=8.3\r\n'.encode('utf-16');self.config.write_bytes(original)
        rows,run=self.execute()
        self.assertEqual(len(rows),2);run.assert_called_once()
        self.assertIn('templates-efd',rows[0]['path'])
        self.assertEqual(self.config.read_bytes(),original)

    def test_nonzero_setup_falls_back_but_forced_setup_reports_the_original_failure(self):
        rows,_=self.execute(native=RuntimeError('setup: code 1'))
        self.assertEqual(len(rows),2)
        with self.assertRaisesRegex(RuntimeError,'code 1'):self.execute('setup',RuntimeError('setup: code 1'))

    def test_stream_mode_does_not_launch_setup_or_touch_launcher_config(self):
        rows,run=self.execute('stream')
        run.assert_not_called();self.assertFalse(self.config.exists());self.assertEqual(len(rows),2)

    def test_auto_large_efd_bypasses_setup(self):
        original=Path.stat
        def size(path,*args,**kwargs):
            info=original(path,*args,**kwargs)
            return SimpleNamespace(st_size=3*1024**3,st_mode=info.st_mode) if path==self.efd else info
        with patch.object(Path,'stat',size):rows,run=self.execute()
        self.assertEqual(len(rows),2);run.assert_not_called();self.assertFalse(self.config.exists())

    def test_cancelling_setup_never_triggers_a_second_operation(self):
        with patch.object(setup,'extract_efd') as extract:
            with self.assertRaises(OperationCancelled):self.execute(native=OperationCancelled('cancelled'))
        extract.assert_not_called();self.assertFalse(self.config.exists())


class MethodUiTests(unittest.TestCase):
    setUp=test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('configuration_install')
        return self.app.configuration_install

    def test_method_is_click_selected_and_readonly_after_completion(self):
        panel=self.panel()
        self.assertEqual(str(panel.setup_choice['state']),'readonly')
        panel.setup_choice.event_generate('<MouseWheel>',delta=-120)
        self.assertEqual(panel.setup_method.get(),'Автоматически')
        panel.start()
        self.assertEqual(str(panel.setup_choice['state']),'disabled')
        panel.finish()
        self.assertEqual(str(panel.setup_choice['state']),'readonly')

    def test_worker_captures_selected_method_before_start(self):
        panel=self.panel();panel.portal=Mock()
        panel.distributions=[DISTRIBUTION];panel.render('distributions')
        panel.trees['distributions'].selection_set('0')
        panel.setup_method.set('Потоковое извлечение EFD')
        with patch.object(panel,'work') as work:panel.execute()
        panel.setup_method.set('Через setup.exe')
        with patch('configuration_install_ui.install_configuration') as install:
            work.call_args.args[0](Mock())
        self.assertEqual(install.call_args.kwargs['method'],'stream')


if __name__=='__main__':unittest.main()
