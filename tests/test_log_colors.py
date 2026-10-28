import tkinter as tk
import unittest

from log_colors import COLORS, configure_log, insert_log, severity
import test_service_ui


class SeverityTests(unittest.TestCase):
    def test_russian_english_sql_and_service_messages(self):
        for line in ('ОШИБКА: недостаточно прав', 'pg_restore: error: archive is invalid',
                     'psql:dump.sql:56: ERROR: permission denied', 'FATAL: database unavailable',
                     '[SC] OpenService FAILED 1060:', 'Сообщение 3013, уровень 16, состояние 1'):
            self.assertEqual(severity(line), 'error', line)
        for line in ('pg_dump: предупреждение: cyclic foreign keys', 'WARNING: old version',
                     'Внимание! Значение NULL исключено'):
            self.assertEqual(severity(line), 'warning', line)
        for line in ('pg_dump: чтение индексов public._document35980_2', '100 проц. обработано.',
                     'psql -v ON_ERROR_STOP=1', 'Подключено: SQL01. Доступных БД: 202.'):
            self.assertEqual(severity(line), 'info', line)

    def test_coloring_preserves_text_and_inserts_runs_without_rescanning(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        widget = tk.Text(root)
        configure_log(widget)
        text = 'Normal\nОШИБКА: failure\nWARNING: caution\nSuccess\n'
        insert_log(widget, text)
        self.assertEqual(widget.get('1.0', 'end-1c'), text)
        for index, expected in (('1.0', 'info'), ('2.0', 'error'), ('3.0', 'warning'), ('4.0', 'info')):
            self.assertIn(expected, widget.tag_names(index))
            self.assertEqual(widget.tag_cget(expected, 'foreground'), COLORS[expected])
        widget.delete('1.0', 'end')
        self.assertFalse(widget.tag_ranges('error'))


class JournalTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def test_explicit_ras_warning_overrides_error_words_for_all_its_lines(self):
        panel=self.app.cluster_infobase
        panel.events([dict(kind='warning',text='RAS недоступен: Ошибка установки соединения\nРазличаются версии клиента и сервера'),
                      dict(kind='message',text='Выбран локальный RAS'),dict(kind='error',text='Не удалось создать ИБ')])
        for index in ('1.0','2.0'):self.assertIn('warning',panel.output.tag_names(index))
        self.assertIn('info',panel.output.tag_names('3.0'))
        self.assertIn('error',panel.output.tag_names('4.0'))

    def test_all_operation_panels_color_new_lines_and_retain_colors_after_mode_switch(self):
        for panel in (self.app.sql, self.app.postgres, self.app.sql_copy,
                      self.app.postgres_copy, self.app.cluster_infobase):
            panel.append('Normal\npg_dump: WARNING: caution\nОШИБКА: failure')
            self.assertTrue(panel.output.tag_ranges('error'))
            self.assertTrue(panel.output.tag_ranges('warning'))
            self.assertEqual(panel.output.cget('state'), 'disabled')
        panel = self.app.postgres
        panel.show_mode('pg_restore')
        panel.show_mode('pg_dump')
        self.assertTrue(panel.output.tag_ranges('error'))
        self.assertTrue(panel.output.tag_ranges('warning'))

    def test_service_failures_are_red_even_when_windows_returns_only_numeric_status(self):
        panel = self.app.services
        panel._results['service_create'] = ('Native Windows failure\nStatus 1060', 'Ошибка', 'danger')
        panel.show_mode('service_create')
        self.assertIn('error', panel.output.tag_names('1.0'))
