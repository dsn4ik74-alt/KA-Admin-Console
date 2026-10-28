import tkinter as tk
import unittest
from unittest.mock import patch

from test_console import app_module
from activity_store import ActivityStore


class ServiceUiTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("services_ui.installed_agents", {}),):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("_discover", None),):
            patcher = patch.object(app_module.App, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("load_settings", {}), ("load_cache", ("", ""))):
            patcher = patch.object(app_module, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(app_module, 'ActivityStore', side_effect=lambda path: ActivityStore())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = app_module.App()
        self.app._switch_mode('servers')
        self.app.root.withdraw()
        self.addCleanup(self.app.root.destroy)
        self.addCleanup(self.app.bookmarks.close)
        self.addCleanup(self.app._cancel_ui_jobs)

    def test_navigation_between_service_forms_accounts_and_servers(self):
        for mode in ("service_create", "service_rename", "accounts", "service_delete", "servers"):
            self.app._switch_mode(mode)
            self.assertEqual(self.app.mode, mode)
            if mode.startswith("service_"):
                self.assertEqual(self.app.services.winfo_manager(), "pack")
                self.assertEqual(self.app.services.forms[mode].winfo_manager(), "pack")
                self.assertEqual(self.app._source_panel.winfo_manager(), "")
                self.assertEqual(self.app.accounts.winfo_manager(), "")
            elif mode == "servers":
                self.assertEqual(self.app.services.winfo_manager(), "")
                self.assertEqual(self.app._source_panel.winfo_manager(), "pack")

    def test_create_preview_follows_port_version_and_respects_manual_path(self):
        panel = self.app.services
        self.app._switch_mode("service_create")
        panel.port.set("4540")
        panel.version.set("8.3.27.1000")
        self.assertEqual(panel.data_path.get(), r"G:\srvinfo_4541")
        self.assertIn("4560:4591", panel.preview.get("1.0", "end-1c"))
        self.assertIn("8.3.27.1000", panel._command.executable)
        panel.data_path.set(r"D:\My data")
        panel.port.set("1640")
        self.assertEqual(panel.data_path.get(), r"D:\My data")
        panel.reset_data()
        self.assertEqual(panel.data_path.get(), r"G:\srvinfo_1641")
        panel.copy_command()
        self.assertEqual(self.app.root.clipboard_get(), panel._command.preview)
        panel.port.set("65540")
        self.assertIsNone(panel._command)
        self.assertEqual(str(panel.execute_button.cget("state")), "disabled")

    def edit_command(self, text):
        panel = self.app.services
        panel.preview.delete('1.0', 'end')
        panel.preview.insert('1.0', text)
        self.app.root.update()

    def test_creation_command_can_be_edited_copied_and_kept_when_fields_or_tabs_change(self):
        panel = self.app.services
        self.app._switch_mode('service_create')
        self.assertEqual(panel.preview.cget('state'), 'normal')
        original = panel.preview.get('1.0', 'end-1c')
        manual = original.replace('start= auto', 'start= demand')
        self.edit_command(manual)
        self.assertIn('demand', panel._command.arguments)
        panel.port.set('4540')
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), manual)
        panel.copy_command()
        self.assertEqual(self.app.root.clipboard_get(), manual)
        self.app._switch_mode('service_rename')
        self.assertEqual(panel.preview.cget('state'), 'disabled')
        self.app._switch_mode('service_create')
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), manual)
        self.assertEqual(panel.preview.cget('state'), 'normal')
        panel.reset_command_button.invoke()
        self.assertIn('start= auto', panel.preview.get('1.0', 'end-1c'))
        self.assertIn('4540', panel._command.name)
        self.assertIsNone(panel._create_draft)

    def test_manual_command_is_captured_for_background_execution_and_not_replaced_by_form(self):
        panel = self.app.services
        self.app._switch_mode('service_create')
        text = panel.preview.get('1.0', 'end-1c').replace('start= auto', 'start= demand')
        self.edit_command(text)
        command = panel._command
        with patch('services_ui.threading.Thread') as thread, patch('services_ui.run_service_command', return_value='OK') as run:
            panel.execute()
            self.assertTrue(panel.running)
            self.assertEqual(panel.preview.cget('state'), 'disabled')
            thread.call_args.kwargs['target']()
            run.assert_called_once_with(command)
            self.app._drain_events()
        self.assertFalse(panel.running)
        self.assertEqual(panel.preview.cget('state'), 'normal')
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), text)
        self.assertIn('start= demand', panel.output.get('1.0', 'end-1c'))

    def test_invalid_manual_command_does_not_run_and_remains_editable(self):
        panel = self.app.services
        self.app._switch_mode('service_create')
        self.edit_command('sc.exe delete "Wrong operation"')
        self.assertIsNone(panel._command)
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), 'sc.exe delete "Wrong operation"')
        self.assertEqual(panel.preview.cget('state'), 'normal')
        self.assertEqual(str(panel.execute_button.cget('state')), 'disabled')
        with patch('services_ui.run_service_command') as run, patch('services_ui.threading.Thread') as thread:
            panel.execute()
            run.assert_not_called()
            thread.assert_not_called()

    def test_create_editor_supports_undo_and_redo_without_programmatic_refresh_resetting_draft(self):
        panel = self.app.services
        self.app._switch_mode('service_create')
        original = panel.preview.get('1.0', 'end-1c')
        panel.preview.insert('end', ' obj= LocalSystem')
        self.app.root.update()
        self.assertEqual(panel._command.arguments[-2:], ('obj=', 'LocalSystem'))
        panel.preview.event_generate('<<Undo>>')
        self.app.root.update()
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), original)
        panel.preview.event_generate('<<Redo>>')
        self.app.root.update()
        self.assertEqual(panel._command.arguments[-2:], ('obj=', 'LocalSystem'))

    def test_field_change_keeps_manual_edit_before_modified_event_has_been_delivered(self):
        panel = self.app.services
        self.app._switch_mode('service_create')
        manual = panel.preview.get('1.0', 'end-1c').replace('start= auto', 'start= demand')
        panel.preview.delete('1.0', 'end')
        panel.preview.insert('1.0', manual)
        panel.port.set('4540')
        self.assertEqual(panel.preview.get('1.0', 'end-1c'), manual)
        self.assertIn('demand', panel._command.arguments)
        panel.preview.insert('end', ' obj= LocalSystem')
        panel.reset_command()
        self.assertIsNone(panel._create_draft)
        self.assertIn('4540', panel._command.name)
        self.assertNotIn('demand', panel._command.arguments)

    def test_rename_delete_forms_require_inputs_and_do_not_execute_on_open(self):
        panel = self.app.services
        with patch("services_ui.run_service_command") as run:
            self.app._switch_mode("service_rename")
            self.assertIsNone(panel._command)
            panel.rename_name.set("1C Agent")
            panel.display_name.set("Новое имя")
            self.assertEqual(panel._command.arguments, ("config", "1C Agent", "DisplayName=", "Новое имя"))
            self.app._switch_mode("service_delete")
            panel.delete_name.set("1C Agent")
            self.assertEqual(panel._command.arguments, ("delete", "1C Agent"))
            run.assert_not_called()

    def test_background_command_captures_form_and_unlocks_after_failure(self):
        panel = self.app.services
        self.app._switch_mode("service_rename")
        panel.rename_name.set("Test Agent")
        panel.display_name.set("New Name")
        command = panel._command
        with patch("services_ui.threading.Thread") as thread, \
                patch("services_ui.run_service_command", side_effect=RuntimeError("SC error")) as run:
            panel.execute()
            self.assertTrue(panel.running)
            self.assertTrue(self.app.bookmarks.enabled)
            self.app._switch_mode("accounts")
            self.assertEqual(self.app.mode, "accounts")
            with patch.object(self.app.root, "destroy") as destroy:
                self.app._on_close()
                destroy.assert_not_called()
            thread.call_args.kwargs["target"]()
            run.assert_called_once_with(command)
            self.app._drain_events()
        self.assertFalse(panel.running)
        self.assertTrue(self.app.bookmarks.enabled)
        self.assertIn("SC error", panel.output.get("1.0", "end-1c"))
        self.assertEqual(str(panel.execute_button.cget("state")), "normal")
        self.assertEqual(panel.rename_name.get(), "Test Agent")

    def test_each_service_operation_keeps_its_own_response_and_status(self):
        panel = self.app.services
        responses = {}
        for mode in ('service_create', 'service_rename', 'service_delete'):
            self.app._switch_mode(mode)
            if mode == 'service_rename':
                panel.rename_name.set('Rename Agent')
                panel.display_name.set('New Name')
            elif mode == 'service_delete':
                panel.delete_name.set('Delete Agent')
            self.assertEqual(panel.output.get('1.0', 'end-1c'), '')
            command = panel._command
            response = 'Response ' + mode
            error = response if mode == 'service_delete' else ''
            panel.finish(command, response if not error else '', error)
            responses[mode] = panel.output.get('1.0', 'end-1c')
        self.app._switch_mode('accounts')
        for mode, response in responses.items():
            self.app._switch_mode(mode)
            self.assertEqual(panel.output.get('1.0', 'end-1c'), response)
            expected = 'Ошибка выполнения' if mode == 'service_delete' else 'Готово'
            self.assertEqual(panel.state_label.cget('text'), expected)


