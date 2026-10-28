import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bookmark_navigation import BookmarkBar, NavigationLayout, PAGES


COLORS = {"bg": "#101722", "panel": "#172130", "border": "#2a3a50", "fg": "#ecf2fa",
          "muted": "#94a7be", "accent": "#55c2b0", "accent_fg": "#10251f", "button": "#243449",
          "button_hover": "#324860", "selected": "#28566a"}


class LayoutTests(unittest.TestCase):
    def test_custom_tabs_are_direct_shortcuts_and_survive_reload_without_moving_pages(self):
        layout = NavigationLayout()
        tab = layout.new_tab('Мой бэкап', 'pg_dump')
        self.assertEqual(layout.nodes[0], dict(id=tab, title='Мой бэкап', page='pg_dump'))
        self.assertEqual(layout.location('pg_dump')[0], 'folder-postgres')
        self.assertTrue(layout.contains(tab, 'pg_dump'))
        layout.rename(tab, 'Архив')
        restored = NavigationLayout(layout.snapshot())
        self.assertEqual(restored.snapshot(), layout.snapshot())
        self.assertTrue(restored.remove_tab(tab))
        self.assertIsNotNone(restored.find('pg_dump'))
        self.assertFalse(restored.remove_tab('pg_dump'))

    def test_defaults_group_pages(self):
        layout = NavigationLayout()
        self.assertEqual([node["title"] for node in layout.nodes], ["1С", "Win", "СУБД", "Linux", "Планировщик"])
        self.assertEqual([node["id"] for node in layout.children("folder-1c")], ["servers", "bases", "cluster_infobase", "jr_archive", "platform_install", "configuration_install"])
        self.assertEqual([node['id'] for node in layout.children('folder-sql')], ['folder-mssql', 'folder-postgres'])
        self.assertEqual([node['id'] for node in layout.children('folder-mssql')], ['db_restore', 'db_backup', 'db_copy'])
        self.assertEqual(layout.location("accounts"), ("folder-win", 0))

    def test_move_between_folders_reorder_and_promote_to_bar(self):
        layout = NavigationLayout()
        self.assertTrue(layout.move("bases", "folder-win", 0))
        self.assertEqual([node["id"] for node in layout.children("folder-win")], ["bases", "accounts", "folder-services", "windirstat"])
        self.assertTrue(layout.move("bases", "folder-win", 2))
        self.assertEqual([node["id"] for node in layout.children("folder-win")], ["accounts", "bases", "folder-services", "windirstat"])
        self.assertTrue(layout.move("folder-win", None, 0))
        self.assertTrue(layout.move("servers", None, 1))
        self.assertEqual([node["id"] for node in layout.nodes], ["folder-win", "servers", "folder-1c", "folder-sql", "folder-linux", "scheduler"])
        self.assertFalse(layout.move("servers", None, 2))

    def test_nested_folders_prevent_cycles_and_remove_preserves_pages(self):
        layout = NavigationLayout()
        inner = layout.new_folder("Администрирование", "folder-1c")
        layout.move("servers", inner)
        before = layout.snapshot()
        self.assertFalse(layout.move("folder-1c", inner))
        self.assertFalse(layout.move(inner, inner))
        self.assertFalse(layout.move(inner, "servers"))
        self.assertEqual(layout.snapshot(), before)
        self.assertTrue(layout.remove_folder(inner))
        self.assertEqual(layout.location("servers")[0], "folder-1c")
        self.assertFalse(layout.remove_folder("accounts"))
        self.assertTrue(layout.remove_folder("folder-1c"))
        self.assertEqual({node["id"] for node, _ in layout.walk() if "children" not in node}, set(PAGES))

    def test_corrupt_saved_layout_is_repaired_without_losing_pages(self):
        layout = NavigationLayout([None, {"id": "servers", "children": []},
                                   {"id": "folder", "title": 12, "children": [
                                       {"id": "bases", "title": "Базы"}, {"id": "bases"},
                                       {"id": "unknown"}]}, {"id": "accounts"}])
        leaves = [node["id"] for node, _ in layout.walk() if "children" not in node]
        self.assertEqual(set(leaves), set(PAGES))
        self.assertEqual(len(leaves), len(PAGES))
        self.assertEqual(layout.find("folder")["title"], "Папка")

    def test_layout_roundtrip_preserves_custom_names_and_moves(self):
        layout = NavigationLayout()
        folder = layout.new_folder("Мои инструменты")
        layout.move("accounts", folder)
        layout.rename(folder, "Работа")
        with tempfile.TemporaryDirectory() as temp:
            settings = Path(temp) / "settings.json"
            settings.write_text(json.dumps({"navigation_layout": layout.snapshot()}, ensure_ascii=False), encoding="utf-8")
            restored = NavigationLayout(json.loads(settings.read_text(encoding="utf-8"))["navigation_layout"])
        self.assertEqual(restored.snapshot(), layout.snapshot())
        snapshot = restored.snapshot()
        snapshot.clear()
        self.assertTrue(restored.nodes)

    def test_upgrade_adds_services_without_changing_old_layout(self):
        old = [{"id": "folder-win", "title": "Windows", "children": [{"id": "accounts", "title": "Учётки"}]},
               {"id": "bases", "title": "Мои базы"}, {"id": "servers", "title": "Серверы"}]
        layout = NavigationLayout(old)
        self.assertEqual(layout.find("folder-win")["title"], "Windows")
        self.assertEqual(layout.nodes[1]["title"], "Мои базы")
        self.assertEqual(layout.location("accounts"), ("folder-win", 0))
        self.assertEqual(layout.location("folder-services"), ("folder-win", 1))
        self.assertEqual(layout.location("service_delete")[0], "folder-services")

    def test_old_sql_menu_is_migrated_into_mssql_once_preserving_names_and_postgres(self):
        layout = NavigationLayout()
        sql = layout.find('folder-sql')
        folder = layout.find('folder-mssql')
        original = list(folder['children'])
        original[0]['title'] = 'Мой рестор'
        sql['children'] = original + [layout.find('folder-postgres')]
        upgraded = NavigationLayout(layout.snapshot())
        self.assertEqual([node['id'] for node in upgraded.children('folder-sql')], ['folder-mssql', 'folder-postgres'])
        self.assertEqual(upgraded.find('db_restore')['title'], 'Мой рестор')
        self.assertEqual(upgraded.location('db_backup')[0], 'folder-mssql')
        self.assertEqual(NavigationLayout(upgraded.snapshot()).snapshot(), upgraded.snapshot())


