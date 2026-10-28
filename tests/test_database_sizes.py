"""Fast catalogs, bounded background reads and stale-response rejection."""
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from postgres_runner import Runner, OperationCancelled
import test_postgres
import test_service_ui


ROWS = [dict(name=name, owner='postgres', database_oid=index, size_bytes=None,
             sizes_pending=True, server_version=180003)
        for index, name in enumerate(('Original', 'Target'), 1)]


class SizeQueryTests(unittest.TestCase):
    def test_fast_catalog_never_computes_sizes_and_retains_server_metadata(self):
        runner = Runner(test_postgres.CONNECTION, Mock())
        with patch.object(runner, 'sql', return_value=json.dumps(ROWS)) as query:
            self.assertEqual(runner.databases(fast=True), ROWS)
        sql = query.call_args.args[0]
        self.assertNotIn('pg_database_size', sql)
        self.assertIn('server_version_num', sql)
        self.assertIn('database_oid', sql)
        self.assertIn('has_database_privilege', sql)

    def test_background_sizes_are_batched_and_have_session_local_timeouts(self):
        events = []
        runner = Runner(test_postgres.CONNECTION, events.append)
        with patch.object(runner, 'sql', return_value='[]') as query:
            result = runner.database_sizes(["O'Brien", *['DB' + str(i) for i in range(9)]])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result['unavailable'], 0)
        self.assertIn("O''Brien", query.call_args_list[0].args[0])
        self.assertIn('statement_timeout=5000', runner.env['PGOPTIONS'])
        self.assertEqual([event['kind'] for event in events], ['database_sizes', 'database_sizes'])

    def test_size_timeout_does_not_invalidate_the_names_or_retry_forever(self):
        runner = Runner(test_postgres.CONNECTION, Mock())
        with patch.object(runner, 'sql', side_effect=RuntimeError('Timed out')) as query:
            result = runner.database_sizes(['One', 'Two'])
        self.assertEqual(result, dict(unavailable=2))
        self.assertEqual(query.call_count, 1)
        runner.cancel_event.set()
        with patch.object(runner, 'sql') as query, self.assertRaises(OperationCancelled):
            runner.database_sizes(['One'])
        query.assert_not_called()


class BackgroundSizesTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_slow_size_query_does_not_block_names_selection_or_starting_a_backup(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        entered, release = threading.Event(), threading.Event()
        callbacks = []
        def slow(payload, event):
            callbacks.append(event)
            entered.set()
            release.wait(3)
            return {}
        try:
            with patch('database_sizes.run_job', side_effect=slow):
                panel.loaded(panel.connection_key(), [dict(row) for row in ROWS])
                self.assertTrue(entered.wait(1))
                self.assertFalse(panel.running)
                self.assertEqual(len(panel.tree.get_children()), 2)
                panel.tree.selection_set(panel.tree.get_children()[0])
                self.assertEqual(panel.selected(), ['Original'])
                self.assertTrue(panel.start())
                self.assertTrue(callbacks[0].cancel_event.is_set())
                panel.finish('Backup completed independently')
        finally:
            release.set()

    def test_size_updates_preserve_shared_source_target_selection_and_ignore_old_oids(self):
        self.app._switch_mode('pg_copy')
        panel = self.app.postgres_copy
        panel.same_server.set(True)
        with patch.object(self.app.database_sizes, 'refresh'):
            panel.loaded(panel.connection_key(), [dict(row) for row in ROWS])
        panel.tree.selection_set(panel.tree.get_children()[0])
        panel.target_tree.selection_set(panel.target_tree.get_children()[1])
        source_rows, target_rows = panel.tree.get_children(), panel.target_tree.get_children()
        job = dict(cancel=threading.Event(), context=panel.connection_key())
        key = panel, 'source'
        self.app.database_sizes.jobs[key] = job
        self.app.database_sizes.apply(key, job, [dict(name='Original', database_oid=1, size_bytes=12345),
                                                dict(name='Target', database_oid=999, size_bytes=67890)])
        self.assertEqual(panel.databases[0]['size_bytes'], 12345)
        self.assertEqual(panel.target_databases[0]['size_bytes'], 12345)
        self.assertIsNone(panel.databases[1]['size_bytes'])
        self.assertEqual(panel.selected(), ['Original'])
        self.assertEqual(panel.target_tree.set(panel.target_tree.selection()[0], 'name'), 'Target')
        self.assertEqual(panel.tree.get_children(), source_rows)
        self.assertEqual(panel.target_tree.get_children(), target_rows)
        self.assertEqual(panel.tree.set(source_rows[0], 'size'), '12,06 КБ')

    def test_changed_endpoint_and_cancelled_jobs_cannot_update_a_new_list(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        with patch.object(self.app.database_sizes, 'refresh'):
            panel.loaded(panel.connection_key(), [dict(row) for row in ROWS])
        key = panel, 'source'
        job = dict(cancel=threading.Event(), context=panel.connection_key())
        self.app.database_sizes.jobs[key] = job
        panel.host.set('OtherServer')
        with patch.object(self.app.database_sizes, 'refresh'):
            panel.loaded(panel.connection_key(), [dict(row) for row in ROWS])
        self.app.database_sizes.apply(key, job, [dict(name='Original', database_oid=1, size_bytes=999)])
        self.assertTrue(job['cancel'].is_set())
        self.assertIsNone(panel.databases[0]['size_bytes'])

    def test_background_failure_keeps_the_list_connected_and_does_not_finish_another_job(self):
        self.app._switch_mode('pg_dump')
        panel = self.app.postgres
        with patch.object(self.app.database_sizes, 'refresh'):
            panel.loaded(panel.connection_key(), [dict(row) for row in ROWS])
        key = panel, 'source'
        job = dict(cancel=threading.Event(), context=panel.connection_key())
        self.app.database_sizes.jobs[key] = job
        with patch.object(panel, 'finish') as finish:
            self.app.database_sizes.finished(key, job, {}, 'Timeout')
        finish.assert_not_called()
        self.assertEqual(panel.connected, panel.connection_key())
        self.assertEqual(len(panel.tree.get_children()), 2)
        self.assertIn('ПРЕДУПРЕЖДЕНИЕ', panel.output.get('1.0', 'end-1c'))
