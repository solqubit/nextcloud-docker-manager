"""Installation, reversible settings, SQL rehearsal and guarded upgrade recovery."""
from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time

from . import archive
from .catalog import Catalog
from .config import access_url, compose, host, initial, port, prefer_first_tunnel, public_host, validate
from .core import Error, Runner, State, atomic, confirm, human, read_json, stage, stamp, version_tuple
from .docker import Engine
from .maintenance import Maintenance, component_series
from .database import DatabaseMigration


class Manager(Maintenance, DatabaseMigration):
    def __init__(self, state):
        self.state = state
        self.catalog = Catalog(state.path / 'cache')

    def new_subnet(self):
        runner = Runner(self.state)
        ids = runner.run(['docker', 'network', 'ls', '--quiet']).stdout.split()
        networks = []
        if ids:
            for entry in json.loads(runner.run(['docker', 'network', 'inspect', *ids]).stdout):
                networks += [ipaddress.ip_network(c['Subnet']) for c in (entry.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
        for _ in range(250):
            candidate = ipaddress.ip_network(f'172.{secrets.randbelow(12) + 19}.{secrets.randbelow(250)}.0/28')
            reserved = candidate.supernet(prefixlen_diff=1)
            if not any(reserved.overlaps(n) for n in networks if n.version == 4):
                return str(candidate)
        raise Error('No hay una subred privada libre / No private subnet available')

    def install(self, version=None, *, supplied=None, database_series='11.4', **settings):
        if self.state.configured:
            raise Error('Ya existe una instalación; usa el menú / Installation already configured')
        with stage('Consultar imágenes oficiales / Resolve official images'):
            version = version or self.catalog.installation_options()[0]['version']
            version_tuple(version)
            self.catalog.require_supported(version)
            images = {'app': self.catalog.resolve('nextcloud', version + '-apache')}
            if database_series not in self.catalog.maintained_databases()['branches']:
                raise Error('La LTS de MariaDB elegida no tiene mantenimiento Community / Selected MariaDB LTS is not under Community maintenance')
            for role, repo, series in (('db', 'mariadb', database_series), ('redis', 'redis', '7.4'), ('proxy', 'caddy', '2')):
                images[role] = self.catalog.resolve(repo, self.catalog.latest(repo, series))
            required = self.catalog.compatible(version, images['db'])
        cfg = initial(version, images, subnet=self.new_subnet(), **settings)
        bind = ipaddress.ip_address(cfg['bind'])
        if bind.is_unspecified or (cfg['environment'] == 'vps' and not bind.is_loopback):
            raise Error('En una VPS la primera configuración usa 127.0.0.1 y un túnel SSH; publica los puertos después de crear '
                        'el administrador / On a VPS the first setup uses 127.0.0.1 and an SSH tunnel; publish ports '
                        'after creating the administrator')
        if bind.is_global:
            # Until the administrator exists, whoever opens the web installer owns the instance.
            raise Error('La IP %s es pública: elige VPS (127.0.0.1 + túnel SSH) o una IP privada de la LAN / '
                        'IP %s is public: choose VPS (127.0.0.1 + SSH tunnel) or a private LAN IP' % (bind, bind))
        print(f"Nextcloud {version}; {cfg['environment']}; {access_url(cfg)}; storage: {cfg['storage']}")
        print(required['source'])
        confirm('INSTALL', supplied)
        engine = Engine(self.state, cfg)
        engine.check_ports()
        with stage('Descargar imágenes / Download images'):
            engine.pull()
        php = engine.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'php', images['app'], '-r',
                                 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;']).stdout.strip()
        if php not in required['php']:
            raise Error('La versión de PHP de la imagen no está en los requisitos documentados / Target PHP is not in the documented requirements')
        revision = self.state.revision(cfg)
        engine = Engine(self.state, cfg, revision)
        engine.validate()
        engine.ensure_volumes()
        revision = self.state.revision(cfg)
        engine = Engine(self.state, cfg, revision)
        self.state.begin('install', target=str(revision), images=images)
        self.state.activate(revision)
        self.state.installed_code()
        with stage('Iniciar instalación local / Start local installation'):
            engine.put_managed()
            engine.up('db', 'redis', 'app', 'proxy', 'cron')
            engine.wait_app(installed=False)
        self.state.done()
        return self.setup_check()

    def setup_check(self):
        engine = Engine(self.state)
        status = engine.status()
        if not status.get('installed'):
            print('[*] Pendiente de configuración web / Awaiting web setup: ' + access_url(engine.cfg))
            return status
        if self.state.pending:
            raise Error('Usa Recuperar antes de completar una operación interrumpida / Use recover before completing an interrupted operation')
        engine.verify(configure_cron=True)
        cfg = engine.cfg
        moved_logs = engine.put_managed()
        first_setup = cfg['status'] == 'awaiting-web' and cfg.get('initial_maintenance_pending', False)
        if first_setup:
            # Only newly created manager installations qualify. An existing
            # database is never silently sent through expensive repairs.
            if engine.db_size() <= 64 * 1024 ** 2:
                with stage('Completar índices y tipos MIME iniciales / Complete initial indices and MIME types'):
                    engine.occ('db:add-missing-indices')
                    engine.occ('maintenance:repair', '--include-expensive')
                cfg['initial_maintenance_pending'] = False
                status = engine.verify(configure_cron=True)
            else:
                status['initial_maintenance_deferred'] = True
        if first_setup or moved_logs:
            with stage('Comprobar integridad del código / Check code integrity'):
                integrity = engine.occ('integrity:check-core', check=False)
            status['core_integrity_ok'] = integrity.returncode == 0
        cfg['status'] = 'ready'
        self.state.activate(self.state.revision(cfg))
        engine.up('cron')
        print('[+] Nextcloud instalado y verificado / Installed and verified: ' + access_url(cfg))
        return status

    def status(self):
        engine = Engine(self.state)
        raw = engine.dc('ps', '--all', '--format', 'json').stdout.strip()
        services = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines()]
        return {'version': engine.cfg['version'], 'environment': engine.cfg['environment'],
                'access': [dict(e, url=access_url(engine.cfg, e)) for e in engine.cfg['domains']],
                'status': engine.status(), 'operation': self.state.pending,
                'services': services, 'support': self.catalog.support_status(engine.cfg['version'])}

    def ready(self, *, complete_setup=False):
        if self.state.pending:
            raise Error('Operación pendiente; usa recover / Pending operation; use recover')
        engine = Engine(self.state)
        engine.check_mount()
        status = engine.status()
        if not status.get('installed'):
            url = access_url(engine.cfg)
            raise Error(f'Completa la instalación web y crea el administrador en {url} / '
                        f'Complete web setup and create the administrator at {url}')
        if status.get('maintenance') or status.get('needsDbUpgrade'):
            raise Error('Completa la instalación o recupera mantenimiento antes de continuar / Nextcloud not ready')
        if engine.cfg['status'] != 'ready':
            if complete_setup:
                # The administrator already exists. Complete the manager's checks,
                # then reload and validate the resulting revision before backing up.
                with stage('Comprobar instalación web / Check web installation'):
                    self.setup_check()
                return self.ready()
            # finish() marks the installation ready; running it first would skip the initial repairs.
            raise Error('Primero elige «Comprobar instalación web» / First run «Check web installation»')
        return engine

    def reopen(self):
        """Before 'applying' the live SQL is untouched: return to the previous revision."""
        with stage('Restablecer el servicio sin cambios / Restore the service unchanged'):
            self.state.activate(self.state.pending['source'])
            Engine(self.state).put_managed()
            self.finish()
            self.state.done()

    @contextlib.contextmanager
    def reopen_on_failure(self):
        """A failed backup or rehearsal must not leave Nextcloud in maintenance mode.
        Interruptions (Ctrl+C) and unknown failures keep the journal for Recover."""
        try:
            yield
        except (Error, OSError) as exc:
            try:
                self.reopen()
            except BaseException:
                pass
            raise exc

    def gate(self, enabled):
        cfg = self.state.load()
        cfg['maintenance_gate'] = enabled
        revision = self.state.revision(cfg)
        self.state.activate(revision)
        return Engine(self.state)

    def quiesce(self):
        engine = self.gate(True)
        engine.up('proxy')
        engine.stop('cron')
        engine.occ('maintenance:mode', '--on')
        inventory = json.loads(engine.occ('app:list', '--output=json').stdout)
        self.state.phase(self.state.pending['phase'], enabled_apps=inventory['enabled'])
        engine.stop('app', 'proxy')
        return engine

    def finish(self):
        """Keep the proxy gate closed while disabling maintenance and checking SQL."""
        engine = Engine(self.state)
        if not engine.cfg['maintenance_gate']:
            engine = self.gate(True)
        engine.up('db', 'redis', 'app', 'proxy')
        self.wait_migration(engine)
        engine.occ('maintenance:mode', '--off')
        engine.verify(configure_cron=True)
        gated_revision = (self.state.path / 'current').resolve()
        if self.state.pending:
            self.state.phase(self.state.pending['phase'], traffic_opened=True)
        engine = self.gate(False)
        try:
            engine.up('proxy', 'cron')
            engine.verify()
        except BaseException:
            self.state.activate(gated_revision)
            guarded = Engine(self.state)
            try:
                guarded.stop('proxy', 'cron')
                guarded.occ('maintenance:mode', '--on', check=False)
                guarded.up('proxy')
            except BaseException:
                pass
            raise
        engine.cfg['status'] = 'ready'
        self.state.activate(self.state.revision(engine.cfg))
        return engine

    @staticmethod
    def wait_migration(engine, timeout=900):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            status = engine.status()
            if (status.get('installed') and not status.get('needsDbUpgrade')
                    and status.get('versionstring') == engine.cfg['version'] and engine.serving()):
                return status
            if status.get('container') in ('exited', 'dead'):
                raise Error('La migración se detuvo; revisa los registros / Migration container stopped; inspect logs')
            time.sleep(3)
        raise Error('La migración no terminó; se conserva el modo mantenimiento / Migration did not finish; maintenance is preserved')

    def backup(self, supplied=None):
        confirm('BACKUP', supplied, 'SQL + aplicación + certificados; ARCHIVOS DE USUARIOS EXCLUIDOS / '
                                    'SQL + application + certificates; USER FILES EXCLUDED')
        live = self.ready(complete_setup=True)
        # Check space while Nextcloud is still online.
        archive.preflight(live)
        self.state.begin('backup', source=str((self.state.path / 'current').resolve()))
        # Known failures reopen the unchanged service; interruptions keep the journal for Recover.
        with self.reopen_on_failure():
            with stage('Respaldo SQL y aplicación / SQL and application backup'):
                engine = self.quiesce()
                path = archive.create(engine)
            self.state.phase('backup-complete', backup=str(path))
        self.finish()
        self.state.done()
        return path

    def settings(self, cfg, supplied=None):
        old_engine = self.ready()
        old = old_engine.cfg
        validate(cfg)
        confirm('APPLY', supplied)
        revision = self.state.revision(cfg)
        new = Engine(self.state, cfg, revision)
        new.check_ports(old)
        new.validate()
        source = (self.state.path / 'current').resolve()
        self.state.begin('settings', source=str(source), target=str(revision))
        try:
            self.state.activate(revision)
            new.put_managed()
            new.up('proxy')
            new.verify()
            self.state.done()
        except BaseException:
            self.state.activate(source)
            previous = Engine(self.state)
            try:
                previous.put_managed()
                previous.up('proxy')
                previous.verify()
                self.state.done()
            except BaseException:
                pass
            raise

    def add_domain(self, name, *, mode='local', tls='off', email='', primary=False, publish_direct=False, external_port=443, supplied=None):
        cfg = self.state.load()
        name = host(name) if mode == 'local' else public_host(name)
        if any(e['host'] == name for e in cfg['domains']):
            raise Error('La dirección ya existe / Address already exists')
        auto_primary = prefer_first_tunnel(cfg, mode)
        cfg['domains'].append(dict(host=name, kind='local' if mode == 'local' else 'public',
                                   mode=mode, tls=tls if mode == 'local' else 'acme', email=email))
        if mode != 'local':
            cfg['domains'][-1]['external_port'] = port(external_port)
        if primary or auto_primary:
            cfg['primary'] = name
        if publish_direct:
            if mode != 'direct':
                raise Error('Publicar TCP 80/443 requiere el modo dominio directo / Publishing TCP 80/443 requires direct-domain mode')
            cfg.update(bind='0.0.0.0', http_port=80, https_port=443)
        self.settings(cfg, supplied)
        return cfg

    def remove_domain(self, name, supplied=None):
        name = host(name)
        cfg = self.state.load()
        remaining = [e for e in cfg['domains'] if e['host'] != host(name)]
        if len(remaining) == len(cfg['domains']):
            raise Error('Dirección no encontrada / Address not found')
        if not remaining:
            raise Error('No se puede quitar la última dirección / Cannot remove the last address')
        if not any(e['kind'] == 'local' for e in remaining):
            import urllib.request
            try:
                with urllib.request.urlopen(access_url(cfg, remaining[0]) + '/status.php', timeout=20) as response:
                    if not json.load(response).get('installed'):
                        raise ValueError('Not installed')
            except (OSError, ValueError) as exc:
                raise Error('Verifica la dirección HTTPS pública antes de quitar el último acceso local / Verify the public HTTPS address before removing the last local access address') from exc
        # Probe an existing alternative before retiring the access address.
        result = json.loads(Engine(self.state).probe_http(remaining[0]))
        if not result[0] or result[0][0].split()[1] not in ('200', '308'):
            raise Error('No se pudo verificar la dirección alternativa / Alternative address not verified')
        cfg['domains'] = remaining
        if cfg['primary'] == name:
            cfg['primary'] = remaining[0]['host']
        self.settings(cfg, supplied)

    def checks(self):
        cfg = self.state.load()
        branches = self.catalog.branches()
        available = self.catalog.patches(version_tuple(cfg['version'])[0])
        current = version_tuple(cfg['version'])
        patches = [v for v in available if version_tuple(v) > current and version_tuple(v)[:2] == current[:2]]
        next_major = [v for v in branches if version_tuple(v)[0] == current[0] + 1]
        ref = self.catalog.resolve('nextcloud', cfg['version'] + '-apache')
        components = {}
        for role, repo in (('db', 'mariadb'), ('redis', 'redis'), ('proxy', 'caddy')):
            installed = cfg['images'][role].split(':', 1)[1].split('@')[0]
            latest = self.catalog.latest(repo, component_series(role, installed))
            components[role] = {'current': installed, 'available': latest,
                                'image_changed': cfg['images'][role] != self.catalog.resolve(repo, latest)}
        return {'current': cfg['version'], 'support': self.catalog.support_status(cfg['version']),
                'patches': patches, 'next_major': next_major,
                'refreshed_image': ref != cfg['images']['app'],
                'branches': [v for v in branches if self.catalog.support_status(v)['state'] in ('supported', 'ending')],
                'components': components}

    def rehearse(self, path, target, app_image=None, db_image=None, components=None):
        """Actual SQL + application clone, excluding all user files and external networking."""
        identity = secrets.token_hex(8)
        temporary = self.state.path / 'cache' / ('rehearsal-' + identity)
        temporary.mkdir(mode=0o700)
        clone_state = State(temporary).open()
        engine = None
        success = False
        try:
            with archive.unpack(self.state, path) as (work, manifest):
                cfg = copy.deepcopy(manifest['configuration'])
                cfg.update(id=identity, project='ndm_' + identity, version=target,
                           storage='named', data_device=None, subnet=self.new_subnet(),
                           primary='localhost', bind='127.0.0.1', maintenance_gate=True,
                           domains=[dict(host='localhost', kind='local', mode='local', tls='off', email='')])
                cfg['volumes'] = {role: f'ndm_{identity}_{role}' for role in cfg['volumes']}
                cfg['images']['app'] = app_image or self.catalog.resolve('nextcloud', target + '-apache')
                if db_image:
                    cfg['images']['db'] = db_image
                cfg['images'].update(components or {})
                revision = clone_state.revision(cfg)
                definition = compose(cfg)
                definition['services']['proxy'].pop('ports')
                definition['networks']['proxy']['internal'] = True
                atomic(revision / 'compose.json', definition)
                clone_state.activate(revision)
                engine = Engine(clone_state)
                engine.pull()
                engine.ensure_volumes()
                engine.check_space(manifest['sql_estimated_bytes'] * 3 + manifest['files']['app.tar']['bytes'] * 2 + 512 * 1024 ** 2)
                archive.restore_volume(engine, cfg['volumes']['app'], work / 'app.tar', '/var/www/html')
                engine.put_managed(rehearsal=True)
                engine.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'sh',
                    '--mount', 'type=volume,src=' + cfg['volumes']['data'] + ',dst=/var/www/html/data',
                    cfg['images']['app'], '-ec', 'chown www-data:www-data /var/www/html/data; '
                    'printf "# Nextcloud data directory\\n" > /var/www/html/data/.ncdata; '
                    'touch /var/www/html/data/.ocdata; chown www-data:www-data /var/www/html/data/.[no]cdata'])
                engine.up('db', 'redis')
                self.wait_database(engine)
                archive.import_sql(engine, work / 'db.sql.gz')
                engine.up('app', 'proxy')
                self.wait_migration(engine)
                engine.occ('maintenance:mode', '--off')
                engine.verify()
                enabled = json.loads(engine.occ('app:list', '--output=json').stdout)['enabled']
                disabled = set(manifest.get('enabled_apps', {})) - set(enabled)
                if disabled:
                    raise Error('El destino desactivaría aplicaciones instaladas; revisa su compatibilidad: %s / Target would disable installed apps; review compatibility first: %s' % ((', '.join(sorted(disabled)),) * 2))
                success = True
                atomic(self.state.path / 'cache' / 'last-rehearsal.json', {
                    'source': manifest['configuration']['version'], 'target': target,
                    'image': cfg['images']['app'], 'images': cfg['images'], 'sql': 'restored and migrated',
                    'user_files': 'excluded', 'external_network': 'isolated', 'passed': True})
        finally:
            if not success:
                try:
                    if engine:
                        engine.dc('logs', '--no-color', '--tail', '150', 'app', 'db', check=False, timeout=15)
                    diagnostic = self.state.path / 'logs' / (stamp() + '-rehearsal-' + identity + '.log')
                    shutil.copyfile(clone_state.log_path, diagnostic)
                    os.chmod(diagnostic, 0o600)
                    if self.state.pending:
                        self.state.phase(self.state.pending['phase'], rehearsal_log=str(diagnostic))
                    print('[!] Registro del ensayo / Rehearsal diagnostics: ' + str(diagnostic))
                except (Error, OSError):
                    pass
            if engine:
                try:
                    engine.remove_owned(delete_volumes=True)
                except BaseException:
                    print(f'[!] Limpieza pendiente / Cleanup pending: {temporary}')
                    engine = None
            if clone_state.lock_file:
                clone_state.lock_file.close()
            if engine or not (temporary / 'current').exists():
                shutil.rmtree(temporary)
        return success

    @staticmethod
    def wait_database(engine):
        # The image's healthcheck (--innodb_initialized) stays unhealthy while the entrypoint
        # runs its temporary init server, so an import never starts against that server.
        until = time.monotonic() + 300
        while time.monotonic() < until:
            cid = engine.container('db', required=False)
            info = engine.inspect('container', cid) if cid else None
            if info and info['State'].get('Health', {}).get('Status') == 'healthy':
                try:
                    engine.sql('SELECT 1;')
                    return
                except Error:
                    pass
            time.sleep(3)
        raise Error('La base de datos no se inició a tiempo / Database initialization timed out')

    def update(self, target=None, *, major=False, refresh=False, supplied=None):
        source_engine = self.ready()
        cfg = source_engine.cfg
        current = version_tuple(cfg['version'])
        if refresh:
            target = cfg['version']
        elif target is None:
            choices = self.checks()['next_major' if major else 'patches']
            if not choices:
                raise Error('No hay actualización disponible / No update available')
            target = choices[0]
        dest = version_tuple(target)
        if major:
            if dest[0] != current[0] + 1:
                raise Error('No se pueden saltar ramas mayores / Major upgrades must be sequential')
            if self.catalog.patches(current[0])[0] != cfg['version']:
                raise Error('Aplica primero el último parche de la rama actual / Install the latest patch of the current major first')
        elif not refresh and (dest <= current or dest[:2] != current[:2]):
            raise Error('Solo parches posteriores de la misma rama X.Y; usa la versión mayor para cambiar de rama / Only later patches in the same X.Y series; use migrate for a major')
        required = self.catalog.compatible(target, cfg['images']['db'])
        image = self.catalog.resolve('nextcloud', target + '-apache')
        if refresh and image == cfg['images']['app']:
            raise Error('La imagen ya está actualizada / Image is current')
        expected = ('MIGRATE-' if major else 'UPDATE-') + target
        print(f"{cfg['version']} -> {target}; {required['source']}")
        confirm(expected, supplied, 'Se respaldará SQL y aplicación; archivos de usuarios excluidos / User files excluded')
        changed = copy.deepcopy(cfg)
        changed['version'] = target
        changed['images']['app'] = image
        changed['maintenance_gate'] = True
        new = Engine(self.state, changed)
        with stage('Preparar imagen destino / Prepare target image'):
            new.pull(['app'])
            php = new.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'php', image,
                '-r', 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;']).stdout.strip()
            if php not in required['php']:
                raise Error('La versión de PHP no cumple los requisitos / PHP requirement mismatch')
        archive.preflight(source_engine)
        self.state.begin('migrate' if major else 'update', source=str((self.state.path / 'current').resolve()),
                         target_version=target, target_image=image)
        with self.reopen_on_failure():
            with stage('Respaldo consistente / Consistent backup'):
                stopped = self.quiesce()
                backup = archive.create(stopped, 'pre-update')
                self.state.phase('backed-up', backup=str(backup))
            with stage('Ensayar SQL y aplicación sin archivos de usuarios / Rehearse SQL without user files'):
                self.rehearse(backup, target, image)
            self.state.phase('rehearsed', backup=str(backup))
        revision = self.state.revision(changed)
        new = Engine(self.state, changed, revision)
        new.validate()
        self.state.phase('applying', target=str(revision))
        self.state.activate(revision)
        new.put_managed()
        with stage('Aplicar y verificar / Apply and verify'):
            self.finish()
        self.state.done()
        self.prune_backups()
        return backup

    def restore(self, path, *, supplied=None, accept_user_files=False, recovery=False):
        current = self.state.load()
        operation = self.state.pending
        guarded = (recovery and operation and not operation.get('traffic_opened') and operation.get('backup') == str(Path(path))
                   and (operation['action'] in ('update', 'migrate', 'db-update', 'component-update')
                        or operation.get('accept_user_files') or operation.get('phase') == 'restoring'))
        if not guarded and not accept_user_files:
            raise Error('Los archivos de usuarios no se restauran; confirma su coherencia (--accept-user-files-state) / User files are not restored. Review consistency and use --accept-user-files-state')
        confirm('RESTORE', supplied, 'SQL + aplicación anteriores. Los archivos de usuarios permanecen / User files remain in place')
        with archive.unpack(self.state, path) as (work, manifest):
            saved = manifest['configuration']
            if saved['id'] != current['id']:
                raise Error('El respaldo pertenece a otra instalación / Backup belongs to a different installation')
            estimate = sum(x['bytes'] for x in manifest['files'].values()) + manifest['sql_estimated_bytes'] * 2
            if shutil.disk_usage(self.state.path).free < estimate:
                raise Error('Espacio insuficiente para preparar la restauración / Insufficient restore staging space')
            if not operation:
                self.state.begin('restore', backup=str(Path(path)), accept_user_files=accept_user_files,
                                 source=str((self.state.path / 'current').resolve()))
            source = Engine(self.state)
            if source.status().get('installed') and source.status().get('container') == 'running':
                source.occ('maintenance:mode', '--on', check=False)
            source.stop('cron', 'app', 'proxy')
            cfg = copy.deepcopy(saved)
            cfg['maintenance_gate'] = True
            suffix = secrets.token_hex(4)
            for role in ('app', 'db', 'proxy_data', 'proxy_config'):
                cfg['volumes'][role] = f"ndm_{cfg['id']}_{role}_r{suffix}"
            # Never replace the live user-file volume with an empty volume from an archive.
            cfg['volumes']['data'] = current['volumes']['data']
            cfg['data_device'] = current['data_device']
            if 'data_fsid' in current:
                cfg['data_fsid'] = current['data_fsid']
            else:
                cfg.pop('data_fsid', None)
            revision = self.state.revision(cfg)
            engine = Engine(self.state, cfg, revision)
            engine.pull()
            engine.ensure_volumes()
            engine.check_space(manifest['sql_estimated_bytes'] * 3 + manifest['files']['app.tar']['bytes'] * 2 + 512 * 1024 ** 2)
            archive.restore_volume(engine, cfg['volumes']['app'], work / 'app.tar', '/var/www/html')
            archive.restore_volume(engine, cfg['volumes']['proxy_data'], work / 'proxy.tar', '/data')
            self.state.phase('restoring', target=str(revision), backup=str(Path(path)))
            self.state.activate(revision)
            engine.up('db', 'redis')
            self.wait_database(engine)
            archive.import_sql(engine, work / 'db.sql.gz')
            engine.dc('exec', '-T', 'redis', 'sh', '-ec', 'REDISCLI_AUTH="$NDM_REDIS_PASSWORD" redis-cli FLUSHDB')
            engine.put_managed()
            self.finish()
            self.state.done()
        return cfg['version']

    def recover(self, action, supplied=None, accept_user_files=False):
        operation = self.state.pending
        if not operation:
            raise Error('No hay operación pendiente / No pending operation')
        if action == 'restore':
            if operation['action'] == 'db-migrate':
                return self.rollback_database(supplied, accept_user_files=accept_user_files)
            if not operation.get('backup'):
                raise Error('No hay respaldo de recuperación / No recovery backup available')
            return self.restore(operation['backup'], supplied=supplied, recovery=True, accept_user_files=accept_user_files)
        if operation['phase'] == 'restoring':
            raise Error('Restauración SQL interrumpida: usa Recuperar → Restaurar para repetirla en volúmenes nuevos / Interrupted SQL restore: use recover --action restore to restart into fresh volumes')
        if operation['action'] == 'db-migrate' and operation['phase'] in ('db-initializing', 'db-importing', 'db-rolling-back'):
            raise Error('Importación LTS incompleta: usa Recuperar → Restaurar para volver al volumen SQL original / Incomplete LTS import: use recover --action restore to restart the original SQL volume safely')
        confirm('RESUME', supplied)
        if operation['action'] == 'install':
            engine = Engine(self.state)
            engine.ensure_volumes()
            engine.put_managed()
            engine.up('db', 'redis', 'app', 'proxy', 'cron')
            engine.wait_app(installed=False)
            self.state.done()
            return self.setup_check()
        if operation['action'] in ('backup', 'settings') or operation['phase'] in ('prepared', 'backed-up', 'rehearsed'):
            self.state.activate(operation['source'])
            Engine(self.state).put_managed()
            self.finish()
            self.state.done()
            return
        # For a partly applied migration, resume the SAME target code. Never downgrade SQL.
        engine = Engine(self.state)
        engine.stop('cron', 'app', 'proxy')
        engine.up('db', 'redis', 'app', 'proxy')
        # A healthy HTTP server means the image entrypoint has finished its own upgrade.
        # Only then may an explicit retry be needed for already-copied application code.
        until = time.monotonic() + 900
        while not engine.serving():
            if time.monotonic() > until:
                raise Error('La imagen no terminó de iniciar; revisa los registros de Nextcloud / Image entrypoint is not ready; inspect app logs before retrying')
            time.sleep(3)
        if engine.status().get('needsDbUpgrade'):
            engine.occ('upgrade', '--no-interaction', timeout=1800)
        self.finish()
        self.state.done()

    def prune_backups(self):
        """Keep the newest automatic pre-update backups; manual backups are the user's to delete."""
        keep = self.state.load().get('retention', 3)
        automatic = [path for path in sorted((self.state.path / 'backups').glob('*.ndm.tar'))
                     if archive.backup_kind(path) == 'pre-update']
        for path in automatic[:-keep]:
            path.unlink()
