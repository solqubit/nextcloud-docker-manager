"""Full disposable acceptance run; mount the dedicated state and bind fixture first."""
import argparse
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, '/workspace/tests')
from integration import Suite
from ndm.config import validate
from ndm.core import Error, LABEL, Runner, State, atomic, read_json
from ndm.docker import Engine
from ndm.operations import Manager
from ndm.catalog import Catalog


def cleanup(root):
    # Every identity comes from revisions in this run's freshly created private state.
    identities = {}
    for path in Path(root).rglob('revisions/*/config.json'):
        cfg = read_json(path)
        validate(cfg)
        identities[cfg['id']] = (cfg, path.parent)
    for cfg, revision in identities.values():
        state = State(revision.parent.parent).open()
        try:
            engine = Engine(state, cfg, revision)
            engine.remove_owned(delete_volumes=True)
            for name in engine.runner.run(['docker', 'volume', 'ls', '--quiet', '--filter',
                        'label=' + LABEL + '.installation=' + cfg['id']]).stdout.split():
                engine.owned(engine.inspect('volume', name), 'volume')
                engine.runner.run(['docker', 'volume', 'rm', name])
        finally:
            state.lock_file.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--major', action='store_true')
    parser.add_argument('--bind', action='store_true')
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('--cleanup-only', action='store_true')
    parser.add_argument('--lts-only', action='store_true')
    parser.add_argument('--root', default='/ndm-state')
    args = parser.parse_args()
    if args.root != '/ndm-state':
        raise SystemExit('This runner only accepts the dedicated /ndm-state mount')
    root = Path(args.root)
    if args.cleanup_only:
        cleanup(root)
        return
    for name in ('main', 'bind'):
        if (root / name / 'current').exists():
            raise SystemExit('Test state must be new; refusing an existing installation')
    runner = Runner()
    unrelated = runner.run(['docker', 'ps', '--quiet']).stdout.split()
    success = False
    try:
        if args.lts_only:
            from lts_integration import run
            run(root)
            success = True
            return
        state = State(root / 'main').open()
        try:
            latest = Catalog.latest
            with patch.object(Catalog, 'latest', lambda catalog, repo, series: '11.4.12' if repo == 'mariadb' else latest(catalog, repo, series)):
                Manager(state).install('33.0.9', storage='named', http_port=18880, https_port=18443, supplied='INSTALL')
            suite = Suite(state)
            suite.prepare()
            suite.domains()
            suite.failures()
            suite.update()
            if args.major:
                suite.manager.update('34.0.5', major=True, supplied='MIGRATE-34.0.5')
                suite.engine = Engine(state)
                assert suite.engine.status()['versionstring'] == '34.0.5'
                suite.check_file()
                print('[PASS] Sequential major 33.0.10 -> 34.0.5, real SQL migration and files/shares retained')
            suite.restore()
            suite.auxiliary()
            app_image = state.load()['images']['app']
            suite.manager.update_component('db', supplied='UPDATE-DB')
            assert state.load()['images']['db'].startswith('mariadb:11.4.13@')
            assert state.load()['images']['app'] == app_image
            suite.engine = Engine(state)
            suite.check_file()
            print('[PASS] Independent MariaDB 11.4.12 -> 11.4.13, SQL rehearsal and unchanged Nextcloud image')
        finally:
            state.lock_file.close()
        if args.bind:
            fixture = Path(os.environ['NDM_BIND_FIXTURE'])
            if not str(fixture).startswith('/var/lib/docker/volumes/ndm-test-bind-'):
                raise Error('Bind fixture must be a dedicated Docker test volume mountpoint')
            state = State(root / 'bind').open()
            try:
                manager = Manager(state)
                manager.install('33.0.9', storage='directory', storage_root=str(fixture / 'persistent'),
                                http_port=28880, https_port=28443, supplied='INSTALL')
                suite = Suite(state)
                suite.prepare()
                backup = manager.backup(supplied='BACKUP')
                atomic(state.path / 'cache' / 'fixture-backup.json', {'path': str(backup)})
                original = suite.engine.volume_path('db')
                suite.restore()
                assert suite.engine.volume_path('db') != original and original.is_dir()
                engine = Engine(state)
                engine.stop('cron', 'app', 'proxy', 'redis', 'db')
                data = engine.volume_path('data')
                unavailable = data.with_name('data-missing-test')
                assert data.resolve().parent == (fixture / 'persistent' / engine.cfg['id']).resolve()
                data.rename(unavailable)
                try:
                    try:
                        engine.up('app')
                        raise AssertionError('Missing storage was silently recreated')
                    except Error:
                        assert not data.exists()
                finally:
                    unavailable.rename(data)
                engine.up('db', 'redis', 'app', 'proxy', 'cron')
                engine.verify()
                suite.check_file()
                print('[PASS] Actual directory-backed install, SQL/application restore into new directories, old generation retained, missing storage blocks startup')
            finally:
                state.lock_file.close()
        if args.latest:
            state = State(root / 'latest').open()
            try:
                manager = Manager(state)
                manager.install(storage='named', http_port=48880, https_port=48443, supplied='INSTALL')
                suite = Suite(state)
                suite.prepare()
                suite.domains()
                backup = manager.backup(supplied='BACKUP')
                cfg = state.load()
                manager.rehearse(backup, cfg['version'], app_image=cfg['images']['app'])
                suite.check_file()
                suite.auxiliary()
                print('[PASS] Latest official Nextcloud ' + cfg['version'] + ': web install, clients HTTP/HTTPS, backup, actual SQL restore rehearsal and installed manager')
            finally:
                state.lock_file.close()
        success = True
    finally:
        if os.environ.get('KEEP_NDM_TESTS') != '1':
            cleanup(root)
        for cid in unrelated:
            info = json.loads(runner.run(['docker', 'container', 'inspect', cid]).stdout)[0]
            assert info['State']['Running'], 'An unrelated container is no longer running'
        print('[PASS] Pre-existing running containers retained')
    if success:
        print('[PASS] FULL DOCKER ACCEPTANCE COMPLETE')


if __name__ == '__main__':
    main()
