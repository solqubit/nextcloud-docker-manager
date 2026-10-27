"""Controlled backend for actual PTY navigation tests; never starts Docker.

The separate Docker acceptance suite validates lifecycle behavior. This fixture
exercises every terminal workflow, including confirmations and failure paths.
"""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import tarfile
import time
from unittest.mock import patch

from ndm.config import initial
from ndm.core import Error, State, atomic, confirm, stage
from ndm.screens import Controller


def images(version='33.0.10'):
    return {role: repo + ':' + tag + '@sha256:' + 'a' * 64 for role, repo, tag in (
        ('app', 'nextcloud', version + '-apache'), ('db', 'mariadb', '11.4.13'),
        ('redis', 'redis', '7.4.12'), ('proxy', 'caddy', '2.11.7'))}


class Catalog:
    def branches(self):
        time.sleep(0.35)
        return ['35.0.2', '34.0.5', '33.0.10']

    def support_status(self, version):
        expired = version.startswith('32.')
        return dict(version=version, major=int(version.split('.')[0]), state='eol' if expired else 'supported',
                    obsolete=expired, support_until='2026-09-30' if expired else '2099-09-15',
                    precision='day', checked='2026-10-27T00:00:00+00:00', stale=False)

    def installation_options(self):
        return [dict(self.support_status(version), recommendation='latest' if n == 0 else 'previous')
                for n, version in enumerate(self.branches()[:2])]


class FixtureManager:
    def __init__(self, state, require_db=False):
        self.state, self.catalog, self.require_db = state, Catalog(), require_db

    def record(self, action, **kwargs):
        with (self.state.path / 'ui-calls.jsonl').open('a') as out:
            out.write(json.dumps({'action': action, **kwargs}) + '\n')

    def status(self):
        cfg = self.state.load()
        return {'version': cfg['version'], 'environment': cfg['environment'],
                'access': [dict(entry, url='http://' + entry['host'] + ':' + str(cfg['http_port'])) for entry in cfg['domains']],
                'status': {'installed': cfg['status'] == 'ready', 'maintenance': False, 'needsDbUpgrade': False},
                'operation': self.state.pending,
                'support': self.catalog.support_status(cfg['version']),
                'services': [dict(Service=role, State='running', Health='healthy', Ports='8080->80/tcp' if role == 'proxy' else '')
                             for role in ('app', 'db', 'redis', 'proxy', 'cron')]}

    def install(self, version, **settings):
        confirm('INSTALL')
        self.record('install', version=version, **settings)
        with stage('Descargar imágenes / Download images'):
            print('PRIVATE-DIAGNOSTIC-NOT-A-UI-LABEL')
            time.sleep(0.45)
        with stage('Iniciar instalación local / Start local installation'):
            cfg = initial(version, images(version), **settings)
            self.state.activate(self.state.revision(cfg))
            time.sleep(0.25)
        return {'installed': False, 'maintenance': False}

    def setup_check(self):
        cfg = self.state.load()
        cfg['status'] = 'ready'
        self.state.activate(self.state.revision(cfg))
        self.record('setup')
        return {'installed': True, 'maintenance': False}

    def checks(self):
        time.sleep(0.25)
        return {'current': self.state.load()['version'], 'patches': ['33.0.11'], 'next_major': ['34.0.5'],
                'components': {'db': '11.4.14', 'redis': '7.4.13', 'proxy': '2.11.8'}}

    def database_choices(self, target):
        return {'required': self.require_db, 'choices': [{'series': '11.8', 'community_until': '2030-06-04'}],
                'for_nextcloud': target}

    def migrate_database(self, target, *, for_nextcloud):
        confirm('MIGRATE-DB-' + target)
        self.record('migrate_database', target=target, for_nextcloud=for_nextcloud)
        return {'database': target}

    def update(self, target, *, major=False, refresh=False):
        confirm(('MIGRATE-' if major else 'UPDATE-') + target)
        self.record('update', target=target, major=major, refresh=refresh)
        for title in ('Respaldo consistente / Consistent backup',
                      'Ensayar SQL y aplicación sin archivos de usuarios / Rehearse SQL without user files',
                      'Aplicar y verificar / Apply and verify'):
            with stage(title):
                time.sleep(0.2)
        return {'version': target}

    def update_component(self, component):
        confirm('UPDATE-' + component.upper())
        self.record('update_component', component=component)
        if component == 'proxy':
            with stage('Aplicar y verificar / Apply and verify'):
                time.sleep(0.15)
                raise Error('No se pudo validar el proxy / Could not validate proxy')
        return {'component': component, 'version': '11.4.14' if component == 'db' else '7.4.13'}

    def backup(self):
        confirm('BACKUP')
        self.record('backup')
        with stage('Respaldo SQL y aplicación / SQL and application backup'):
            time.sleep(0.35)
            path = make_backup(self.state)
        return path

    def restore(self, path, *, accept_user_files):
        confirm('RESTORE')
        self.record('restore', path=str(path), accept_user_files=accept_user_files)
        with stage('Aplicar y verificar / Apply and verify'):
            time.sleep(0.3)
        return {'restored': True}

    def settings(self, cfg):
        confirm('APPLY')
        self.record('settings')
        self.state.activate(self.state.revision(cfg))

    def add_domain(self, name, **kwargs):
        confirm('APPLY')
        cfg = self.state.load()
        cfg['domains'].append(dict(host=name, kind='local' if kwargs['mode'] == 'local' else 'public',
                                   mode=kwargs['mode'], tls='off' if kwargs['mode'] == 'local' else 'acme',
                                   email=kwargs['email'], external_port=kwargs['external_port']))
        self.state.activate(self.state.revision(cfg))
        self.record('add_domain', name=name, **kwargs)

    def remove_domain(self, name):
        confirm('APPLY')
        cfg = self.state.load()
        cfg['domains'] = [entry for entry in cfg['domains'] if entry['host'] != name]
        if cfg['primary'] == name:
            cfg['primary'] = cfg['domains'][0]['host']
        self.state.activate(self.state.revision(cfg))
        self.record('remove_domain', name=name)

    def recover(self, action, *, accept_user_files):
        confirm('RESTORE' if action == 'restore' else 'RESUME')
        self.record('recover', selected=action, accept_user_files=accept_user_files)
        self.state.done()
        return {'recovered': True}

    def maintenance(self):
        self.record('maintenance')
        return {'errors': ['Cron needs review'] if (self.state.path / 'fail-checks').exists() else [],
                'status': {'installed': True}, 'updates': self.checks()}

    def schedule(self, action):
        if action != 'status':
            confirm('SCHEDULE-' + action.upper())
        self.record('schedule', selected=action)
        return {'schedule': action, 'updates_applied_automatically': False}

    def self_update(self, source):
        confirm('UPDATE-MANAGER')
        self.record('self_update', source=source)
        return {'manager': '0.1.1', 'previous_code': '/opt/nextcloud-docker-manager/manager-previous'}


