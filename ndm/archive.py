"""Streamed SQL/application backups. No traversal of the user-file volume."""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading

from .core import Error, atomic, human, progress_values, sha, stamp, step, visual_stage

FILES = {'manifest.json', 'db.sql.gz', 'app.tar', 'proxy.tar'}


def members(archive, *, application=False):
    seen = set()
    total = 0
    items = archive.getmembers()
    if len(items) > 150000:
        raise Error('Too many archive entries')
    for item in items:
        path = PurePosixPath(item.name)
        if (path.is_absolute() or '..' in path.parts or '\\' in item.name or '\x00' in item.name
                or any(ord(c) < 32 for c in item.name) or not (item.isdir() or item.isfile() or item.issym() or item.islnk())
                or str(path) in seen):
            raise Error('Unsafe archive entry: ' + repr(item.name))
        seen.add(str(path))
        if item.issym() or item.islnk():
            target = item.linkname
            resolved = PurePosixPath(posixpath.normpath(posixpath.join(str(path.parent) if item.issym() else '', target)))
            if (PurePosixPath(target).is_absolute() or '\\' in target or any(ord(c) < 32 for c in target)
                    or '..' in resolved.parts or (application and resolved.parts and resolved.parts[0] == 'data')):
                raise Error('Unsafe archive link: ' + repr(item.name))
        if application and path.parts and path.parts[0] == 'data':
            raise Error('User data must not be included in an application backup')
        if getattr(item, 'sparse', None):
            raise Error('Sparse archive entries are not accepted')
        total += item.size
        if total > 2 ** 44:
            raise Error('Archive expanded size exceeds supported limit')
    index = {str(PurePosixPath(item.name)): item for item in items}
    for item in items:
        if item.issym() or item.islnk():
            target = item
            visited = set()
            while target.issym() or target.islnk():
                resolved = posixpath.normpath(posixpath.join(str(PurePosixPath(target.name).parent) if target.issym() else '', target.linkname))
                if resolved in visited or resolved not in index or len(visited) > 40:
                    raise Error('Archive link has a missing/cyclic target: ' + item.name)
                visited.add(resolved)
                target = index[resolved]
            if not target.isfile():
                raise Error('Archive link must resolve to a regular file: ' + item.name)
    return items


def stream_out(args, path, *, compress=False, timeout=3600):
    """Bound memory; avoid password argv and ensure interrupted children exit."""
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(x) for x in args], stdout=subprocess.PIPE, stderr=errors)
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            with (gzip.open(path, 'wb') if compress else Path(path).open('wb')) as target:
                shutil.copyfileobj(process.stdout, target, length=1024 * 1024)
            process.stdout.close()
            if process.wait() != 0:
                errors.seek(0)
                raise Error(errors.read(2200).decode(errors='replace') or 'Backup stream failed')
            os.chmod(path, 0o600)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()


def extract_checked(path, destination):
    try:
        return _extract_checked(path, destination)
    except (tarfile.TarError, EOFError, KeyError, TypeError, ValueError) as exc:
        raise Error('Respaldo inválido o incompleto / Invalid or incomplete backup') from exc


def _extract_checked(path, destination):
    with tarfile.open(path, 'r:') as archive:
        items = members(archive)
        if {m.name for m in items} != FILES or any(not m.isfile() for m in items):
            raise Error('Invalid backup members')
        if next(m for m in items if m.name == 'manifest.json').size > 1024 * 1024:
            raise Error('Backup manifest exceeds supported size')
        # Python < 3.11.4 lacks extraction filters; members() above already admits only
        # the four expected regular files, so the plain extraction is equivalent there.
        archive.extractall(destination, members=items, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    manifest = json.loads((Path(destination) / 'manifest.json').read_text())
    if manifest.get('format') != 1:
        raise Error('Unsupported backup format')
    from .config import validate
    validate(manifest['configuration'])
    for name in FILES - {'manifest.json'}:
        item = Path(destination) / name
        expected = manifest['files'].get(name, {})
        if item.stat().st_size != expected.get('bytes') or sha(item) != expected.get('sha256'):
            raise Error('Backup checksum mismatch: ' + name)
    for name in ('app.tar', 'proxy.tar'):
        with tarfile.open(Path(destination) / name) as inner:
            members(inner, application=name == 'app.tar')
    # Validate the gzip checksum and SQL stream without loading it into memory.
    with gzip.open(Path(destination) / 'db.sql.gz', 'rb') as sql:
        if not sql.read(100):
            raise Error('Empty SQL backup')
        for _ in iter(lambda: sql.read(1024 * 1024), b''):
            pass
    return manifest


@contextlib.contextmanager
def unpack(state, path):
    path = Path(path).absolute()
    if not path.is_file() or path.is_symlink():
        raise Error('El respaldo no es un archivo normal / Backup is not a regular file')
    if shutil.disk_usage(state.path / 'backups').free < path.stat().st_size + 32 * 1024 ** 2:
        raise Error('Espacio insuficiente para verificar el respaldo / Insufficient staging space to verify the backup')
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=state.path / 'backups') as work:
        with visual_stage('Verificar integridad del respaldo / Verify backup integrity'):
            manifest = extract_checked(path, work)
        yield Path(work), manifest


def backup_kind(path):
    """Manifest kind of a published backup, or None when it cannot be read."""
    try:
        with tarfile.open(path, 'r:') as archive:
            member = archive.getmember('manifest.json')
            if not member.isfile() or member.size > 1024 * 1024:
                return None
            return json.load(archive.extractfile(member)).get('kind')
    except (OSError, tarfile.TarError, KeyError, ValueError, AttributeError):
        return None


