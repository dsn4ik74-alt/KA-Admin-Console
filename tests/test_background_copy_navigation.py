"""Mouse navigation during an executing copy, without database connections."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_database_copy
import test_service_ui


class BackgroundCopyNavigationTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp
    sql_panel = test_database_copy.CopyUiTests.sql_panel
    pg_panel = test_database_copy.CopyUiTests.pg_panel

    def click_navigation(self, key):
        root, bar = self.app.root, self.app.bookmarks
        root.update()
        widget = next(entry for entry in bar._widgets if entry._node == key)
        self.assertTrue(widget.winfo_viewable(), key)
        x, y = widget.winfo_width() // 2, widget.winfo_height() // 2
        for event in ('<ButtonPress-1>', '<ButtonRelease-1>'):
            widget.event_generate(event, x=x, y=y, rootx=widget.winfo_rootx() + x,
                                  rooty=widget.winfo_rooty() + y)
        root.update()

    def exercise_copy(self, mode):
        panel = self.sql_panel() if mode == 'db_copy' else self.pg_panel()
        root = self.app.root
        root.deiconify()
        root.geometry('960x540+10+10')
        root.update()
        errors = Mock()
        root.report_callback_exception = errors
        entered, release = threading.Event(), threading.Event()

        def operation(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test worker was not released')
            return dict(backup_path='test-only.dump')

        backend = 'backup_and_restore' if mode == 'db_copy' else 'run_job'
        try:
            with patch('database_copy_ui.messagebox.askyesno', return_value=True), \
                    patch('database_copy_ui.' + backend, side_effect=operation), \
                    patch.object(self.app.activity, 'begin', return_value=None):
                panel.execute_button.invoke()
                self.assertTrue(entered.wait(1))
                self.assertTrue(panel.running)
                self.assertIsNone(root.grab_current())
                for key in ('folder-1c', 'bases', 'folder-win', 'accounts',
                            'folder-sql', 'folder-mssql', 'db_backup',
                            'folder-sql', 'folder-postgres', 'pg_dump'):
                    self.click_navigation(key)
                self.assertEqual(self.app.mode, 'pg_dump')
                other = self.app._current_panel()
                self.assertEqual(str(other.execute_button.cget('state')), 'normal')
                self.assertTrue(other.start())
                other.finish('Second independent operation finished')
                self.assertTrue(panel.running)
                self.app.running_tasks.entries[mode].event_generate('<Button-1>')
                self.assertIs(self.app._current_panel(), panel)
                errors.assert_not_called()
        finally:
            release.set()
            deadline = time.monotonic() + 3
            while panel.running and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
        self.assertFalse(panel.running)

    def test_mssql_copy_keeps_folder_navigation_and_other_actions_available(self):
        self.exercise_copy('db_copy')

    def test_postgres_copy_keeps_folder_navigation_and_other_actions_available(self):
        self.exercise_copy('pg_copy')
