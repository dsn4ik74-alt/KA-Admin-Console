"""Editable bookmark navigation; layout contains no account or server data."""
from copy import deepcopy
import tkinter as tk
from tkinter import simpledialog, ttk
from uuid import uuid4
from clipboard_shortcuts import bind_entry_shortcuts


PAGES = {"servers": "Серверы", "bases": "ИБ", "accounts": "Создание УЗ",
         "service_create": "Создать службу 1С", "service_rename": "Переименовать службу",
         "service_delete": "Удалить службу", "db_backup": "Бэкап БД",
         "db_restore": "Восстановление БД", "jr_archive": "Архивирование ЖР",
         "pg_dump": "Бэкап · pg_dump", "pg_restore": "Восстановление · pg_restore / psql",
         "ibcmd_dump": "Выгрузка DT · ibcmd", "ibcmd_restore": "Восстановление DT · ibcmd",
         "cluster_infobase": "Добавить базу в кластер", "db_copy": "Бэкап + восстановление · MSSQL",
         "pg_copy": "Бэкап + восстановление", "platform_install": "Установка 1С из ИТС", "windirstat": "WinDirStat",
         "scheduler": "Планировщик", "configuration_install": "Установка конфигураций",
         "postgres_install": "Установка PostgreSQL из ИТС", "linux_disk": "Анализ диска по SSH",
         "web_publication": "Публикация ИБ", "file_transfer": "Передача файлов / Robocopy"}


def postgres_folder():
    return {"id": "folder-postgres", "title": "PostgreSQL", "children": [
        {"id": key, "title": PAGES[key]} for key in ('pg_dump', 'pg_restore', 'pg_copy', 'ibcmd_dump', 'ibcmd_restore','postgres_install')]}


def service_folder():
    return {"id": "folder-services", "title": "Службы", "children": [
        {"id": key, "title": title} for key, title in PAGES.items() if key.startswith("service_")]}


def default_layout():
    return [{"id": "folder-1c", "title": "1С", "children": [
                {"id": "servers", "title": PAGES["servers"]},
                {"id": "bases", "title": PAGES["bases"]},
                {"id": "cluster_infobase", "title": PAGES["cluster_infobase"]},
                {"id": "jr_archive", "title": PAGES["jr_archive"]},
                {"id": "platform_install", "title": PAGES["platform_install"]},
                {"id": "configuration_install", "title": PAGES["configuration_install"]},
                {"id": "web_publication", "title": PAGES["web_publication"]}]},
            {"id": "folder-win", "title": "Win", "children": [
                {"id": "accounts", "title": PAGES["accounts"]}, service_folder(), {'id': 'windirstat', 'title': PAGES['windirstat']},
                {'id':'file_transfer','title':PAGES['file_transfer']}]},
            sql_folder(),{'id':'folder-linux','title':'Linux','children':[{'id':'linux_disk','title':PAGES['linux_disk']}]}]


def sql_folder():
    return {"id": "folder-sql", "title": "СУБД", "children": [mssql_folder(), postgres_folder()]}


def mssql_folder():
    return {'id': 'folder-mssql', 'title': 'MSSQL', 'children': [
        {'id': key, 'title': PAGES[key]} for key in ('db_restore', 'db_backup', 'db_copy')]}