class BookmarkUiTests(unittest.TestCase):
    """Exercise actual Tk hit testing, hover windows and mouse-grab drag delivery."""
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("850x400+50+50")
        self.activate, self.changed = Mock(), Mock()
        self.bar = BookmarkBar(self.root, COLORS, None, self.activate, self.changed)
        self.bar.pack(fill=tk.X, padx=10, pady=10)
        self.root.update()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.bar.close)

    def widget(self, key):
        return next(widget for widget in self.bar._widgets if widget._node == key)

    def test_new_tab_is_direct_has_no_folder_icon_and_can_be_closed(self):
        with patch('bookmark_navigation.TabDialog', return_value=SimpleNamespace(result=('Мой бэкап', 'pg_dump'))):
            self.bar.tab_button.invoke()
        tab = self.bar.layout.nodes[0]['id']
        widget = self.widget(tab)
        self.assertFalse(widget.cget('image'))
        self.activate.assert_called_once_with('pg_dump')
        self.activate.reset_mock()
        self.bar._click(widget)
        self.activate.assert_called_once_with('pg_dump')
        self.bar._close_tab(tab)
        self.assertIsNone(self.bar.layout.find(tab))
        self.assertIsNotNone(self.bar.layout.find('pg_dump'))

    def test_overflow_has_clickable_arrows_and_still_ignores_mouse_wheel(self):
        for number in range(20):
            self.bar.layout.new_tab('Длинная вкладка ' + str(number), 'pg_dump')
        self.bar.redraw()
        self.root.update()
        self.assertTrue(self.bar.overflow.winfo_ismapped())
        initial = self.bar.canvas.xview()
        self.bar.overflow_buttons[1].invoke()
        self.assertGreater(self.bar.canvas.xview()[0], initial[0])
        before = self.bar.canvas.xview()
        self.bar.canvas.event_generate('<MouseWheel>', delta=-120)
        self.assertEqual(self.bar.canvas.xview(), before)

    def event(self, widget, x=None, y=None):
        return SimpleNamespace(x_root=widget.winfo_rootx() + (widget.winfo_width() // 2 if x is None else x),
                               y_root=widget.winfo_rooty() + (widget.winfo_height() // 2 if y is None else y))

    def test_hover_shows_pages_and_click_activates_selected_page(self):
        self.bar._hover(self.widget("folder-1c"))
        self.root.update()
        self.assertEqual([key for key, _ in self.bar._menus], ["folder-1c"])
        self.bar._click(self.widget("bases"))
        self.activate.assert_called_once_with("bases")
        self.assertEqual(self.bar._menus, [])

    def test_drag_from_popup_to_other_folder_persists(self):
        self.bar._open(self.widget("folder-1c"))
        self.root.update()
        source = self.widget("bases")
        self.bar._press(self.event(source), source)
        target = self.event(self.widget("folder-win"))
        self.bar._motion(target)
        self.assertIsNotNone(self.bar._ghost)
        self.root.update()
        self.bar._release(target)
        self.assertEqual(self.bar.layout.location("bases")[0], "folder-win")
        self.assertEqual([key for key, _ in self.bar._menus], ['folder-win'])
        self.changed.assert_called_once_with(self.bar.layout.snapshot())
        self.assertIsNone(self.bar._ghost)
        self.assertIsNone(self.root.grab_current())

    def test_wheel_does_not_move_folder_strip_or_close_open_folder(self):
        for number in range(20):
            self.bar.layout.new_folder('Folder ' + str(number))
        self.bar.redraw()
        self.root.update()
        self.bar._open(self.widget('folder-1c'))
        before = self.bar.canvas.xview()
        self.bar.canvas.event_generate('<MouseWheel>', delta=-120)
        self.widget('folder-1c').event_generate('<MouseWheel>', delta=120)
        self.assertEqual(self.bar.canvas.xview(), before)
        self.assertEqual([key for key, _ in self.bar._menus], ['folder-1c'])

    def test_drag_into_empty_nested_popup_and_reorder_root_folder(self):
        inner = self.bar.layout.new_folder("Мои", "folder-win")
        self.bar.redraw()
        self.bar._open(self.widget("folder-1c"))
        self.root.update()
        source = self.widget("servers")
        self.bar._press(self.event(source), source)
        self.bar._motion(self.event(self.widget("folder-win")))
        self.bar._hover(self.widget("folder-win"))
        self.root.update()
        self.bar._hover(self.widget(inner))
        self.root.update()
        popup = self.bar._menus[-1][1]
        target = self.event(popup)
        self.bar._motion(target)
        self.bar._release(target)
        self.assertEqual(self.bar.layout.location("servers")[0], inner)
        self.root.update()
        source = self.widget("folder-win")
        self.bar._press(self.event(source), source)
        target = self.event(self.widget("folder-1c"), x=2)
        self.bar._motion(target)
        self.bar._release(target)
        self.assertEqual(self.bar.layout.nodes[0]["id"], "folder-win")

    def test_escape_and_invalid_drop_leave_layout_unchanged(self):
        before = self.bar.layout.snapshot()
        source = self.widget("folder-1c")
        self.bar._press(self.event(source), source)
        self.bar._motion(self.event(self.widget("folder-win")))
        self.bar._escape()
        self.assertEqual(self.bar.layout.snapshot(), before)
        self.changed.assert_not_called()
        self.assertIsNone(self.root.grab_current())

    def test_busy_blocks_opening_and_edits(self):
        self.bar.set_enabled(False)
        self.bar._click(self.widget("folder-1c"))
        self.bar._press(self.event(self.widget("folder-win")), self.widget("folder-win"))
        with patch("bookmark_navigation.simpledialog.askstring") as dialog:
            self.bar._new_folder()
            dialog.assert_not_called()
        self.assertEqual(self.bar._menus, [])
        self.assertIsNone(self.bar._pressed)
        self.activate.assert_not_called()

    def test_tk_mouse_bindings_move_folder_to_bar_edge(self):
        source = self.widget("folder-win")
        destination = self.widget("folder-1c")
        start = self.event(source)
        end = self.event(destination, x=2)
        source.event_generate("<ButtonPress-1>", x=20, y=10, rootx=start.x_root, rooty=start.y_root)
        source.event_generate("<B1-Motion>", x=-20, y=10, rootx=end.x_root, rooty=end.y_root)
        self.assertEqual(self.bar._drag, "folder-win")
        self.bar.event_generate("<ButtonRelease-1>", x=end.x_root - self.bar.winfo_rootx(),
                                y=end.y_root - self.bar.winfo_rooty(), rootx=end.x_root, rooty=end.y_root)
        self.assertEqual(self.bar.layout.nodes[0]["id"], "folder-win")
        self.changed.assert_called_once()

    def test_folder_icons_nested_creation_and_all_menus_hide_outside(self):
        win = self.widget("folder-win")
        self.assertTrue(win.cget("image"))
        self.bar._open(win)
        self.root.update()
        self.bar._open(self.widget("folder-services"))
        self.root.update()
        self.assertEqual(len(self.bar._menus), 2)
        self.assertFalse(self.widget("service_create").cget("image"))
        self.assertIsNotNone(self.bar._watch_job)
        # A short timer pumps Tk while the cursor is outside every folder/menu.
        with patch.object(self.bar, "winfo_pointerxy", return_value=(0, 0)):
            self.root.after(400, self.root.quit)
            self.root.mainloop()
        self.assertEqual(self.bar._menus, [])
        self.assertIsNone(self.bar._watch_job)
        self.bar._open(self.widget("folder-win"))
        self.root.update()
        popup = self.bar._menus[-1][1]
        creation = next(widget for widget in self.descendants(popup)
                        if isinstance(widget, tk.Button) and widget.cget("text") == "+ Папка здесь")
        with patch("bookmark_navigation.simpledialog.askstring", return_value="Вложенная"):
            creation.invoke()
        node = self.bar.layout.children("folder-win")[-1]
        self.assertEqual(node["title"], "Вложенная")
        self.assertIn("children", node)

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_new_folder_rename_and_context_menu_actions(self):
        with patch("bookmark_navigation.simpledialog.askstring", return_value="Свои"):
            self.bar._new_folder()
        key = self.bar.layout.nodes[-1]["id"]
        with patch("bookmark_navigation.simpledialog.askstring", return_value="Инструменты"):
            self.bar._rename(key)
        self.assertEqual(self.bar.layout.find(key)["title"], "Инструменты")
        # Windows enters a native modal loop when posting a menu. Inspect the
        # real menu's entries without entering that interactive loop in a test.
        with patch.object(tk.Menu, "tk_popup"):
            self.bar._context(self.event(self.widget(key)), key)
        menu = self.bar._context_menu
        self.assertEqual(menu.entrycget(0, "label"), "Новая вкладка")
        self.assertEqual(menu.entrycget(1, "label"), "Новая папка здесь")
        self.assertEqual(menu.entrycget(2, "label"), "Новая вложенная папка")
        self.assertEqual(menu.entrycget(3, "label"), "Переименовать")
        self.assertTrue(menu.winfo_exists())
        self.bar._remove(key)
        self.assertIsNone(self.bar.layout.find(key))


if __name__ == "__main__":
    unittest.main()
