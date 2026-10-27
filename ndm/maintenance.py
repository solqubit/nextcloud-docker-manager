"""Explicit component updates and read-only daily health checks."""
import ast
import copy
import datetime as dt
import json
from pathlib import Path
import re
import shutil
import ssl
import time

from . import archive
from .core import Error, ROOT, Runner, VERSION, atomic, confirm, version_tuple
from .docker import Engine


class Maintenance:
    def update_component(self, component, supplied=None):
        cfg = self.ready().cfg
        repo = {'db': 'mariadb', 'redis': 'redis', 'proxy': 'caddy'}[component]
        old = cfg['images'][component].split(':', 1)[1].split('@')[0]
        tag = self.catalog.latest(repo, '.'.join(old.split('.')[:2]))
        image = self.catalog.resolve(repo, tag)
        if image == cfg['images'][component]:
            raise Error('El componente ya está actualizado / Component is current')
        self.catalog.compatible(cfg['version'], image if component == 'db' else cfg['images']['db'])
        confirm('UPDATE-' + component.upper(), supplied, f'{repo}: {old} -> {tag}')
        changed = copy.deepcopy(cfg)
        changed['images'][component] = image
        changed['maintenance_gate'] = True
        revision = self.state.revision(changed)
        proposed = Engine(self.state, changed, revision)
        proposed.pull([component])
        proposed.validate()
        self.state.begin('db-update' if component == 'db' else 'component-update',
                         source=str((self.state.path / 'current').resolve()), component=component)
        path = archive.create(self.quiesce(), 'pre-update')
        self.state.phase('backed-up', backup=str(path))
        self.rehearse(path, cfg['version'], app_image=cfg['images']['app'], components={component: image})
        self.state.phase('applying', target=str(revision))
        self.state.activate(revision)
        self.finish()
        self.state.done()
        self.prune_backups()
        return {'component': component, 'version': tag, 'backup': str(path)}

    def maintenance(self):
        result = {'checked': dt.datetime.now(dt.timezone.utc).isoformat(), 'errors': [], 'warnings': []}
        engine = Engine(self.state)
        result['status'] = engine.status()
        result['support'] = self.catalog.support_status(engine.cfg['version'])
        if result['support']['obsolete']:
            result['errors'].append('Nextcloud is end of life; plan a sequential upgrade')
        elif result['support']['state'] == 'unknown':
            result['errors'].append('Nextcloud support could not be verified')
        elif result['support']['state'] == 'ending':
            result['warnings'].append('Nextcloud maintenance ends soon: ' + result['support']['support_until'])
        if result['support']['stale']:
            result['warnings'].append('Release calendar unavailable; using saved official dates')
        if self.state.pending:
            result['errors'].append('Pending operation requires recovery')
        elif result['status'].get('installed'):
            try:
                engine.check_mount()
                engine.verify()
                result['updates'] = self.checks()
                backups = sorted((self.state.path / 'backups').glob('*.ndm.tar'))
                if backups:
                    with archive.unpack(self.state, backups[-1]):
                        result['backup'] = {'name': backups[-1].name, 'valid': True}
                else:
                    result['backup'] = {'available': False}
                cron = engine.occ('config:app:get', 'core', 'lastcron', check=False).stdout.strip()
                result['cron_last_run'] = cron
                if not cron.isdigit() or time.time() - int(cron) > 1200:
                    result['errors'].append('Cron has not completed in the last 20 minutes')
                result['certificates'] = self.check_certificates(engine)
            except (Error, OSError, ValueError) as exc:
                result['errors'].append(str(exc))
        else:
            result['errors'].append('Nextcloud is stopped or web installation is pending')
        result['free_bytes'] = shutil.disk_usage(self.state.path).free
        if result['free_bytes'] < 2 * 1024 ** 3:
            result['errors'].append('Less than 2 GiB free on manager filesystem')
        atomic(self.state.path / 'last-maintenance.json', result)
        for path in sorted((self.state.path / 'logs').glob('*.log'))[:-30]:
            if path.is_file() and not path.is_symlink() and path != self.state.log_path:
                path.unlink()
        return result

    @staticmethod
    def check_certificates(engine):
        result = engine.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'sh',
                    '--mount', 'type=volume,src=' + engine.cfg['volumes']['proxy_data'] + ',dst=/data,readonly',
                    engine.cfg['images']['proxy'], '-ec',
                    'find /data/caddy -type f -name "*.crt" -exec cat {} \\;'], check=False)
        certificates = []
        for index, pem in enumerate(re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', result.stdout, re.S)):
            path = engine.state.path / 'cache' / f'certificate-{index}.pem'
            atomic(path, pem)
            decoded = ssl._ssl._test_decode_cert(str(path))
            names = [value for kind, value in decoded.get('subjectAltName', []) if kind in ('DNS', 'IP Address')]
            remaining = int(ssl.cert_time_to_seconds(decoded['notAfter']) - time.time())
            certificates.append({'names': names, 'seconds_remaining': remaining, 'days_remaining': round(remaining / 86400, 2)})
            path.unlink()
        missing = [e['host'] for e in engine.cfg['domains'] if e['tls'] != 'off'
                   and not any(e['host'] in c['names'] for c in certificates)]
        if missing:
            raise Error('Certificate not yet issued: ' + ', '.join(missing))
        for entry in engine.cfg['domains']:
            if entry['tls'] != 'off':
                threshold = 3600 if entry['tls'] == 'internal' else 7 * 86400
                valid = [c['seconds_remaining'] for c in certificates if entry['host'] in c['names']]
                if not valid or max(valid) < threshold:
                    raise Error('Certificate is near expiry; inspect Caddy logs: ' + entry['host'])
        return certificates

    def schedule(self, action):
        if not Path('/run/systemd/system').exists():
            raise Error('Scheduling requires a Linux host running systemd; container tests cannot enable it')
        name = 'nextcloud-manager-' + self.state.load()['id']
        service, timer = (Path('/etc/systemd/system') / (name + ext) for ext in ('.service', '.timer'))
        runner = Runner(self.state)
        if action == 'status':
            return runner.run(['systemctl', 'status', name + '.timer', '--no-pager'], check=False).stdout
        confirm('SCHEDULE-' + action.upper())
        if action == 'enable':
            self.state.installed_code()
            invocation = ' '.join(json.dumps(str(v).replace('%', '%%')) for v in ('/bin/bash', self.state.path / 'start.sh', 'maintenance'))
            atomic(service, '[Unit]\nDescription=Nextcloud manager health and update checks\nAfter=docker.service\n'
                   '[Service]\nType=oneshot\nUMask=0077\nExecStart=' + invocation + '\n', 0o644)
            atomic(timer, '[Unit]\nDescription=Daily Nextcloud manager checks\n[Timer]\nOnCalendar=daily\n'
                   'RandomizedDelaySec=1h\nPersistent=true\n[Install]\nWantedBy=timers.target\n', 0o644)
            runner.run(['systemctl', 'daemon-reload'])
            runner.run(['systemctl', 'enable', '--now', name + '.timer'])
        else:
            runner.run(['systemctl', 'disable', '--now', name + '.timer'])
            service.unlink(missing_ok=True)
            timer.unlink(missing_ok=True)
            runner.run(['systemctl', 'daemon-reload'])
        return {'schedule': action, 'updates_applied_automatically': False}

    def self_update(self, source=None, supplied=None):
        source = Path(source).resolve() if source else ROOT
        for name in ('ndm', 'locales', 'VERSION', 'LICENSE', 'start.sh'):
            if not (source / name).exists():
                raise Error('Download the manager checkout first and select --source /path/to/checkout')
        if source == self.state.path / 'manager':
            raise Error('Download the new manager checkout and use self-update --source /path/to/checkout')
        for path in (source / 'ndm').rglob('*.py'):
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            except SyntaxError as exc:
                raise Error('New manager source has a syntax error; installed code preserved: ' + str(path)) from exc
        target = (source / 'VERSION').read_text().strip()
        if version_tuple(target) < version_tuple(VERSION):
            raise Error('Manager downgrade requires manual review')
        Runner(self.state).run(['bash', '-n', source / 'start.sh'])
        confirm('UPDATE-MANAGER', supplied, f'Manager {VERSION} -> {target}; source: {source}')
        self.state.installed_code(source)
        return {'manager': target, 'previous_code': str(self.state.path / 'manager-previous')}
