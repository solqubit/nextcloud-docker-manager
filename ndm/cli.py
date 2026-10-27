"""Interactive terminal and explicit commands share the same guarded operations."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import traceback

from . import archive
from .config import access_url, host
from .core import Error, Runner, State, VERSION
from .console import Console
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


def menu(manager, language=None, console=None):
    from .screens import Controller
    Controller(manager, language, console).run()


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
        visual = interactive or sys.stdin.isatty() and sys.stdout.isatty()
        language = args.lang or (state.load()['language'] if state.configured else 'es')
        console = Console(Terminal(messages(language)), state) if visual else None
        if interactive:
            console.welcome()
            console.run('prepare_host', lambda: bootstrap(Runner(state)))
        if console:
            console.run('check_engine', lambda: require_engine(Runner(state)))
        else:
            require_engine(Runner(state))
        manager = Manager(state)
        if interactive:
            menu(manager, language, console)
        elif console:
            from .screens import Controller
            result = console.run(args.command, lambda: dispatch(manager, args))
            Controller(manager, language, console).report(args.command, result, style='accent')
        else:
            show(dispatch(manager, args))
        return 0
    except EOFError:
        return 0
    except (Error, OSError, ValueError, KeyboardInterrupt) as exc:
        if state and hasattr(state, 'log_path'):
            with state.log_path.open('a', encoding='utf-8') as log:
                traceback.print_exc(file=log)
            if 'console' in locals() and console is not None:
                console.error(exc, pending=bool(state.pending))
            else:
                print(f'[!] {exc}\nLog: {state.log_path}', file=sys.stderr)
        else:
            print(f'[!] {exc}', file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if state and getattr(state, 'lock_file', None):
            state.lock_file.close()