class NavigationLayout:
    """A tree with one entry per application page and folders of arbitrary depth."""
    def __init__(self, saved=None):
        self.nodes = []
        seen = set()

        def normalize(entries, depth=0):
            result = []
            if not isinstance(entries, list) or depth > 16:
                return result
            for entry in entries[:250]:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("id")
                if not isinstance(key, str) or not key or key in seen:
                    continue
                tab = key.startswith('tab-') and entry.get('page') in PAGES
                if "children" not in entry and key not in PAGES and not tab:
                    continue
                if "children" in entry and (key in PAGES or tab):
                    continue
                seen.add(key)
                title = entry.get("title")
                title = title.strip()[:80] if isinstance(title, str) else ""
                node = {"id": key, "title": title or PAGES.get(key, "Папка")}
                if tab:
                    node['page'] = entry['page']
                    node['title'] = title or PAGES[entry['page']]
                if "children" in entry:
                    node["children"] = normalize(entry["children"], depth + 1)
                result.append(node)
            return result

        self.nodes = normalize(saved if isinstance(saved, list) else default_layout())
        missing_services = [key for key in PAGES if key.startswith("service_") and key not in seen]
        if missing_services:
            folder = service_folder()
            if folder["id"] in seen:
                folder["id"] = "folder-" + uuid4().hex
            folder["children"] = [node for node in folder["children"] if node["id"] in missing_services]
            win = self.find("folder-win")
            (win["children"] if win is not None and "children" in win else self.nodes).append(folder)
            seen.update(missing_services)
        missing_sql = [key for key in ("db_restore", "db_backup") if key not in seen]
        if missing_sql:
            sql = self.find('folder-sql')
            if sql is None or 'children' not in sql:
                sql = {'id': 'folder-sql' if sql is None else 'folder-' + uuid4().hex,
                       'title': 'СУБД', 'children': []}
                self.nodes.append(sql)
            sql['children'].extend({'id': key, 'title': PAGES[key]} for key in missing_sql)
            seen.update(missing_sql)
        if 'jr_archive' not in seen:
            one_c = self.find('folder-1c')
            (one_c['children'] if one_c is not None and 'children' in one_c else self.nodes).append(
                {'id': 'jr_archive', 'title': PAGES['jr_archive']})
            seen.add('jr_archive')
        if 'cluster_infobase' not in seen:
            one_c = self.find('folder-1c')
            if one_c is None or 'children' not in one_c:
                one_c = {'id': 'folder-' + uuid4().hex, 'title': '1С', 'children': []}
                self.nodes.append(one_c)
            one_c['children'].append({'id': 'cluster_infobase', 'title': PAGES['cluster_infobase']})
            seen.add('cluster_infobase')
        missing_pg = [key for key in ('pg_dump', 'pg_restore', 'ibcmd_dump', 'ibcmd_restore') if key not in seen]
        if missing_pg:
            folder = postgres_folder()
            if folder['id'] in seen:
                folder['id'] = 'folder-' + uuid4().hex
            folder['children'] = [node for node in folder['children'] if node['id'] in missing_pg]
            sql = self.find('folder-sql')
            (sql['children'] if sql is not None and 'children' in sql else self.nodes).append(folder)
            seen.update(missing_pg)
        for key, folder_key in (('db_copy', 'folder-sql'), ('pg_copy', 'folder-postgres')):
            if key not in seen:
                folder = self.find(folder_key)
                (folder['children'] if folder is not None and 'children' in folder else self.nodes).append(
                    {'id': key, 'title': PAGES[key]})
                seen.add(key)
        if 'platform_install' not in seen:
            folder = self.find('folder-1c')
            (folder['children'] if folder is not None and 'children' in folder else self.nodes).append(
                dict(id='platform_install', title=PAGES['platform_install']))
            seen.add('platform_install')
        if 'configuration_install' not in seen:
            folder = self.find('folder-1c')
            (folder['children'] if folder is not None and 'children' in folder else self.nodes).append(
                dict(id='configuration_install', title=PAGES['configuration_install']))
            seen.add('configuration_install')
        if 'windirstat' not in seen:
            folder = self.find('folder-win')
            (folder['children'] if folder is not None and 'children' in folder else self.nodes).append(dict(id='windirstat', title=PAGES['windirstat']))
            seen.add('windirstat')
        for key,folder_key,title in (('postgres_install','folder-postgres','PostgreSQL'),('linux_disk','folder-linux','Linux')):
            if key in seen:continue
            folder=self.find(folder_key)
            if folder is None or 'children' not in folder:
                folder=dict(id=folder_key if folder is None else 'folder-'+uuid4().hex,title=title,children=[])
                parent=self.find('folder-sql') if key=='postgres_install' else None
                (parent['children'] if parent is not None and 'children' in parent else self.nodes).append(folder)
            folder['children'].append(dict(id=key,title=PAGES[key]));seen.add(key)
        for key, folder_key in (('web_publication','folder-1c'),('file_transfer','folder-win')):
            if key not in seen:
                folder=self.find(folder_key)
                (folder['children'] if folder and 'children' in folder else self.nodes).append({'id':key,'title':PAGES[key]})
                seen.add(key)

        for key, title in PAGES.items():
            if key not in seen:
                self.nodes.append({"id": key, "title": title})

        # Upgrade the old СУБД menu once, retaining custom names and item order.
        sql = self.find('folder-sql')
        if sql is not None and 'children' in sql:
            direct = [node for node in sql['children'] if node['id'] in ('db_restore', 'db_backup', 'db_copy')]
            if direct:
                folder = next((node for node in sql['children']
                               if node['id'] == 'folder-mssql' and 'children' in node), None)
                if folder is None:
                    key = 'folder-mssql' if self.find('folder-mssql') is None else 'folder-' + uuid4().hex
                    folder = dict(id=key, title='MSSQL', children=[])
                    sql['children'].insert(sql['children'].index(direct[0]), folder)
                folder['children'].extend(direct)
                sql['children'] = [node for node in sql['children'] if node not in direct]

    def snapshot(self):
        return deepcopy(self.nodes)

    def walk(self, entries=None, parent=None):
        for node in self.nodes if entries is None else entries:
            yield node, parent
            if "children" in node:
                yield from self.walk(node["children"], node["id"])

    def find(self, key):
        return next((node for node, _ in self.walk() if node["id"] == key), None)

    def children(self, parent=None):
        if parent is None:
            return self.nodes
        node = self.find(parent)
        if node is None or "children" not in node:
            raise ValueError("Папка не найдена")
        return node["children"]

    def location(self, key):
        for node, parent in self.walk():
            if node["id"] == key:
                return parent, self.children(parent).index(node)
        raise ValueError("Пункт не найден")

    def contains(self, folder, key):
        node = self.find(folder)
        return bool(node and any(child['id'] == key or child.get('page') == key for child, _ in self.walk([node])))

    def new_tab(self, title, page):
        title = title.strip()[:80]
        if not title or page not in PAGES:
            raise ValueError('Укажите название вкладки и раздел приложения')
        key = 'tab-' + uuid4().hex
        index = next((index for index, node in enumerate(self.nodes) if 'page' not in node), len(self.nodes))
        self.nodes.insert(index, dict(id=key, title=title, page=page))
        return key

    def remove_tab(self, key):
        node = self.find(key)
        if not node or 'page' not in node:
            return False
        parent, index = self.location(key)
        self.children(parent).pop(index)
        return True

    def can_move(self, key, parent):
        return self.find(key) is not None and (parent is None or (
            self.find(parent) is not None and "children" in self.find(parent)
            and not self.contains(key, parent)))

    def move(self, key, parent, index=None):
        if not self.can_move(key, parent):
            return False
        old_parent, old_index = self.location(key)
        target = self.children(parent)
        index = len(target) if index is None else max(0, min(index, len(target)))
        if old_parent == parent and old_index < index:
            index -= 1
        if old_parent == parent and old_index == index:
            return False
        node = self.children(old_parent).pop(old_index)
        target.insert(index, node)
        return True

    def new_folder(self, title, parent=None):
        title = title.strip()[:80]
        if not title:
            raise ValueError("Введите название папки")
        key = "folder-" + uuid4().hex
        self.children(parent).append({"id": key, "title": title, "children": []})
        return key

    def rename(self, key, title):
        node = self.find(key)
        if node is None or not title.strip():
            return False
        node["title"] = title.strip()[:80]
        return True

    def remove_folder(self, key):
        node = self.find(key)
        if node is None or "children" not in node:
            return False
        parent, index = self.location(key)
        self.children(parent)[index:index + 1] = node["children"]
        return True


