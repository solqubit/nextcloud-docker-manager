"""Interactive terminal and explicit commands share the same guarded operations."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import traceback
from zoneinfo import ZoneInfo, available_timezones

from . import archive
from .config import access_url, host, port
from .core import Error, Runner, State, VERSION, atomic, confirm
from .docker import Engine, require_engine
from .i18n import messages
from .operations import Manager
from .system import bootstrap, host_info
from .terminal_ui import Terminal


def parser():
    p = argparse.ArgumentParser(description='Nextcloud Docker Manager: SQL/application backups exclude user files')
    p.add_argument('--version', action='version', version=VERSION)
    p.add_argument('--state-dir', default=os.environ.get('NDM_STATE_DIR', '/opt/nextcloud-docker-manager'))
    p.add_argument('--lang', choices=['es', 'en'])
    p.add_argument('--plain', action='store_true')
    sub = p.add_subparsers(dest='command')
    sub.add_parser('menu')
    install = sub.add_parser('install')
    install.add_argument('--nextcloud-version')
    install.add_argument('--environment', choices=['lan', 'vps'], default='lan')
    install.add_argument('--address', default='127.0.0.1')
    install.add_argument('--bind', default='127.0.0.1')
    install.add_argument('--http-port', type=int, default=8080)
    install.add_argument('--https-port', type=int, default=8443)
    install.add_argument('--timezone', default='UTC')
    install.add_argument('--storage', choices=['named', 'directory'], default='directory')
    install.add_argument('--storage-root', default='/opt/nextcloud-docker-manager/data')
    install.add_argument('--confirm')
    for name in ('status', 'setup-check', 'check-updates', 'diagnose', 'start', 'stop', 'maintenance', 'export-ca'):
        sub.add_parser(name)
    for name in ('update', 'migrate', 'refresh-image'):
        child = sub.add_parser(name)
        child.add_argument('--target')
        child.add_argument('--confirm')
    child = sub.add_parser('backup')
    child.add_argument('--confirm')
    child = sub.add_parser('verify-backup')
    child.add_argument('path')
    child = sub.add_parser('restore')
    child.add_argument('path')
    child.add_argument('--accept-user-files-state', action='store_true')
    child.add_argument('--confirm')
    child = sub.add_parser('recover')
    child.add_argument('--action', choices=['resume', 'restore'], required=True)
    child.add_argument('--confirm')
    child.add_argument('--accept-user-files-state', action='store_true')
    child = sub.add_parser('domain-add')
    child.add_argument('host')
    child.add_argument('--mode', choices=['local', 'direct', 'tunnel'], default='local')
    child.add_argument('--local-https', action='store_true')
    child.add_argument('--email', default='')
    child.add_argument('--primary', action='store_true')
    child.add_argument('--publish-direct', action='store_true', help='After web setup: bind TCP 80/443 on all interfaces (direct domain only)')
    child.add_argument('--external-port', type=int, default=443, help='Client-visible HTTPS port, independent of the local forwarded port')
    child.add_argument('--confirm')
    for name in ('domain-remove', 'domain-primary'):
        child = sub.add_parser(name)
        child.add_argument('host')
        child.add_argument('--confirm')
    child = sub.add_parser('network')
    child.add_argument('--bind')
    child.add_argument('--http-port', type=int)
    child.add_argument('--https-port', type=int)
    child.add_argument('--confirm')
    child = sub.add_parser('logs')
    child.add_argument('service', choices=['app', 'db', 'redis', 'proxy', 'cron'])
    child.add_argument('--tail', type=int, default=100)
    child = sub.add_parser('export-images')
    child.add_argument('path')
    child = sub.add_parser('schedule')
    child.add_argument('action', choices=['enable', 'disable', 'status'])
    child = sub.add_parser('update-component')
    child.add_argument('component', choices=['db', 'redis', 'proxy'])
    child.add_argument('--confirm')
    child = sub.add_parser('migrate-db', help='Only when the next Nextcloud major requires a new MariaDB LTS')
    child.add_argument('--target', required=True, help='MariaDB LTS series, for example 11.8')
    child.add_argument('--for-nextcloud', required=True, help='Exact Nextcloud upgrade requiring the change')
    child.add_argument('--confirm')
    child = sub.add_parser('self-update')
    child.add_argument('--source')
    child.add_argument('--confirm')
    return p


def show(value):
    if value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str) if not isinstance(value, str) else value)


def choose(term, title, choices, **kwargs):
    return term.run(lambda: term.menu(term.t(title), [(value, term.t(label), '') for value, label in choices], **kwargs))


def edit(term, key, value, validator=lambda value: value, hint=''):
    return term.run(lambda: term.edit(term.t(key), str(value), hint, validator, default=str(value)))


def installation(manager, term):
    env = choose(term, 'environment', [('lan', 'lan'), ('vps', 'vps')])
    if env is None:
        return
    versions = manager.catalog.branches()
    version = term.run(lambda: term.menu(term.t('version'), [(v, v, '') for v in versions]))
    if version is None:
        return
    info = host_info()
    if info['panel_detected']:
        print('[!] Panel existente detectado: revisa los puertos; no se modifica el panel / Existing panel detected')
    address = info['address'] if env == 'lan' else '127.0.0.1'
    fields = dict(environment=env, bind=address, address=address, http_port=8080, https_port=8443,
                  timezone='UTC', storage='directory', storage_root=str(manager.state.path / 'data'),
                  language='es' if term.msg == messages('es') else 'en')
    labels = dict(bind='bind', address='address', http_port='http_port', https_port='https_port',
                  timezone='timezone', storage='storage', storage_root='storage_root')
    while True:
        options = [(key, f'{term.t(label)}: {fields[key]}', '') for key, label in labels.items()]
        key = term.run(lambda: term.menu(term.t('configure'), options + [('apply', term.t('apply'), 'success')]))
        if key is None:
            return
        if key == 'apply':
            return manager.install(version, **fields)
        if key == 'storage':
            value = choose(term, 'storage', [('directory', 'directory'), ('named', 'named')])
        elif key == 'timezone':
            value = term.run(lambda: term.menu(term.t('timezone'), [(v, v, '') for v in sorted(available_timezones())], search=True))
        else:
            value = edit(term, labels[key], fields[key], port if key.endswith('_port') else host if key == 'address' else lambda v: v)
        if value is not None:
            fields[key] = value


def domains(manager, term):
    while True:
        cfg = manager.state.load()
        action = choose(term, 'domains', [(key, key) for key in
            ('add_local', 'add_direct', 'add_tunnel', 'remove', 'primary', 'local_tls', 'ca', 'network')],
            notice=' · '.join(e['host'] for e in cfg['domains']))
        if action is None:
            return
        if action.startswith('add_'):
            name = edit(term, 'name', '', host)
            if name is None:
                continue
            mode = {'add_local': 'local', 'add_direct': 'direct', 'add_tunnel': 'tunnel'}[action]
            email = edit(term, 'email', '') if mode != 'local' else ''
            if email is None:
                continue
            publish = False
            if mode == 'direct':
                selected = term.run(lambda: term.menu('Publicar puertos / Publish ports', [
                    ('keep', 'Conservar IP y puertos actuales / Keep current binding', ''),
                    ('publish', 'Publicar TCP 80/443 en todas las interfaces / Publish TCP 80/443', '')]))
                if selected is None:
                    continue
                publish = selected == 'publish'
            manager.add_domain(name, mode=mode, email=email, publish_direct=publish)
            if mode == 'tunnel':
                print(f"cloudflared se instala y configura manualmente / Configure cloudflared manually: http://{cfg['bind']}:{cfg['http_port']}, hostname: {name}")
            elif mode == 'direct':
                print('Apunta DNS al servidor y publica TCP 80/443 hacia el proxy. Usa network para ajustar IP/puertos. / Point DNS and forward TCP 80/443 to the proxy.')
        elif action == 'network':
            candidate = copy.deepcopy(cfg)
            cancelled = False
            for key in ('bind', 'http_port', 'https_port'):
                value = edit(term, key, candidate[key], port if key.endswith('_port') else lambda v: str(__import__('ipaddress').IPv4Address(v)))
                if value is None:
                    cancelled = True
                    break
                candidate[key] = value
            if not cancelled:
                manager.settings(candidate)
        elif action == 'ca':
            show(Engine(manager.state).ca_certificate())
        else:
            entries = [e for e in cfg['domains'] if action != 'local_tls' or e['kind'] == 'local']
            name = term.run(lambda: term.menu(term.t(action), [(e['host'], access_url(cfg, e), '') for e in entries]))
            if name is None:
                continue
            if action == 'remove':
                manager.remove_domain(name)
            else:
                if action == 'primary':
                    cfg['primary'] = name
                else:
                    entry = next(e for e in cfg['domains'] if e['host'] == name)
                    entry['tls'] = 'internal' if entry['tls'] == 'off' else 'off'
                manager.settings(cfg)


def backup_menu(manager, term):
    from .archive import unpack
    action = choose(term, 'backups', [(key, key) for key in ('backup', 'verify', 'restore', 'export')])
    if action is None:
        return
    if action == 'backup':
        return manager.backup()
    if action == 'export':
        return Engine(manager.state).export_images(manager.state.path / 'backups' / 'docker-images.tar')
    paths = sorted((manager.state.path / 'backups').glob('*.ndm.tar'), reverse=True)
    path = term.run(lambda: term.menu(term.t(action), [(str(p), p.name, '') for p in paths]))
    if path is None:
        return
    if action == 'verify':
        with unpack(manager.state, path) as (_, manifest):
            show({k: manifest[k] for k in ('created', 'kind', 'excluded') if k in manifest})
    else:
        print('Los archivos actuales permanecerán. SQL antiguo puede referir otro estado de archivos. / User files stay; old SQL may refer to a different file state.')
        confirm('USER-FILES-UNCHANGED')
        return manager.restore(path, accept_user_files=True)


def menu(manager, language=None):
    language = language or (manager.state.load()['language'] if manager.state.configured else 'es')
    term = Terminal(messages(language), context=str(manager.state.path))
    while True:
        configured = manager.state.configured
        keys = ['status', 'setup', 'domains', 'updates', 'major', 'refresh', 'components', 'backups',
                'logs', 'diagnose', 'start', 'stop', 'maintenance', 'self_update', 'language', 'exit'] if configured else ['install', 'language', 'exit']
        if manager.state.pending:
            keys.insert(0, 'recover')
        action = choose(term, 'menu', [(key, key) for key in keys], notice=term.t('pending' if manager.state.pending else 'notice'))
        if action is None or action == 'exit':
            return
        try:
            if action == 'language':
                selected = term.run(lambda: term.menu('Idioma / Language', [('es', 'Español', ''), ('en', 'English', '')]))
                if selected:
                    term = Terminal(messages(selected), context=str(manager.state.path))
                    if configured:
                        cfg = manager.state.load()
                        cfg['language'] = selected
                        manager.state.activate(manager.state.revision(cfg))
                continue
            if action == 'install':
                show(installation(manager, term))
            elif action == 'domains':
                domains(manager, term)
            elif action == 'backups':
                show(backup_menu(manager, term))
            elif action == 'setup':
                show(manager.setup_check())
            elif action in ('updates', 'major', 'refresh'):
                available = manager.checks()
                show(available)
                if action == 'major' and available['next_major']:
                    target_version = available['next_major'][0]
                    plan = manager.database_choices(target_version)
                    if plan['required']:
                        print('Nextcloud requiere otra MariaDB LTS / Nextcloud requires a different MariaDB LTS')
                        if not plan['choices']:
                            raise Error('No maintained LTS is compatible with both versions; a coordinated migration needs separate review')
                        target = term.run(lambda: term.menu('MariaDB LTS requerida / Required MariaDB LTS', [
                            (item['series'], item['series'] + ' · Community: ' + item['community_until'], '') for item in plan['choices']]))
                        if not target:
                            continue
                        show(manager.migrate_database(target, for_nextcloud=target_version))
                show(manager.update(major=action == 'major', refresh=action == 'refresh'))
            elif action == 'components':
                component = term.run(lambda: term.menu(term.t('components'), [(v, v, '') for v in ('db', 'redis', 'proxy')]))
                if component:
                    show(manager.update_component(component))
            elif action == 'recover':
                selected = term.run(lambda: term.menu(term.t('recover'), [('resume', 'Continuar / Resume', ''), ('restore', 'Restaurar / Restore', '')]))
                if selected:
                    acknowledged = False
                    if selected == 'restore' and manager.state.pending.get('traffic_opened'):
                        print('El servicio pudo aceptar cambios de archivos / Traffic may have accepted file changes')
                        confirm('USER-FILES-UNCHANGED')
                        acknowledged = True
                    show(manager.recover(selected, accept_user_files=acknowledged))
            elif action == 'logs':
                service = term.run(lambda: term.menu(term.t('logs'), [(v, v, '') for v in ('app', 'db', 'redis', 'proxy', 'cron')]))
                if service:
                    show(Engine(manager.state).dc('logs', '--no-color', '--tail', '100', service).stdout)
            elif action == 'maintenance':
                selected = term.run(lambda: term.menu(term.t('maintenance'), [
                    ('run', 'Comprobar ahora / Check now', ''), ('enable', 'Programar diario / Schedule daily', ''),
                    ('disable', 'Desactivar programación / Disable schedule', ''), ('status', 'Estado / Status', '')]))
                if selected:
                    show(manager.maintenance() if selected == 'run' else manager.schedule(selected))
            elif action == 'self_update':
                source = edit(term, 'source', '', lambda value: str(Path(value).expanduser().resolve()) if value else '')
                if not source:
                    continue
                show(manager.self_update(source))
            else:
                show(dispatch(manager, argparse.Namespace(command=action)))
        except (Error, OSError, ValueError) as exc:
            print('[!] ' + str(exc), file=sys.stderr)
        except KeyboardInterrupt:
            print('\nCancelado / Cancelled. Check status / recover if an operation is pending.')
        print(f'Log: {manager.state.log_path}')
        input(term.t('continue') + ': ')


def dispatch(manager, args):
    command = args.command
    state = manager.state
    if command == 'install':
        return manager.install(args.nextcloud_version, supplied=args.confirm,
            **{key: getattr(args, key) for key in ('environment', 'address', 'bind', 'http_port', 'https_port', 'timezone', 'storage', 'storage_root')},
            language=args.lang or 'es')
    if not state.configured:
        raise Error('Primero instala Nextcloud / Install Nextcloud first')
    engine = Engine(state)
    if command == 'status':
        return manager.status()
    if command == 'setup-check':
        return manager.setup_check()
    if command == 'check-updates':
        return manager.checks()
    if command in ('update', 'migrate', 'refresh-image'):
        return manager.update(args.target, major=command == 'migrate', refresh=command == 'refresh-image', supplied=args.confirm)
    if command == 'backup':
        return str(manager.backup(args.confirm))
    if command == 'verify-backup':
        with archive.unpack(state, args.path) as (_, manifest):
            return {'valid': True, 'version': manifest['configuration']['version'], 'excluded': manifest['excluded']}
    if command == 'restore':
        return manager.restore(args.path, supplied=args.confirm, accept_user_files=args.accept_user_files_state)
    if command == 'recover':
        return manager.recover(args.action, args.confirm, args.accept_user_files_state)
    if command == 'domain-add':
        manager.add_domain(args.host, mode=args.mode, tls='internal' if args.local_https else 'off',
                           email=args.email, primary=args.primary, publish_direct=args.publish_direct,
                           external_port=args.external_port, supplied=args.confirm)
        return {'access': access_url(state.load(), next(e for e in state.load()['domains'] if e['host'] == host(args.host)))}
    if command == 'domain-remove':
        return manager.remove_domain(args.host, args.confirm)
    if command in ('domain-primary', 'network'):
        cfg = copy.deepcopy(engine.cfg)
        if command == 'domain-primary':
            cfg['primary'] = host(args.host)
        else:
            for key in ('bind', 'http_port', 'https_port'):
                value = getattr(args, key)
                if value is not None:
                    cfg[key] = value
        return manager.settings(cfg, args.confirm)
    if command == 'start':
        if state.pending:
            raise Error('Pending operation: use recover')
        engine.up('db', 'redis', 'app', 'proxy', 'cron')
        return engine.wait_app(installed=engine.cfg['status'] == 'ready')
    if command == 'stop':
        return engine.stop('cron', 'app', 'proxy', 'redis', 'db')
    if command == 'logs':
        return engine.dc('logs', '--no-color', '--tail', str(min(max(args.tail, 1), 2000)), args.service).stdout
    if command == 'export-ca':
        return str(engine.ca_certificate())
    if command == 'export-images':
        return engine.export_images(args.path)
    if command == 'diagnose':
        return {'host': host_info(), 'state': manager.status(), 'disk_free': __import__('shutil').disk_usage(state.path).free,
                'sql_application_free_bytes': engine.check_space(0),
                'last_maintenance': __import__('ndm.core', fromlist=['read_json']).read_json(state.path / 'last-maintenance.json') if (state.path / 'last-maintenance.json').exists() else None}
    if command == 'maintenance':
        return manager.maintenance()
    if command == 'schedule':
        return manager.schedule(args.action)
    if command == 'update-component':
        return manager.update_component(args.component, args.confirm)
    if command == 'migrate-db':
        return manager.migrate_database(args.target, for_nextcloud=args.for_nextcloud, supplied=args.confirm)
    if command == 'self-update':
        return manager.self_update(args.source, args.confirm)
    raise Error('Unknown command')


def main(argv=None):
    args = parser().parse_args(argv)
    if args.plain:
        os.environ['NDM_UI'] = 'plain'
    state = None
    try:
        if os.name != 'posix':
            raise Error('Run this installer on Linux. Use a Linux Docker runner for Windows tests.')
        state = State(args.state_dir).open()
        interactive = args.command in (None, 'menu')
        if interactive:
            bootstrap(Runner(state))
        require_engine(Runner(state))
        manager = Manager(state)
        if interactive:
            menu(manager, args.lang)
        else:
            show(dispatch(manager, args))
        return 0
    except (Error, OSError, ValueError, KeyboardInterrupt) as exc:
        if state and hasattr(state, 'log_path'):
            with state.log_path.open('a', encoding='utf-8') as log:
                traceback.print_exc(file=log)
            print(f'[!] {exc}\nLog: {state.log_path}', file=sys.stderr)
        else:
            print(f'[!] {exc}', file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if state and getattr(state, 'lock_file', None):
            state.lock_file.close()