def clean_leftovers(state):
    """Remove staging left by a killed process. The state lock guarantees no other writer."""
    folder = state.path / 'backups'
    for path in folder.glob('*.partial'):
        if path.is_file() and not path.is_symlink():
            path.unlink()
    for path in [*folder.glob('.backup-*'), *folder.glob('.verify-*')]:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def preflight(engine):
    """Space needed by create(); checked while Nextcloud is still online."""
    clean_leftovers(engine.state)
    estimated = engine.db_size()
    app_size = engine.app_size()
    # Stream, completed package and validation extraction coexist at the peak.
    budget = max(estimated * 6, 128 * 1024 * 1024) + app_size * 3 + 256 * 1024 * 1024
    free = shutil.disk_usage(engine.state.path).free
    if free < budget:
        raise Error(f'Espacio insuficiente para el respaldo: {human(free)} libres, {human(budget)} necesarios / '
                    f'Insufficient backup space: {human(free)} free, {human(budget)} required')
    return estimated, app_size, budget, free


def create(engine, kind='manual'):
    """Caller quiesces app/cron/proxy; SQL remains available for a consistent dump."""
    state, cfg = engine.state, engine.cfg
    estimated, app_size, budget, free = preflight(engine)
    print(f'SQL estimado / estimated: {human(estimated)}; aplicación / application: {human(app_size)}; '
          f'espacio de trabajo / working budget: {human(budget)}', flush=True)
    progress_values(sql_estimated_bytes=human(estimated), application_size=human(app_size),
                    working_budget=human(budget), disk_free=human(free))
    identity = stamp() + '-' + secrets.token_hex(4)
    destination = state.path / 'backups' / (identity + '.ndm.tar')
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=state.path / 'backups') as work:
        work = Path(work)
        cid = engine.container('app')
        dump = ('MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot '
                '--single-transaction --quick --routines --events --triggers --hex-blob '
                '--skip-lock-tables nextcloud')
        sql_args = ['docker', 'compose', '-p', cfg['project'], '-f', engine.revision / 'compose.json',
                    'exec', '-T', 'db', 'sh', '-ec', dump]
        # Large databases need more than an hour: allow about 2 MiB/s of SQL.
        stream_out(sql_args, work / 'db.sql.gz', compress=True, timeout=max(3600, estimated // (2 * 1024 * 1024)))
        stream_out(['docker', 'run', '--rm', '--network', 'none', '--volumes-from', cid + ':ro',
                    '--entrypoint', 'tar', cfg['images']['app'], '-C', '/var/www/html',
                    '--exclude=./data', '--exclude=./nextcloud-init-sync.lock', '-cf', '-', '.'], work / 'app.tar')
        stream_out(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'tar',
                    '--mount', 'type=volume,src=' + cfg['volumes']['proxy_data'] + ',dst=/data,readonly',
                    cfg['images']['proxy'], '-C', '/data', '-cf', '-', '.'], work / 'proxy.tar')
        manifest = {'format': 1, 'id': identity, 'created': stamp(), 'kind': kind,
                    'configuration': cfg, 'excluded': ['/var/www/html/data', 'external user storage'],
                    'enabled_apps': (state.pending or {}).get('enabled_apps', {}),
                    'sql_estimated_bytes': estimated,
                    'files': {name: {'bytes': (work / name).stat().st_size, 'sha256': sha(work / name)}
                              for name in FILES - {'manifest.json'}}}
        atomic(work / 'manifest.json', manifest)
        temp = destination.with_suffix('.partial')
        try:
            with tarfile.open(temp, 'w') as archive:
                for name in sorted(FILES):
                    archive.add(work / name, arcname=name, recursive=False)
            os.chmod(temp, 0o600)
            # Test the same parser used during restoration before publishing the backup.
            with tempfile.TemporaryDirectory(dir=work) as verify:
                extract_checked(temp, verify)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
    print(f'[+] Respaldo verificado / Verified backup: {destination.name} ({human(destination.stat().st_size)})', flush=True)
    return destination


@step('Importar SQL en el volumen nuevo / Import SQL into the new volume')
def import_sql(engine, path):
    args = ['docker', 'compose', '-p', engine.cfg['project'], '-f', engine.revision / 'compose.json',
            'exec', '-T', 'db', 'sh', '-ec',
            'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot nextcloud']
    # Allow roughly 1 MiB/s of compressed SQL, never less than an hour.
    limit = max(3600, Path(path).stat().st_size // (1024 * 1024))
    with tempfile.TemporaryFile() as errors, gzip.open(path, 'rb') as source:
        process = subprocess.Popen([str(x) for x in args], stdin=subprocess.PIPE, stdout=errors, stderr=errors)
        timer = threading.Timer(limit, process.kill)
        timer.start()
        try:
            try:
                shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
                process.stdin.close()
            except BrokenPipeError:
                # mariadb stopped at an SQL error; its stderr below explains why.
                pass
            if process.wait() != 0:
                errors.seek(0)
                detail = errors.read(2200).decode(errors='replace')
                raise Error(f'Falló la importación SQL: {detail} / SQL restore failed: {detail}')
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()


@step('Restaurar volumen persistente / Restore persistent volume')
def restore_volume(engine, name, path, mount):
    with Path(path).open('rb') as source, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'tar',
            '--mount', 'type=volume,src=' + name + ',dst=' + mount,
            engine.cfg['images']['app'], '-xpf', '-', '-C', mount], stdin=source, stdout=errors, stderr=errors)
        try:
            code = process.wait(timeout=max(1800, Path(path).stat().st_size // (1024 * 1024)))
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise Error('La restauración del volumen superó el tiempo límite / Volume restore timed out') from exc
        if code != 0:
            errors.seek(0)
            detail = errors.read(2200).decode(errors='replace')
            raise Error(f'Falló la restauración de la aplicación: {detail} / Application restore failed: {detail}')