class ResizeTests(unittest.TestCase):
    def test_bottom_controls_keep_full_size_at_small_window_and_high_dpi(self):
        for scale in (1.33, 2.0, 2.5):
            with self.subTest(scale=scale):
                root = tk.Tk()
                root.tk.call("tk", "scaling", scale)
                with patch.object(app_module.tk, "Tk", return_value=root), \
                        patch.object(app_module.App, "_discover"), \
                        patch.object(app_module, "load_settings", return_value={}), \
                        patch.object(app_module, "load_cache", return_value=("", "")), \
                        patch("services_ui.installed_agents", return_value={}):
                    app = app_module.App()
                try:
                    root.geometry("960x540+20+20")
                    app._set_errors(["Test error details"])
                    for mode in ("accounts", "service_create", "service_rename", "service_delete", "db_backup", "db_restore", "jr_archive",
                                 "pg_dump", "pg_restore", "ibcmd_dump", "ibcmd_restore", "servers", "bases"):
                        app._switch_mode(mode)
                        root.update()
                        self.assertEqual(app._footer.winfo_y() + app._footer.winfo_height(), root.winfo_height())
                        self.assertTrue(app.progress.winfo_ismapped())
                        self.assertTrue(app.btn_errors.winfo_ismapped())
                        self.assertEqual(app.btn_errors.winfo_height(), app.btn_errors.winfo_reqheight())
                        self.assertEqual(app.btn_errors.winfo_width(), app.btn_errors.winfo_reqwidth())
                        self.assertEqual(app.status_lbl.winfo_height(), app.status_lbl.winfo_reqheight())
                        if mode == "accounts":
                            buttons = (app.accounts.create_button, app.accounts.preview_button,
                                       app.accounts.copy_button, app.accounts.clear_button)
                        elif mode.startswith("service_"):
                            buttons = (app.services.execute_button, app.services.copy_button)
                        elif mode.startswith('db_') or mode == 'jr_archive':
                            panel = app.sql if mode.startswith('db_') else app.archive
                            buttons = (panel.execute_button, panel.copy_button, panel.clear_button)
                        elif mode in ('pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore'):
                            panel = app.postgres
                            buttons = (panel.execute_button, panel.copy_button, panel.clear_button)
                        else:
                            buttons = (app.btn_update,)
                        for button in buttons:
                            self.assertTrue(button.winfo_ismapped(), button)
                            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight(), (mode, str(button)))
                            self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                            bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
                            self.assertLessEqual(bottom, app._footer.winfo_y())
                    restore_height = round(700 * max(1, scale / 1.33)) + 150
                    root.geometry(f"1380x{restore_height}")
                    app._switch_mode("accounts")
                    root.update()
                    self.assertFalse(app._compact_mode)
                    self.assertTrue(app.accounts.heading.winfo_ismapped())
                    self.assertTrue(app.accounts.input_hint.winfo_ismapped())
                finally:
                    app._cancel_ui_jobs()
                    app.bookmarks.close()
                    root.destroy()


if __name__ == "__main__":
    unittest.main()