class FixtureEngine:
    def __init__(self, state, cfg=None):
        self.state, self.cfg = state, cfg or state.load()

    def check_ports(self, old=None):
        if self.cfg['http_port'] == 8088:
            raise Error('Puerto ocupado por otro contenedor / Port owned by another container: 8088')

    def dc(self, *args, **kwargs):
        from subprocess import CompletedProcess
        return CompletedProcess([], 0, '\n'.join(f'cron: línea de registro {i:03d} ' + 'x' * 80 for i in range(220)), '')

    def up(self, *args):
        self.record('engine_start')

    def stop(self, *args):
        self.record('engine_stop')

    def record(self, action):
        with (self.state.path / 'ui-calls.jsonl').open('a') as out:
            out.write(json.dumps({'action': action}) + '\n')

    def wait_app(self, **kwargs):
        return {'installed': True}

    def check_space(self, *args):
        return 8 * 1024 ** 3

    def export_images(self, path):
        path.write_text('image-fixture')
        return path

    def ca_certificate(self):
        path = self.state.path / 'local-ca.crt'
        path.write_text('public-certificate-fixture')
        return path


def make_backup(state):
    path = state.path / 'backups' / '20261027T090000Z-fixture.ndm.tar'
    manifest = {'created': '20261027T090000Z', 'kind': 'manual', 'configuration': state.load(),
                'excluded': ['/var/www/html/data']}
    value = json.dumps(manifest).encode()
    with tarfile.open(path, 'w') as out:
        item = tarfile.TarInfo('manifest.json')
        item.size = len(value)
        out.addfile(item, io.BytesIO(value))
    return path


@contextmanager
def unpack(state, path):
    with tarfile.open(path) as source:
        manifest = json.load(source.extractfile('manifest.json'))
    yield Path(path).parent, manifest


def main():
    state = State(sys.argv[1]).open()
    mode = sys.argv[2]
    if mode not in ('empty', 'empty-english'):
        version = '32.0.15' if mode == 'obsolete' else '33.0.10'
        # Existing installations keep their saved nonstandard ports.
        cfg = initial(version, images(version), storage='named', http_port=8080, https_port=8443,
                      language='en' if mode == 'english' else 'es')
        cfg['status'] = 'ready'
        state.activate(state.revision(cfg))
        make_backup(state)
    if mode == 'pending':
        state.begin('update', traffic_opened=True)
    manager = FixtureManager(state, require_db=mode == 'required-db')
    try:
        with patch('ndm.screens.Engine', FixtureEngine), patch('ndm.cli.Engine', FixtureEngine), patch('ndm.screens.archive.unpack', unpack):
            Controller(manager, 'en' if mode == 'empty-english' else None).run()
    finally:
        state.lock_file.close()


if __name__ == '__main__':
    main()
