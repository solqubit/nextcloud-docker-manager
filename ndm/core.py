"""Private state, atomic revisions, bounded logs and interprocess exclusion."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import functools
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / 'VERSION').read_text().strip()
LABEL = 'com.solqubit.ndm'


class Error(RuntimeError):
    pass


class Cancelled(Error):
    """A user returned from a confirmation without applying its operation."""


_presentation = None


@contextlib.contextmanager
def presentation(view):
    """Bind the terminal adapter without coupling lifecycle code to curses."""
    global _presentation
    previous, _presentation = _presentation, view
    try:
        yield
    finally:
        _presentation = previous


def progress_values(**values):
    """Optional human-readable metrics; never pass credentials or raw argv."""
    if _presentation is not None:
        _presentation.values(values)


@contextlib.contextmanager
def visual_stage(message):
    """Additional presentation must not change machine-readable CLI output."""
    if _presentation is None:
        yield
    else:
        with _presentation.stage(message):
            yield


def step(message):
    """Present an existing synchronous operation as a timed lifecycle step."""
    def decorate(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            with visual_stage(message):
                return function(*args, **kwargs)
        return run
    return decorate


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Error(f'Cannot read configuration / No se puede leer: {path}') from exc


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '-' + secrets.token_hex(4))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        tmp.unlink(missing_ok=True)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def version_tuple(value):
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)\.[0-9]+\.[0-9]+', value):
        raise Error('Use an exact stable version / Usa una versión estable exacta: X.Y.Z')
    return tuple(map(int, value.split('.')))


def human(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.1f} {unit}'
        size /= 1024


def confirm(expected, supplied=None, message=''):
    if supplied == expected:
        return
    if supplied is not None or not sys.stdin.isatty():
        raise Error(f'Confirmation required / Confirmación requerida: --confirm {expected}')
    if _presentation is not None:
        return _presentation.confirm(expected, message)
    print(message or expected)
    if input(f'Escribe / Type {expected} (Enter = cancelar): ').strip() != expected:
        raise Error('Cancelado / Cancelled')


class State:
    def __init__(self, path):
        selected = Path(path).expanduser().absolute()
        self.path = selected.resolve()
        protected = {'/', '/opt', '/etc', '/usr', '/usr/local', '/var', '/var/lib', '/var/lib/docker',
                     '/var/log', '/var/tmp', '/home', '/root', '/mnt', '/media', '/tmp', '/srv', '/run', '/dev', '/proc', '/sys', '/boot'}
        if str(self.path) in protected or selected.is_symlink() or any(ord(c) < 32 for c in str(self.path)):
            raise Error('Invalid state path / Ruta de estado inválida')
        self.log = None
        self.lock_file = None

    def open(self):
        import fcntl
        if self.path.exists():
            names = {p.name for p in self.path.iterdir()}
            marker = self.path / 'owner.json'
            legacy = {'manager.lock', 'revisions', 'backups', 'logs', 'cache'} <= names
            if marker.exists():
                if read_json(marker) != {'application': 'solqubit.nextcloud-docker-manager', 'format': 1}:
                    raise Error('State directory belongs to another application')
            elif names and not legacy and names != {'data'}:
                raise Error('Choose a new empty state directory; existing files were preserved')
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path, 0o700)
        atomic(self.path / 'owner.json', {'application': 'solqubit.nextcloud-docker-manager', 'format': 1})
        for part in ('revisions', 'backups', 'logs', 'cache'):
            folder = self.path / part
            if folder.is_symlink():
                raise Error(f'Unsafe state directory: {folder}')
            folder.mkdir(mode=0o700, exist_ok=True)
        self.lock_file = (self.path / 'manager.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error('Otra operación está en ejecución / Another operation is running') from exc
        self.log = self.path / 'logs' / f'{stamp()}-{secrets.token_hex(3)}.log'
        self.log.touch(mode=0o600)
        return self

    @property
    def log_path(self):
        return self.log

    @property
    def configured(self):
        return (self.path / 'current').is_symlink()

    def load(self):
        if not self.configured:
            raise Error('No hay instalación configurada / No configured installation')
        target = (self.path / 'current').resolve()
        if target.parent != (self.path / 'revisions').resolve():
            raise Error('Invalid revision path')
        from .config import validate
        cfg = read_json(target / 'config.json')
        try:
            validate(cfg)
        except (KeyError, TypeError, ValueError) as exc:
            raise Error('Invalid configuration schema; original file preserved') from exc
        return cfg

    def revision(self, cfg):
        from .config import compose, validate
        validate(cfg)
        folder = self.path / 'revisions' / (stamp() + '-' + secrets.token_hex(4))
        folder.mkdir(mode=0o700)
        atomic(folder / 'config.json', cfg)
        atomic(folder / 'compose.json', compose(cfg))
        return folder

    def activate(self, folder):
        folder = Path(folder).resolve()
        if folder.parent != (self.path / 'revisions').resolve():
            raise Error('Invalid revision')
        tmp = self.path / '.current-new'
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(Path('revisions') / folder.name)
        os.replace(tmp, self.path / 'current')

    def begin(self, action, **extra):
        if self.pending:
            raise Error('Operación pendiente: usa recover / Pending operation: use recover')
        self.journal({'action': action, 'phase': 'prepared', 'started': stamp(), **extra})

    def journal(self, value):
        atomic(self.path / 'operation.json', value)

    @property
    def pending(self):
        path = self.path / 'operation.json'
        return read_json(path) if path.exists() else None

    def phase(self, phase, **extra):
        value = self.pending
        value.update(phase=phase, **extra)
        self.journal(value)

    def done(self):
        value = self.pending
        if value:
            value.update(completed=stamp())
            with (self.path / 'history.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps(value) + '\n')
            (self.path / 'operation.json').unlink()

    def installed_code(self, source=None):
        """The installed manager remains usable after deleting the downloaded checkout."""
        folder = self.path / 'manager'
        source = Path(source) if source else ROOT
        if source.resolve() == folder.resolve():
            return
        stage = self.path / ('.manager-' + secrets.token_hex(4))
        stage.mkdir(mode=0o700)
        for item in ('ndm', 'locales', 'VERSION', 'LICENSE', 'start.sh'):
            src = source / item
            if src.is_dir():
                shutil.copytree(src, stage / item, ignore=shutil.ignore_patterns('__pycache__'))
            else:
                shutil.copy2(src, stage / item)
        previous = self.path / 'manager-previous'
        if previous.exists():
            shutil.rmtree(previous)
        if folder.exists():
            folder.rename(previous)
        stage.rename(folder)
        atomic(self.path / 'start.sh', '#!/usr/bin/env bash\nset -e\nexec bash "$(dirname -- "$0")/manager/start.sh" --state-dir "$(dirname -- "$0")" "$@"\n', 0o700)


class Runner:
    def __init__(self, state=None):
        self.state = state

    def run(self, args, *, check=True, input=None, timeout=600, quiet=False):
        if _presentation is not None:
            _presentation.command(args)
        # Never shell=True; command data and credentials are never evaluated as code.
        try:
            result = subprocess.run([str(x) for x in args], input=input, capture_output=True,
                                    timeout=timeout, text=not isinstance(input, bytes))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error(f'Command failed / Falló el comando: {args[0]} ({type(exc).__name__})') from exc
        stdout = result.stdout.decode(errors='replace') if isinstance(result.stdout, bytes) else result.stdout
        stderr = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
        if self.state and self.state.log:
            # Arguments may contain environment values; record output, not argv.
            with self.state.log.open('a', encoding='utf-8') as out:
                out.write(stdout + stderr)
        if check and result.returncode:
            detail = (stderr or stdout)[-2200:]
            raise Error(f'{args[0]}: {detail}')
        return subprocess.CompletedProcess(args, result.returncode, stdout, stderr)


@contextlib.contextmanager
def stage(message):
    if _presentation is not None:
        with _presentation.stage(message):
            yield
        return
    start = time.monotonic()
    print(f'[*] {message}', flush=True)
    try:
        yield
    except BaseException:
        print(f'[!] {message}: ERROR ({time.monotonic() - start:.0f}s)', flush=True)
        raise
    print(f'[+] {message} ({time.monotonic() - start:.0f}s)', flush=True)
