"""Portable payload integrity and tab lifecycle; no real program is launched."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from windirstat_host import NativeHost, fitted_widths, prepare_executable, launch, vendor_directory
from bookmark_navigation import NavigationLayout
import test_service_ui


class PortableTests(unittest.TestCase):
    def test_original_portable_payloads_and_sources_match_pinned_hashes(self):
        root = vendor_directory()
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'], '2.9.3')
        for relative, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root / relative).read_bytes()).hexdigest(), digest)
        self.assertIn('Version 3, 29 June 2007', (root / 'LICENSE.txt').read_text(encoding='utf-8'))

    def test_user_cache_contains_original_executable_and_retains_portable_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = prepare_executable(directory)
            ini = executable.with_suffix('.ini')
            self.assertIn('LanguageId=1049', ini.read_text(encoding='utf-16'))
            translation = executable.with_name('lang_ru.txt')
            self.assertEqual(translation.read_bytes(), (vendor_directory() / 'lang_ru.txt').read_bytes())
            ini.write_text('[Options]\nLanguageId=1033\n', encoding='utf-16')
            translation.write_text('outdated cached translation', encoding='utf-8')
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            self.assertEqual(prepare_executable(directory), executable)
            self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), digest)
            self.assertIn('LanguageId=1033', ini.read_text(encoding='utf-16'))
            self.assertEqual(translation.read_bytes(), (vendor_directory() / 'lang_ru.txt').read_bytes())

    def test_launch_passes_one_owned_folder_argument_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory, patch('windirstat_host.subprocess.Popen') as spawn:
            launch(r'C:\KA\WinDirStat.exe', directory)
            self.assertEqual(spawn.call_args.args[0], [r'C:\KA\WinDirStat.exe', str(Path(directory).resolve())])
            self.assertNotIn('shell', spawn.call_args.kwargs)
            self.assertEqual(spawn.call_args.kwargs['startupinfo'].wShowWindow, 1)

    def test_navigation_upgrade_places_windirstat_in_win_folder(self):
        layout = NavigationLayout([dict(id='folder-win', title='Win', children=[dict(id='accounts', title='Создание УЗ')])])
        self.assertIn('windirstat', [node['id'] for node in layout.children('folder-win')])
        self.assertEqual(sum(node['id'] == 'windirstat' for node, _ in layout.walk()), 1)


class UiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_real_hidden_windows_report_control_fits_without_changing_data_or_hidden_columns(self):
        import ctypes
        from ctypes import wintypes
        import os
        import tkinter as tk
        native=NativeHost()
        dll=native.dll
        dll.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,
            ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
        dll.CreateWindowExW.restype=wintypes.HWND
        dll.DestroyWindow.argtypes=[wintypes.HWND]
        dll.DestroyWindow.restype=wintypes.BOOL
        class Column(ctypes.Structure):
            _fields_=[('mask',wintypes.UINT),('fmt',ctypes.c_int),('cx',ctypes.c_int),
                      ('text',wintypes.LPWSTR),('capacity',ctypes.c_int),('subitem',ctypes.c_int),
                      ('image',ctypes.c_int),('order',ctypes.c_int),('minimum',ctypes.c_int),
                      ('default',ctypes.c_int),('ideal',ctypes.c_int)]
        frame=tk.Frame(self.app.root)
        parent=frame.winfo_id()
        control=dll.CreateWindowExW(0,'SysListView32','owned test table',0x50000001,0,0,700,200,parent,None,None,None)
        self.assertTrue(control)
        def send(message,index=0,value=0):
            result=ctypes.c_size_t()
            self.assertTrue(dll.SendMessageTimeoutW(control,message,index,value,2,100,ctypes.byref(result)))
            return result.value
        try:
            for index,width in enumerate((900,200,0,200)):
                column=Column(mask=2,cx=width)
                send(0x1061,index,ctypes.addressof(column))
            before_count=send(0x1004)
            with patch.object(dll,'IsWindowVisible',return_value=True):native.fit_columns(parent,os.getpid())
            result=[send(0x101d,index) for index in range(4)]
            rect=wintypes.RECT()
            self.assertTrue(dll.GetClientRect(control,ctypes.byref(rect)))
            self.assertEqual(sum(result),rect.right-rect.left-2)
            self.assertEqual(result[2],0)
            self.assertEqual(send(0x1004),before_count)
        finally:
            dll.DestroyWindow(control)
            frame.destroy()

    def panel_with_process(self):
        self.app._switch_mode('windirstat')
        panel = self.app.windirstat
        panel.process, panel.native, panel.hwnd = Mock(pid=1234), Mock(), 123
        panel.process.poll.return_value = None
        panel.native.dialogs.return_value = []
        panel.native.menus.return_value = [(0,'Файл',321)]
        panel._deadline = time.monotonic()+30
        self.addCleanup(lambda:setattr(panel,'process',None))
        return panel

    def test_uninitialized_frame_is_not_attached_or_cached_as_raw_menu(self):
        panel = self.panel_with_process()
        panel.native.ready.return_value = False
        with patch.object(panel.host,'winfo_ismapped',return_value=True):
            panel.poll_window()
            panel.native.attach.assert_not_called()
            self.assertFalse(panel.menu_bar.winfo_children())
            panel.native.ready.return_value = True
            panel.poll_window()
        panel.native.attach.assert_called_once_with(123,panel.host.winfo_id())
        self.assertTrue(panel.embedded)
        self.assertEqual([widget.cget('text') for widget in panel.menu_bar.winfo_children()],['Файл'])

    def test_shell_commands_detach_before_posting_and_reattach_same_process(self):
        for command in (32805,32779):
            with self.subTest(command=command):
                panel = self.panel_with_process()
                panel.embedded = True
                process = panel.process
                order=[]
                panel.native.detach.side_effect=lambda hwnd:order.append('detach')
                panel.native.command.side_effect=lambda hwnd,cmd:order.append(cmd)
                panel.native_command(command)
                self.assertEqual(order,['detach',command])
                self.assertFalse(panel.embedded)
                self.assertFalse(panel.external)
                panel.native.dialogs.return_value=[456]
                panel.poll_window()
                panel.native.show_dialog.assert_called_once_with(456,123)
                panel.native.dialogs.return_value=[]
                panel._resume_at = 0
                with patch.object(panel.host,'winfo_ismapped',return_value=True):panel.poll_window()
                self.assertTrue(panel.embedded)
                self.assertIs(panel.process,process)

    def test_dialog_opened_from_native_context_menu_detaches_and_does_not_block_other_tabs(self):
        panel = self.panel_with_process()
        panel.embedded = True
        panel.native.dialogs.return_value=[456]
        panel.poll_window()
        panel.native.detach.assert_called_once_with(123)
        panel.native.show_dialog.assert_called_once_with(456,123)
        panel.poll_window()
        panel.native.show_dialog.assert_called_once()
        self.app._switch_mode('accounts')
        self.assertEqual(self.app.mode,'accounts')
        self.assertIsNotNone(panel.process)

    def test_failed_attachment_leaves_visible_external_window_and_manual_retry(self):
        panel = self.panel_with_process()
        panel.native.attach.side_effect=RuntimeError('cannot attach')
        with patch.object(panel.host,'winfo_ismapped',return_value=True):
            panel.poll_window()
            self.assertFalse(panel.embedded)
            self.assertTrue(panel.external)
            panel.native.detach.assert_called_once_with(123)
            self.assertIn('cannot attach',panel.state.cget('text'))
            panel.native.attach.side_effect=None
            panel.toggle_window()
            panel.poll_window()
        self.assertTrue(panel.embedded)
        self.assertFalse(panel.external)

    def test_modal_or_unready_frame_does_not_receive_another_command(self):
        panel = self.panel_with_process()
        panel._seen_dialogs={456}
        panel.native_command(32805)
        panel.native.command.assert_not_called()
        panel._seen_dialogs.clear()
        panel.native.ready.return_value=False
        panel.native_command(32805)
        panel.native.command.assert_not_called()

    def test_failed_launch_can_be_retried_and_close_during_preparation_does_not_spawn(self):
        self.app._switch_mode('windirstat')
        panel=self.app.windirstat
        panel.starting=True
        panel.started(None,'fixture launch failed')
        self.assertFalse(panel.starting)
        self.assertEqual(panel.open_button.cget('state'),'normal')
        self.assertIn('fixture launch failed',panel.state.cget('text'))
        prepared=threading.Event()
        release=threading.Event()
        def prepare():
            prepared.set()
            release.wait(5)
            return 'fixture.exe'
        with patch('windirstat_ui.prepare_executable',side_effect=prepare), \
                patch('windirstat_ui.launch') as spawn,patch.object(self.app,'_post') as post:
            panel.open()
            try:
                self.assertTrue(prepared.wait(2))
                panel.close()
            finally:release.set()
            deadline=time.monotonic()+3
            while not post.called and time.monotonic()<deadline:time.sleep(.01)
            spawn.assert_not_called()
            self.assertTrue(post.called)
            callback,*args=post.call_args.args
            callback(*args)
        self.assertFalse(panel.starting)
        self.assertFalse(panel.closing)
        self.assertEqual(panel.open_button.cget('state'),'normal')

    def test_close_without_main_window_terminates_only_owned_process_after_deadline(self):
        panel=self.panel_with_process()
        panel.hwnd=None
        panel.native.find_window.return_value=None
        owned=panel.process
        panel.close()
        panel._close_deadline=time.monotonic()-1
        panel.poll()
        owned.terminate.assert_called_once()
        panel.process.poll.return_value=0
        panel.poll()
        self.assertIsNone(panel.process)
        self.assertEqual(panel.open_button.cget('state'),'normal')

    def test_hidden_tab_is_not_resized_to_a_single_pixel(self):
        panel=self.panel_with_process()
        panel.embedded=True
        with patch.object(panel.host,'winfo_ismapped',return_value=False):panel.resize()
        panel.native.resize.assert_not_called()

    def test_late_launch_callback_after_tab_destroy_terminates_owned_process(self):
        self.app._switch_mode('windirstat')
        panel=self.app.windirstat
        panel.destroy()
        owned=Mock()
        owned.poll.return_value=None
        panel.started(owned,None)
        owned.terminate.assert_called_once()

    def test_tab_does_not_start_scanning_on_open_and_can_be_left_while_running(self):
        with patch('windirstat_ui.launch') as spawn:
            self.app._switch_mode('windirstat')
            panel = self.app.windirstat
            spawn.assert_not_called()
            self.assertEqual(panel.host.winfo_manager(), 'pack')
            process, native = Mock(), Mock()
            process.poll.return_value = None
            panel.process, panel.native, panel.hwnd = process, native, 123
            self.app._switch_mode('accounts')
            self.assertEqual(self.app.mode, 'accounts')
            self.assertIs(panel.process, process)
            self.assertFalse(panel.close())
            native.close.assert_called_once_with(123)
            panel.process = panel.native = panel.hwnd = None


class NativeTests(unittest.TestCase):
    def host(self):
        host=object.__new__(NativeHost)
        host.dll=Mock()
        host._menus={}
        host._frames={}
        host.menus=Mock(return_value=[(0,'Файл',321)])
        return host

    def test_ready_rejects_early_raw_menu_modal_and_unresponsive_frames(self):
        native=self.host()
        self.assertTrue(native.ready(123))
        for name in ('IsWindow','IsWindowVisible','IsWindowEnabled','GetWindow','SendMessageTimeoutW'):
            with self.subTest(name=name):
                method=getattr(native.dll,name)
                method.return_value=0
                self.assertFalse(native.ready(123))
                method.return_value=1
        for menus in ([],[(0,'IDS_MENU_FILE',321)],[(0,'',321)]):
            native.menus.return_value=menus
            self.assertFalse(native.ready(123))

    def test_early_menu_is_not_cached_or_reparented(self):
        native=self.host()
        native.menus.return_value=[(0,'IDS_MENU_FILE',321)]
        with self.assertRaisesRegex(RuntimeError,'меню'):native.attach(123,456)
        native.dll.SetParent.assert_not_called()
        self.assertFalse(native._menus)

    def test_explorer_command_reports_missing_native_selection_instead_of_silent_noop(self):
        native=self.host()
        native.dll.GetMenuState.return_value=1
        with self.assertRaisesRegex(ValueError,'Выберите файл или папку'):native.command(123,32779)
        native.dll.PostMessageW.assert_not_called()
        native.dll.GetMenuState.return_value=0
        native.command(123,32779)
        native.dll.PostMessageW.assert_called_once_with(123,0x111,32779,0)

    def test_detach_clears_child_style_before_desktop_parent_and_restores_menu(self):
        native=self.host()
        native._frames[123]=(0x16CF0000,(20,30,800,600),789)
        native._menus[123]=[(0,'Файл',321)]
        native.dll.GetParent.return_value=None
        native.detach(123)
        names=[call[0] for call in native.dll.method_calls]
        self.assertLess(names.index('SetWindowLongPtrW'),names.index('SetParent'))
        native.dll.SetMenu.assert_called_once_with(123,789)
        self.assertNotIn(123,native._menus)

    def test_only_our_tk_owner_is_reenabled_when_reowning_native_dialog(self):
        import os
        native=self.host()
        native.dll.GetWindow.return_value=456
        native.dll.GetWindowThreadProcessId.side_effect=lambda hwnd,pid:setattr(pid._obj,'value',os.getpid())
        native.show_dialog(789,123)
        native.dll.SetWindowLongPtrW.assert_called_once_with(789,-8,123)
        native.dll.EnableWindow.assert_called_once_with(456,True)
        native.dll.reset_mock()
        native.dll.GetWindowThreadProcessId.side_effect=lambda hwnd,pid:setattr(pid._obj,'value',os.getpid()+1)
        native.show_dialog(789,123)
        native.dll.SetWindowLongPtrW.assert_not_called()
        native.dll.EnableWindow.assert_not_called()

    def test_auto_fit_only_changes_owned_visible_report_columns_and_respects_hidden_columns(self):
        native=self.host()
        native._column_profiles={}
        native.callback=lambda function:function
        native.dll.GetDpiForWindow.return_value=144
        native.dll.GetWindowLongPtrW.return_value=1
        columns=[900,150,0,150]
        def enumerate_children(hwnd,callback,unused):
            for control in (101,102,103):callback(control,0)
        native.dll.EnumChildWindows.side_effect=enumerate_children
        native.dll.IsWindowVisible.side_effect=lambda control:control!=103
        native.dll.GetWindowThreadProcessId.side_effect=lambda control,pid:setattr(pid._obj,'value',77 if control==101 else 88)
        native.dll.GetClassNameW.side_effect=lambda control,name,size:setattr(name,'value','SysListView32')
        def client(control,rect):
            rect._obj.right=602
            return 1
        native.dll.GetClientRect.side_effect=client
        operations=[]
        def message(control,kind,index,value,flags,timeout,result):
            operations.append((control,kind,index,value))
            if kind==0x101f:result._obj.value=201
            elif kind==0x1200:result._obj.value=len(columns)
            elif kind==0x101d:result._obj.value=columns[index]
            elif kind==0x101e:columns[index]=value;result._obj.value=1
            return 1
        native.dll.SendMessageTimeoutW.side_effect=message
        native.fit_columns(123,77)
        self.assertEqual(sum(columns),600)
        self.assertEqual(columns[2],0)
        self.assertTrue(all(control in (101,201) for control,*_ in operations))
        self.assertFalse(any(kind==0x101e and index==2 for control,kind,index,value in operations))
        operations.clear()
        native.fit_columns(123,77)
        self.assertFalse(any(kind==0x101e for control,kind,index,value in operations))

    def test_unresponsive_native_header_leaves_columns_untouched(self):
        native=self.host()
        native._column_profiles={}
        native.callback=lambda function:function
        native.dll.GetWindowLongPtrW.return_value=1
        native.dll.EnumChildWindows.side_effect=lambda hwnd,callback,unused:callback(101,0)
        native.dll.GetWindowThreadProcessId.side_effect=lambda hwnd,pid:setattr(pid._obj,'value',77)
        native.dll.GetClassNameW.side_effect=lambda hwnd,name,size:setattr(name,'value','SysListView32')
        native.dll.SendMessageTimeoutW.return_value=0
        native.fit_columns(123,77)
        native.dll.SendMessageTimeoutW.assert_called_once()
        native.dll.PostMessageW.assert_not_called()


class WidthTests(unittest.TestCase):
    def test_wide_table_keeps_numeric_widths_and_gives_spare_space_to_names(self):
        self.assertEqual(fitted_widths(900,[250,90,0,110]),[700,90,0,110])

    def test_smaller_and_hidpi_tables_fit_exactly_without_unhiding_columns(self):
        for scale in (1,1.25,1.5,2):
            for width in (96,300,600,1000,2200):
                with self.subTest(width=width,scale=scale):
                    result=fitted_widths(width,[900,110,0,110,150,1],scale)
                    self.assertEqual(sum(result),width)
                    self.assertEqual(result[2],0)
                    self.assertTrue(all(value>0 for index,value in enumerate(result) if index!=2))

    def test_repeated_resizing_uses_original_profile_and_does_not_accumulate_rounding_error(self):
        profile=[250,130,90,90,90,0,120]
        initial=fitted_widths(700,profile)
        for width in (400,2500,1100,96,799,700):
            result=fitted_widths(width,profile)
            self.assertEqual(sum(result),width)
        self.assertEqual(result,initial)
        self.assertEqual(fitted_widths(100,[0,0]),[0,0])
