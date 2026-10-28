from collections import deque
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import unittest
from unittest.mock import patch

from bookmark_navigation import BookmarkBar
from ui_dispatch import JobEvents
import test_service_ui


class DeliveryTests(unittest.TestCase):
    def test_worker_flood_has_one_pending_callback_and_keeps_last_messages(self):
        callbacks, seen, done = deque(), [], []
        delivery = JobEvents(callbacks.append, seen.extend, limit=256)
        def worker():
            delivery.emit({'kind': 'stage', 'text': 'Backup'})
            for number in range(20000):
                delivery.emit({'kind': 'log', 'text': f'line {number}'})
            delivery.emit({'kind': 'progress', 'percent': 100})
            delivery.complete(lambda: done.append(len(seen)))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(callbacks), 1)
        self.assertLessEqual(len(delivery._items), 257)
        callbacks.popleft()()
        self.assertEqual(done, [])
        while callbacks:
            callbacks.popleft()()
        self.assertEqual(done, [len(seen)])
        self.assertEqual(seen[1]['text'], 'Backup')
        self.assertEqual(seen[-2]['text'], 'line 19999')
        self.assertEqual(seen[-1]['percent'], 100)
        self.assertIn('19746', seen[0]['text'])

    def test_phase_order_and_terminal_survive_a_consumer_exception(self):
        callbacks, seen, done = deque(), [], []
        def consume(events):
            seen.extend(events)
            if len(seen) == 128:
                raise RuntimeError('render failed')
        delivery = JobEvents(callbacks.append, consume)
        for number in range(260):
            delivery.emit({'kind': 'log', 'text': str(number)})
        delivery.complete(done.append, 'finished')
        with self.assertRaisesRegex(RuntimeError, 'render failed'):
            callbacks.popleft()()
        while callbacks:
            callbacks.popleft()()
        self.assertEqual([int(event['text']) for event in seen], list(range(260)))
        self.assertEqual(done, ['finished'])

    def test_measurements_coalesce_only_within_their_phase(self):
        callbacks, seen = deque(), []
        delivery = JobEvents(callbacks.append, seen.extend)
        for phase in ('Backup', 'Restore'):
            for percent in range(101):
                delivery.emit({'kind': 'progress', 'phase': phase, 'percent': percent})
        while callbacks:
            callbacks.popleft()()
        self.assertEqual([(event['phase'], event['percent']) for event in seen],
                         [('Backup', 100), ('Restore', 100)])

    def test_long_lines_are_bounded_by_content_size_too(self):
        callbacks, seen = deque(), []
        delivery = JobEvents(callbacks.append, seen.extend, byte_limit=10000)
        for _ in range(20):
            delivery.emit({'kind': 'log', 'text': 'a' * 2000})
        self.assertLessEqual(delivery._size, 10000)
        while callbacks:
            callbacks.popleft()()
        self.assertEqual(len(seen), 6)

    def test_stage_flood_is_bounded_and_keeps_latest_stage(self):
        callbacks, seen = deque(), []
        delivery = JobEvents(callbacks.append, seen.extend, limit=8)
        for number in range(100):
            delivery.emit({'kind': 'stage', 'text': str(number)})
        self.assertEqual(len(delivery._items), 8)
        while callbacks:
            callbacks.popleft()()
        self.assertEqual(seen[-1]['text'], '99')


class InterfaceTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_callback_failure_is_reported_without_closing_app_or_exposing_passwords(self):
        app = self.app
        panel = app.postgres_copy
        panel.password.set('fixture-source-secret')
        panel.target_password.set('fixture-target-secret')
        app._switch_mode('pg_copy')
        def fail():
            raise RuntimeError('Failure with ' + panel.password.get() + ' and ' + panel.target_password.get())
        app.root.after(1, fail)
        app.root.after(30, app.root.quit)
        app.root.mainloop()
        self.assertTrue(app.root.winfo_exists())
        self.assertTrue(app._errors)
        details = '\n'.join(app._errors)
        self.assertIn('[скрыто]', details)
        self.assertNotIn('fixture-source-secret', details)
        self.assertNotIn('fixture-target-secret', details)
        self.assertEqual(panel.password.get(), 'fixture-source-secret')
        seen = []
        app._post(seen.append, 'Still responsive')
        app._drain_events()
        self.assertEqual(seen, ['Still responsive'])

    def test_f5_updates_current_source_or_target_without_retyping_connection(self):
        app = self.app
        app.root.deiconify()
        for mode in ('db_backup', 'db_restore', 'pg_dump', 'pg_restore', 'cluster_infobase'):
            app._switch_mode(mode)
            panel = app._pages[mode]
            with patch.object(app.hub, 'refresh_databases') as refresh:
                app._on_update_clicked()
                refresh.assert_called_once_with(panel, 'source')
        for mode in ('db_copy', 'pg_copy'):
            app._switch_mode(mode)
            panel = app._pages[mode]
            app.root.update()
            panel.target_tree.focus_force()
            app.root.update()
            with patch.object(app.hub, 'refresh_databases') as refresh:
                app._on_update_clicked()
                refresh.assert_called_once_with(panel, 'target')
        panel.start()
        with patch.object(app.hub, 'refresh_databases') as refresh:
            app._on_update_clicked()
            refresh.assert_not_called()
        panel.finish('Test complete')

    def test_event_pump_yields_to_tk_and_survives_a_callback_error(self):
        app = self.app
        done = []
        def slow(number):
            time.sleep(.005)
            done.append(number)
        def fail():
            raise RuntimeError('test callback')
        app._post(fail)
        for number in range(30):
            app._post(slow, number)
        with patch.object(app.root, 'report_callback_exception') as report:
            app._drain_events()
            report.assert_called_once()
        self.assertGreater(len(done), 0)
        self.assertLess(len(done), 30)
        counts = []
        app.root.after(0, lambda: counts.append(len(done)))
        deadline = time.monotonic() + 3
        while len(done) < 30 and time.monotonic() < deadline:
            app.root.update()
        self.assertEqual(done, list(range(30)))
        self.assertLess(counts[0], 30)

    def test_flood_is_batched_while_tk_remains_responsive_and_completion_is_last(self):
        panel, app = self.app.postgres, self.app
        app._switch_mode('pg_dump')
        app.root.deiconify()
        app.root.update()
        panel.start()
        ticks, completed = [], []
        def heartbeat():
            ticks.append(time.monotonic())
            if panel.running:
                app.root.after(5, heartbeat)
        def operation(emit):
            emit({'kind': 'stage', 'text': 'Backup'})
            for number in range(20000):
                emit({'kind': 'log', 'text': f'row {number}'})
            emit({'kind': 'progress', 'percent': 100})
            return 'done'
        def finish(result):
            completed.append(panel.output.get('1.0', 'end-1c'))
            panel.finish(result)
        with patch.object(panel, 'append', wraps=panel.append) as append:
            panel.work(operation, finish)
            heartbeat()
            deadline = time.monotonic() + 8
            while panel.running and time.monotonic() < deadline:
                app.root.update()
            self.assertFalse(panel.running)
            self.assertLess(append.call_count, 200)
        self.assertIn('row 19999', completed[0])
        self.assertGreater(len(ticks), 1)
        self.assertLess(int(panel.output.index('end-1c').split('.')[0]), 5001)

    def test_log_preserves_manual_scroll_and_follows_when_at_bottom(self):
        app, panel = self.app, self.app.sql
        app._switch_mode('db_backup')
        app.root.deiconify()
        app.root.update()
        panel.append('\n'.join(f'line {number}' for number in range(500)))
        app.root.update()
        panel.output.yview_moveto(0)
        app.root.update()
        top = panel.output.index('@0,0')
        panel.append('new message')
        app.root.update()
        self.assertEqual(panel.output.index('@0,0'), top)
        panel.output.see(tk.END)
        panel.append('last message')
        app.root.update()
        self.assertGreater(panel.output.yview()[1], .99)

    def test_batch_progress_uses_last_measurement_after_last_stage(self):
        panel = self.app.sql
        panel.events([{'kind': 'stage', 'text': 'Backup'},
                      {'kind': 'log', 'text': '10 percent processed.\n20 percent processed.'},
                      {'kind': 'stage', 'text': 'Verify'},
                      {'kind': 'progress', 'percent': 3}])
        self.assertEqual(panel.progress_state.percent, 3)
        self.assertEqual(panel.state_label.cget('text'), 'Verify')

    def test_result_render_failure_unlocks_controls_without_repeating_operation(self):
        panel = self.app.sql
        panel.start()
        calls = []
        def operation(emit):
            calls.append('executed')
            return 'done'
        def success(result):
            raise ValueError('bad response')
        with patch('administration_ui.threading.Thread') as thread:
            panel.work(operation, success)
            thread.call_args.kwargs['target']()
        self.app._drain_events()
        self.assertFalse(panel.running)
        self.assertTrue(self.app.bookmarks.enabled)
        self.assertEqual(calls, ['executed'])
        self.assertIn('bad response', panel.output.get('1.0', 'end-1c'))


class CompactNavigationTests(unittest.TestCase):
    def test_fractional_dpi_has_compact_icons_and_legible_folder_rows(self):
        for scale in (1.33, 1.67, 2.0, 2.5):
            with self.subTest(scale=scale):
                root = tk.Tk()
                try:
                    root.tk.call('tk', 'scaling', scale)
                    bar = BookmarkBar(root, test_service_ui.app_module.C, None, lambda page: None, lambda layout: None)
                    bar.pack(fill=tk.X)
                    root.update_idletasks()
                    self.assertLessEqual(bar.folder_icon.width(), round(18 * max(1, scale / 1.333)) + 1)
                    row = bar._widgets[0]
                    font_height = tkfont.Font(root, font=row.cget('font')).metrics('linespace')
                    self.assertLessEqual(row.winfo_reqheight(), font_height + 13)
                    self.assertIn('PostgreSQL', bar.breadcrumb('pg_copy'))
                    self.assertIn('›', bar.breadcrumb('pg_copy'))
                    bar._open(row)
                    self.assertTrue(bar._menus)
                    bar.close()
                finally:
                    root.destroy()
