"""Logical migration between maintained MariaDB LTS branches; original SQL stays intact."""
import copy
import json
import secrets

from . import archive
from .core import Error, confirm, version_tuple
from .docker import Engine


class DatabaseMigration:
    def database_choices(self, for_nextcloud=None):
        cfg = self.state.load()
        current = '.'.join(cfg['images']['db'].split(':', 1)[1].split('@')[0].split('.')[:2])
        if not for_nextcloud:
            raise Error('Indica la versión de Nextcloud que exige otra LTS de MariaDB / Specify the Nextcloud upgrade that requires a different MariaDB LTS')
        if version_tuple(for_nextcloud)[0] != version_tuple(cfg['version'])[0] + 1:
            raise Error('El cambio de base solo se prepara para la siguiente versión mayor / Database preparation is only for the next sequential Nextcloud major')
        allowed = set(self.catalog.requirements(version_tuple(cfg['version'])[0])['mariadb'])
        target_allowed = set(self.catalog.requirements(version_tuple(for_nextcloud)[0])['mariadb'])
        required = current not in target_allowed
        policy = self.catalog.maintained_databases()
        allowed &= target_allowed
        choices = [{'series': series, 'community_until': end} for series, end in policy['branches'].items()
                   if required and series in allowed and version_tuple(series + '.0') > version_tuple(current + '.0')]
        return {'current': current, 'community_until': policy['branches'].get(current),
                'source': policy['source'], 'required': required, 'choices': choices, 'for_nextcloud': for_nextcloud}

    def migrate_database(self, target, *, for_nextcloud=None, supplied=None):
        original = self.ready()
        cfg = original.cfg
        choices = self.database_choices(for_nextcloud)
        if not choices['required']:
            raise Error('La MariaDB actual ya cumple los requisitos; no se permite cambiar de LTS / Current MariaDB already meets the requested Nextcloud requirements; LTS change is not permitted')
        if self.catalog.patches(version_tuple(cfg['version'])[0])[0] != cfg['version']:
            raise Error('Aplica primero el último parche de la rama actual / Install the latest patch of the current Nextcloud major before preparing its upgrade')
        nextcloud_image = self.catalog.resolve('nextcloud', for_nextcloud + '-apache')
        if target not in [entry['series'] for entry in choices['choices']]:
            raise Error('Elige una LTS posterior compatible con ambas versiones de Nextcloud / Choose a later maintained LTS compatible with the current and requested Nextcloud versions')
        image = self.catalog.resolve('mariadb', self.catalog.latest('mariadb', target))
        self.catalog.compatible(cfg['version'], image)
        if for_nextcloud:
            self.catalog.compatible(for_nextcloud, image)
        print(json.dumps(choices, ensure_ascii=False, indent=2))
        confirm('MIGRATE-DB-' + target, supplied, 'SQL a un volumen NUEVO; archivos de usuarios sin cambios / SQL into a NEW volume; user files unchanged')
        changed = copy.deepcopy(cfg)
        changed['images']['db'] = image
        changed['maintenance_gate'] = True
        changed['volumes']['db'] = cfg['project'] + '_db_lts' + secrets.token_hex(4)
        revision = self.state.revision(changed)
        proposed = Engine(self.state, changed, revision)
        proposed.pull(['db'])
        proposed.validate()
        archive.preflight(original)
        required_space = original.db_size() * 3 + 512 * 1024 ** 2
        self.state.begin('db-migrate', source=str((self.state.path / 'current').resolve()),
                         target=str(revision), database_series=target, for_nextcloud=for_nextcloud,
                         for_nextcloud_image=nextcloud_image)
        # Until the original SQL engine stops, a failure reopens the unchanged service.
        with self.reopen_on_failure():
            path = archive.create(self.quiesce(), 'pre-update')
            self.state.phase('backed-up', backup=str(path))
            self.rehearse(path, cfg['version'], app_image=cfg['images']['app'], components={'db': image})
            self.state.phase('rehearsed')
            proposed.ensure_volumes()
            proposed.check_space(required_space)
        # No target-engine process is ever given the original SQL directory.
        self.state.phase('db-initializing')
        original.stop('db')
        self.state.activate(revision)
        proposed.up('db', 'redis')
        self.wait_database(proposed)
        self.state.phase('db-importing')
        with archive.unpack(self.state, path) as (work, _):
            archive.import_sql(proposed, work / 'db.sql.gz')
        proposed.dc('exec', '-T', 'redis', 'sh', '-ec', 'REDISCLI_AUTH="$NDM_REDIS_PASSWORD" redis-cli FLUSHDB')
        self.state.phase('db-imported')
        proposed.put_managed()
        self.finish()
        self.state.done()
        self.prune_backups()
        return {'database': image, 'previous_sql_volume': cfg['volumes']['db'], 'backup': str(path)}

    def rollback_database(self, supplied=None, accept_user_files=False):
        operation = self.state.pending
        if not operation or operation['action'] != 'db-migrate':
            raise Error('No hay un cambio de LTS pendiente / No pending LTS database migration')
        if operation.get('traffic_opened') and not accept_user_files:
            raise Error('Puede que el tráfico se reabriera; confirma la coherencia de los archivos (--accept-user-files-state) / Traffic may have reopened; review user-file consistency and use --accept-user-files-state')
        confirm('RESTORE', supplied, 'Recuperar SQL y motor originales / Restart the ORIGINAL SQL volume with its ORIGINAL engine')
        from .core import read_json
        source = operation['source']
        cfg = read_json(__import__('pathlib').Path(source) / 'config.json')
        engine = Engine(self.state, cfg, source)
        engine.owned(engine.inspect('volume', cfg['volumes']['db']) or {}, 'volume')
        if self.state.load()['images']['app'] != cfg['images']['app']:
            raise Error('La imagen cambió fuera de esta operación; usa el respaldo SQL/aplicación correspondiente / Application image changed outside this operation; use the matching SQL/application backup')
        self.state.phase('db-rolling-back')
        Engine(self.state).stop('cron', 'app', 'proxy', 'db')
        self.state.activate(source)
        engine.put_managed()
        # Locks and cache written while the new SQL engine served traffic no longer apply.
        engine.up('redis')
        engine.dc('exec', '-T', 'redis', 'sh', '-ec', 'REDISCLI_AUTH="$NDM_REDIS_PASSWORD" redis-cli FLUSHDB')
        self.finish()
        self.state.phase('rolled-back')
        self.state.done()
        return {'restored_database': cfg['images']['db'], 'original_sql_volume': cfg['volumes']['db']}
