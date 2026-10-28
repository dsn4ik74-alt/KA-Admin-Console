"""Read/verify cluster state and delete registrations; no real 1C or SQL server."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from add_cluster_infobase import ClusterClient
from postgres_runner import OperationCancelled
import test_cluster_infobase as existing

CLUSTER, INFOBASE, PARAMS = existing.CLUSTER, existing.INFOBASE, existing.PARAMS
SUMMARY = existing.CREATED + 'name : "Name"\ndescr : "Description"\n'
INFO_ON = existing.CREATED + 'scheduled-jobs-deny : on\n'


class BackendTests(unittest.TestCase):
    client = existing.ClientTests.client

    def test_creation_checkbox_is_repaired_only_after_mismatch_is_read(self):
        client, events = self.client()
        with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, existing.INFO_OFF, '', INFO_ON]) as run:
            result = client.create(CLUSTER, dict(PARAMS, **{'scheduled-jobs-deny': 'on'}))
        self.assertTrue(result['scheduled_jobs_deny'])
        self.assertFalse(result['settings_error'])
        update, info = [call.args[0] for call in run.call_args_list[-2:]]
        self.assertEqual(update[1:3], ['infobase', 'update'])
        self.assertIn('--scheduled-jobs-deny=on', update)
        self.assertIn('--infobase=' + INFOBASE, update)
        self.assertEqual(info[1:3], ['infobase', 'info'])
        self.assertIn('Проверено', str(events))

    def test_creation_time_flag_is_verified_without_additional_update(self):
        client, events = self.client()
        with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, INFO_ON]) as run:
            result = client.create(CLUSTER, dict(PARAMS, **{'scheduled-jobs-deny': 'on'}))
        self.assertTrue(result['scheduled_jobs_deny'])
        self.assertFalse(result['settings_error'])
        self.assertIn('--scheduled-jobs-deny=on', run.call_args_list[3].args[0])
        self.assertFalse(any(call.args[0][1:3] == ['infobase', 'update'] for call in run.call_args_list))

    def test_permission_denied_on_read_keeps_registration_unknown_without_update_or_retry(self):
        client, events = self.client()
        denied = RuntimeError('rac: код 4294967295. Недостаточно прав пользователя на информационную базу')
        with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, denied]) as run:
            result = client.create(CLUSTER, dict(PARAMS, **{'scheduled-jobs-deny': 'on'}))
        self.assertEqual(result['infobase_id'], INFOBASE)
        self.assertIsNone(result['scheduled_jobs_deny'])
        self.assertTrue(result['jobs_access_required'])
        self.assertIn('передан при создании', result['settings_error'])
        self.assertIn('Авторизация ИБ', result['settings_error'])
        self.assertEqual(run.call_count, 5)
        self.assertFalse(any(call.args[0][1:3] in (['infobase', 'update'], ['infobase', 'drop']) for call in run.call_args_list))

    def test_creation_verification_passes_infobase_auth_separately_from_sql_credentials(self):
        client, _ = self.client('ClusterAdmin', 'cluster-secret')
        client.ib_user, client.ib_password = 'IBAdmin', 'ib-secret'
        with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, INFO_ON]) as run:
            client.create(CLUSTER, dict(PARAMS, **{'scheduled-jobs-deny': 'on'}))
        info = run.call_args.args[0]
        self.assertIn('--infobase-user=IBAdmin', info)
        self.assertIn('--infobase-pwd=ib-secret', info)
        self.assertIn('--cluster-user=ClusterAdmin', info)
        self.assertFalse(any(arg.startswith('--db-user=') for arg in info))

    def test_missing_wrong_or_other_infobase_state_never_reports_checkbox_success(self):
        for info in (existing.INFO_OFF, existing.CREATED, INFO_ON.replace(INFOBASE, 'eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee')):
            client, _ = self.client()
            with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, existing.INFO_OFF, '', info]) as run:
                result = client.create(CLUSTER, dict(PARAMS, **{'scheduled-jobs-deny': 'on'}))
            self.assertEqual(result['infobase_id'], INFOBASE)
            self.assertIsNone(result['scheduled_jobs_deny'])
            self.assertIn('не удалось', result['settings_error'])
            self.assertEqual(sum(call.args[0][1:3] == ['infobase', 'create'] for call in run.call_args_list), 1)

    def test_cancel_after_creation_says_infobase_exists_without_retry_or_drop(self):
        client, _ = self.client()
        with patch.object(client, 'run', side_effect=[existing.HELP, existing.CLUSTERS, '', existing.CREATED, OperationCancelled('Stop')]) as run:
            with self.assertRaisesRegex(OperationCancelled, 'ИБ уже добавлена'):
                client.create(CLUSTER, PARAMS)
        self.assertFalse(any(call.args[0][1:3] == ['infobase', 'drop'] for call in run.call_args_list))

    def test_overview_reads_only_the_selected_cluster_and_does_not_read_every_base_info(self):
        client, _ = self.client()
        other = 'eeeeeeee-2222-4333-8444-555555555555'
        clusters = existing.CLUSTERS + '\ncluster : ' + other + '\nname : Other\n'
        with patch.object(client, 'run', side_effect=[clusters, SUMMARY]) as run:
            result = client.overview(CLUSTER)
        self.assertEqual(result['cluster'], CLUSTER)
        self.assertEqual(result['bases'][0]['name'], 'Name')
        self.assertIn('--cluster=' + CLUSTER, run.call_args.args[0])
        self.assertEqual(run.call_count, 2)
        with patch.object(client, 'run', return_value=clusters) as run:
            self.assertEqual(client.overview()['bases'], [])
        self.assertEqual(run.call_count, 1)

    def test_drop_uses_checked_uuid_and_never_requests_drop_or_clear_of_database(self):
        client, _ = self.client('cluster-user', 'cluster-secret')
        client.ib_user, client.ib_password = 'IBAdmin', 'ib-secret'
        with patch.object(client, 'run', side_effect=[existing.CLUSTERS, SUMMARY, '']) as run:
            self.assertEqual(client.drop(CLUSTER, INFOBASE, 'Name'), INFOBASE)
        args = run.call_args.args[0]
        self.assertEqual(args[1:3], ['infobase', 'drop'])
        self.assertIn('--cluster=' + CLUSTER, args)
        self.assertIn('--infobase=' + INFOBASE, args)
        self.assertIn('--infobase-user=IBAdmin', args)
        self.assertIn('--infobase-pwd=ib-secret', args)
        self.assertFalse(any(arg.startswith(('--drop-database', '--clear-database')) for arg in args))
        self.assertEqual(run.call_count, 3)

    def test_renamed_missing_or_invalid_uuid_never_executes_drop(self):
        for summary in ('', SUMMARY.replace('Name', 'Renamed')):
            client, _ = self.client()
            with patch.object(client, 'run', side_effect=[existing.CLUSTERS, summary]) as run, self.assertRaises(ValueError):
                client.drop(CLUSTER, INFOBASE, 'Name')
            self.assertEqual(run.call_count, 2)
        client, _ = self.client()
        with patch.object(client, 'connect') as connect, self.assertRaises(ValueError):
            client.drop(CLUSTER, 'invalid-id', 'Name')
        connect.assert_not_called()

    def test_existing_infobase_jobs_update_uses_separate_auth_and_reads_actual_state(self):
        client, _ = self.client()
        client.ib_user, client.ib_password = 'IBAdmin', 'ib-secret'
        with patch.object(client, 'run', side_effect=[existing.CLUSTERS, SUMMARY, '', INFO_ON]) as run:
            self.assertTrue(client.apply_scheduled_jobs(CLUSTER, INFOBASE, 'Name', True))
        for call in run.call_args_list[-2:]:
            self.assertIn('--infobase-user=IBAdmin', call.args[0])
            self.assertIn('--infobase-pwd=ib-secret', call.args[0])
            self.assertFalse(any(arg.startswith('--db-pwd') for arg in call.args[0]))


# Real child processes implement a server fixture that deliberately ignores the
# creation-time flag. The update/read-back must therefore repair that state.
FAKE_RAC = r'''
import json,os,pathlib,sys
args=sys.argv[1:]
statefile=pathlib.Path(os.environ['CONSOLE_TEST_RAC_STATE'])
state=json.loads(statefile.read_text())
state['calls'].append(args)
verb=args[2] if len(args)>2 else ''
cluster=os.environ['CONSOLE_TEST_RAC_CLUSTER']
infobase=os.environ['CONSOLE_TEST_RAC_INFOBASE']
if args[1:]==['help','infobase']:
    print(os.environ['CONSOLE_TEST_RAC_HELP'])
elif args[1:3]==['cluster','list']:
    print('cluster : '+cluster+'\nname : Main')
elif args[1:4]==['infobase','summary','list']:
    if state['registered']:
        print('infobase : '+infobase+'\nname : Name\ndescr : Description')
elif verb=='create':
    state['registered']=True
    state['jobs']='off'
    print('infobase : '+infobase)
elif verb=='update':
    if not state.get('ignore_update'):
        state['jobs']=next(arg.split('=',1)[1] for arg in args if arg.startswith('--scheduled-jobs-deny='))
elif verb=='info':
    print('infobase : '+infobase+'\nscheduled-jobs-deny : '+state['jobs'])
elif verb=='drop':
    state['registered']=False
statefile.write_text(json.dumps(state))
'''


class ProcessTests(unittest.TestCase):
    def test_ignored_creation_flag_repaired_then_registration_drop_keeps_sql_data(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            script, statefile = directory / 'rac.py', directory / 'state.json'
            script.write_text(FAKE_RAC, encoding='utf-8')
            statefile.write_text(json.dumps(dict(registered=False, jobs='off', sql_data='preserved', calls=[])))
            client, events = existing.ClientTests.client(self)
            native_run = client.run
            client.run = lambda args, timeout=15: native_run([sys.executable, str(script), *args], timeout=timeout)
            with patch.dict(os.environ, CONSOLE_TEST_RAC_STATE=str(statefile), CONSOLE_TEST_RAC_CLUSTER=CLUSTER,
                            CONSOLE_TEST_RAC_INFOBASE=INFOBASE, CONSOLE_TEST_RAC_HELP=existing.HELP):
                parameters = dict(PARAMS, name='Name', **{'scheduled-jobs-deny': 'on'})
                result = client.create(CLUSTER, parameters)
                self.assertTrue(result['scheduled_jobs_deny'])
                self.assertEqual(statefile.read_text().count('"jobs": "on"'), 1)
                client.drop(CLUSTER, INFOBASE, 'Name')
            state = json.loads(statefile.read_text())
            self.assertFalse(state['registered'])
            self.assertEqual(state['sql_data'], 'preserved')
            self.assertEqual(sum(args[1:3] == ['infobase', 'create'] for args in state['calls']), 1)
            self.assertNotIn('db-secret', str(events))


class UiTests(unittest.TestCase):
    setUp = existing.UiTests.setUp
    panel = existing.UiTests.panel

    def select(self):
        panel = self.panel()
        panel.tree.selection_set(panel.tree.get_children()[0])
        self.app.root.update()
        return panel

    def test_right_table_contains_infobases_and_cluster_selector_is_separate(self):
        panel = self.select()
        self.assertEqual(panel.tree.set('0', 'name'), 'Existing')
        self.assertEqual(panel.tree.set('0', 'jobs'), 'Не проверена')
        self.assertEqual(panel.selected_cluster()['cluster'], CLUSTER)
        self.assertIn('Основной', panel.cluster_combo.get())
        self.assertEqual(panel.selected_base()['name'], 'Existing')
        self.assertEqual(str(panel.delete_button.cget('state')), 'normal')

    def test_delete_cancel_default_no_does_not_start_worker_or_client(self):
        panel = self.select()
        with (patch('cluster_infobase_ui.messagebox.askyesno', return_value=False) as confirm, patch.object(panel, 'work') as work,
              patch('cluster_infobase_ui.ClusterClient') as client):
            panel.delete_button.invoke()
        self.assertEqual(confirm.call_args.kwargs['default'], 'no')
        self.assertIn('СУБД останется', confirm.call_args.args[1])
        self.assertIn('Existing', confirm.call_args.args[1])
        self.assertIn(CLUSTER, confirm.call_args.args[1])
        client.assert_not_called()
        work.assert_not_called()

    def test_search_filters_names_and_descriptions_without_network_or_losing_cached_bases(self):
        panel = self.panel()
        rows = [dict(infobase=INFOBASE, name='Бухгалтерия', descr='Производство'),
                dict(infobase='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee', name='ЗУП', descr='Кадры')]
        panel.render_bases(rows, INFOBASE)
        with patch.object(panel, 'work') as work, patch('cluster_infobase_ui.ClusterClient') as client:
            panel.base_search.set('  ПРоиЗВоД  ')
            self.assertEqual([panel.tree.set(iid, 'name') for iid in panel.tree.get_children()], ['Бухгалтерия'])
            self.assertEqual(panel.selected_base()['infobase'], INFOBASE)
            self.assertEqual(panel.base_count.cget('text'), '1 из 2')
            panel.base_search.set('зуп')
            self.assertEqual(panel.tree.get_children(), ('1',))
            self.assertIsNone(panel.selected_base())
            self.assertEqual(str(panel.delete_button.cget('state')), 'disabled')
            self.assertEqual(panel.address.get(), '')
            panel.tree.selection_set('1')
            self.assertEqual(panel.selected_base()['name'], 'ЗУП')
            panel.base_search.set('ничего')
            self.assertEqual(panel.tree.get_children(), ())
            self.assertEqual(panel.base_count.cget('text'), '0 из 2')
            panel.base_search.set('')
            self.assertEqual(len(panel.tree.get_children()), 2)
            self.assertEqual(panel.bases, rows)
            self.assertEqual(panel.base_cache[panel.cache_key()], rows)
        client.assert_not_called()
        work.assert_not_called()

    def test_search_applies_after_refresh_and_sorting_keeps_correct_infobase(self):
        panel = self.panel()
        panel.base_search.set('test')
        rows = [dict(infobase=INFOBASE, name='Test Z'),
                dict(infobase='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee', name='Test A'),
                dict(infobase='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', name='Other')]
        panel.render_bases(rows)
        panel.tree.tk.call(panel.tree.heading('name', 'command'))
        self.assertEqual([panel.tree.set(iid, 'name') for iid in panel.tree.get_children()], ['Test A', 'Test Z'])
        panel.tree.selection_set(panel.tree.get_children()[0])
        self.assertEqual(panel.selected_base()['infobase'], rows[1]['infobase'])
        panel.base_search.set('Test A')
        self.assertEqual(panel.selected_base()['infobase'], rows[1]['infobase'])

    def test_search_is_visible_clickable_across_its_width_after_window_resize(self):
        panel = self.panel()
        panel.render_bases([dict(infobase=INFOBASE, name='Production'),
                            dict(infobase='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee', name='Payroll')])
        self.app.root.deiconify()
        self.app.root.lift()
        self.app.root.focus_force()
        entry = panel.base_search_entry
        with patch.object(panel, 'work') as work, patch('cluster_infobase_ui.ClusterClient') as client:
            for width, height in ((1600, 900), (1200, 740), (960, 540), (1600, 900)):
                self.app.root.geometry(f'{width}x{height}+10+10')
                self.app.root.update()
                self.assertTrue(entry.winfo_ismapped())
                self.assertGreater(entry.winfo_width(), 80)
                for fraction in (.05, .5, .95):
                    with self.subTest(size=(width, height), fraction=fraction):
                        x, y = round(entry.winfo_width() * fraction), entry.winfo_height() // 2
                        target = self.app.root.winfo_containing(entry.winfo_rootx() + x, entry.winfo_rooty() + y)
                        self.assertIs(target, entry, 'Поле поиска перекрыто другим виджетом')
                        entry.event_generate('<ButtonPress-1>', x=x, y=y)
                        entry.event_generate('<ButtonRelease-1>', x=x, y=y)
                        self.app.root.update()
                        self.assertIs(self.app.root.focus_get(), entry)
                        entry.delete(0, 'end')
                        for letter in 'prod':
                            # Use Tk's normal key-insertion handler: the active
                            # Windows layout may not have Latin keycodes.
                            entry.tk.call('tk::EntryInsert', entry, letter)
                        self.app.root.update()
                        self.assertEqual(entry.get(), 'prod')
                        self.assertEqual([panel.tree.set(row, 'name') for row in panel.tree.get_children()], ['Production'])
                for button in (self.app.hub.refresh_buttons[(panel, 'source')],
                               self.app.hub.select_buttons[(panel, 'source')],
                               self.app.hub.all_buttons[(panel, 'source')]):
                    x = button.winfo_rootx() + button.winfo_width() // 2
                    y = button.winfo_rooty() + button.winfo_height() // 2
                    self.assertIs(self.app.root.winfo_containing(x, y), button)
            client.assert_not_called()
            work.assert_not_called()

    def test_rollback_has_no_ai_page_and_keeps_the_scheduler(self):
        from bookmark_navigation import PAGES
        self.assertNotIn('ai_assistant', PAGES)
        self.assertNotIn('ai_assistant', self.app._pages)
        self.assertIn('scheduler', PAGES)
        self.assertIn('scheduler', self.app._pages)

    def test_confirmed_delete_keeps_other_cluster_cache_and_does_not_retry_failed_refresh(self):
        panel = self.select()
        base = dict(panel.selected_base())
        other_cluster = 'eeeeeeee-2222-4333-8444-555555555555'
        untouched = dict(server=existing.TARGET['server'], port='4540', cluster_id=other_cluster,
                         cluster='Other', base='Other', infobase_id=INFOBASE)
        self.app._all_bases.append(untouched)
        with (patch('cluster_infobase_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work,
              patch('cluster_infobase_ui.ClusterClient') as client):
            panel.delete_selected()
        operation, success = work.call_args.args
        client.return_value.bases.side_effect = RuntimeError('Refresh failed')
        result = operation(Mock())
        client.return_value.drop.assert_called_once_with(CLUSTER, base['infobase'], 'Existing')
        success(result)
        self.assertEqual(panel.bases, [])
        self.assertIn(untouched, self.app._all_bases)
        self.assertEqual(len(self.app._all_bases), 1)
        self.assertIn('Refresh failed', panel.output.get('1.0', 'end-1c'))
        self.assertFalse(panel.running)

    def test_create_verification_error_keeps_registered_base_visible_and_shows_error(self):
        panel = self.panel()
        result = dict(name='New', infobase_id=INFOBASE, scheduled_jobs_deny=None,
                      settings_error='ИБ добавлена, но блокировка не подтверждена',
                      bases=[dict(infobase=INFOBASE, name='New', descr='')])
        panel.start()
        panel.created(existing.TARGET, panel.selected_cluster(), result)
        self.assertEqual(panel.selected_base()['name'], 'New')
        self.assertEqual(panel.tree.set(panel.tree.selection()[0], 'jobs'), 'Не проверена')
        self.assertEqual(panel.state_label.cget('text'), 'ИБ создана · блокировка не проверена')
        self.assertIn('Регистрация ИБ сохранена', panel.output.get('1.0', 'end-1c'))

    def test_apply_existing_jobs_verifies_then_updates_table_without_creation(self):
        panel = self.select()
        base = dict(panel.selected_base())
        panel.deny_jobs.set(True)
        panel.ib_user.set('IBAdmin')
        panel.ib_password.set('ib-secret')
        with (patch('cluster_infobase_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work') as work,
              patch('cluster_infobase_ui.ClusterClient') as client):
            panel.apply_jobs()
        operation, success = work.call_args.args
        self.assertEqual(client.call_args.kwargs['ib_user'], 'IBAdmin')
        self.assertEqual(client.call_args.kwargs['ib_password'], 'ib-secret')
        client.return_value.apply_scheduled_jobs.return_value = True
        success(operation(Mock()))
        client.return_value.apply_scheduled_jobs.assert_called_once_with(CLUSTER, base['infobase'], 'Existing', True)
        client.return_value.create.assert_not_called()
        self.assertEqual(panel.tree.set(panel.tree.selection()[0], 'jobs'), 'Включена')
        self.assertNotIn('ib-secret', panel.output.get('1.0', 'end-1c'))
        self.assertNotIn('ib-secret', str(self.app._settings))

    def test_switch_cluster_loads_only_that_cluster_and_keeps_creation_fields(self):
        panel = self.panel()
        other = 'eeeeeeee-2222-4333-8444-555555555555'
        panel.render_clusters([dict(cluster=CLUSTER, name='Main'), dict(cluster=other, name='Other')], CLUSTER)
        panel.cluster_combo.current(1)
        with patch.object(panel, 'work') as work, patch('cluster_infobase_ui.ClusterClient') as client:
            panel.cluster_changed()
        operation, success = work.call_args.args
        client.return_value.bases.return_value = [dict(infobase=INFOBASE, name='OtherBase')]
        success(operation(Mock()))
        client.return_value.bases.assert_called_once_with(other)
        self.assertEqual(panel.tree.set('0', 'name'), 'OtherBase')
        self.assertEqual(panel.name.get(), PARAMS['name'])

    def test_empty_cluster_list_does_not_leave_previous_bases_or_mutation_buttons(self):
        panel = self.select()
        panel.start()
        panel.loaded(dict(clusters=[], cluster='', bases=[]), CLUSTER)
        self.assertIsNone(panel.selected_cluster())
        self.assertEqual(panel.tree.get_children(), ())
        self.assertEqual(str(panel.delete_button.cget('state')), 'disabled')
        self.assertEqual(str(panel.jobs_button.cget('state')), 'disabled')

    def test_base_headers_sort_without_mixing_uuid_selection_and_mutations(self):
        panel = self.panel()
        panel.render_bases([dict(infobase=INFOBASE, name='Яна', descr='Z'),
                           dict(infobase='eeeeeeee-bbbb-4ccc-8ddd-eeeeeeeeeeee', name='Анна', descr='A')], INFOBASE)
        panel.tree.tk.call(panel.tree.heading('name', 'command'))
        self.assertEqual([panel.tree.set(row, 'name') for row in panel.tree.get_children()], ['Анна', 'Яна'])
        self.assertEqual(panel.selected_base()['infobase'], INFOBASE)
        panel.tree.tk.call(panel.tree.heading('name', 'command'))
        self.assertEqual([panel.tree.set(row, 'name') for row in panel.tree.get_children()], ['Яна', 'Анна'])

    def test_small_window_keeps_toolbar_and_primary_actions_inside_window(self):
        panel = self.select()
        self.app.root.deiconify()
        self.app.root.geometry('960x540+20+20')
        self.app.root.update()
        right = self.app.root.winfo_rootx() + self.app.root.winfo_width()
        bottom = self.app.root.winfo_rooty() + self.app.root.winfo_height()
        for widget in (panel.delete_button, panel.jobs_button, panel.refresh_button, panel.execute_button):
            self.assertTrue(widget.winfo_viewable())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom)

    def test_failed_jobs_change_clears_stale_previously_verified_status(self):
        panel = self.select()
        panel.selected_base()['scheduled_jobs_deny'] = True
        panel.deny_jobs.set(True)
        with (patch('cluster_infobase_ui.messagebox.askyesno', return_value=True), patch.object(panel, 'work'),
              patch('cluster_infobase_ui.ClusterClient')):
            panel.apply_jobs()
        panel.finish(error='Блокировка не подтверждена')
        self.assertIsNone(panel.selected_base()['scheduled_jobs_deny'])
        self.assertEqual(panel.tree.set(panel.tree.selection()[0], 'jobs'), 'Не проверена')


if __name__ == '__main__':
    unittest.main()
