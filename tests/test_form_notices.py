"""Visible validation failures and error state isolated between background tabs."""
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import test_database_copy
import test_service_ui


class FormNoticeTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def open_window(self):
        self.app.root.deiconify()
        self.app.root.geometry('960x540+15+15')
        self.app.root.update()

    def test_empty_forms_show_reason_without_starting_a_worker_or_modal_dialog(self):
        self.open_window()
        for mode in ('db_backup', 'db_restore', 'pg_dump', 'pg_restore', 'db_copy', 'pg_copy', 'cluster_infobase'):
            with self.subTest(mode=mode):
                self.app._switch_mode(mode)
                panel = self.app._current_panel()
                with patch.object(panel, 'work') as work, patch('tkinter.messagebox.showerror') as modal:
                    panel.execute_button.invoke()
                self.app.root.update()
                self.assertFalse(panel.running)
                work.assert_not_called()
                modal.assert_not_called()
                self.assertTrue(self.app.form_notice.winfo_viewable())
                self.assertIn('Запрос не запущен', self.app.form_notice.heading.cget('text'))
                self.assertEqual(self.app.form_notice.message.cget('text'), panel._last_status)
                self.assertIsNone(self.app.root.grab_current())

    def test_missing_copy_path_is_revealed_and_actions_remain_visible(self):
        self.open_window()
        for mode in ('db_copy', 'pg_copy'):
            panel = (test_database_copy.CopyUiTests.sql_panel(self) if mode == 'db_copy'
                     else test_database_copy.CopyUiTests.pg_panel(self))
            (panel.server_path if mode == 'db_copy' else panel.path).set('')
            with patch.object(panel, 'work') as work:
                panel.execute_button.invoke()
            self.app.root.update()
            self.assertIn('папку', self.app.form_notice.message.cget('text'))
            self.assertTrue(panel.path_entry.winfo_viewable())
            self.assertTrue(panel.execute_button.winfo_viewable())
            self.assertLessEqual(panel.execute_button.winfo_rooty() + panel.execute_button.winfo_height(),
                                 self.app.root.winfo_rooty() + self.app.root.winfo_height())
            work.assert_not_called()

    def test_error_is_kept_for_its_tab_and_cleared_when_retry_starts(self):
        self.app._switch_mode('pg_copy')
        first = self.app._current_panel()
        first.execute()
        message = self.app.form_notice.message.cget('text')
        self.app._switch_mode('db_copy')
        self.assertFalse(self.app.form_notice.winfo_manager())
        self.app._switch_mode('pg_copy')
        self.assertEqual(self.app.form_notice.message.cget('text'), message)
        first.start()
        self.assertIsNone(first._notice)
        self.assertFalse(self.app.form_notice.winfo_manager())
        first.finish('Finished')

    def test_hidden_failure_cannot_replace_visible_tabs_error_or_take_focus(self):
        self.app._switch_mode('pg_copy')
        first = self.app._current_panel()
        first.start()
        self.app._switch_mode('db_copy')
        second = self.app._current_panel()
        second.execute()
        foreground = self.app.form_notice.message.cget('text')
        first.finish('', 'pg_dump 17 cannot dump server 18')
        self.assertEqual(self.app.form_notice.message.cget('text'), foreground)
        self.assertIs(self.app._current_panel(), second)
        self.app._switch_mode('pg_copy')
        self.assertEqual(self.app.form_notice.message.cget('text'), 'pg_dump 17 cannot dump server 18')
        self.assertIn('Ошибка выполнения', self.app.form_notice.heading.cget('text'))
        self.assertIsNone(self.app.root.grab_current())

    def test_dismiss_and_duplicate_tabs_keep_independent_notices(self):
        self.app._switch_mode('pg_copy')
        original = self.app._current_panel()
        original.execute()
        key = self.app.bookmarks.layout.new_tab('Второй сервер', 'pg_copy')
        self.app._switch_mode('pg_copy', key)
        duplicate = self.app._current_panel()
        self.assertFalse(self.app.form_notice.winfo_manager())
        self.app._panel_error(duplicate, 'Choose a backup folder')
        close = next(child for child in self.app.form_notice.winfo_children() if isinstance(child, tk.Button))
        close.invoke()
        self.assertIsNone(duplicate._notice)
        self.assertIsNotNone(original._notice)
        self.assertFalse(self.app.form_notice.winfo_manager())

    def test_bad_parallel_format_does_not_start_and_names_the_correction(self):
        panel = test_database_copy.CopyUiTests.pg_panel(self)
        panel.dump_jobs.set('4')
        with patch.object(panel, 'work') as work:
            panel.execute()
        work.assert_not_called()
        self.assertIn('Каталог .pgdir', self.app.form_notice.message.cget('text'))

    def test_invalid_selection_and_accounts_have_visible_validation(self):
        self.app._switch_mode('pg_dump')
        panel = self.app._current_panel()
        self.app.hub.choose_selected(panel, 'source')
        self.assertIn('одну базу', self.app.form_notice.message.cget('text'))
        self.app._switch_mode('accounts')
        with patch('accounts_ui.run_account_batch') as work:
            self.app.accounts.start()
        work.assert_not_called()
        self.assertIn('ФИО', self.app.form_notice.message.cget('text'))

    def test_unexpected_callback_error_is_visible_and_password_is_redacted(self):
        self.app._switch_mode('pg_dump')
        self.app.postgres.password.set('private-test-value')
        error = ValueError('Invalid connection private-test-value')
        self.app._report_callback_exception(ValueError, error, None)
        self.assertIn('[скрыто]', self.app.form_notice.message.cget('text'))
        self.assertNotIn('private-test-value', self.app.form_notice.message.cget('text'))

    def test_server_search_without_source_and_base_selection_show_the_reason(self):
        self.app._switch_mode('bases')
        self.app._do_rac_search()
        self.assertIn('appsrvrs.lst', self.app.form_notice.message.cget('text'))
        self.assertFalse(self.app.search_running)
        self.app._choose_base()
        self.assertIn('одну базу', self.app.form_notice.message.cget('text'))
        self.app._switch_mode('pg_copy')
        self.assertFalse(self.app.form_notice.winfo_manager())
        self.app._switch_mode('bases')
        self.assertIn('одну базу', self.app.form_notice.message.cget('text'))

    def test_copy_validation_names_the_missing_side(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = (test_database_copy.CopyUiTests.sql_panel(self) if mode == 'db_copy'
                     else test_database_copy.CopyUiTests.pg_panel(self))
            panel.target_tree.selection_remove(*panel.target_tree.selection())
            panel.execute()
            self.assertIn('Получатель:', self.app.form_notice.message.cget('text'))
            panel.tree.selection_remove(*panel.tree.selection())
            panel.execute()
            self.assertIn('Источник:', self.app.form_notice.message.cget('text'))