class BookmarkBar(tk.Frame):
    """Hover menus built from Tk widgets, allowing drag across menu windows."""
    def __init__(self, parent, colors, saved, activate, changed, vertical=False):
        super().__init__(parent, bg=colors['panel'] if vertical else colors["bg"])
        self.vertical = vertical
        self.c, self.activate, self.changed = colors, activate, changed
        self.activate_tab = self.can_close_tab = None
        self.layout = NavigationLayout(saved)
        self.enabled = True
        self.active = "servers"
        self._menus = []
        self._hover_job = self._hide_job = self._watch_job = None
        self._pressed = self._drag = self._drop = None
        self._ghost = self._marked = None
        self._context_menu = None
        self._widgets = []
        self.folder_icon = self._folder_icon()
        background = colors['panel'] if vertical else colors['bg']
        self.canvas = tk.Canvas(self, bg=background, bd=0, highlightthickness=0,
                                width=1 if vertical else 300, height=1 if vertical else 28)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH if vertical else tk.X, expand=True)
        self.items = tk.Frame(self.canvas, bg=background)
        self._canvas_item = self.canvas.create_window(0, 0, anchor=tk.NW, window=self.items)
        self.items.bind("<Configure>", self._size)
        self.canvas.bind("<Configure>", self._size)
        self.canvas.bind("<MouseWheel>", self._scroll)
        self.canvas.bind("<Button-3>", lambda event: self._context(event))
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.add_button = tk.Button(self, text="+ Папка", image=self.folder_icon, compound=tk.LEFT, command=self._new_folder,
                                    bg=colors["button"], fg=colors["muted"], relief="flat", bd=0,
                                    activebackground=colors["button_hover"], activeforeground=colors["fg"],
                                    font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2")
        self.add_button.pack(side=tk.RIGHT, padx=(10, 0))
        self.tab_button = tk.Button(self, text='+ Вкладка', command=self._new_tab,
                                    bg=colors['button'], fg=colors['muted'], relief='flat', bd=0,
                                    activebackground=colors['button_hover'], activeforeground=colors['fg'],
                                    font=('Segoe UI', 9), padx=8, pady=4, cursor='hand2')
        self.tab_button.pack(side=tk.RIGHT, padx=(8, 0))
        self.vertical_scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vertical_scroll.set)
        if vertical:
            self.add_button.configure(text='+ Раздел', image='')
            self.add_button.pack_forget()
            self.tab_button.pack_forget()
            self.add_button.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0), before=self.canvas)
            self.tab_button.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0), before=self.canvas)
        self.overflow = tk.Frame(self, bg=colors['bg'])
        self.overflow_buttons = []
        for caption, direction in (('‹', -1), ('›', 1)):
            button = tk.Button(self.overflow, text=caption,
                               command=lambda step=direction: self.canvas.xview_scroll(step * 3, 'units'),
                               bg=colors['button'], fg=colors['fg'], relief='flat', bd=0,
                               font=('Segoe UI', 10), padx=4, pady=1, cursor='hand2')
            button.pack(side=tk.LEFT, padx=1)
            self.overflow_buttons.append(button)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Leave>", self._schedule_hide)
        self.bind("<Enter>", lambda event: self._cancel("_hide_job"))
        self.bind("<Escape>", lambda event: self._escape())
        self._outside_binding = parent.winfo_toplevel().bind("<Button-1>", self._outside, add="+")
        self.redraw()

    def _folder_icon(self):
        # A code-drawn image stays legible in Tk without external icon files.
        scale = max(1, float(self.tk.call("tk", "scaling")) / (96 / 72))
        icon = tk.PhotoImage(master=self, width=round(17 * scale), height=round(14 * scale))
        for color, box in (("#b58a42", (1, 2, 8, 6)), ("#b58a42", (1, 5, 16, 13)),
                           ("#efc370", (2, 6, 15, 12)), ("#f7d38c", (2, 6, 15, 8))):
            icon.put(color, to=tuple(round(value * scale) for value in box))
        return icon

    def _size(self, event=None):
        if self.vertical:
            self.canvas.itemconfigure(self._canvas_item, width=max(1, self.canvas.winfo_width()))
            self.canvas.configure(scrollregion=self.canvas.bbox('all'))
            if self.items.winfo_reqheight() > self.canvas.winfo_height() + 2:
                if not self.vertical_scroll.winfo_manager():
                    self.vertical_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            elif self.vertical_scroll.winfo_manager():
                self.vertical_scroll.pack_forget()
            return
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.items.winfo_reqwidth() > max(1, self.canvas.winfo_width()) + 2:
            if not self.overflow.winfo_manager():
                self.overflow.pack(side=tk.RIGHT)
        elif self.overflow.winfo_manager() and self.items.winfo_reqwidth() <= self.canvas.winfo_width():
            self.overflow.pack_forget()
        height = max(28, self.items.winfo_reqheight())
        if int(self.canvas.cget('height')) != height:
            self.canvas.configure(height=height)

    def _scroll(self, event):
        # Wheel input must not move the folder strip while scrolling a page.
        return "break"

    def redraw(self):
        self._close_menus()
        self._widgets.clear()
        for child in self.items.winfo_children():
            child.destroy()
        for node in self.layout.nodes:
            widget = self._entry(self.items, node, None, 0, horizontal=True)
            if self.vertical:
                widget.pack(fill=tk.X, pady=1)
            else:
                widget.pack(side=tk.LEFT, padx=(0, 4), pady=1)
        self._size()

    def _entry(self, parent, node, container, depth, horizontal=False):
        folder = "children" in node
        text = (node["title"] + ("  ▾" if horizontal and not self.vertical else "  ›")) if folder else node["title"]
        selected = self.layout.contains(node["id"], self.active)
        section = self.vertical and horizontal
        if section:
            symbol = {'folder-1c': '◈', 'folder-win': '⊞', 'folder-sql': '▤',
                      'folder-linux': '⌘', 'scheduler': '◷'}.get(node['id'], '◇' if folder else '•')
            text = symbol + '   ' + text
        background = (self.c['accent'] if section else self.c['selected']) if selected else self.c[
            'bg' if horizontal and not self.vertical else 'panel']
        foreground = self.c['accent_fg'] if section and selected else self.c['fg']
        widget = tk.Label(parent, text=text, image=self.folder_icon if folder and not section else "", compound=tk.LEFT,
                          font=("Segoe UI", 9), anchor=tk.W,
                          bg=background, fg=foreground if self.enabled else self.c['muted'],
                          bd=0, padx=8, pady=5 if section else 4, cursor="hand2" if self.enabled else "arrow",
                          highlightthickness=1, highlightbackground=self.c["bg"] if horizontal and not self.vertical else self.c["panel"],
                          highlightcolor=self.c["accent"], takefocus=True)
        widget._node, widget._container, widget._depth = node["id"], container, depth
        widget._horizontal = horizontal
        widget._section = section
        widget._base = widget.cget("bg")
        widget._base_fg = foreground
        self._widgets.append(widget)
        widget.bind("<Enter>", lambda event: self._enter(widget))
        widget.bind("<Leave>", lambda event: self._leave(widget))
        widget.bind("<ButtonPress-1>", lambda event: self._press(event, widget))
        widget.bind("<B1-Motion>", self._motion)
        widget.bind("<ButtonRelease-1>", self._release)
        widget.bind("<Button-3>", lambda event: self._context(event, node["id"], container))
        widget.bind("<Return>", lambda event: self._click(widget))
        widget.bind("<space>", lambda event: self._click(widget))
        widget.bind("<Escape>", lambda event: self._escape())
        if horizontal:
            widget.bind("<MouseWheel>", self._scroll)
        return widget

    def set_active(self, page):
        if self.active == page:
            return
        self.active = page
        for widget in self._widgets:
            if widget.winfo_exists():
                selected = self.layout.contains(widget._node, page)
                widget._base = self.c['accent' if widget._section else 'selected'] if selected else self.c[
                    "bg" if widget._horizontal and not self.vertical else "panel"]
                widget._base_fg = self.c['accent_fg'] if widget._section and selected else self.c['fg']
                widget.configure(bg=widget._base, fg=widget._base_fg if self.enabled else self.c['muted'])

    def breadcrumb(self, page):
        node = self.layout.find(page)
        if node is None:
            return ""
        titles = [node['title']]
        parent, _ = self.layout.location(page)
        while parent is not None:
            titles.append(self.layout.find(parent)['title'])
            parent, _ = self.layout.location(parent)
        return '  ›  '.join(reversed(titles))

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.add_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        self.tab_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        for button in self.overflow_buttons:
            button.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        if not enabled:
            self._escape()
        for widget in self._widgets:
            if widget.winfo_exists():
                widget.configure(fg=widget._base_fg if enabled else self.c["muted"],
                                 cursor="hand2" if enabled else "arrow")

    def _cancel(self, attr):
        job = getattr(self, attr)
        if job is not None:
            self.after_cancel(job)
            setattr(self, attr, None)

    def _enter(self, widget):
        self._cancel("_hide_job")
        self._cancel("_hover_job")
        if self.enabled and not self._pressed:
            active_section = widget._section and widget._base == self.c['accent']
            widget.configure(bg=widget._base if active_section else self.c["button_hover"],
                             fg=widget._base_fg)
            self._hover_job = self.after(180, lambda: self._hover(widget))

    def _hover(self, widget):
        self._cancel("_hover_job")
        if widget.winfo_exists():
            node = self.layout.find(widget._node)
            if node and "children" in node:
                self._open(widget)
            else:
                self._close_menus(widget._depth)

    def _leave(self, widget):
        widget.configure(bg=widget._base, fg=widget._base_fg)
        self._cancel("_hover_job")
        self._schedule_hide()

    def _schedule_hide(self, event=None):
        self._cancel("_hide_job")
        self._hide_job = self.after(170, self._hide_if_outside)

    def _inside(self, x, y):
        for widget in [*(entry for entry in self._widgets if entry._horizontal), *(menu[1] for menu in self._menus)]:
            if widget.winfo_ismapped() and (widget.winfo_rootx() - 5 <= x <= widget.winfo_rootx() + widget.winfo_width() + 5
                                          and widget.winfo_rooty() - 5 <= y <= widget.winfo_rooty() + widget.winfo_height() + 5):
                return True
        return False

    def _hide_if_outside(self):
        self._hide_job = None
        if not self._pressed and not self._drag and not self._inside(*self.winfo_pointerxy()):
            self._close_menus()

    def _watch_pointer(self):
        self._watch_job = None
        if not self._menus:
            return
        if not self._pressed and not self._drag:
            if self._inside(*self.winfo_pointerxy()):
                self._cancel("_hide_job")
            elif self._hide_job is None:
                self._schedule_hide()
        self._watch_job = self.after(90, self._watch_pointer)

    def _outside(self, event):
        if not self._pressed and not self._inside(event.x_root, event.y_root):
            self._close_menus()

    def _open(self, widget):
        if not self.enabled or not widget.winfo_exists():
            return
        node = self.layout.find(widget._node)
        depth = widget._depth
        if node is None or "children" not in node:
            return
        if len(self._menus) > depth and self._menus[depth][0] == node["id"]:
            return
        self._close_menus(depth)
        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.c["border"])
        # A scrollable menu keeps folders with many entries usable on small screens.
        view = tk.Canvas(popup, bg=self.c["panel"], bd=0, highlightthickness=0)
        popup._view = view
        scroll = ttk.Scrollbar(popup, command=view.yview)
        content = tk.Frame(view, bg=self.c["panel"], padx=4, pady=4)
        view.create_window(0, 0, anchor=tk.NW, window=content)
        view.configure(yscrollcommand=scroll.set)
        view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=1, pady=1)
        for surface in (popup, view, content):
            surface._nav_container = node["id"]
        for child in node["children"]:
            self._entry(content, child, node["id"], depth + 1).pack(fill=tk.X, pady=1)
        if not node["children"]:
            tk.Label(content, text="Пустая папка · перетащите сюда пункт", bg=self.c["panel"],
                     fg=self.c["muted"], font=("Segoe UI", 9), padx=10, pady=8).pack()
        tk.Frame(content, bg=self.c["border"], height=1).pack(fill=tk.X, pady=(5, 3))
        tk.Button(content, text="+ Папка здесь", image=self.folder_icon, compound=tk.LEFT,
                  command=lambda: self._new_folder(node["id"]), font=("Segoe UI", 9),
                  bg=self.c["panel"], fg=self.c["muted"], activebackground=self.c["button_hover"],
                  activeforeground=self.c["fg"], relief="flat", bd=0, padx=8, pady=4, anchor=tk.W,
                  cursor="hand2").pack(fill=tk.X)
        popup.update_idletasks()
        width = max(190, content.winfo_reqwidth())
        height = min(content.winfo_reqheight(), max(180, self.winfo_screenheight() - 160))
        view.configure(width=width, height=height, scrollregion=view.bbox("all"))
        if content.winfo_reqheight() > height:
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
        below = widget._horizontal and not self.vertical
        x = widget.winfo_rootx() if below else widget.winfo_rootx() + widget.winfo_width() - 2
        y = widget.winfo_rooty() + widget.winfo_height() + 2 if below else widget.winfo_rooty() - 4
        popup.update_idletasks()
        if x + popup.winfo_reqwidth() > self.winfo_screenwidth() and not below:
            x = widget.winfo_rootx() - popup.winfo_reqwidth() + 2
        x = max(0, min(x, self.winfo_screenwidth() - popup.winfo_reqwidth()))
        y = max(0, min(y, self.winfo_screenheight() - popup.winfo_reqheight() - 40))
        popup.geometry(f"+{x}+{y}")
        popup.bind("<Enter>", lambda event: self._cancel("_hide_job"))
        popup.bind("<Leave>", self._schedule_hide)
        popup.bind("<Escape>", lambda event: self._escape())
        popup.bind("<Button-1>", self._outside, add="+")
        popup.bind("<Button-3>", lambda event: self._context(event, container=node["id"]))
        popup.bind("<MouseWheel>", lambda event: view.yview_scroll(-1 if event.delta > 0 else 1, "units"))
        self._menus.append((node["id"], popup))
        popup.deiconify()
        if self._watch_job is None:
            self._watch_job = self.after(90, self._watch_pointer)

    def _close_menus(self, depth=0):
        self._cancel("_hover_job")
        for _, popup in self._menus[depth:]:
            popup.destroy()
        del self._menus[depth:]
        self._widgets = [widget for widget in self._widgets if widget.winfo_exists()]
        if not self._menus:
            self._cancel("_watch_job")
            self._cancel("_hide_job")

    def _press(self, event, widget):
        if self.enabled:
            self._cancel("_hover_job")
            widget.focus_set()
            self._pressed = (widget._node, event.x_root, event.y_root, widget)
        return "break"

    def _click(self, widget):
        if not self.enabled or not widget.winfo_exists():
            return "break"
        node = self.layout.find(widget._node)
        if "children" in node:
            self._open(widget)
            if len(self._menus) > widget._depth:
                popup = self._menus[widget._depth][1]
                first = next((entry for entry in self._widgets
                              if entry.winfo_toplevel() == popup), None)
                if first is not None:
                    first.focus_set()
        else:
            self._close_menus()
            self._activate_node(node)
        return "break"

    def _activate_node(self, node):
        if self.activate_tab:
            self.activate_tab(node.get('page', node['id']), node['id'])
        else:
            self.activate(node.get('page', node['id']))

    def _target(self, x, y):
        widget = self.winfo_containing(x, y)
        while widget is not None:
            if hasattr(widget, "_node"):
                node = self.layout.find(widget._node)
                horizontal = widget._horizontal and not self.vertical
                pos = x - widget.winfo_rootx() if horizontal else y - widget.winfo_rooty()
                size = widget.winfo_width() if horizontal else widget.winfo_height()
                margin = min(16, size / 4)
                if "children" in node and margin <= pos <= size - margin:
                    return (node["id"], len(node["children"]), widget, True)
                parent, index = self.layout.location(node["id"])
                return (parent, index + (pos >= size / 2), widget, False)
            if hasattr(widget, "_nav_container"):
                parent = widget._nav_container
                return (parent, len(self.layout.children(parent)), widget, False)
            if widget in (self, self.items, self.canvas):
                return (None, len(self.layout.nodes), self.canvas, False)
            widget = widget.master
        return None

    def _motion(self, event):
        if not self._pressed:
            return
        key, x, y, _ = self._pressed
        if self._drag is None:
            if abs(event.x_root - x) + abs(event.y_root - y) < 7:
                return
            self._drag = key
            self.grab_set()
            self._ghost = tk.Toplevel(self)
            self._ghost.overrideredirect(True)
            self._ghost.attributes("-topmost", True)
            tk.Label(self._ghost, text=self.layout.find(key)["title"], bg=self.c["accent"],
                     fg=self.c["accent_fg"], font=("Segoe UI", 10, "bold"), padx=10, pady=6).pack()
        self._ghost.geometry(f"+{event.x_root + 16}+{event.y_root + 20}")
        target = self._target(event.x_root, event.y_root)
        valid = target is not None and self.layout.can_move(key, target[0])
        if target != self._drop:
            self._unmark()
            self._cancel("_hover_job")
            self._drop = target
            if valid:
                widget = target[2]
                if hasattr(widget, "_node"):
                    self._marked = widget
                    widget.configure(highlightbackground=self.c["accent"], bg=self.c["selected"], fg=self.c['fg'])
                if target[3]:
                    self._hover_job = self.after(400, lambda: self._hover(widget))
        self.configure(cursor="fleur" if valid else "X_cursor")
        for _, popup in self._menus:
            if (popup.winfo_rootx() <= event.x_root <= popup.winfo_rootx() + popup.winfo_width()
                    and popup.winfo_rooty() <= event.y_root <= popup.winfo_rooty() + popup.winfo_height()):
                offset = event.y_root - popup.winfo_rooty()
                if offset < 20:
                    popup._view.yview_scroll(-1, "units")
                elif offset > popup.winfo_height() - 20:
                    popup._view.yview_scroll(1, "units")
        # Scroll the root strip when dragging to an edge.
        if self.canvas.winfo_rooty() <= event.y_root <= self.canvas.winfo_rooty() + self.canvas.winfo_height():
            offset = event.y_root - self.canvas.winfo_rooty() if self.vertical else event.x_root - self.canvas.winfo_rootx()
            size = self.canvas.winfo_height() if self.vertical else self.canvas.winfo_width()
            scroll = self.canvas.yview_scroll if self.vertical else self.canvas.xview_scroll
            if offset < 24:
                scroll(-1, "units")
            elif offset > size - 24:
                scroll(1, "units")
        return "break"

    def _unmark(self):
        if self._marked is not None and self._marked.winfo_exists():
            self._marked.configure(highlightbackground=self.c["border"], bg=self._marked._base,
                                   fg=self._marked._base_fg)
        self._marked = None

    def _release(self, event):
        pressed, drag = self._pressed, self._drag
        target = self._target(event.x_root, event.y_root) if drag else None
        self._end_drag()
        if drag and target and self.layout.move(drag, target[0], target[1]):
            keep_open = target[0] or (self._menus[-1][0] if self._menus else None)
            self._commit(keep_open)
        elif pressed and not drag:
            self._click(pressed[3])
        return "break"

    def _end_drag(self):
        self._cancel("_hover_job")
        self._unmark()
        if self._drag:
            self.grab_release()
        if self._ghost is not None:
            self._ghost.destroy()
        self._ghost = self._pressed = self._drag = self._drop = None
        self.configure(cursor="")

    def _escape(self):
        self._end_drag()
        self._cancel("_hide_job")
        self._close_menus()
        if self._context_menu is not None:
            self._context_menu.unpost()
        return "break"

    def _commit(self, keep_open=None):
        self.redraw()
        if keep_open and self.layout.find(keep_open):
            chain = [keep_open]
            parent, _ = self.layout.location(keep_open)
            while parent is not None:
                chain.insert(0, parent)
                parent, _ = self.layout.location(parent)
            for key in chain:
                widget = next((entry for entry in self._widgets if entry._node == key), None)
                if widget is None:
                    break
                self._open(widget)
            self._cancel('_hide_job')
        self.changed(self.layout.snapshot())

    def _new_folder(self, parent=None):
        if not self.enabled:
            return
        self._escape()
        title = simpledialog.askstring("Новая папка", "Название папки:", parent=self.winfo_toplevel())
        if title and title.strip():
            self.layout.new_folder(title, parent)
            self._commit()

    def _new_tab(self):
        if not self.enabled:
            return
        self._escape()
        current = self.layout.find(self.active)
        page = current.get('page', self.active) if current else self.active
        dialog = TabDialog(self.winfo_toplevel(), self.c, page)
        if dialog.result:
            title, page = dialog.result
            key = self.layout.new_tab(title, page)
            self._commit()
            self.canvas.xview_moveto(0)
            self._activate_node(self.layout.find(key))

    def _close_tab(self, key):
        if self.can_close_tab and not self.can_close_tab(key):
            return
        if self.enabled and self.layout.remove_tab(key):
            self._commit()

    def _duplicate_tab(self, key):
        node = self.layout.find(key)
        if node is None or 'children' in node:
            return
        page = node.get('page', key)
        title = node['title']
        titles = {entry['title'] for entry, _ in self.layout.walk()}
        number = 2
        while title + ' · ' + str(number) in titles:
            number += 1
        new_key = self.layout.new_tab(title + ' · ' + str(number), page)
        self._commit()
        self._activate_node(self.layout.find(new_key))

    def _rename(self, key):
        self._escape()
        node = self.layout.find(key)
        title = simpledialog.askstring("Переименовать", "Новое название:", initialvalue=node["title"],
                                       parent=self.winfo_toplevel())
        if title and self.layout.rename(key, title):
            self._commit()

    def _remove(self, key):
        if self.layout.remove_folder(key):
            self._commit()

    def _reset(self):
        keep = []
        if self.can_close_tab:
            for node, _ in list(self.layout.walk()):
                if 'page' in node and not self.can_close_tab(node['id']):
                    keep.append(node)
        self.layout = NavigationLayout()
        self.layout.nodes[0:0] = keep
        self._commit()

    def _context(self, event, key=None, container=None):
        if not self.enabled:
            return "break"
        self._cancel("_hide_job")
        self._cancel("_hover_job")
        if self._context_menu is not None:
            self._context_menu.destroy()
        menu = tk.Menu(self, tearoff=False, bg=self.c["panel"], fg=self.c["fg"],
                       activebackground=self.c["selected"], activeforeground=self.c["fg"])
        self._context_menu = menu
        node = self.layout.find(key)
        folder = node is not None and "children" in node
        menu.add_command(label='Новая вкладка', command=self._new_tab)
        menu.add_command(label="Новая папка здесь", command=lambda: self._new_folder(container))
        if folder:
            menu.add_command(label="Новая вложенная папка", command=lambda: self._new_folder(key))
        if node is not None:
            menu.add_command(label="Переименовать", command=lambda: self._rename(key))
            if folder:
                menu.add_command(label="Убрать папку · сохранить пункты", command=lambda: self._remove(key))
            elif 'page' in node:
                menu.add_command(label='Закрыть вкладку', command=lambda: self._close_tab(key))
            if not folder:
                menu.add_command(label='Открыть в новой вкладке', command=lambda: self._duplicate_tab(key))
            # An accessible alternative to dragging, also handy for long menus.
            move = tk.Menu(menu, tearoff=False)
            parent, _ = self.layout.location(key)
            destinations = [(None, "Панель сверху")]
            for candidate, _ in self.layout.walk():
                if "children" in candidate and self.layout.can_move(key, candidate["id"]):
                    path = [candidate["title"]]
                    ancestor, _ = self.layout.location(candidate["id"])
                    while ancestor is not None:
                        path.insert(0, self.layout.find(ancestor)["title"])
                        ancestor, _ = self.layout.location(ancestor)
                    destinations.append((candidate["id"], " / ".join(path)))
            for destination, title in destinations:
                if destination != parent:
                    move.add_command(label=title, command=lambda value=destination: self._move_to(key, value))
            menu.add_cascade(label="Переместить в", menu=move)
        menu.add_separator()
        menu.add_command(label="По умолчанию", command=self._reset)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _move_to(self, key, parent):
        if self.layout.move(key, parent):
            self._commit()

    def close(self):
        self._escape()
        if self._context_menu is not None:
            self._context_menu.destroy()
        self.winfo_toplevel().unbind("<Button-1>", self._outside_binding)


