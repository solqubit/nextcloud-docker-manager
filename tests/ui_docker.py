"""Opt-in real Docker acceptance through the actual terminal installer."""
import json
from pathlib import Path
import secrets
import sys

from ndm.catalog import Catalog
from ndm.core import Runner, State, atomic, read_json
from ndm.dashboard import GROUPS
from ndm.docker import Engine
from integration import Suite
from pty_driver import Driver
from run_integration import cleanup


ROOT = Path('/ndm-state')
STATE = ROOT / 'terminal'


def launch():
    ui = Driver(['/bin/bash', 'start.sh', '--state-dir', str(STATE)], timeout=240)
    try:
        if not (STATE / 'language').exists() and not (STATE / 'current' / 'config.json').exists():
            ui.wait('Idioma / Language')
            ui.send(b'\r', 'Administrar Nextcloud')
        else:
            ui.wait('Administrar Nextcloud')
    except BaseException:
        ui.close()
        raise
    return ui


def password_recovery():
    credentials_path = STATE / 'cache' / 'fixture-credentials.json'
    old = read_json(credentials_path)
    new_password = 'UiReset-' + secrets.token_urlsafe(28)
    ui = launch()
    try:
        shown = '\n'.join(ui.screen.display)
        assert 'Cuentas' in shown and 'Cambiar contraseña' in shown
        ui.capture('live-11-accounts-on-dashboard')
        actions = [key for _, keys in GROUPS for key in keys if key not in ('setup', 'recover')]
        ui.send(b'\x1bOH' + b'\t' * actions.index('reset_password') + b'\r', 'Selecciona una cuenta')
        ui.send(b'\r', 'Nueva contraseña')
        assert old['user'] in '\n'.join(ui.screen.display)
        ui.send(new_password.encode() + b'\r', 'Repite la nueva')
        ui.send(new_password.encode() + b'\r', 'Revisar y confirmar')
        assert old['user'] in '\n'.join(ui.screen.display)
        ui.capture('live-12-confirm-selected-account')
        ui.send(b'\x1bOB\r', 'Contraseña restablecida')
        assert new_password.encode() not in ui.output
        ui.send(b'\r', 'Administrar Nextcloud')
        ui.send(b'\x1bOF\r')
        ui.process.wait(timeout=5)
        assert ui.process.returncode == 0
    finally:
        ui.close()
    state = State(STATE).open()
    try:
        suite = Suite(state)
        path = '/remote.php/dav/files/' + old['user'] + '/'
        assert suite.request(path, 'PROPFIND', authenticated=True, extra={'Depth': '0'})[0] == 401
        atomic(credentials_path, dict(user=old['user'], password=new_password))
        assert suite.request(path, 'PROPFIND', authenticated=True, extra={'Depth': '0'})[0] == 207
        suite.check_file()
        for log in (STATE / 'logs').glob('*.log'):
            assert new_password not in log.read_text(errors='replace')
        assert state.pending is None
        print('[PASS] Dashboard Accounts resets the selected real Nextcloud user: old password rejected, new password accepted, documents/shares retained and no password in UI/logs')
    finally:
        state.lock_file.close()


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
        target = Catalog(ROOT / 'catalog-cache').installation_options()[0]['version']
        active = launch()
        active.send(b'\r', 'Entorno de instalación')
        active.send(b'\x1bOB\r', 'mantenimiento oficial')
        active.send(b'\r', 'Configuración inicial')
        for index, value in ((4, b'18880'), (5, b'18443')):
            active.select(index, 'entre 1 y 65535')
            active.send(b'\x15' + value + b'\r', 'actualizado en el formulario')
        active.select(7, 'Almacenamiento')
        active.send(b'\x1bOB\r', 'actualizado en el formulario')
        active.capture('live-01-install-ready')
        active.send(b'\x1bOF\x1bOA\r', 'Confirmar instalación')
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
        # Reproduce a user creating the web administrator and going straight to Backup.
        suite.prepare(complete_setup=False)
        assert opened.load()['status'] == 'awaiting-web'
        assert opened.load()['initial_maintenance_pending'] is True
        opened.lock_file.close()
        opened = None
        print('[PASS] Real terminal installer, exact official images, local web setup and authenticated WebDAV')
        active = launch()
        assert 'Comprobar instalación web' in '\n'.join(active.screen.display)
        active.capture('live-04-pending-setup')
        # The pending setup action precedes start/stop, so Backups is seventh here.
        active.send(b'\x1bOH' + b'\t' * 6 + b'\r', 'Selecciona un respaldo')
        active.send(b'\r', 'Revisar y confirmar')
        active.send(b'\x1bOB\r', 'Respaldo SQL y aplicación')
        active.read(0.4)
        active.capture('live-05-backup-progress')
        active.wait('Operación completada')
        active.capture('live-06-backup-result')
        checked = read_json(STATE / 'current' / 'config.json')
        assert checked['status'] == 'ready' and checked['initial_maintenance_pending'] is False
        assert not (STATE / 'operation.json').exists()
        print('[PASS] First terminal backup automatically completes pending web verification before archiving')
        active.send(b'\r', 'Selecciona un respaldo')
        active.select(2, 'Detalle del respaldo')
        active.capture('live-07-backup-detail')
        active.send(b'\r', 'Integridad verificada')
        active.send(b'\r', 'Selecciona un respaldo')
        active.select(2, 'Detalle del respaldo')
        # Detail actions: verify, restore, delete, back; End then Up twice reaches Restore.
        active.send(b'\x1bOF\x1bOA\x1bOA\r', 'Revisar y confirmar')
        active.send(b'\x1bOB\r', 'Escribe RESTORE')
        active.send(b'RESTORE\r', 'Restaurar volumen persistente')
        active.capture('live-08-restore-progress')
        active.wait('Operación completada')
        active.capture('live-09-restore-result')
        active.send(b'\r', 'Selecciona un respaldo')
        active.send(b'\x1b', 'Administrar Nextcloud')
        active.capture('live-10-dashboard')
        assert 'Comprobar instalación web' not in '\n'.join(active.screen.display)
        active.send(b'\x1bOF\r')
        active.process.wait(timeout=5)
        assert active.process.returncode == 0
        active.close()
        active = None
        opened = State(STATE).open()
        Suite(opened).check_file()
        Engine(opened).verify()
        Suite(opened).logging_health()
        assert opened.pending is None
        print('[PASS] Real terminal SQL/app backup, verification, guarded restoration, live files/shares retained and return to backup list')
        opened.lock_file.close()
        opened = None
        password_recovery()
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
