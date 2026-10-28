"""Independent tab state, concurrent workers and foreground progress routing."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_service_ui


class BackgroundTabTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def duplicate(self, page='pg_dump', title='Другой сервер'):
        key = self.app.bookmarks.layout.new_tab(title, page)
        self.app._switch_mode(page, key)
        return key, self.app._current_panel()

    def test_duplicate_tabs_keep_independent_servers_passwords_logs_and_cancellation(self):
        self.app._switch_mode('pg_dump')
        first = self.app.postgres
        first.host.set('server-one')
        first.password.set('first-secret')
        self.assertTrue(first.start())
        first.append('First job log')
        key, second = self.duplicate()
        second.host.set('server-two')
        second.password.set('second-secret')
        self.assertIsNot(first, second)
        self.assertEqual(first.host.get(), 'server-one')
        self.assertEqual(first.password.get(), 'first-secret')
        self.assertEqual(first.output.get('1.0', 'end-1c'), 'First job log\n')
        self.assertNotIn('First job log', second.output.get('1.0', 'end-1c'))
        self.assertTrue(second.start())
        self.assertFalse(second.start())
        self.assertEqual(len(self.app.running_tasks.entries), 2)
        second.interrupt()
        self.assertTrue(second.cancel_event.is_set())
        self.assertFalse(first.cancel_event.is_set())
        self.assertEqual(str(first.host_entry.cget('state')) if hasattr(first, 'host_entry') else str(first.execute_button.cget('state')), 'disabled')
        self.assertEqual(self.app._active_tab_key, key)
        first.finish('First finished')
        self.assertEqual(len(self.app.running_tasks.entries), 1)
        second.finish_cancelled('Second stopped')

    def test_hidden_progress_status_and_finish_do_not_overwrite_visible_job(self):
        self.app._switch_mode('pg_dump')
        first = self.app.postgres
        first.start()
        key, second = self.duplicate()
        second.start()
        second.event(dict(kind='stage', text='Second stage'))
        second.event(dict(kind='progress', percent=79, phase='Second'))
        before = self.app.progress_pct.cget('text')
        first.event(dict(kind='stage', text='Hidden first stage'))
        first.event(dict(kind='progress', percent=20, phase='First'))
        self.assertEqual(self.app.progress_pct.cget('text'), before)
        self.assertEqual(self.app.status_lbl.cget('text'), 'Second stage')
        first.finish('First finished')
        self.assertEqual(self.app.progress_pct.cget('text'), before)
        self.assertEqual(self.app.status_lbl.cget('text'), 'Second stage')
        self.assertTrue(second.running)
        self.app._switch_mode('pg_dump')
        self.assertIs(self.app.postgres, first)
        self.assertIn('First finished', first.output.get('1.0', 'end-1c'))
        self.assertIn('завершено', self.app.progress_pct.cget('text'))
        self.app._switch_mode('pg_dump', key)
        self.assertIs(self.app.postgres, second)
        self.assertIn('79%', self.app.progress_pct.cget('text'))
        second.finish('Done')

    def test_two_real_workers_run_at_the_same_time_and_deliver_to_own_logs(self):
        self.app._switch_mode('pg_dump')
        first = self.app.postgres
        _, second = self.duplicate()
        ready = [threading.Event(), threading.Event()]
        release = threading.Event()
        def operation(number):
            def run(event):
                ready[number].set()
                if not release.wait(3):
                    raise RuntimeError('Concurrent worker did not start')
                event(dict(kind='message', text='Worker ' + str(number)))
                return number
            return run
        try:
            for number, panel in enumerate((first, second)):
                self.assertTrue(panel.start())
                panel.work(operation(number), lambda result, p=panel: p.finish('Finished ' + str(result)))
            self.assertTrue(all(event.wait(1) for event in ready))
            self.assertTrue(first.running and second.running)
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while any(panel.running for panel in (first, second)) and time.monotonic() < deadline:
            self.app._drain_events()
            self.app.root.update()
            time.sleep(.01)
        for number, panel in enumerate((first, second)):
            self.assertFalse(panel.running)
            self.assertIn('Worker ' + str(number), panel.output.get('1.0', 'end-1c'))
            self.assertNotIn('Worker ' + str(1-number), panel.output.get('1.0', 'end-1c'))

    def test_busy_custom_tab_cannot_close_and_task_badge_reopens_exact_instance(self):
        key, panel = self.duplicate()
        panel.start()
        self.app.bookmarks._close_tab(key)
        self.assertIsNotNone(self.app.bookmarks.layout.find(key))
        self.app._switch_mode('accounts')
        self.app.root.deiconify()
        self.app.root.update()
        badge = self.app.running_tasks.entries[key]
        badge.event_generate('<Button-1>')
        self.assertIs(self.app._current_panel(), panel)
        self.assertEqual(self.app._active_tab_key, key)
        self.assertTrue(self.app.bookmarks.enabled)
        self.assertIsNotNone(self.app.running_tasks._job)
        panel.finish('Done')
        self.assertEqual(self.app.running_tasks.entries, {})
        self.assertIsNone(self.app.running_tasks._job)

    def test_active_shared_mode_is_split_when_another_mode_is_opened(self):
        self.app._switch_mode('pg_dump')
        first = self.app.postgres
        first.start()
        self.app._switch_mode('pg_restore')
        restore = self.app.postgres
        self.assertIsNot(first, restore)
        self.assertEqual(first.mode, 'pg_dump')
        self.assertEqual(restore.mode, 'pg_restore')
        self.assertEqual(str(restore.execute_button.cget('state')), 'normal')
        self.app._switch_mode('pg_dump')
        self.assertIs(self.app.postgres, first)
        first.finish('Done')

    def test_new_tab_context_opens_a_new_instance_without_touching_running_job(self):
        self.app._switch_mode('pg_dump')
        first = self.app.postgres
        first.host.set('kept-host')
        first.start()
        with patch.object(self.app, '_save_settings'):
            self.app.bookmarks._duplicate_tab('pg_dump')
        self.assertIsNot(self.app.postgres, first)
        self.assertEqual(first.host.get(), 'kept-host')
        self.assertTrue(first.running)
        self.assertTrue(self.app._active_tab_key.startswith('tab-'))
        first.finish('Done')

    def test_reset_keeps_running_custom_tab_and_close_releases_idle_instance(self):
        key, panel = self.duplicate()
        panel.start()
        with patch.object(self.app, '_save_settings'):
            self.app.bookmarks._reset()
        self.assertIsNotNone(self.app.bookmarks.layout.find(key))
        panel.finish('Done')
        with patch.object(self.app, '_save_settings'):
            self.app.bookmarks._close_tab(key)
        self.assertNotIn(key, self.app._pages)
        self.assertFalse(panel.winfo_exists())
        self.assertEqual(self.app.mode, 'home')

    def test_server_search_does_not_block_refreshing_another_tab(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        self.app.search_running = True
        try:
            with patch.object(self.app.hub, 'refresh_databases') as refresh:
                self.app._on_update_clicked()
                refresh.assert_called_once_with(panel, 'source')
            self.assertTrue(panel.start())
            panel.finish('Independent task completed')
        finally:
            self.app.search_running = False