class TabDialog(simpledialog.Dialog):
    def __init__(self, parent, colors, active='servers'):
        self.c = colors
        self.pages = list(PAGES)
        self.active = active if active in PAGES else 'servers'
        super().__init__(parent, title='Новая вкладка')

    def body(self, master):
        master.configure(bg=self.c['panel'])
        tk.Label(master, text='Раздел приложения', bg=self.c['panel'], fg=self.c['fg'],
                 font=('Segoe UI', 10)).grid(row=0, column=0, sticky='w', pady=(4, 6))
        self.page = ttk.Combobox(master, values=[PAGES[key] for key in self.pages], state='readonly',
                                  width=46, font=('Segoe UI', 10))
        self.page.current(self.pages.index(self.active))
        self.page.grid(row=1, column=0, sticky='ew')
        self.page.bind('<MouseWheel>', lambda event: 'break')
        self.page.bind('<Shift-MouseWheel>', lambda event: 'break')
        tk.Label(master, text='Название вкладки', bg=self.c['panel'], fg=self.c['fg'],
                 font=('Segoe UI', 10)).grid(row=2, column=0, sticky='w', pady=(12, 6))
        self.name = tk.Entry(master, font=('Segoe UI', 10), width=46)
        self.name.insert(0, PAGES[self.active])
        self.name.grid(row=3, column=0, sticky='ew', pady=(0, 8))
        bind_entry_shortcuts(self.name)
        self._previous_title = PAGES[self.active]
        def selected(event):
            title = PAGES[self.pages[self.page.current()]]
            if self.name.get() == self._previous_title:
                self.name.delete(0, tk.END)
                self.name.insert(0, title)
            self._previous_title = title
        self.page.bind('<<ComboboxSelected>>', selected)
        return self.name

    def validate(self):
        if self.page.current() < 0 or not self.name.get().strip():
            self.bell()
            return False
        return True

    def apply(self):
        self.result = self.name.get().strip()[:80], self.pages[self.page.current()]

    def buttonbox(self):
        box = tk.Frame(self)
        tk.Button(box, text='Добавить', width=12, command=self.ok, default=tk.ACTIVE).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(box, text='Отмена', width=12, command=self.cancel).pack(side=tk.LEFT, padx=5, pady=5)
        self.bind('<Return>', self.ok)
        self.bind('<Escape>', self.cancel)
        box.pack()
