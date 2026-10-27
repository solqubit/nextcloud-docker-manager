"""Real 11.4 -> 11.8 migration using an explicitly SIMULATED future Nextcloud requirement.

The real supported Nextcloud releases currently do not require this LTS change.
The test first proves that the normal policy blocks it, then substitutes only the
future requirements matrix to exercise a real SQL migration and interrupted import.
"""
import sys
from unittest.mock import patch

sys.path.insert(0, '/workspace/tests')
from integration import Suite
from ndm import archive
from ndm.core import Error, State
from ndm.docker import Engine
from ndm.operations import Manager


def run(root):
    state = State(root / 'lts').open()
    try:
        manager = Manager(state)
        manager.install('33.0.10', storage='named', http_port=38880, https_port=38443, supplied='INSTALL')
        suite = Suite(state)
        suite.prepare()
        original = state.load()
        try:
            manager.migrate_database('11.8', for_nextcloud='34.0.5', supplied='MIGRATE-DB-11.8')
            raise AssertionError('Unnecessary LTS change allowed')
        except Error:
            assert state.pending is None and state.load()['images'] == original['images']
        requirements = manager.catalog.requirements
        def future_requirement(major):
            values = dict(requirements(major))
            if major == 34:
                values['mariadb'] = ['11.8']
                values['source'] = 'TEST FIXTURE: simulated future MariaDB requirement, not the real NC34 policy'
            return values
        importer = archive.import_sql
        def fail_main_import(engine, path):
            if engine.cfg['id'] == original['id']:
                raise Error('Injected SQL import interruption')
            return importer(engine, path)
        with patch.object(manager.catalog, 'requirements', future_requirement):
            try:
                with patch('ndm.archive.import_sql', side_effect=fail_main_import):
                    manager.migrate_database('11.8', for_nextcloud='34.0.5', supplied='MIGRATE-DB-11.8')
                raise AssertionError('Interrupted import ignored')
            except Error:
                assert state.pending['phase'] == 'db-importing'
                assert state.load()['volumes']['db'] != original['volumes']['db']
                assert state.load()['maintenance_gate'] is True
            try:
                manager.recover('resume', supplied='RESUME')
                raise AssertionError('Partial import resumed as a complete database')
            except Error:
                assert state.pending['phase'] == 'db-importing'
            manager.recover('restore', supplied='RESTORE')
            assert state.load()['volumes']['db'] == original['volumes']['db']
            assert state.load()['images']['db'] == original['images']['db']
            suite.engine = Engine(state)
            suite.check_file()
            print('[PASS] Simulated required-LTS scenario: interrupted real import stays gated; original SQL volume/engine rollback retains files and shares')
            manager.migrate_database('11.8', for_nextcloud='34.0.5', supplied='MIGRATE-DB-11.8')
        current = state.load()
        assert current['images']['db'].startswith('mariadb:11.8.')
        assert current['images']['app'] == original['images']['app']
        assert current['volumes']['db'] != original['volumes']['db']
        assert current['volumes']['data'] == original['volumes']['data']
        assert Engine(state).inspect('volume', original['volumes']['db'])
        suite.engine = Engine(state)
        suite.check_file()
        print('[PASS] Real MariaDB 11.4 -> 11.8 with actual-SQL rehearsal, new SQL volume, original SQL retained and unchanged user files; trigger requirement was explicitly simulated')
    finally:
        state.lock_file.close()
