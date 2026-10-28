import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from activity_store import ActivityStore, clean_profile, context_key
from activity_ui import endpoint, history_values
from postgres_runner import OperationCancelled, Runner
import test_service_ui
import test_database_copy


PROFILE = dict(kind='postgres', page='pg_dump', values=dict(execution='Локально · Windows',
    host='DB01', port='5432', username='postgres', sslmode='prefer', password='never-store-this',
    ib_password='never-store-this-either', target_host='DB02', target_port='5433', target_user='other',
    target_ssl='require', target_password='target-secret'))


def mock_dpapi(test):
    # The sandbox cannot access Windows user keys. Only the OS boundary is mocked;
    # the production JSON/envelope/base64 path is exercised. The native API has a
    # separate check in build/check_favorite_dpapi.py outside the sandbox.
    def fixture_cipher(data, decrypt=False):
        return bytes(value ^ 0xa7 for value in data)
    patcher = patch('favorite_secrets._crypt', side_effect=fixture_cipher)
    patcher.start()
    test.addCleanup(patcher.stop)


class StoreTests(unittest.TestCase):
    def setUp(self):
        mock_dpapi(self)

    def test_favorite_secrets_survive_reload_and_rename_but_never_enter_recents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'favorites.json'
            store = ActivityStore(path)
            identifier = store.save_favorite('Main', PROFILE)
            store.remember(PROFILE, 'Main')
            store.close(wait=True)
            loaded = ActivityStore(path)
            favorite = loaded.snapshot('favorites')[0]
            self.assertEqual(loaded.credentials(favorite), {key: PROFILE['values'][key]
                for key in ('password', 'ib_password', 'target_password')})
            loaded.save_favorite('Renamed', favorite, identifier)
            renamed = loaded.snapshot('favorites')[0]
            self.assertEqual(renamed['credentials'], favorite['credentials'])
            self.assertEqual(renamed['name'], 'Renamed')
            loaded.save_favorite('Renamed', dict(PROFILE, values=dict(PROFILE['values'], password='')))
            self.assertEqual(len(loaded.snapshot('favorites')), 1)
            self.assertEqual(loaded.credentials(loaded.snapshot('favorites')[0])['password'], '')
            loaded.close(wait=True)
            for secret in ('never-store-this', 'target-secret'):
                self.assertNotIn(secret, path.read_text(encoding='utf-8'))
            self.assertNotIn('credentials', loaded.snapshot('recent')[0]['profile'])

    def test_encryption_failure_preserves_existing_favorite(self):
        store = ActivityStore()
        store.save_favorite('Main', PROFILE)
        before = store.snapshot('favorites')
        with patch('favorite_secrets._crypt', side_effect=ValueError('Test protection failure')):
            with self.assertRaises(ValueError):
                store.save_favorite('Main', PROFILE)
        self.assertEqual(store.snapshot('favorites'), before)

    def test_malformed_record_does_not_hide_other_favorites_recents_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            valid = dict(clean_profile(PROFILE), id='valid', name='Keep')
            recent = dict(profile=clean_profile(PROFILE), database='Main')
            path.write_text(json.dumps(dict(favorites=[None, {}, valid], recent=[None, recent],
                                            history=[None, dict(id='history', status='Успешно')])), encoding='utf-8')
            store = ActivityStore(path)
            self.assertEqual([row['id'] for row in store.snapshot('favorites')], ['valid'])
            self.assertEqual([row['database'] for row in store.snapshot('recent')], ['Main'])
            self.assertEqual([row['id'] for row in store.snapshot('history')], ['history'])
            self.assertIn('4', store.error)

    def test_snapshot_limit_copies_only_requested_rows_and_does_not_expose_store(self):
        store = ActivityStore()
        for number in range(20):
            store.remember(PROFILE, str(number))
        rows = store.snapshot('recent', limit=3)
        self.assertEqual(len(rows), 3)
        rows[0]['profile']['values']['host'] = 'Changed outside store'
        self.assertEqual(len(store.snapshot('recent')), 20)
        self.assertEqual(store.snapshot('recent')[0]['profile']['values']['host'], 'DB01')

    def test_roundtrip_excludes_all_passwords_and_raw_output_and_keeps_artifact_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            store = ActivityStore(path)
            identifier = store.save_favorite('Работа', PROFILE)
            store.remember(PROFILE, 'Main')
            token = store.begin(dict(operation='Бэкап', server='DB01', database='Main',
                                     password='secret', output='secret-output'))
            store.artifact(token, dict(path='/BACKUP/Main.dump', size=123456789))
            store.end(token, 'Успешно')
            store.close(wait=True)
            raw = path.read_text(encoding='utf-8')
            for forbidden in ('password', 'secret', 'never-store', 'secret-output'):
                self.assertNotIn(forbidden, raw)
            loaded = ActivityStore(path)
            self.assertEqual(loaded.snapshot('favorites')[0]['id'], identifier)
            self.assertEqual(loaded.snapshot('recent')[0]['database'], 'Main')
            row = loaded.snapshot('history')[0]
            self.assertEqual(row['status'], 'Успешно')
            self.assertEqual(row['artifacts'][0]['size'], 123456789)
            self.assertGreaterEqual(row['duration'], 0)
            self.assertIn('/BACKUP/Main.dump', history_values(row))
            self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_recent_is_scoped_to_endpoint_user_executor_and_role(self):
        store = ActivityStore()
        store.remember(PROFILE, 'Main')
        store.remember(PROFILE, 'Main', 'target')
        other = dict(PROFILE, values=dict(PROFILE['values'], host='DB03'))
        store.remember(other, 'Main')
        rows = store.snapshot('recent')
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['context'] for row in rows}), 3)
        self.assertEqual(endpoint(clean_profile(PROFILE), 'target'), 'DB02:5433')
        with_password = dict(PROFILE, values=dict(PROFILE['values'], password='changed'))
        self.assertEqual(context_key(with_password), context_key(PROFILE))
        store.remember(other, 'Main')
        self.assertEqual(len(store.snapshot('recent')), 3)

    def test_finished_history_clear_preserves_running_and_reopen_marks_unfinished(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            store = ActivityStore(path)
            done = store.begin(dict(operation='One'))
            store.end(done, 'Успешно')
            active = store.begin(dict(operation='Two'))
            store.clear_history()
            self.assertEqual([row['id'] for row in store.snapshot('history')], [active[0]])
            store.close(wait=True)
            loaded = ActivityStore(path)
            self.assertEqual(loaded.snapshot('history')[0]['status'], 'Не завершено')

    def test_known_size_survives_later_unknown_metadata_and_failure(self):
        store = ActivityStore()
        token = store.begin(dict(operation='Бэкап + восстановление'))
        for size in (None, 123, None):
            store.artifact(token, dict(path='/BACKUP/fresh.dump', size=size))
        store.end(token, 'Ошибка')
        row = store.snapshot('history')[0]
        self.assertEqual(row['artifacts'], [dict(path='/BACKUP/fresh.dump', size=123)])
        self.assertEqual(row['status'], 'Ошибка')

    def test_corrupt_file_and_write_failure_do_not_crash_the_application(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'activity.json'
            path.write_text('{broken', encoding='utf-8')
            store = ActivityStore(path)
            self.assertTrue(store.error)
            with patch('activity_store.os.replace', side_effect=PermissionError('readonly')):
                store.save_favorite('One', PROFILE)
                store.close(wait=True)
            self.assertIn('readonly', store.error)
            self.assertEqual(len(store.snapshot('favorites')), 1)
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    def test_history_and_recents_are_bounded(self):
        store = ActivityStore()
        for number in range(520):
            token = store.begin(dict(operation=str(number)))
            store.end(token, 'Успешно')
            store.remember(PROFILE, str(number))
        self.assertEqual(len(store.snapshot('history')), 500)
        self.assertEqual(len(store.snapshot('recent')), 100)

    def test_artifact_sizes_are_measured_at_the_executor_for_files_and_directories(self):
        from test_postgres import CONNECTION
        events = []
        runner = Runner(CONNECTION, events.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'toc.dat').write_bytes(b'abc')
            (path / 'data').mkdir()
            (path / 'data' / '1.dat.gz').write_bytes(b'abcdef')
            runner.report_artifact(path)
            self.assertEqual(events[-1]['size'], 9)
            runner.report_artifact(path / 'toc.dat')
            self.assertEqual(events[-1]['size'], 3)

    def test_sql_history_uses_compressed_size_with_case_insensitive_column_names(self):
        from sql_server import report_backup_artifact, restore_plan
        from test_sql_server import HEADER, FILES, TARGET
        header = dict(HEADER, compressedbackupsize='12345', BackupSize=999999)
        plan = restore_plan('Target', r'D:\Backup\file.bak', header, FILES, TARGET)
        self.assertEqual(plan.backup_size, 12345)
        events = []
        report_backup_artifact({}, plan.path, events.append, [[], [], [header]])
        self.assertEqual(events[-1]['size'], 12345)
        report_backup_artifact({}, plan.path, events.append, [[], [], [dict(CompressedBackupSize='invalid')]])
        self.assertIsNone(events[-1]['size'])


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self)
        mock_dpapi(self)

    def test_saved_copy_favorite_restores_all_passwords_without_starting_network(self):
        panel = self.app.postgres_copy
        panel.password.set('Source-pass')
        panel.target_password.set('Target-pass')
        panel.ib_password.set('1C-pass')
        panel.host.set('SOURCE')
        panel.target_host.set('TARGET')
        with patch('activity_ui.simpledialog.askstring', return_value='Pair'):
            self.app.hub.save(panel)
        favorite = self.app.activity.snapshot('favorites')[0]
        panel.password.set('Changed')
        with patch.object(panel, 'work') as work:
            self.assertTrue(self.app.hub.apply(favorite))
            work.assert_not_called()
        self.assertEqual((panel.password.get(), panel.target_password.get(), panel.ib_password.get()),
                         ('Source-pass', 'Target-pass', '1C-pass'))
        self.assertNotIn('Source-pass', json.dumps(favorite))

    def test_unreadable_password_does_not_change_existing_form(self):
        panel = self.app.postgres_copy
        panel.host.set('Keep-host')
        panel.password.set('Keep-secret')
        self.app.activity.save_favorite('Pair', dict(PROFILE, page='pg_copy'))
        favorite = self.app.activity.snapshot('favorites')[0]
        with patch('activity_store.unprotect', side_effect=ValueError('Unreadable credentials')):
            self.assertFalse(self.app.hub.apply(favorite))
        self.assertEqual((panel.host.get(), panel.password.get()), ('Keep-host', 'Keep-secret'))

    def test_sql_and_cluster_favorites_restore_their_respective_credentials(self):
        sql = self.app.sql
        sql.password.set('SQL-pass')
        self.app.activity.save_favorite('SQL', self.app.hub.profile(sql, include_passwords=True))
        favorite = self.app.activity.snapshot('favorites')[0]
        sql.password.set('')
        self.assertTrue(self.app.hub.apply(favorite))
        self.assertEqual(sql.password.get(), 'SQL-pass')
        self.app.all_items = [dict(server='ONEC', port='1540', version='8.3.27.1859')]
        self.app._switch_mode('cluster_infobase')
        panel = self.app.cluster_infobase
        self.app.user_var.set('Cluster-admin')
        self.app.pwd_var.set('Cluster-pass')
        panel.db_user.set('postgres')
        panel.db_password.set('DB-pass')
        panel.ib_user.set('IB-admin')
        panel.ib_password.set('IB-pass')
        self.app.activity.save_favorite('Cluster', self.app.hub.profile(panel, include_passwords=True))
        favorite = self.app.activity.snapshot('favorites')[0]
        panel.db_password.set('')
        panel.ib_password.set('')
        self.app.pwd_var.set('')
        self.assertTrue(self.app.hub.apply(favorite))
        self.assertEqual((self.app.pwd_var.get(), panel.db_password.get(), panel.ib_password.get()),
                         ('Cluster-pass', 'DB-pass', 'IB-pass'))

    def test_refresh_buttons_call_current_connection_for_each_database_list(self):
        for panel in (self.app.sql, self.app.postgres, self.app.sql_copy, self.app.postgres_copy):
            for role in ('source', 'target') if hasattr(panel, 'target_tree') else ('source',):
                name = ('refresh_' + role) if hasattr(panel, 'refresh_' + role) else 'connect'
                with patch.object(panel, name) as refresh:
                    self.app.hub.refresh_buttons[(panel, role)].invoke()
                    refresh.assert_called_once_with()
        panel = self.app.cluster_infobase
        with patch.object(panel, 'selected_cluster', return_value={'cluster': 'cluster-id'}), patch.object(panel, 'load_bases') as refresh:
            self.app.hub.refresh_buttons[(panel, 'source')].invoke()
            refresh.assert_called_once_with()

    def test_home_has_real_sections_and_badge_returns_to_home(self):
        self.app._switch_mode('home')
        self.app.root.deiconify()
        self.app.root.update()
        self.assertEqual(set(self.app.home.tables), {'favorites', 'recent', 'history'})
        self.assertIn('Консоль администратора КА', self.app.root.title())
        self.assertLess(self.app.home_badge.winfo_height(), 55)
        self.app._switch_mode('pg_dump')
        self.app.home_badge.event_generate('<Button-1>')
        self.app.root.update()
        self.assertEqual(self.app.mode, 'home')
        self.assertTrue(self.app.home.winfo_ismapped())

    def test_favorite_application_only_fills_form_and_clears_old_passwords(self):
        panel = self.app.postgres_copy
        panel.password.set('old-source-secret')
        panel.target_password.set('old-target-secret')
        profile = clean_profile(dict(PROFILE, page='pg_copy'))
        with patch('postgres_ui.run_job') as run, patch('database_copy_ui.run_job') as copy:
            self.assertTrue(self.app.hub.apply(profile))
            run.assert_not_called()
            copy.assert_not_called()
        self.assertEqual(panel.host.get(), 'DB01')
        self.assertEqual(panel.target_host.get(), 'DB02')
        self.assertEqual(panel.password.get(), '')
        self.assertEqual(panel.target_password.get(), '')

    def test_favorite_manager_can_rename_remove_and_history_can_be_copied(self):
        identifier = self.app.activity.save_favorite('Main', PROFILE)
        window = self.app.hub.dialog('favorites')
        window.tree.selection_set(window.tree.get_children()[0])
        with patch('activity_ui.simpledialog.askstring', return_value='Renamed'):
            window.rename()
        self.assertEqual(self.app.activity.snapshot('favorites')[0]['name'], 'Renamed')
        window.tree.selection_set(window.tree.get_children()[0])
        window.remove()
        self.assertFalse(self.app.activity.snapshot('favorites'))
        token = self.app.activity.begin(dict(operation='Backup', server='DB01', database='Main'))
        self.app.activity.artifact(token, dict(path='/BACKUP/Main.dump', size=1024))
        self.app.activity.end(token, 'Успешно')
        history = self.app.hub.dialog('history')
        history.tree.selection_set(history.tree.get_children()[0])
        history.show_details()
        history.copy()
        self.assertIn('/BACKUP/Main.dump', self.app.root.clipboard_get())
        with patch('activity_ui.messagebox.askyesno', return_value=False):
            history.clear()
        self.assertEqual(len(self.app.activity.snapshot('history')), 1)

    def test_recent_database_is_selected_after_connecting_to_matching_server(self):
        profile = clean_profile(PROFILE)
        recent = dict(profile=profile, context=context_key(profile), database='Main', role='source')
        self.app.hub.apply(profile, recent)
        panel = self.app.postgres
        panel.loaded(panel.connection_key(), [dict(name='Other', owner='postgres'), dict(name='Main', owner='postgres')])
        self.assertEqual(panel.selected(), ['Main'])
        self.app.root.update()
        self.assertEqual(self.app.activity.snapshot('recent')[0]['database'], 'Main')

    def test_select_isolates_exact_name_and_lists_remain_independent(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = test_database_copy.CopyUiTests.sql_panel(self) if mode == 'db_copy' else test_database_copy.CopyUiTests.pg_panel(self)
            rows = panel.databases
            rows.append(dict(rows[0], name='Original_extra'))
            panel.render()
            target_before = [panel.target_tree.set(item, 'name') for item in panel.target_tree.get_children()]
            source_row = next(item for item in panel.tree.get_children() if panel.tree.set(item, 'name') == 'Original')
            panel.tree.selection_set(source_row)
            self.app.hub.select_buttons[(panel, 'source')].invoke()
            self.assertEqual([panel.tree.set(item, 'name') for item in panel.tree.get_children()], ['Original'])
            self.assertEqual([panel.target_tree.set(item, 'name') for item in panel.target_tree.get_children()], target_before)
            self.assertEqual(len(panel.tree.selection()), 1)
            self.app.hub.all_buttons[(panel, 'source')].invoke()
            self.assertIn('Original_extra', [panel.tree.set(item, 'name') for item in panel.tree.get_children()])
            self.app.hub.select_buttons[(panel, 'target')].invoke()
            self.assertEqual(len(panel.target_tree.get_children()), 1)

    def test_select_button_in_regular_postgres_requires_a_single_row(self):
        panel = self.app.postgres
        self.app._switch_mode('pg_dump')
        panel.loaded(panel.connection_key(), [dict(name=name, owner='postgres') for name in ('One', 'One_extra', 'Two')])
        panel.tree.selection_set(panel.tree.get_children()[:2])
        self.app.hub.select_buttons[(panel, 'source')].invoke()
        self.assertEqual(len(panel.tree.get_children()), 3)
        panel.tree.selection_set(panel.tree.get_children()[0])
        self.app.hub.select_buttons[(panel, 'source')].invoke()
        self.assertEqual([panel.tree.set(item, 'name') for item in panel.tree.get_children()], ['One'])
        self.app.hub.all_buttons[(panel, 'source')].invoke()
        self.assertEqual(len(panel.tree.get_children()), 3)

    def test_double_left_click_isolates_the_clicked_database_without_running_a_job(self):
        for mode in ('db_copy', 'pg_copy'):
            panel = test_database_copy.CopyUiTests.sql_panel(self) if mode == 'db_copy' else test_database_copy.CopyUiTests.pg_panel(self)
            self.app.root.deiconify()
            self.app.root.update()
            for tree, role in ((panel.tree, 'source'), (panel.target_tree, 'target')):
                row = tree.get_children()[0]
                expected = tree.set(row, 'name')
                box = tree.bbox(row)
                x, y = box[0] + 20, box[1] + box[3] // 2
                with patch.object(panel, 'work') as work:
                    for stamp in (1000, 1050):
                        tree.event_generate('<ButtonPress-1>', x=x, y=y, time=stamp)
                        tree.event_generate('<ButtonRelease-1>', x=x, y=y, time=stamp + 5)
                    self.app.root.update()
                    work.assert_not_called()
                self.assertEqual([tree.set(item, 'name') for item in tree.get_children()], [expected])
                self.assertEqual(len(tree.selection()), 1)
                self.app.hub.all_buttons[(panel, role)].invoke()

    def test_cluster_and_main_database_select_buttons_preserve_full_data(self):
        app = self.app
        target = dict(server='ONEC', port='1540', version='8.3.27.1859')
        app.all_items = [target]
        app._switch_mode('cluster_infobase')
        panel = app.cluster_infobase
        panel.render_clusters([dict(cluster='cluster-id', name='Main')])
        panel.render_bases([dict(infobase=str(number), name=name) for number, name in enumerate(('One', 'One_extra'))])
        panel.tree.selection_set('0')
        app.hub.select_buttons[(panel, 'source')].invoke()
        self.assertEqual(len(panel.tree.get_children()), 1)
        self.assertEqual(len(panel.bases), 2)
        app.hub.all_buttons[(panel, 'source')].invoke()
        self.assertEqual(len(panel.tree.get_children()), 2)
        app._all_bases = [dict(target, base=name, cluster='Main', cluster_id='cluster-id') for name in ('One', 'One_extra')]
        app._switch_mode('bases')
        app.tree.selection_set('0')
        app.choose_base_button.invoke()
        self.assertEqual(len(app.tree.get_children()), 1)
        app.all_bases_button.invoke()
        self.assertEqual(len(app.tree.get_children()), 2)
        app.root.deiconify()
        app.root.update()
        box = app.tree.bbox(app.tree.get_children()[0])
        with patch.object(app, '_do_connect') as connect, patch.object(app, '_open_base') as launch:
            for stamp in (1000, 1050):
                app.tree.event_generate('<ButtonPress-1>', x=box[0]+10, y=box[1]+box[3]//2, time=stamp)
                app.tree.event_generate('<ButtonRelease-1>', x=box[0]+10, y=box[1]+box[3]//2, time=stamp+5)
            app.root.update()
            connect.assert_not_called()
            launch.assert_called_once_with()
        self.assertEqual(len(app.tree.get_children()), 2)

    def test_history_tracks_success_error_cancel_and_keeps_backup_when_restore_fails(self):
        panel = self.app.postgres
        for failure, expected in ((None, 'Успешно'), (RuntimeError('secret-error'), 'Ошибка'),
                                  (OperationCancelled('stop'), 'Прервано')):
            panel.start()
            def operation(emit):
                emit(dict(kind='artifact', path='/BACKUP/fresh.dump', size=456))
                if failure:
                    raise failure
                return 'done'
            with patch('administration_ui.threading.Thread') as thread:
                panel.work(operation, lambda result: panel.finish(result),
                           history=panel.history_context('Бэкап + восстановление', 'Source', 'Target'))
                thread.call_args.kwargs['target']()
            self.app._drain_events()
            row = self.app.activity.snapshot('history')[0]
            self.assertEqual(row['status'], expected)
            self.assertEqual(row['artifacts'][0]['path'], '/BACKUP/fresh.dump')
            self.assertNotIn('secret-error', json.dumps(row))
            self.assertFalse(panel.running)

    def test_black_green_journal_does_not_rewrap_during_window_resizing(self):
        app, panel = self.app, self.app.postgres
        app._switch_mode('pg_dump')
        app.root.deiconify()
        panel.append(('Long log message ' * 20 + '\n') * 4990)
        app.root.update()
        self.assertEqual(panel.output.cget('wrap'), 'none')
        self.assertEqual(panel.output.cget('bg'), '#0b0f0c')
        self.assertEqual(panel.output.cget('fg'), '#75ed91')
        ticks = []
        def heartbeat():
            ticks.append(time.monotonic())
            if len(ticks) < 100:
                app.root.after(1, heartbeat)
        heartbeat()
        start = time.monotonic()
        for width in range(1000, 1400, 10):
            app.root.geometry(f'{width}x700')
            app.root.update()
        self.assertLess(time.monotonic() - start, 8)
        self.assertGreater(len(ticks), 2)
