import unittest
from unittest.mock import Mock, patch

from test_activity import mock_dpapi
import test_service_ui


class UpdateUiTests(unittest.TestCase):
    setUp = test_service_ui.ServiceUiTests.setUp

    def panel(self):
        self.app._switch_mode('platform_install')
        return self.app.platform_install

    def test_update_requires_its_credentials_and_never_starts_on_navigation(self):
        with patch('platform_install_ui.update_tools') as update:
            panel = self.panel()
            with patch.object(panel, 'work') as work:
                panel.ras_update_button.invoke()
            work.assert_not_called()
            update.assert_not_called()
            self.assertFalse(panel.running)
            self.assertIn('логин и пароль ИТС', self.app.form_notice.message.cget('text'))

    def test_update_is_background_and_success_keeps_installer_settings_and_encrypts_credentials(self):
        mock_dpapi(self)
        panel = self.panel()
        panel.username.set('fixture-user')
        panel.password.set('fixture-secret')
        panel.service_port.set('4540')
        with (patch('platform_install_ui.update_tools', return_value=dict(version='8.5.2.1234', updated=True)) as update,
              patch('platform_install_ui.Portal') as portal, patch.object(panel, 'work') as work,
              patch.object(self.app, '_save_settings') as save):
            panel.ras_update_button.invoke()
            self.assertTrue(panel.running)
            update.assert_not_called()
            operation, success = work.call_args.args
            result = operation(Mock())
            portal.return_value.login.assert_called_once_with('fixture-user', 'fixture-secret')
            update.assert_called_once()
            success(result)
        self.assertFalse(panel.running)
        self.assertEqual(panel.service_port.get(), '4540')
        self.assertIn('8.5.2.1234', panel.output.get('1.0', 'end-1c'))
        self.assertNotIn('fixture-secret', str(self.app._settings))
        save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
