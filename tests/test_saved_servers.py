"""Successful endpoint history, encrypted passwords and editable suggestions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from activity_store import ActivityStore
import test_activity
import test_service_ui


SQL = dict(kind='sql', page='db_backup', values=dict(server='SQL01', username='admin', password='sql-test-secret',
           windows_auth=False, encrypt=True, trust_certificate=True))
PG = dict(kind='postgres', page='pg_dump', values=dict(host='PG01', port='5433', username='backup', password='pg-test-secret',
          sslmode='require', execution='SSH · Linux', ssh_host='VM01', ssh_port='2222', ssh_user='operator', ssh_key=''))


class StoredServersTests(unittest.TestCase):
    def setUp(self):
        test_activity.mock_dpapi(self)

    def test_remove_connection_persists_without_removing_other_servers_or_favorites(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'activity.json'
            store = ActivityStore(path)
            sql_id = store.save_connection(SQL)
            pg_id = store.save_connection(PG)
            store.save_favorite('Keep PG', PG)
            self.assertTrue(store.remove_connection(pg_id))
            self.assertFalse(store.remove_connection('missing'))
            store.close(wait=True)
            loaded = ActivityStore(path)
            self.assertEqual([row['id'] for row in loaded.snapshot('connections')], [sql_id])
            self.assertEqual(len(loaded.snapshot('favorites')), 1)
            self.assertEqual(loaded.credentials(loaded.snapshot('connections')[0])['password'], 'sql-test-secret')

    def test_history_survives_restart_without_plaintext_passwords_and_updates_same_login(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'activity.json'
            store = ActivityStore(path)
            store.save_connection(PG)
            store.save_connection(dict(PG, values=dict(PG['values'], password='updated-test-secret')))
            store.close(wait=True)
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('pg-test-secret', content)
            self.assertNotIn('updated-test-secret', content)
            loaded = ActivityStore(path)
            rows = loaded.snapshot('connections')
            self.assertEqual(len(rows), 1)
            self.assertEqual(loaded.credentials(rows[0])['password'], 'updated-test-secret')
            self.assertEqual(rows[0]['values']['ssh_port'], '2222')
            loaded.close(wait=True)

    def test_failed_encryption_does_not_change_a_saved_connection(self):
        store = ActivityStore()
        store.save_connection(SQL)
        original = store.snapshot('connections')
        with patch('activity_store.protect', side_effect=ValueError('Encryption failed')):
            with self.assertRaises(ValueError):
                store.save_connection(dict(SQL, values=dict(SQL['values'], password='new')))
        self.assertEqual(store.snapshot('connections'), original)


class ServerSuggestionsTests(unittest.TestCase):
    def setUp(self):
        test_service_ui.ServiceUiTests.setUp(self)
        test_activity.mock_dpapi(self)

    def choose(self, widget, profile):
        self.app.activity.save_connection(profile)
        self.app.saved_servers.refresh(widget)
        value = next(iter(widget._saved_choices))
        widget.set(value)
        widget.event_generate('<<ComboboxSelected>>')

    def test_sql_selection_restores_credentials_and_flags_without_running_a_request(self):
        self.app._switch_mode('db_copy')
        panel = self.app.sql_copy
        with patch.object(panel, 'work') as work:
            self.choose(panel.server_entry, SQL)
        work.assert_not_called()
        self.assertEqual(panel.connection(), SQL['values'])
        self.assertEqual(str(panel.server_entry.cget('state')), 'normal')
        self.assertEqual(self.app.mode, 'db_copy')

    def test_postgres_source_restores_executor_and_target_selection_preserves_source(self):
        self.app._switch_mode('pg_copy')
        panel = self.app.postgres_copy
        self.choose(panel.host_entry, PG)
        self.assertEqual(panel.host.get(), 'PG01')
        self.assertEqual(panel.password.get(), 'pg-test-secret')
        self.assertEqual((panel.execution.get(), panel.ssh_host.get(), panel.ssh_user.get()),
                         ('SSH · Linux', 'VM01', 'operator'))
        target = dict(PG, values=dict(PG['values'], host='PG02', username='target', password='target-test-secret'))
        self.app.activity.save_connection(target)
        panel.target_host_entry.set('PG02')
        self.app.saved_servers.refresh(panel.target_host_entry)
        panel.target_host_entry.set(next(label for label in panel.target_host_entry._saved_choices if label.startswith('PG02:')))
        panel.target_host_entry.event_generate('<<ComboboxSelected>>')
        self.assertEqual((panel.host.get(), panel.password.get()), ('PG01', 'pg-test-secret'))
        self.assertEqual((panel.target_host.get(), panel.target_user.get(), panel.target_password.get()),
                         ('PG02', 'target', 'target-test-secret'))
        self.assertEqual(panel.ssh_host.get(), 'VM01')

    def test_only_successful_connects_are_remembered_and_available_in_other_tabs(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        panel.host.set('PG01')
        panel.password.set('saved-test-secret')
        with patch.object(panel, 'work') as work:
            panel.connect()
        self.assertEqual(self.app.activity.snapshot('connections'), [])
        _, success = work.call_args.args
        success([dict(name='Main', owner='backup', size_bytes=100, server_version=180003)])
        rows = self.app.activity.snapshot('connections')
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.app.activity.credentials(rows[0])['password'], 'saved-test-secret')
        self.assertTrue(any(value.startswith('PG01:') for value in self.app.postgres_copy.target_host_entry['values']))
        self.assertNotIn('saved-test-secret', json.dumps(rows))
        self.assertEqual(str(panel.host_entry.cget('state')), 'normal')

    def test_prefix_filters_suggestions_and_password_is_never_in_the_labels(self):
        panel = self.app.postgres_copy
        self.app.activity.save_connection(PG)
        self.app.activity.save_connection(dict(PG, values=dict(PG['values'], host='OTHER')))
        panel.host.set('pg')
        self.app.saved_servers.refresh(panel.host_entry, filter_prefix=True)
        self.assertEqual(len(panel.host_entry['values']), 1)
        self.assertTrue(panel.host_entry['values'][0].startswith('PG01:'))
        self.assertNotIn('pg-test-secret', str(panel.host_entry['values']))

    def test_unreadable_credentials_restore_previous_host_and_show_error(self):
        panel = self.app.sql_copy
        panel.server.set('Previous')
        panel.password.set('previous-secret')
        self.app.activity.save_connection(SQL)
        panel.server_entry.set('')
        self.app.saved_servers.refresh(panel.server_entry)
        panel.server.set('Previous')
        panel.server_entry._before_choice = 'Previous'
        panel.server_entry.set(next(iter(panel.server_entry._saved_choices)))
        with patch.object(self.app.activity, 'credentials', side_effect=ValueError('Cannot decrypt')):
            self.app.saved_servers.choose(panel.server_entry)
        self.assertEqual(panel.server.get(), 'Previous')
        self.assertEqual(panel.password.get(), 'previous-secret')
