"""Password recovery identifies the account and keeps secrets out of process arguments and logs."""
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ndm.core import Cancelled, Error, Runner
from ndm.docker import Engine
from ndm.operations import Manager


class PasswordRecoveryTests(unittest.TestCase):
    def manager(self, encrypted=False):
        manager = Manager.__new__(Manager)
        engine = Mock()
        engine.occ.return_value.stdout = '{"enabled":true}' if encrypted else '{"enabled":false}'
        manager.ready = Mock(return_value=engine)
        return manager, engine

    def test_reset_uses_selected_account_and_requires_confirmation(self):
        manager, engine = self.manager()
        with patch('ndm.operations.confirm') as confirmation:
            self.assertEqual(manager.reset_password('chosen-admin', 'new-secret'), {'user': 'chosen-admin'})
        confirmation.assert_called_once_with('RESET-PASSWORD', None, 'chosen-admin')
        engine.reset_password.assert_called_once_with('chosen-admin', 'new-secret')

    def test_cancellation_does_not_change_password(self):
        manager, engine = self.manager()
        with patch('ndm.operations.confirm', side_effect=Cancelled('cancel')), self.assertRaises(Cancelled):
            manager.reset_password('chosen-admin', 'new-secret')
        engine.reset_password.assert_not_called()

    def test_encrypted_files_require_the_nextcloud_recovery_path(self):
        manager, engine = self.manager(encrypted=True)
        with self.assertRaisesRegex(Error, 'Cifrado'):
            manager.reset_password('chosen-admin', 'new-secret', 'RESET-PASSWORD')
        engine.reset_password.assert_not_called()

    def test_empty_password_and_control_characters_are_rejected(self):
        for user, password in (('', 'valid'), ('admin\n', 'valid'), ('admin', ''), ('admin', 'a\x00b')):
            manager, engine = self.manager()
            with self.subTest(user=user), self.assertRaises(Error):
                manager.reset_password(user, password, 'RESET-PASSWORD')
            engine.reset_password.assert_not_called()

    def test_password_is_stdin_only_and_command_output_is_private(self):
        engine = Engine.__new__(Engine)
        engine.dc = Mock(return_value=SimpleNamespace(returncode=0))
        secret = "Secret ' $Unicode-ñ-123!"
        engine.reset_password('chosen-admin', secret)
        args, kwargs = engine.dc.call_args
        self.assertNotIn(secret, str(args))
        self.assertNotIn('env', kwargs)
        self.assertEqual(kwargs['input'], secret)
        self.assertTrue(kwargs['quiet'])
        self.assertEqual(args[-2:], ('--', 'chosen-admin'))

    def test_reset_failure_does_not_disclose_password(self):
        engine = Engine.__new__(Engine)
        engine.dc = Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='Rejected secret-value'))
        with self.assertRaises(Error) as error:
            engine.reset_password('chosen-admin', 'secret-value')
        self.assertNotIn('secret-value', str(error.exception))

    def test_private_command_omits_both_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'private.log'
            runner = Runner(SimpleNamespace(log=log))
            result = subprocess.CompletedProcess([], 1, b'secret-output', b'secret-error')
            with patch('ndm.core.subprocess.run', return_value=result), self.assertRaises(Error) as error:
                runner.run(['fixture'], quiet=True)
            self.assertNotIn('secret', log.read_text())
            self.assertNotIn('secret', str(error.exception))
