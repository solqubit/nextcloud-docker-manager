import tempfile
import unittest
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ndm.catalog import Catalog
from ndm.core import Error, State
from ndm.docker import Engine
from ndm.operations import Manager
from test_foundation import configuration


class DatabasePolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(self.folder.name).open()
        self.state.activate(self.state.revision(configuration()))
        self.manager = Manager(self.state)
        self.manager.ready = lambda: Engine(self.state)
        self.manager.catalog.maintained_databases = lambda: {
            'source': 'fixture', 'branches': {'11.4': '2099-05-29', '11.8': '2099-06-04'}}
        self.manager.catalog.requirements = lambda major: {'mariadb': ['11.4', '11.8'], 'php': ['8.4'], 'source': 'fixture'}

    def tearDown(self):
        self.state.lock_file.close()
        self.folder.cleanup()

    def test_nextcloud_version_is_required(self):
        with self.assertRaises(Error):
            self.manager.database_choices()

    def test_compatible_current_lts_blocks_unnecessary_change(self):
        plan = self.manager.database_choices('34.0.5')
        self.assertFalse(plan['required'])
        self.assertEqual(plan['choices'], [])
        with self.assertRaises(Error):
            self.manager.migrate_database('11.8', for_nextcloud='34.0.5', supplied='MIGRATE-DB-11.8')
        self.assertIsNone(self.state.pending)
        self.assertIn('mariadb:11.4.', self.state.load()['images']['db'])

    def test_actual_requirement_selects_common_maintained_lts(self):
        self.manager.catalog.requirements = lambda major: {'mariadb': ['11.4', '11.8'] if major == 33 else ['11.8']}
        plan = self.manager.database_choices('34.0.5')
        self.assertTrue(plan['required'])
        self.assertEqual([row['series'] for row in plan['choices']], ['11.8'])

    def test_cannot_prepare_a_skipped_nextcloud_major(self):
        with self.assertRaises(Error):
            self.manager.database_choices('35.0.2')

    def test_unpublished_nextcloud_target_cannot_trigger_sql_migration(self):
        cfg = self.state.load()
        cfg['version'] = '33.0.10'
        cfg['images']['app'] = 'nextcloud:33.0.10-apache@sha256:' + 'a' * 64
        self.state.activate(self.state.revision(cfg))
        self.manager.catalog.requirements = lambda major: {'mariadb': ['11.4', '11.8'] if major == 33 else ['11.8']}
        self.manager.catalog.patches = lambda major: ['33.0.10']
        with patch.object(self.manager.catalog, 'resolve', side_effect=Error('Target image unavailable')) as resolve:
            with self.assertRaises(Error):
                self.manager.migrate_database('11.8', for_nextcloud='34.0.5', supplied='MIGRATE-DB-11.8')
        resolve.assert_called_once_with('nextcloud', '34.0.5-apache')
        self.assertIsNone(self.state.pending)
        self.assertEqual(self.state.load()['images'], cfg['images'])

    def test_community_dates_are_independent_of_paid_support(self):
        document = ('<table><tr><td>11.4</td><td>29 May 2024</td><td>29 May 2099</td><td>29 May 2100</td></tr>'
                    '<tr><td>10.6</td><td>6 Jul 1999</td><td>6 Jul 2001</td><td>6 Jul 2100</td></tr></table>')
        with patch('ndm.catalog.fetch', return_value=document.encode()):
            result = Catalog(self.state.path / 'other-cache').maintained_databases()
        self.assertEqual(result['branches'], {'11.4': '2099-05-29'})

    def test_rehearsal_failure_diagnostics_survive_clone_cleanup(self):
        cfg = self.state.load()
        self.state.begin('update')
        self.manager.new_subnet = lambda: '172.25.10.0/28'
        @contextlib.contextmanager
        def unpack(*args):
            yield self.state.path, {'configuration': cfg}
        class FailedClone:
            def __init__(self, state):
                self.state = state
            def pull(self):
                raise Error('Injected clone failure')
            def dc(self, *args, **kwargs):
                with self.state.log_path.open('a') as log:
                    log.write('diagnostic evidence\n')
                return SimpleNamespace(stdout='')
            def remove_owned(self, **kwargs):
                pass
        with patch('ndm.operations.archive.unpack', unpack), patch('ndm.operations.Engine', FailedClone):
            with self.assertRaises(Error):
                self.manager.rehearse('fixture.ndm.tar', cfg['version'], app_image=cfg['images']['app'])
        diagnostic = Path(self.state.pending['rehearsal_log'])
        self.assertIn('diagnostic evidence', diagnostic.read_text())
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list((self.state.path / 'cache').glob('rehearsal-*')))

    def test_rollback_after_possible_user_writes_requires_acknowledgement(self):
        self.state.begin('db-migrate', source=str((self.state.path / 'current').resolve()))
        self.state.phase('db-imported', traffic_opened=True)
        with self.assertRaises(Error):
            self.manager.rollback_database(supplied='RESTORE')
        self.assertEqual(self.state.pending['phase'], 'db-imported')

    def test_insufficient_sql_filesystem_space_blocks_import(self):
        engine = Engine(self.state)
        output = 'Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 100 99 1 99% /ndm-space\n'
        with patch.object(engine.runner, 'run', return_value=SimpleNamespace(stdout=output)):
            with self.assertRaises(Error):
                engine.check_space(2048)
