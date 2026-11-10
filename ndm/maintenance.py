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
from .core import LABEL, Error, ROOT, Runner, VERSION, atomic, confirm, version_tuple
from .docker import Engine


def component_series(role, version):
    """MariaDB and Redis stay on their X.Y branch; Caddy only patches its newest 2.x minor."""
    return version.split('.')[0] if role == 'proxy' else '.'.join(version.split('.')[:2])


class Maintenance:
    def update_component(self, component, supplied=None):
        live = self.ready()
        cfg = live.cfg
        repo = {'db': 'mariadb', 'redis': 'redis', 'proxy': 'caddy'}[component]
        old = cfg['images'][component].split(':', 1)[1].split('@')[0]
        tag = self.catalog.latest(repo, component_series(component, old))
        image = self.catalog.resolve(repo, tag)
        if image == cfg['images'][component]:
            raise Error('El componente ya está actualizado / Component is current')
        self.catalog.compatible(cfg['version'], image if component == 'db' else cfg['images']['db'])
        confirm('UPDATE-' + component.upper(), supplied, f'{repo}: {old} -> {tag}')
        changed = copy.deepcopy(cfg)
        changed['images'][component] = image
        if component != 'db':
            return self.swap_component(component, changed, tag)
        changed['maintenance_gate'] = True
        revision = self.state.revision(changed)
        proposed = Engine(self.state, changed, revision)
        proposed.pull([component])
        proposed.validate()
        archive.preflight(live)
        self.state.begin('db-update',
                         source=str((self.state.path / 'current').resolve()), component=component)
        with self.reopen_on_failure():
            path = archive.create(self.quiesce(), 'pre-update')
            self.state.phase('backed-up', backup=str(path))
            self.rehearse(path, cfg['version'], app_image=cfg['images']['app'], components={component: image})
        self.state.phase('applying', target=str(revision))
        self.state.activate(revision)
        self.finish()
        self.state.done()
        self.prune_backups()
        return {'component': component, 'version': tag, 'backup': str(path)}

    @staticmethod
    def app_updates(engine):
        """Store apps with a newer compatible release, as reported by Nextcloud itself."""
        output = engine.occ('app:update', '--showonly', check=False, timeout=300).stdout
        updates = []
        for line in output.splitlines():
            name, marker, version = line.partition(' new version available: ')
            if marker and name.strip():
                updates.append({'app': name.strip(), 'version': version.strip()})
        return updates

    def update_apps(self, supplied=None):
        """Store app updates cannot be rehearsed (the clone has no network), so a verified
        backup comes first and the closed proxy gate keeps users out until verification."""
        live = self.ready()
        apps = self.app_updates(live)
        if not apps:
            raise Error('No hay actualizaciones de aplicaciones / No app updates available')
        confirm('UPDATE-APPS', supplied, ', '.join(f'{item["app"]} {item["version"]}' for item in apps))
        archive.preflight(live)
        self.state.begin('apps-update', source=str((self.state.path / 'current').resolve()))
        with self.reopen_on_failure():
            backup = archive.create(self.quiesce(), 'pre-update')
        self.state.phase('applying', backup=str(backup))
        engine = Engine(self.state)
        engine.up('db', 'redis', 'app')
        self.wait_migration(engine)
        # Apps update with Nextcloud running, as from its web interface; the proxy gate
        # and the stopped cron keep users and jobs out until finish() verifies everything.
        engine.occ('maintenance:mode', '--off')
        engine.occ('app:update', '--all', timeout=1800)
        if engine.status().get('needsDbUpgrade'):
            engine.occ('upgrade', '--no-interaction', timeout=1800)
        self.finish(tune=True)
        self.state.done()
        self.prune_backups()
        return {'apps': [f'{item["app"]} {item["version"]}' for item in apps], 'backup': str(backup)}

    def swap_component(self, component, changed, tag):
        """Redis (cache without persistence) and Caddy (proxy) never touch SQL: replace only
        that container, online, and return to the previous image if verification fails."""
        revision = self.state.revision(changed)
        proposed = Engine(self.state, changed, revision)
        proposed.pull([component])
        proposed.validate()
        source = (self.state.path / 'current').resolve()
        self.state.begin('component-update', source=str(source), target=str(revision), component=component)
        try:
            self.state.activate(revision)
            proposed.up(component)
            Engine(self.state).verify()
            self.state.done()
        except BaseException:
            self.state.activate(source)
            previous = Engine(self.state)
            try:
                previous.up(component)
                previous.verify()
                self.state.done()
            except BaseException:
                pass
            raise
        return {'component': component, 'version': tag}

    def maintenance(self):
        result = {'checked': dt.datetime.now(dt.timezone.utc).isoformat(), 'errors': [], 'warnings': []}
        engine = Engine(self.state)
        result['status'] = engine.status()
        result['support'] = self.catalog.support_status(engine.cfg['version'])
        if result['support']['obsolete']:
            result['errors'].append('Nextcloud sin mantenimiento oficial; planifica una actualización secuencial / Nextcloud is end of life; plan a sequential upgrade')
        elif result['support']['state'] == 'unknown':
            result['errors'].append('No se pudo verificar el soporte de Nextcloud / Nextcloud support could not be verified')
        elif result['support']['state'] == 'ending':
            result['warnings'].append('El mantenimiento de Nextcloud termina pronto: %s / Nextcloud maintenance ends soon: %s' % ((result['support']['support_until'],) * 2))
        if result['support']['stale']:
            result['warnings'].append('Calendario no disponible; se usan las fechas oficiales guardadas / Release calendar unavailable; using saved official dates')
        if self.state.pending:
            result['errors'].append('Hay una operación pendiente: usa Recuperar / Pending operation requires recovery')
        elif result['status'].get('installed'):
            # Independent checks: a registry outage must not hide an expiring certificate.
            def check(function):
                try:
                    function()
                except (Error, OSError, ValueError, KeyError) as exc:
                    result['errors'].append(str(exc))

            def service():
                engine.check_mount()
                engine.verify()

            def updates():
                result['updates'] = self.checks()

            def database():
                support = self.database_support(engine.cfg)
                result['database'] = support
                # Information only: the branch changes when a Nextcloud upgrade requires it.
                current = support['current']
                if support['state'] == 'eol':
                    result['warnings'].append(f'MariaDB {current} terminó su soporte; se cambiará de LTS cuando Nextcloud lo exija / '
                                              f'MariaDB {current} is out of support; the LTS changes when Nextcloud requires it')
                elif support['state'] == 'ending':
                    until = support['community_until']
                    result['warnings'].append(f'El soporte de MariaDB {current} termina el {until}; se cambiará de LTS cuando Nextcloud lo exija / '
                                              f'MariaDB {current} support ends on {until}; the LTS changes when Nextcloud requires it')

            def backup():
                if not self.state.locked:
                    # Another manager session is open: never race it over backup staging folders.
                    result['warnings'].append('El gestor estaba abierto; se omitió verificar el respaldo / '
                                              'The manager was open; backup verification skipped')
                    return
                backups = sorted((self.state.path / 'backups').glob('*.ndm.tar'))
                if backups:
                    with archive.unpack(self.state, backups[-1]):
                        result['backup'] = {'name': backups[-1].name, 'valid': True}
                else:
                    result['backup'] = {'available': False}
                    result['warnings'].append('Sin respaldos todavía / No backup yet')

            def cron():
                value = engine.occ('config:app:get', 'core', 'lastcron', check=False).stdout.strip()
                result['cron_last_run'] = value
                if not value.isdigit() or time.time() - int(value) > 1200:
                    result['errors'].append('Cron no se ha completado en 20 minutos / Cron has not completed in the last 20 minutes')

            def certificates():
                result['certificates'] = self.check_certificates(engine)

            def integrity():
                ok = engine.occ('integrity:check-core', check=False, timeout=900).returncode == 0
                result['core_integrity_ok'] = ok
                if not ok:
                    result['warnings'].append('La integridad del código tiene avisos; consulta «Lista de archivos inválidos» en Nextcloud / '
                                              'Code integrity has warnings; see «List of invalid files» in Nextcloud')

            def apps():
                updates = self.app_updates(engine)
                result['app_updates'] = [f'{item["app"]} {item["version"]}' for item in updates]
                if updates:
                    result['warnings'].append(f'{len(updates)} aplicaciones con actualización en Actualizaciones / '
                                              f'{len(updates)} apps can be updated in Updates')

            for function in (service, updates, database, backup, cron, certificates, integrity, apps):
                check(function)
        else:
            result['errors'].append('Nextcloud está detenido o falta la instalación web / Nextcloud is stopped or web installation is pending')
        result['free_bytes'] = shutil.disk_usage(self.state.path).free
        if result['free_bytes'] < 2 * 1024 ** 3:
            result['errors'].append('Menos de 2 GiB libres para el gestor / Less than 2 GiB free on manager filesystem')
        atomic(self.state.path / 'last-maintenance.json', result)
        if not self.state.locked:
            return result
        for path in sorted((self.state.path / 'logs').glob('*.log'))[:-30]:
            if path.is_file() and not path.is_symlink() and path != self.state.log_path:
                path.unlink()
        return result

    def wait_certificate(self, name, timeout=90):
        """After adding a public domain: a 308 redirect alone does not prove HTTPS works.
        False means not issued yet (DNS, ports 80/443 or the tunnel); Caddy keeps retrying."""
        engine = Engine(self.state)
        until = time.monotonic() + timeout
        while True:
            if any(name in item['names'] for item in self.read_certificates(engine)):
                return True
            if time.monotonic() > until:
                return False
            time.sleep(5)

    def cleanup_candidates(self):
        """Volumes this installation no longer uses (left by restores and LTS changes),
        Docker images earlier revisions used (left by updates) and configuration revisions
        older than the five newest. Never the active ones."""
        if self.state.pending:
            raise Error('Hay una operación pendiente: usa Recuperar / Pending operation: use recover')
        cfg = self.state.load()
        runner = Runner(self.state)
        names = runner.run(['docker', 'volume', 'ls', '--quiet', '--filter',
                            f'label={LABEL}.installation={cfg["id"]}']).stdout.split()
        volumes = sorted(name for name in names if name not in set(cfg['volumes'].values()))
        current = (self.state.path / 'current').resolve()
        revisions = sorted(path for path in (self.state.path / 'revisions').iterdir()
                           if path.is_dir() and not path.is_symlink())
        old = [path for path in revisions[:-5] if path != current]
        # Only images this installation pulled itself, as recorded by its revisions.
        used = set()
        for path in revisions:
            try:
                used.update(json.loads((path / 'config.json').read_text(encoding='utf-8'))['images'].values())
            except (OSError, ValueError, KeyError, AttributeError):
                continue
        active = set(cfg['images'].values())
        active_ids = {runner.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref], check=False).stdout.strip()
                      for ref in active}
        images = []
        for ref in sorted(used - active):
            found = runner.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref], check=False)
            if found.returncode == 0 and found.stdout.strip() not in active_ids:
                images.append(ref)
        return {'volumes': volumes, 'revisions': [path.name for path in old], 'images': images}

    def cleanup(self, supplied=None):
        found = self.cleanup_candidates()
        if not found['volumes'] and not found['revisions'] and not found['images']:
            return {'removed_volumes': [], 'removed_revisions': 0, 'removed_images': []}
        confirm('CLEANUP', supplied, ', '.join(found['volumes'] + found['images']))
        engine = Engine(self.state)
        root = Path(engine.cfg['storage_root']) / engine.cfg['id']
        active = {engine.volume_path(role).resolve() for role in engine.cfg['volumes']} if engine.cfg['storage'] == 'directory' else set()
        removed, kept = [], []
        for name in found['volumes']:
            info = engine.inspect('volume', name)
            if not info:
                continue
            engine.owned(info, 'volume')
            if engine.runner.run(['docker', 'volume', 'rm', name], check=False).returncode:
                kept.append(name)
                continue
            removed.append(name)
            # Server-folder storage keeps the files after the volume is removed.
            # Docker reports "Options": null for plain named volumes.
            device = (info.get('Options') or {}).get('device')
            if device:
                folder = Path(device).resolve()
                if folder.is_relative_to(root.resolve()) and folder not in active and folder.is_dir():
                    shutil.rmtree(folder)
        for name in found['revisions']:
            shutil.rmtree(self.state.path / 'revisions' / name)
        images = []
        for ref in found['images']:
            # repo@sha256:digest; Docker refuses while any container still uses the image.
            reference = ref.split(':', 1)[0] + '@' + ref.split('@', 1)[1]
            if engine.runner.run(['docker', 'image', 'rm', reference], check=False, timeout=300).returncode:
                kept.append(ref)
            else:
                images.append(ref)
        return {'removed_volumes': removed, 'removed_revisions': len(found['revisions']),
                'removed_images': images, 'in_use': kept}

    @staticmethod
    def read_certificates(engine):
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
        return certificates

    @classmethod
    def check_certificates(cls, engine):
        certificates = cls.read_certificates(engine)
        missing = [e['host'] for e in engine.cfg['domains'] if e['tls'] != 'off'
                   and not any(e['host'] in c['names'] for c in certificates)]
        if missing:
            raise Error('Certificado aún no emitido: %s / Certificate not yet issued: %s' % ((', '.join(missing),) * 2))
        for entry in engine.cfg['domains']:
            if entry['tls'] != 'off':
                threshold = 3600 if entry['tls'] == 'internal' else 7 * 86400
                valid = [c['seconds_remaining'] for c in certificates if entry['host'] in c['names']]
                if not valid or max(valid) < threshold:
                    raise Error('Certificado a punto de caducar; revisa los registros de Caddy: %s / Certificate is near expiry; inspect Caddy logs: %s' % ((entry['host'],) * 2))
        return certificates

    def schedule_enabled(self):
        """None without systemd; otherwise whether this installation's daily timer exists."""
        if not Path('/run/systemd/system').exists():
            return None
        return (Path('/etc/systemd/system') / f"nextcloud-manager-{self.state.load()['id']}.timer").exists()

    def schedule(self, action, supplied=None):
        if not Path('/run/systemd/system').exists():
            raise Error('La programación requiere systemd / Scheduling requires a Linux host running systemd')
        name = 'nextcloud-manager-' + self.state.load()['id']
        service, timer = (Path('/etc/systemd/system') / (name + ext) for ext in ('.service', '.timer'))
        runner = Runner(self.state)
        if action == 'status':
            return runner.run(['systemctl', 'status', name + '.timer', '--no-pager'], check=False).stdout
        confirm('SCHEDULE-' + action.upper(), supplied)
        if action == 'enable':
            # Only provide the installed copy when missing; replacing it is self_update's job,
            # which checks syntax and refuses downgrades.
            if not (self.state.path / 'manager' / 'start.sh').exists():
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
                raise Error('Descarga primero el repositorio del gestor e indica su ruta / Download the manager checkout first and select --source /path/to/checkout')
        if source == self.state.path / 'manager':
            raise Error('Indica la ruta de una copia nueva del gestor, no la instalada / Download the new manager checkout and use self-update --source /path/to/checkout')
        for path in (source / 'ndm').rglob('*.py'):
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            except SyntaxError as exc:
                raise Error('La copia nueva tiene un error de sintaxis; se conserva la instalada: %s / New manager source has a syntax error; installed code preserved: %s' % (path, path)) from exc
        for path in (source / 'locales').glob('*.json'):
            try:
                json.loads(path.read_text(encoding='utf-8'))
            except ValueError as exc:
                raise Error('Archivo de idioma inválido: %s / Invalid locale file: %s' % (path, path)) from exc
        target = (source / 'VERSION').read_text().strip()
        if version_tuple(target) < version_tuple(VERSION):
            raise Error('Volver a una versión anterior del gestor requiere revisión manual / Manager downgrade requires manual review')
        Runner(self.state).run(['bash', '-n', source / 'start.sh'])
        confirm('UPDATE-MANAGER', supplied, f'Manager {VERSION} -> {target}; source: {source}')
        self.state.installed_code(source)
        return {'manager': target, 'previous_code': str(self.state.path / 'manager-previous')}
