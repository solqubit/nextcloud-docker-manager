"""End-to-end keyboard checks of the actual controller in an 80×24 terminal."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest

from pty_driver import Driver


ROOT_ACTIONS = ['status', 'start', 'stop', 'logs', 'updates', 'backups', 'domains', 'language',
                'maintenance', 'diagnose', 'self_update', 'exit']


class WorkflowTests(unittest.TestCase):
    def launch(self, mode='configured', **kwargs):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        state = Path(work.name) / 'state'
        driver = Driver([sys.executable, '-u', 'tests/ui_fixture.py', str(state), mode], **kwargs)
        self.addCleanup(driver.close)
        driver.state = state
        driver.wait('Manage Nextcloud' if mode in ('english', 'empty-english') else 'Administrar Nextcloud')
        return driver

    def calls(self, ui):
        path = ui.state / 'ui-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def root(self, ui, action, expect):
        return ui.send(b'\x1bOH' + b'\t' * ROOT_ACTIONS.index(action) + b'\r', expect)

    def form_review(self, ui, expect='Comprueba los valores'):
        ui.send(b'\x1bOF\x1bOA\r', expect)

    def accept(self, ui, expect):
        return ui.send(b'\x1bOB\r', expect)

    def wizard(self, ui):
        ui.send(b'\r', 'Entorno de instalación')
        ui.send(b'\x1bOB\r', 'mantenimiento oficial')
        ui.capture('01-version-picker')
        ui.send(b'\r', 'Configuración inicial')

    def test_wizard_accepts_edit_highlights_busy_port_and_cancels_without_install(self):
        ui = self.launch('empty')
        ui.capture('00-welcome')
        self.wizard(ui)
        ui.capture('02-install-form')
        self.assertNotIn('Revisar configuración', '\n'.join(ui.screen.display))
        self.assertIn('Instalar Nextcloud', '\n'.join(ui.screen.display))
        ui.select(4, 'Puerto HTTP entre')
        ui.send(b'\x158088\r', 'actualizado en el formulario')
        output = ui.send(b'\x1bOF\x1bOA\r', 'Puerto ocupado por otro contenedor')
        self.assertIn(b'\x1b[31m', output)
        ui.capture('03-busy-port')
        ui.select(4, 'Puerto HTTP entre')
        ui.send(b'\x1518880\r', 'actualizado en el formulario')
        ui.capture('04-install-ready')
        ui.send(b'\x1bOF\x1bOA\r', 'Confirmar instalación')
        self.assertIn('Instalar ahora', '\n'.join(ui.screen.display))
        self.assertEqual(self.calls(ui), [])
        ui.capture('05-install-confirm')
        ui.send(b'\x1b', 'Instalación cancelada')
        self.assertIn('18880', '\n'.join(ui.screen.display))
        ui.capture('35-install-back-to-form')
        self.assertFalse((ui.state / 'current').exists())
        self.assertEqual(self.calls(ui), [])
        ui.send(b'\x1b', '¿Descartar los cambios')
        self.accept(ui, 'Administrar Nextcloud')

    def test_install_spinner_results_and_web_setup_flow(self):
        ui = self.launch('empty')
        self.wizard(ui)
        ui.send(b'\x1bOF\x1bOA\r', 'Confirmar instalación')
        start = len(ui.output)
        self.accept(ui, 'Descargar imágenes')
        ui.read(0.15)
        ui.capture('06-install-progress')
        ui.wait('crea las credenciales', start=start)
        ui.capture('07-web-setup-result')
        self.assertGreaterEqual(len({frame for frame in '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' if frame.encode() in ui.output[start:]}), 3)
        self.assertNotIn(b'PRIVATE-DIAGNOSTIC-NOT-A-UI-LABEL', ui.output)
        cfg = json.loads((ui.state / 'current' / 'config.json').read_text())
        for secret in (cfg[key] for key in ('db_password', 'root_password', 'redis_password')):
            self.assertNotIn(secret.encode(), ui.output)
        self.assertEqual(cfg['environment'], 'vps')
        self.assertEqual(cfg['bind'], '127.0.0.1')
        self.assertEqual((cfg['http_port'], cfg['https_port']), (80, 443))
        self.assertIn(b'ssh -L 8080:127.0.0.1:80', ui.output)
        ui.send(b'\r', 'Administrar Nextcloud')
        # The pending web-setup check is offered and preselected right after installing.
        self.assertIn('Comprobar instalación web', '\n'.join(ui.screen.display))
        ui.send(b'\r', 'Operación completada')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertNotIn('Comprobar instalación web', '\n'.join(ui.screen.display))
        self.root(ui, 'status', 'Configuración web')
        ui.capture('08-service-status')
        self.assertEqual([call['action'] for call in self.calls(ui)], ['install', 'setup'])

    def test_timezone_picker_draft_and_discard_are_keyboard_only(self):
        ui = self.launch('empty')
        self.wizard(ui)
        ui.select(6, 'Zona horaria: región')
        ui.send(b'America\r', 'Zona horaria: ciudad')
        ui.send(b'Guayaquil\r', 'actualizado en el formulario')
        ui.capture('09-timezone-selected')
        ui.send(b'\x1b', '¿Descartar los cambios')
        ui.send(b'\x1b', 'Configuración inicial')
        ui.send(b'\x1b', '¿Descartar los cambios')
        self.accept(ui, 'Administrar Nextcloud')
        self.assertFalse((ui.state / 'current').exists())

    def test_dashboard_columns_status_logs_and_scrolling(self):
        ui = self.launch()
        ui.capture('10-dashboard')
        self.root(ui, 'status', 'Configuración web')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.root(ui, 'logs', 'Registros')
        ui.send(b'\r', '219')
        ui.capture('11-logs-newest')
        ui.send(b'\x1bOH', '000')
        ui.send(b'\x1bOF', '219')
        # Logs return to the service list, not to the dashboard.
        ui.send(b'\x1b', 'Muestra las últimas 200')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        ui.send(b'\x1bOH\x1bOC\r', 'Principal:')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertEqual(self.calls(ui), [])

    def test_tunnel_form_valid_name_and_return_to_domains(self):
        ui = self.launch()
        self.root(ui, 'domains', 'Principal:')
        ui.capture('36-domain-access-menu')
        ui.select(2, 'El primer dominio')
        ui.send(b'\r', 'IP o dominio sin')
        ui.send(b'cloud.example.com\r', 'actualizado en el formulario')
        ui.send(b'\x1bOF\x1bOA\r', 'Revisar y confirmar')
        self.accept(ui, 'Dominio guardado como principal')
        ui.capture('12-tunnel-result')
        ui.send(b'\r', 'Principal:')
        ui.capture('37-domain-access-public')
        cfg = json.loads((ui.state / 'current' / 'config.json').read_text())
        self.assertEqual([entry['host'] for entry in cfg['domains']], ['127.0.0.1', 'cloud.example.com'])
        self.assertEqual(cfg['primary'], 'cloud.example.com')
        # The result notice now precedes the summary, so lines may wrap differently.
        shown = ' '.join(' '.join(ui.screen.display).split())
        self.assertIn('Principal: https://cloud.example.com', shown)
        self.assertIn('Local: http://127.0.0.1:8080', shown)
        self.assertEqual(self.calls(ui)[0]['action'], 'add_domain')
        ui.send(b'\x1b', 'Administrar Nextcloud')

    def test_patch_and_major_keep_database_migration_conditional(self):
        ui = self.launch()
        self.root(ui, 'updates', 'Se hará un ensayo')
        self.assertIn('después de aplicar el último parche', '\n'.join(ui.screen.display))
        ui.send(b'\r', 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.capture('13-update-result')
        # Back on the refreshed updates screen, the major is now the first choice.
        ui.send(b'\r', 'Se hará un ensayo')
        self.assertNotIn('33.0.11', '\n'.join(ui.screen.display))
        ui.send(b'\r', 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        self.assertEqual([item['action'] for item in self.calls(ui)], ['update', 'update'])
        self.assertTrue(self.calls(ui)[1]['major'])

    def test_required_lts_review_is_separate_and_can_cancel_nextcloud(self):
        ui = self.launch('required-db')
        self.root(ui, 'updates', 'Se hará un ensayo')
        ui.send(b'\r', 'MariaDB LTS requerida')
        ui.capture('14-required-database')
        ui.send(b'\r', 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Revisar y confirmar')
        ui.send(b'\x1b', 'Cancelado')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertEqual([item['action'] for item in self.calls(ui)], ['migrate_database'])
        self.assertEqual(self.calls(ui)[0]['for_nextcloud'], '34.0.5')

    def test_backup_restore_requires_ack_and_word_then_returns_to_list(self):
        ui = self.launch()
        self.root(ui, 'backups', 'Selecciona un respaldo')
        ui.capture('15-backup-list')
        ui.select(2, 'Detalle del respaldo')
        ui.capture('16-backup-detail')
        ui.send(b'\x1bOF\x1bOA\x1bOA\r', 'Revisar y confirmar')
        ui.send(b'\x1b', 'Selecciona un respaldo')
        self.assertEqual(self.calls(ui), [])
        ui.select(2, 'Detalle del respaldo')
        ui.send(b'\x1bOF\x1bOA\x1bOA\r', 'Revisar y confirmar')
        self.accept(ui, 'Escribe RESTORE')
        ui.capture('17-restore-confirm')
        ui.send(b'WRONG\r', 'Escribe RESTORE')
        self.assertEqual(self.calls(ui), [])
        ui.send(b'\x15RESTORE\r', 'Operación completada')
        ui.send(b'\x1b', 'Selecciona un respaldo')
        self.assertTrue(self.calls(ui)[0]['accept_user_files'])
        self.assertEqual(self.calls(ui)[0]['action'], 'restore')

    def test_components_maintenance_stop_and_self_update(self):
        ui = self.launch()
        self.root(ui, 'updates', 'Se hará un ensayo')
        ui.select(3, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Se hará un ensayo')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.root(ui, 'maintenance', 'Comprobaciones diarias desactivadas')
        ui.select(1, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Comprobaciones de salud')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.root(ui, 'stop', '¿Detener los servicios?')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.root(ui, 'self_update', 'descargado y revisado')
        ui.send(b'/tmp/reviewed-checkout\r', 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        self.assertEqual([call['action'] for call in self.calls(ui)], ['update_component', 'schedule', 'self_update'])

    def test_no_color_small_terminal_and_language_change(self):
        ui = self.launch(extra_env={'NO_COLOR': '1'})
        ui.capture('18-no-color')
        self.assertNotRegex(ui.output.decode(errors='replace'), r'\x1b\[(?:3[0-7]|4[0-7])m')
        self.root(ui, 'language', 'English')
        ui.send(b'\x1bOB\r', 'Manage Nextcloud')
        ui.capture('19-english-dashboard')
        self.root(ui, 'status', 'Web setup')
        small = self.launch(width=50, height=18)
        small.capture('20-small-dashboard')
        small.send(b'\x1bOF\r')
        small.process.wait(timeout=3)
        self.assertEqual(small.process.returncode, 0)

    def test_pending_recovery_preserves_files_acknowledgement(self):
        ui = self.launch('pending')
        # Pending recovery is in the data column; Tab still visits all actions.
        ui.send(b'\r', 'Continuar operación')
        ui.select(1, 'Revisar y confirmar')
        self.accept(ui, 'Escribe RESTORE')
        ui.send(b'RESTORE\r', 'Operación completada')
        self.assertTrue(self.calls(ui)[0]['accept_user_files'])
        self.assertFalse((ui.state / 'operation.json').exists())

    def test_domain_primary_tls_ca_and_network_draft(self):
        ui = self.launch()
        self.root(ui, 'domains', 'Principal:')
        ui.select(0, 'Selecciona un campo')
        ui.send(b'\r', 'IP o dominio sin')
        ui.send(b'nube.internal\r', 'actualizado en el formulario')
        ui.send(b'\x1bOF\x1bOA\r', 'Revisar y confirmar')
        self.accept(ui, 'Dirección agregada')
        ui.send(b'\r', 'Principal:')
        ui.select(4, 'Cambiar dirección principal')
        ui.select(1, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Principal:')
        ui.select(5, 'Activar o desactivar HTTPS local')
        self.assertIn('Sin HTTPS local', '\n'.join(ui.screen.display))
        ui.select(1, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Principal:')
        ui.select(6, 'Importa esta CA pública')
        ui.send(b'\r', 'Principal:')
        ui.select(7, 'Selecciona un campo')
        ui.select(1, 'Puerto HTTP entre')
        ui.send(b'\x158088\r', 'actualizado en el formulario')
        # A single review: «Apply changes» goes straight to the port check and confirmation.
        self.form_review(ui, 'Puerto ocupado por otro contenedor')
        ui.capture('23-network-invalid-port')
        ui.send(b'\x1b', '¿Descartar los cambios')
        self.accept(ui, 'Principal:')
        cfg = json.loads((ui.state / 'current' / 'config.json').read_text())
        self.assertEqual(cfg['http_port'], 8080)
        self.assertEqual(cfg['primary'], 'nube.internal')
        self.assertEqual(cfg['domains'][1]['tls'], 'internal')
        ui.select(3, 'Quitar dirección')
        ui.select(1, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        self.assertEqual(self.calls(ui)[-1]['action'], 'remove_domain')

    def test_backup_verify_export_and_create_workflows(self):
        ui = self.launch()
        self.root(ui, 'backups', 'Selecciona un respaldo')
        ui.select(2, 'Detalle del respaldo')
        ui.send(b'\r', 'Integridad verificada')
        ui.capture('24-backup-verified')
        ui.send(b'\r', 'Selecciona un respaldo')
        ui.select(1, 'Operación completada')
        ui.send(b'\r', 'Selecciona un respaldo')
        ui.select(0, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Selecciona un respaldo')
        self.assertEqual(self.calls(ui)[-1]['action'], 'backup')

    def test_start_stop_diagnose_and_all_schedule_actions(self):
        ui = self.launch()
        for action in ('start', 'stop'):
            self.root(ui, action, '¿Detener los servicios?' if action == 'stop' else 'Operación completada')
            if action == 'stop':
                self.accept(ui, 'Operación completada')
            ui.send(b'\r', 'Administrar Nextcloud')
        self.root(ui, 'diagnose', 'Espacio libre')
        ui.capture('25-diagnose')
        ui.send(b'\r', 'Administrar Nextcloud')
        self.root(ui, 'maintenance', 'Comprueba servicios')
        for index in (0, 1, 2):
            ui.select(index, 'Revisar y confirmar' if index == 1 else 'Operación completada')
            if index == 1:
                self.accept(ui, 'Operación completada')
            # Health checks stay open after each action.
            ui.send(b'\r', 'Comprueba servicios')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertEqual([row['action'] for row in self.calls(ui)],
                         ['engine_start', 'engine_stop', 'maintenance', 'schedule', 'schedule'])

    def test_failed_operation_and_failed_checks_are_visible(self):
        ui = self.launch()
        self.root(ui, 'updates', 'Se hará un ensayo')
        ui.select(5, 'Revisar y confirmar')
        self.accept(ui, 'No se pudo validar el proxy')
        ui.capture('26-operation-error')
        self.assertIn('No se pudo validar', '\n'.join(ui.screen.display))
        ui.send(b'\x1b', 'La operación no terminó')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertNotIn('La operación no terminó', '\n'.join(ui.screen.display))
        (ui.state / 'fail-checks').touch()
        self.root(ui, 'maintenance', 'Comprueba servicios')
        ui.send(b'\r', 'Hay comprobaciones con problemas')
        ui.capture('27-maintenance-warning')
        self.assertIn('Problemas detectados', '\n'.join(ui.screen.display))

    def test_english_wizard_and_named_storage_hide_unused_folder(self):
        ui = self.launch('empty-english')
        ui.send(b'\r', 'Installation environment')
        ui.send(b'\x1bOB\r', 'officially maintained')
        ui.send(b'\r', 'Initial configuration')
        ui.select(7, 'Storage')
        ui.send(b'\x1bOB\r', 'updated in the form')
        self.assertNotIn('Data folder', '\n'.join(ui.screen.display))
        ui.capture('28-english-form')
        ui.send(b'\x1bOF\x1bOA\r', 'Confirm installation')
        self.assertIn('Install now', '\n'.join(ui.screen.display))
        ui.send(b'\r', 'Installation cancelled')
        self.assertNotIn('Data folder', '\n'.join(ui.screen.display))
        ui.send(b'\x1b', 'Discard the form changes')
        self.accept(ui, 'Manage Nextcloud')
        self.assertFalse((ui.state / 'current').exists())

    def test_refresh_and_direct_domain_with_publication_choice(self):
        ui = self.launch()
        self.root(ui, 'updates', 'Se hará un ensayo')
        ui.select(2, 'Revisar y confirmar')
        self.accept(ui, 'Operación completada')
        ui.send(b'\r', 'Se hará un ensayo')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.root(ui, 'domains', 'Principal:')
        ui.select(1, 'Selecciona un campo')
        ui.send(b'\r', 'IP o dominio sin')
        ui.send(b'public.example.com\r', 'actualizado en el formulario')
        ui.select(3, 'Publicar TCP 80/443')
        ui.send(b'\x1bOB\r', 'actualizado en el formulario')
        ui.capture('29-direct-domain-ready')
        ui.send(b'\x1bOF\x1bOA\r', 'Revisar y confirmar')
        self.accept(ui, 'Apunta el DNS')
        self.assertTrue(self.calls(ui)[0]['refresh'])
        self.assertTrue(self.calls(ui)[1]['publish_direct'])

    def test_resize_while_navigating_keeps_escape_and_selection(self):
        ui = self.launch()
        ui.resize(50, 18)
        ui.capture('30-resized-dashboard')
        # On the dashboard Esc exits, and the legend says so.
        self.assertIn('Esc Salir', '\n'.join(ui.screen.display))
        ui.send(b'\t\t')
        ui.resize(100, 32)
        ui.capture('31-expanded-dashboard')
        ui.send(b'\r', '¿Detener los servicios?')
        ui.send(b'\x1b', 'Administrar Nextcloud')
        self.assertEqual(self.calls(ui), [])

    def test_dashboard_escape_moves_to_exit_before_closing(self):
        ui = self.launch()
        ui.send(b'\x1b', 'Pulsa Enter o Esc otra vez para salir')
        self.assertIsNone(ui.process.poll())
        ui.send(b'\x1b')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 0)

    def test_backup_delete_asks_and_returns_to_list(self):
        ui = self.launch()
        self.root(ui, 'backups', 'Selecciona un respaldo')
        ui.select(2, 'Detalle del respaldo')
        ui.send(b'\x1bOF\x1bOA\r', '¿Eliminar este respaldo?')
        ui.send(b'\x1b', 'Selecciona un respaldo')
        self.assertTrue(any((ui.state / 'backups').glob('*.ndm.tar')))
        ui.select(2, 'Detalle del respaldo')
        ui.send(b'\x1bOF\x1bOA\r', '¿Eliminar este respaldo?')
        self.accept(ui, 'Respaldo eliminado')
        self.assertFalse(any((ui.state / 'backups').glob('*.ndm.tar')))
        self.assertIn('Todavía no hay respaldos', '\n'.join(ui.screen.display))

    def test_fresh_install_shows_recommended_and_previous_with_support_dates(self):
        ui = self.launch('empty')
        ui.send(b'\r', 'Entorno de instalación')
        ui.send(b'\r', 'mantenimiento oficial')
        shown = '\n'.join(ui.screen.display)
        self.assertIn('35.0.2', shown)
        self.assertIn('Recomendada', shown)
        self.assertIn('34.0.5', shown)
        self.assertIn('Anterior', shown)
        self.assertNotIn('33.0.10', shown)
        self.assertNotIn('32.0.15', shown)
        ui.capture('32-supported-versions')

    def test_obsolete_installation_is_marked_on_dashboard_and_status(self):
        ui = self.launch('obsolete')
        self.assertIn('OBSOLETA: Nextcloud 32', '\n'.join(ui.screen.display))
        ui.capture('33-obsolete-dashboard')
        self.root(ui, 'status', 'OBSOLETA')
        self.assertIn('sin mantenimiento', '\n'.join(ui.screen.display))
        ui.capture('34-obsolete-status')
        self.assertEqual(self.calls(ui), [])


if __name__ == '__main__':
    unittest.main()
