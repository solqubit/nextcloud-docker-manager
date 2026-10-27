"""Opt-in real Docker acceptance through the actual terminal installer."""
import json
from pathlib import Path
import sys

from ndm.catalog import Catalog
from ndm.core import Runner, State, read_json
from ndm.docker import Engine
from integration import Suite
from pty_driver import Driver
from run_integration import cleanup


ROOT = Path('/ndm-state')
STATE = ROOT / 'terminal'


def launch():
    ui = Driver(['/bin/bash', 'start.sh', '--state-dir', str(STATE)], timeout=240)
    ui.wait('Administrar Nextcloud')
    return ui


def main():
    if not ROOT.is_mount() or any(ROOT.iterdir()):
        raise SystemExit('Use a NEW dedicated test volume mounted at /ndm-state')
    runner = Runner()
    existing = runner.run(['docker', 'ps', '--quiet']).stdout.split()
    records = json.loads(runner.run(['docker', 'container', 'inspect', *existing]).stdout) if existing else []
    # Parallel unit runners can finish naturally while this suite is active.
    # Keep the isolation assertion on every unrelated non-test container.
    previous = [item['Id'] for item in records if item['Config'].get('Image') != 'ndm-test-runner:local'
                and not (item['Config'].get('Labels') or {}).get('com.solqubit.ndm.test')]
    active, opened = None, None
    try:
        versions = Catalog(ROOT / 'catalog-cache').branches()
        target = versions[0]
        active = launch()
        active.send(b'\r', 'Entorno de instalación')
        active.send(b'\x1bOB\r', 'Versiones estables oficiales')
        active.send(b'\r', 'Configuración inicial')
        for index, value in ((4, b'18880'), (5, b'18443')):
            active.select(index, 'entre 1 y 65535')
            active.send(b'\x15' + value + b'\r', 'actualizado en el formulario')
        active.select(7, 'Almacenamiento')
        active.send(b'\x1bOB\r', 'actualizado en el formulario')
        active.send(b'\x1bOF\x1bOA\r', 'Comprueba los valores')
        active.capture('live-01-install-review')
        active.send(b'\r', 'Revisar y confirmar')
        active.send(b'\x1bOB\r', 'Iniciar instalación local')
        active.capture('live-02-install-progress')
        active.wait('crea las credenciales')
        active.capture('live-03-web-setup')
        active.send(b'\r', 'Administrar Nextcloud')
        active.send(b'\x1bOF\r')
        active.process.wait(timeout=5)
        assert active.process.returncode == 0
        active.close()
        active = None
        opened = State(STATE).open()
        cfg = opened.load()
        assert cfg['version'] == target and cfg['storage'] == 'named' and cfg['bind'] == '127.0.0.1'
        assert not Engine(opened).status()['installed']
        suite = Suite(opened)
        suite.prepare()
        opened.lock_file.close()
        opened = None
        print('[PASS] Real terminal installer, exact official images, local web setup and authenticated WebDAV')
        active = launch()
        active.send(b'\r', 'Configuración web')
        active.capture('live-04-services')
        active.send(b'\x1b', 'Administrar Nextcloud')
        active.send(b'\x1bOH\x1bOC\r', 'Revisar y confirmar')
        active.send(b'\x1bOB\r', 'Respaldo SQL y aplicación')
        active.read(0.4)
        active.capture('live-05-backup-progress')
        active.wait('Operación completada')
        active.capture('live-06-backup-result')
        active.send(b'\r', 'Administrar Nextcloud')
        active.send(b'\x1bOH' + b'\t' * 10 + b'\r', 'Selecciona un respaldo')
        active.select(2, 'Detalle del respaldo')
        active.capture('live-07-backup-detail')
        active.send(b'\r', 'Integridad verificada')
        active.send(b'\r', 'Selecciona un respaldo')
        active.select(2, 'Detalle del respaldo')
        active.send(b'\x1bOF\x1bOA\r', 'Revisar y confirmar')
        active.send(b'\x1bOB\r', 'Escribe RESTORE')
        active.send(b'RESTORE\r', 'Restaurar volumen persistente')
        active.capture('live-08-restore-progress')
        active.wait('Operación completada')
        active.capture('live-09-restore-result')
        active.send(b'\r', 'Selecciona un respaldo')
        active.send(b'\x1b', 'Administrar Nextcloud')
        active.capture('live-10-dashboard')
        active.send(b'\x1bOF\r')
        active.process.wait(timeout=5)
        assert active.process.returncode == 0
        active.close()
        active = None
        opened = State(STATE).open()
        Suite(opened).check_file()
        Engine(opened).verify()
        assert opened.pending is None
        print('[PASS] Real terminal SQL/app backup, verification, guarded restoration, live files/shares retained and return to backup list')
        print(json.dumps({'nextcloud': target, 'services': 5, 'backup_restore': 'verified', 'user_files': 'retained'}))
    finally:
        if active:
            active.close()
        if opened:
            opened.lock_file.close()
        cleanup(ROOT)
        current = Runner().run(['docker', 'ps', '--quiet', '--no-trunc']).stdout.split()
        assert set(previous) <= set(current), 'An unrelated running container was disturbed'


if __name__ == '__main__':
    main()
