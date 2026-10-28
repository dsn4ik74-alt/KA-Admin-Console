import io
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from operation_progress import PERCENT, ProgressState, duration
from postgres_runner import ByteProgress, OperationCancelled, Runner, VerboseProgress
import test_service_ui
import test_postgres


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.state = ProgressState(lambda: self.now)

    def test_elapsed_without_measurements_never_invents_a_percent(self):
        self.now = 600
        self.assertIsNone(self.state.percent)
        self.assertIsNone(self.state.remaining())
        self.assertIn('10:00', self.state.text())
        self.assertIn('неизвестно', self.state.text())

    def test_eta_uses_observed_rate_and_stops_predicting_after_a_stall(self):
        self.state.update(0)
        self.now = 10
        self.state.update(10)
        self.now = 20
        self.state.update(20)
        self.assertAlmostEqual(self.state.remaining(), 80)
        self.now = 25
        self.assertAlmostEqual(self.state.remaining(), 75)
        self.assertEqual(self.state.percent, 20)
        self.now = 100
        self.assertIsNone(self.state.remaining())
        self.assertEqual(self.state.percent, 20)

    def test_stage_or_percent_reset_does_not_reuse_previous_eta(self):
        self.now = 10
        self.state.update(10)
        self.now = 20
        self.state.update(20)
        self.state.stage('Verification')
        self.assertIsNone(self.state.remaining())
        self.assertIsNone(self.state.percent)
        self.state.update(80, phase='Stream', estimated=True, unit='объектов SQL')
        self.now = 30
        self.state.update(81, phase='Stream', estimated=True, unit='объектов SQL')
        self.assertAlmostEqual(self.state.remaining(), 190)
        self.assertIn('≈81%', self.state.text())
        self.state.update(1)
        self.assertIsNone(self.state.remaining())

    def test_sql_russian_percent_abbreviation_and_multiline_messages(self):
        for text, expected in [('10 проц. обработано.', '10'), ('20 процентов обработано.', '20'),
                               ('Context changed.\n\n30 percent processed.', '30'), ('45,5% processed', '45,5')]:
            self.assertEqual(PERCENT.search(text)[1], expected)
        self.assertIsNone(PERCENT.search('pg_restore: creating TABLE "public.50%"'))

    def test_error_never_marks_complete_and_out_of_range_measurements_are_ignored(self):
        self.state.update(50)
        for value in (float('nan'), float('inf'), -1, 101):
            self.state.update(value)
            self.assertEqual(self.state.percent, 50)
        self.assertNotIn('100%', self.state.finished_text(False))
        self.assertIn('100%', self.state.finished_text(True))
        self.assertEqual(duration(3661), '01:01:01')


class PgTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.runner = Runner(test_postgres.CONNECTION, self.events.append)

    def percentages(self):
        return [item['percent'] for item in self.events if item.get('kind') == 'progress']

    def test_dump_counts_completed_tables_not_catalog_lines_or_duplicate_messages(self):
        progress = VerboseProgress(self.runner, 'Data', 2, {'public.one', 'public.two'})
        progress.line('pg_dump: reading indexes for table "public.one"')
        self.assertEqual(self.percentages(), [])
        progress.line('pg_dump: выгрузка содержимого таблицы "public.one"')
        self.assertEqual(self.percentages(), [0])
        progress.line('pg_dump: dumping contents of table "public.one"')
        self.assertEqual(self.percentages(), [0])
        progress.line('pg_dump: dumping contents of table "public.two"')
        self.assertEqual(self.percentages(), [0, 50])
        progress.finish()
        self.assertEqual(self.percentages(), [0, 50, 100])
        self.assertTrue(self.events[-1]['estimated'])

    def test_restore_counts_verbose_objects_and_waits_for_success_for_100(self):
        progress = VerboseProgress(self.runner, 'Restore', 3)
        for line in ('pg_restore: создаётся TABLE "public.one"',
                     'pg_restore: обрабатываются данные таблицы "public.one"',
                     'pg_restore: creating INDEX "public.idx"'):
            progress.line(line)
        self.assertLess(self.percentages()[-1], 100)
        progress.finish()
        self.assertEqual(self.percentages()[-1], 100)

    def test_bad_catalog_probe_is_optional_but_cancel_is_not_swallowed(self):
        with patch.object(self.runner, 'sql', side_effect=RuntimeError('Access denied')):
            self.assertEqual(self.runner.dump_progress('Target').total, 0)
        with patch.object(self.runner, 'sql', side_effect=OperationCancelled('Stop')):
            with self.assertRaises(OperationCancelled):
                self.runner.dump_progress('Target')

    def test_stream_progress_does_not_change_sql_bytes_or_commit_before_process_exit(self):
        stream = io.BytesIO(b'SELECT 1;\n' * 20000)
        progress = ByteProgress(self.runner, 'SQL', 'подачи SQL', len(stream.getvalue()), stream.tell)
        code = 'import sys; data=sys.stdin.buffer.read();print(len(data))'
        result = self.runner.run([sys.executable, '-c', code], source=stream, capture=True, progress=progress)
        self.assertEqual(int(result), len(stream.getvalue()))
        self.assertEqual(self.percentages()[-1], 100)
        self.assertLess(self.percentages()[-2], 100)

    def test_failed_consumer_does_not_emit_successful_completion(self):
        stream = io.BytesIO(b'SELECT 1;\n' * 20000)
        progress = ByteProgress(self.runner, 'SQL', 'подачи SQL', len(stream.getvalue()), stream.tell)
        with self.assertRaises(RuntimeError):
            self.runner.run([sys.executable, '-c', 'import sys;sys.exit(7)'], source=stream, progress=progress)
        self.assertNotIn(100, self.percentages())


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_sql_percent_eta_and_verification_stage_reset_are_visible(self):
        panel = self.app.sql
        self.app._switch_mode('db_backup')
        now = [0]
        panel.progress_state.clock = lambda: now[0]
        panel.start()
        panel.progress_state.complete_task = True
        panel.event(dict(kind='stage', text='FULL'))
        now[0] = 10
        panel.event(dict(kind='message', text='10 проц. обработано.'))
        now[0] = 20
        panel.event(dict(kind='message', text='20 проц. обработано.'))
        self.assertIn('20% этапа', self.app.progress_pct.cget('text'))
        self.assertIn('Осталось ≈01:20', self.app.progress_pct.cget('text'))
        panel.event(dict(kind='stage', text='Verification'))
        self.assertNotIn('20%', self.app.progress_pct.cget('text'))
        self.assertIsNone(panel.progress_state.remaining())
        panel.finish('Ready')
        self.assertIn('100%', self.app.progress_pct.cget('text'))
        self.assertIsNone(panel._progress_job)

    def test_pg_footer_shows_estimated_unit_and_error_or_cancel_never_shows_100(self):
        panel = self.app.postgres
        self.app._switch_mode('pg_dump')
        panel.start()
        panel.event(dict(kind='progress', percent=25, unit='таблиц', estimated=True, phase='Data', text=''))
        self.assertIn('≈25% таблиц', self.app.progress_pct.cget('text'))
        self.assertNotIn('25', panel.output.get('1.0', 'end-1c'))
        panel.finish_cancelled('Stop')
        self.assertNotIn('100%', self.app.progress_pct.cget('text'))
        self.assertIsNone(panel._progress_job)

    def test_percentage_panel_stays_visible_when_window_is_small_and_clears_on_navigation(self):
        self.app._switch_mode('pg_restore')
        panel = self.app.postgres
        self.app.root.deiconify()
        self.app.root.geometry('960x540+20+20')
        panel.start()
        panel.event(dict(kind='progress', percent=50, unit='объектов SQL', estimated=True, phase='Restore', text=''))
        self.app.root.update()
        label = self.app.progress_pct
        self.assertTrue(label.winfo_viewable())
        self.assertLessEqual(label.winfo_rootx() + label.winfo_width(), self.app.root.winfo_rootx() + self.app.root.winfo_width())
        self.assertLessEqual(label.winfo_rooty() + label.winfo_height(), self.app.root.winfo_rooty() + self.app.root.winfo_height())
        self.assertTrue(panel.execute_button.winfo_viewable())
        panel.finish('Ready')
        self.app._switch_mode('servers')
        self.assertEqual(label.cget('text'), '')
