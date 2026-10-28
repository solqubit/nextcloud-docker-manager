"""Setup fixes remain scoped to fresh, small installations and public TLS."""
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ndm.config import caddyfile, compose, managed_php, validate
from ndm.core import Error, State
from ndm.operations import Manager
from test_foundation import configuration


class SetupHealthTests(unittest.TestCase):
    def test_public_hsts_does_not_force_local_http_or_local_certificates(self):
        cfg = configuration()
        self.assertNotIn('Strict-Transport-Security', caddyfile(cfg))
        cfg['domains'].append(dict(host='nube.internal', kind='local', mode='local', tls='internal', email=''))
        self.assertNotIn('Strict-Transport-Security', caddyfile(cfg))
        cfg['domains'].append(dict(host='nube.example.com', kind='public', mode='tunnel', tls='acme', email=''))
        text = caddyfile(cfg)
        self.assertEqual(text.count('header Strict-Transport-Security "max-age=15552000"'), 2)
        local_route = text.split('@entry0 host 127.0.0.1')[1].split('@entry1')[0]
        self.assertNotIn('Strict-Transport-Security', local_route)
        local_tls = text.split('\nhttps://nube.internal {')[1].split('\nhttps://nube.example.com {')[0]
        self.assertNotIn('Strict-Transport-Security', local_tls)
        cfg['domains'][-1]['mode'] = 'direct'
        self.assertEqual(caddyfile(cfg).count('header Strict-Transport-Security'), 1)

    def test_new_redo_log_tuning_preserves_legacy_database_configuration(self):
        cfg = configuration()
        self.assertIn('--innodb-log-file-size=268435456', compose(cfg)['services']['db']['command'])
        legacy = copy.deepcopy(cfg)
        legacy.pop('db_redo_log_size')
        legacy.pop('initial_maintenance_pending')
        validate(legacy)
        self.assertFalse(any('innodb-log-file-size' in arg for arg in compose(legacy)['services']['db']['command']))

    def test_logging_uses_data_directory_without_disabling_integrity(self):
        php = managed_php(configuration())
        payload = php.split("base64_decode('")[1].split("'")[0]
        values = json.loads(base64.b64decode(payload))
        self.assertEqual(values['logfile'], '/var/www/html/data/nextcloud.log')
        self.assertNotIn('integrity.check.disabled', values)

    def setup_fixture(self, *, legacy=False, ready=False, size=8 * 1024 ** 2, moved=0):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        state = State(Path(work.name) / 'state').open()
        self.addCleanup(state.lock_file.close)
        cfg = configuration()
        if legacy:
            cfg.pop('db_redo_log_size')
            cfg.pop('initial_maintenance_pending')
        if ready:
            cfg['status'] = 'ready'
        state.activate(state.revision(cfg))
        engine = Mock(cfg=cfg)
        engine.status.return_value = {'installed': True}
        engine.verify.side_effect = lambda **kwargs: {'installed': True}
        engine.db_size.return_value = size
        engine.put_managed.return_value = moved
        engine.occ.return_value = SimpleNamespace(returncode=0)
        return state, engine

    def test_initial_repairs_run_once_only_on_a_new_small_database(self):
        state, engine = self.setup_fixture()
        with patch('ndm.operations.Engine', return_value=engine):
            result = Manager(state).setup_check()
        self.assertTrue(result['core_integrity_ok'])
        self.assertFalse(state.load()['initial_maintenance_pending'])
        self.assertIn(('db:add-missing-indices',), [call.args for call in engine.occ.call_args_list])
        self.assertIn(('maintenance:repair', '--include-expensive'), [call.args for call in engine.occ.call_args_list])
        engine.reset_mock()
        with patch('ndm.operations.Engine', return_value=engine):
            Manager(state).setup_check()
        engine.occ.assert_not_called()

    def test_large_database_defers_repairs_and_keeps_installation_usable(self):
        state, engine = self.setup_fixture(size=64 * 1024 ** 2 + 1)
        with patch('ndm.operations.Engine', return_value=engine):
            result = Manager(state).setup_check()
        self.assertTrue(result['initial_maintenance_deferred'])
        self.assertEqual(state.load()['status'], 'ready')
        self.assertTrue(state.load()['initial_maintenance_pending'])
        self.assertNotIn(('maintenance:repair', '--include-expensive'), [call.args for call in engine.occ.call_args_list])

    def test_failed_initial_repair_is_not_reported_as_complete(self):
        state, engine = self.setup_fixture()
        engine.occ.side_effect = Error('Repair failed')
        with patch('ndm.operations.Engine', return_value=engine), self.assertRaises(Error):
            Manager(state).setup_check()
        self.assertEqual(state.load()['status'], 'awaiting-web')
        self.assertTrue(state.load()['initial_maintenance_pending'])

    def test_legacy_installation_only_relocates_logs_and_rechecks_integrity(self):
        state, engine = self.setup_fixture(legacy=True, moved=1)
        with patch('ndm.operations.Engine', return_value=engine):
            result = Manager(state).setup_check()
        engine.db_size.assert_not_called()
        engine.occ.assert_called_once_with('integrity:check-core', check=False)
        self.assertTrue(result['core_integrity_ok'])

    def test_other_integrity_errors_are_reported_without_breaking_the_installation(self):
        state, engine = self.setup_fixture(legacy=True, moved=1)
        engine.occ.return_value.returncode = 1
        with patch('ndm.operations.Engine', return_value=engine):
            result = Manager(state).setup_check()
        self.assertFalse(result['core_integrity_ok'])
        self.assertEqual(state.load()['status'], 'ready')

    def backup_fixture(self, **kwargs):
        state, engine = self.setup_fixture(**kwargs)
        manager = Manager(state)
        manager.quiesce = Mock(return_value=engine)
        manager.finish = Mock()
        manager.setup_check = Mock(wraps=manager.setup_check)
        factory = self.enterContext(patch('ndm.operations.Engine', return_value=engine))
        preflight = self.enterContext(patch('ndm.operations.archive.preflight'))
        create = self.enterContext(patch('ndm.operations.archive.create',
                                         return_value=state.path / 'backups' / 'fixture.ndm.tar'))
        return state, engine, manager, factory, preflight, create

    def test_backup_completes_initial_checks_before_archiving(self):
        state, engine, manager, _, preflight, create = self.backup_fixture()
        def ready_before_backup(live):
            self.assertEqual(state.load()['status'], 'ready')
            self.assertFalse(state.load()['initial_maintenance_pending'])
            self.assertIsNone(state.pending)
        preflight.side_effect = ready_before_backup
        path = manager.backup(supplied='BACKUP')
        manager.setup_check.assert_called_once_with()
        preflight.assert_called_once_with(engine)
        create.assert_called_once_with(engine)
        self.assertEqual(path, create.return_value)
        self.assertIsNone(state.pending)

    def test_backup_does_not_repeat_completed_initial_checks(self):
        state, engine, manager, _, _, create = self.backup_fixture(ready=True)
        manager.backup(supplied='BACKUP')
        manager.setup_check.assert_not_called()
        engine.occ.assert_not_called()
        create.assert_called_once()

    def test_backup_on_large_database_keeps_expensive_repairs_deferred(self):
        state, engine, manager, _, _, create = self.backup_fixture(size=64 * 1024 ** 2 + 1)
        manager.backup(supplied='BACKUP')
        self.assertEqual(state.load()['status'], 'ready')
        self.assertTrue(state.load()['initial_maintenance_pending'])
        self.assertNotIn(('maintenance:repair', '--include-expensive'),
                         [call.args for call in engine.occ.call_args_list])
        create.assert_called_once()

    def test_failed_automatic_setup_does_not_start_backup_or_mark_ready(self):
        state, engine, manager, _, preflight, create = self.backup_fixture()
        engine.occ.side_effect = Error('Repair failed')
        with self.assertRaisesRegex(Error, 'Repair failed'):
            manager.backup(supplied='BACKUP')
        self.assertEqual(state.load()['status'], 'awaiting-web')
        self.assertIsNone(state.pending)
        preflight.assert_not_called()
        create.assert_not_called()
        manager.quiesce.assert_not_called()

    def test_backup_without_web_administrator_explains_where_to_complete_setup(self):
        state, engine, manager, _, _, create = self.backup_fixture()
        engine.status.return_value = {'installed': False}
        with self.assertRaisesRegex(Error, 'instalación web.*http://127.0.0.1'):
            manager.backup(supplied='BACKUP')
        manager.setup_check.assert_not_called()
        create.assert_not_called()
        self.assertIsNone(state.pending)

    def test_cancelled_backup_does_not_run_initial_repairs(self):
        state, engine, manager, factory, _, create = self.backup_fixture()
        with self.assertRaises(Error):
            manager.backup(supplied='CANCEL')
        factory.assert_not_called()
        engine.occ.assert_not_called()
        manager.setup_check.assert_not_called()
        create.assert_not_called()
        self.assertEqual(state.load()['status'], 'awaiting-web')

    def test_automatic_setup_keeps_maintenance_upgrade_and_pending_guards(self):
        for condition in ('maintenance', 'needsDbUpgrade', 'pending'):
            with self.subTest(condition=condition):
                state, engine, manager, _, _, create = self.backup_fixture()
                if condition == 'pending':
                    state.begin('backup')
                else:
                    engine.status.return_value = {'installed': True, condition: True}
                with self.assertRaises(Error):
                    manager.backup(supplied='BACKUP')
                manager.setup_check.assert_not_called()
                create.assert_not_called()

    def test_automatic_setup_must_persist_ready_before_backup_can_proceed(self):
        state, _, manager, _, _, create = self.backup_fixture()
        manager.setup_check = Mock(return_value={'installed': True})
        with self.assertRaises(Error):
            manager.backup(supplied='BACKUP')
        manager.setup_check.assert_called_once_with()
        create.assert_not_called()
        self.assertIsNone(state.pending)

    def test_shared_readiness_completes_pending_verification_by_default(self):
        state, engine, manager, _, _, _ = self.backup_fixture()
        self.assertIs(manager.ready(), engine)
        manager.setup_check.assert_called_once_with()
        self.assertEqual(state.load()['status'], 'ready')

    def test_every_component_update_completes_setup_before_resolving_images(self):
        for component in ('redis', 'db', 'proxy'):
            with self.subTest(component=component):
                state, _, manager, _, _, create = self.backup_fixture()
                manager.catalog.latest = Mock(side_effect=Error('Fixture registry unavailable'))
                with self.assertRaisesRegex(Error, 'Fixture registry unavailable'):
                    manager.update_component(component, supplied='UPDATE-' + component.upper())
                manager.setup_check.assert_called_once_with()
                self.assertEqual(state.load()['status'], 'ready')
                self.assertIsNone(state.pending)
                create.assert_not_called()

    def test_settings_keep_verified_state_when_form_predates_automatic_setup(self):
        state, _, manager, _, _, _ = self.backup_fixture()
        draft = state.load()
        draft['domains'].append(dict(host='another.local', kind='local', mode='local', tls='off', email=''))
        manager.settings(draft, supplied='APPLY')
        manager.setup_check.assert_called_once_with()
        self.assertEqual(state.load()['status'], 'ready')
        self.assertFalse(state.load()['initial_maintenance_pending'])
        self.assertEqual(state.load()['domains'][-1]['host'], 'another.local')
