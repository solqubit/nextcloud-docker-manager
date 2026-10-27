"""Keyboard workflows: drafts, reviews, status cards and lifecycle operations."""
from __future__ import annotations

import copy
import datetime as dt
import ipaddress
import json
from pathlib import Path
import tarfile
import traceback
from zoneinfo import ZoneInfo, available_timezones

from . import archive
from .config import access_url, host, port, public_host
from .console import Console
from .core import Cancelled, Error, human
from .dashboard import dashboard
from .docker import Engine
from .i18n import messages, remember_language, remembered_language
from .system import host_info
from .terminal_ui import SEPARATOR, Terminal
from .views import SERVICES, backup_rows, pair, release_rows, result_rows, support_summary


def folder(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(ord(char) < 32 for char in value):
        raise Error('Use an absolute folder / Usa una carpeta absoluta')
    return str(path)


class Form:
    """In-memory drafts. Escape never writes configuration or changes services."""
    def __init__(self, console, title, data, fields, *, installed=False, versions=(), releases=()):
        self.view, self.ui, self.title = console, console.term, title
        self.data, self.original = copy.deepcopy(data), copy.deepcopy(data)
        self._fields, self.installed, self.versions = fields, installed, versions
        self.releases = releases
        self.selected, self.invalid = 0, ''
        self.notice, self.notice_style = self.ui.t('form_intro'), 'accent'
        self.reviewing = False

    @property
    def fields(self):
        return [key for key in self._fields if key != 'storage_root' or self.data.get('storage') != 'named']

    def display(self, key, value=None):
        value = self.data[key] if value is None else value
        if key in ('environment', 'storage', 'publish'):
            return self.ui.t(('environment_' + value) if key == 'environment' else
                             ('yes' if value else 'no') if key == 'publish' else value)
        return str(value) if value != '' else self.ui.t('optional' if key == 'email' else 'pending_value')

    def normalize(self, key, value):
        if key.endswith('_port'):
            return port(value)
        if key == 'bind':
            address = ipaddress.IPv4Address(value)
            if not self.installed and (address.is_unspecified or self.data.get('environment') == 'vps' and not address.is_loopback):
                raise Error(self.ui.t('initial_binding'))
            return str(address)
        if key in ('address', 'name'):
            return (public_host if key == 'name' and self.data.get('public') else host)(value)
        if key == 'timezone':
            ZoneInfo(value)
        if key == 'storage_root':
            return folder(value)
        if key == 'email' and value and ('@' not in value or any(char.isspace() for char in value)):
            raise Error(self.ui.t('invalid_email'))
        return value

    def problem(self, key):
        try:
            self.normalize(key, str(self.data[key]))
        except (Error, ValueError, OSError, KeyError):
            return True
        return key == self.invalid or key in ('http_port', 'https_port') and self.data.get('http_port') == self.data.get('https_port')

    def rows(self, review=False):
        if self.ui.screen is not None:
            self.ui.measure()
        width = getattr(self.ui, 'width', 76)
        names = {key: self.ui.msg.get('field_' + key, self.ui.t(key)) for key in self.fields}
        column = min(width // 2 - 4, max(len(name) for name in names.values()) + 3)
        rows = []
        for key in self.fields:
            invalid = self.problem(key)
            pending = key == 'name' and not self.data[key] and self.invalid != key
            changed = self.data[key] != self.original[key]
            state = self.ui.t('pending_value' if pending else 'field_invalid' if invalid else 'field_changed' if changed else
                              'field_current' if self.installed else 'field_default')
            tone = 'warning' if pending else 'error' if invalid else 'ok' if changed else 'dim'
            marker = '[!]' if pending else '[✗]' if invalid else '[+]'
            value = self.display(key)
            room = max(4, width - column - len(state) - 10)
            if len(value) > room:
                value = value[:room - 1] + '…'
            label = [(marker + ' ', 'warning' if pending else 'error' if invalid else 'ok'),
                     (names[key][:column - 2].ljust(column), ''),
                     (value, 'warning' if pending else 'error' if invalid else 'accent'),
                     ('  ' + state, tone + '>')]
            rows.append((key, label, 'error' if invalid and not pending else ''))
        rows += [(SEPARATOR, '', '')]
        rows += [('apply' if review else 'review', '▶ ' + self.ui.t('apply_changes' if self.installed else
                                                   'deploy' if review else 'review'), 'ok')]
        if review:
            rows.append(('edit', '← ' + self.ui.t('return_edit'), 'accent'))
        rows.append(('cancel', '← ' + self.ui.t('cancel'), ''))
        return rows

    def timezone(self):
        zones = sorted(available_timezones())
        while True:
            areas = sorted({zone.split('/')[0] for zone in zones if '/' in zone})
            suggested = self.data.get('timezone', 'UTC')
            area = self.view.pick('timezone_area', [('=' + suggested, '★ ' + suggested, 'ok'), ('UTC', 'UTC', '')]
                                  + [(value, value, '') for value in areas], search=True)
            if area is None or area == 'UTC':
                return area
            if area.startswith('='):
                return area[1:]
            value = self.view.pick('timezone_city', [(zone, zone, '') for zone in zones if zone.startswith(area + '/')],
                                   search=True)
            if value is not None:
                return value

    def edit(self, key):
        data, value = self.data, None
        if key in ('environment', 'storage', 'publish'):
            values = {'environment': ['lan', 'vps'], 'storage': ['directory', 'named'], 'publish': [False, True]}[key]
            value = self.view.pick(key, [(v, self.display(key, v), 'warning' if key == 'publish' and v else '')
                                        for v in values], selected=values.index(data[key]),
                                   help_text=self.ui.t('environment_help') if key == 'environment' else '')
        elif key == 'version':
            value = self.view.pick(key, release_rows(self.ui, self.releases) if self.releases else
                                   [(v, 'Nextcloud ' + v, '') for v in self.versions],
                                   selected=self.versions.index(data[key]))
        elif key == 'timezone':
            value = self.timezone()
        elif key in ('bind', 'address'):
            choices = list(dict.fromkeys([data[key], host_info()['address'], '127.0.0.1']))
            if key == 'bind' and self.installed:
                choices.append('0.0.0.0')
            choices = list(dict.fromkeys(choices))
            value = self.view.pick(key, [(v, v, '') for v in choices if v != '0.0.0.0' or key == 'bind']
                                   + [('manual', self.ui.t('manual_value'), 'accent')])
            if value == 'manual':
                value = self.view.edit(key, data[key], lambda text: self.normalize(key, text),
                                       hint=self.ui.t('field_help_' + key), default=self.original[key])
        else:
            preview = None
            if key in ('http_port', 'https_port'):
                def preview(text):
                    try:
                        port(text)
                        return self.ui.t('https_reserved', text) if key == 'https_port' else self.ui.t(
                            'access_preview', f'http://{data.get("address", data.get("bind", "127.0.0.1"))}:{text}')
                    except (Error, ValueError):
                        return ''
            value = self.view.edit(key, data[key], lambda text: self.normalize(key, text),
                                   hint=self.ui.t('field_help_' + key), default=self.original[key], preview=preview)
        if value is not None:
            self.data[key] = value
            if key == 'environment' and value == 'vps':
                self.data.update(bind='127.0.0.1', address='127.0.0.1')
            self.invalid = ''
            self.notice, self.notice_style = self.ui.t('field_saved', self.ui.t(key)), 'ok'

    def field_help(self, key):
        if key in self.fields:
            full = self.display(key)
            hint = self.ui.msg.get('field_help_' + key, self.ui.t('form_edit_hint'))
            return full + ' · ' + hint
        return self.ui.t('review_help') if key in ('review', 'apply') else self.ui.t('form_cancel_hint')

    def run(self):
        while True:
            if 'version' in self.data:
                self.ui.context = f'Nextcloud {self.data["version"]} · MariaDB 11.4 LTS'
            review = self.reviewing
            def screen():
                rows = self.rows(review)
                return self.ui.menu(self.ui.t('review' if review else self.title), rows,
                                    len(self.fields) + 1 if review else self.selected,
                                    notice=self.ui.t('review_help') if review else self.notice,
                                    notice_style='accent' if review else self.notice_style,
                                    help_text=self.field_help)
            with self.view.prompt():
                choice = self.ui.run(screen)
            if choice is None or choice in ('cancel', 'edit'):
                if review:
                    self.reviewing = False
                    if choice != 'cancel':
                        continue
                if choice == 'edit':
                    continue
                if self.data != self.original:
                    leave = self.view.pick('discard_changes', [
                        ('edit', '← ' + self.ui.t('return_edit'), 'accent'),
                        ('discard', self.ui.t('discard'), 'warning')])
                    if leave != 'discard':
                        continue
                return None
            if choice in ('review', 'apply'):
                invalid = next((key for key in self.fields if self.problem(key)), '')
                if invalid:
                    self.invalid, self.selected = invalid, self.fields.index(invalid)
                    self.notice, self.notice_style = self.ui.t('invalid_field', self.ui.t(invalid)), 'error'
                    self.reviewing = False
                    continue
                if choice == 'apply':
                    return copy.deepcopy(self.data)
                self.reviewing = True
                continue
            self.selected, self.reviewing = self.fields.index(choice), False
            self.edit(choice)


class Controller:
    def __init__(self, manager, language=None, console=None):
        self.manager, self.state = manager, manager.state
        self.language = language or remembered_language(self.state) or 'es'
        self.view = console or Console(Terminal(messages(self.language)), self.state)
        self.term = self.view.term
        self.last = self.notice = ''
        self.notice_style, self.snapshot = 'accent', None
        self.verified = {}

    def pick(self, title, keys, **kwargs):
        return self.view.pick(title, [(key, self.term.t(key), '') for key in keys], **kwargs)

    def report(self, action, data=None, *, notice='', style='ok'):
        if isinstance(data, dict) and (data.get('errors') or data.get('warnings')):
            style = 'warning'
            notice = self.term.t('checks_warning')
        if isinstance(data, dict) and data.get('support', {}).get('state') in ('eol', 'ending', 'unknown'):
            notice, style = support_summary(self.term, data['support'])
        rows = result_rows(self.term, action, data, self.state)
        if action in ('install', 'setup') and self.state.configured:
            cfg = self.state.load()
            rows = pair(self.term, 'access_local', access_url(cfg)) + rows
            if not (data or {}).get('installed'):
                notice, style = self.term.t('web_setup_help'), 'warning'
                if cfg['environment'] == 'vps':
                    rows += pair(self.term, 'ssh_forward', f'ssh -L {cfg["http_port"]}:127.0.0.1:{cfg["http_port"]} usuario@servidor')
        rows += pair(self.term, 'private_log', self.state.log_path, 'dim')
        self.view.view(action, rows, notice=notice or self.term.t('operation_done'), style=style)
        self.notice, self.notice_style = notice or self.term.t('operation_done'), style

    def perform(self, action, function, *, context=None, notice='', style='ok'):
        result = self.view.run(action, function, context=context)
        if action == 'backup' and isinstance(result, Path) and result.is_file():
            self.mark_verified(result)
        self.report(action, result, notice=notice, style=style)
        return result

    def mark_verified(self, path):
        value = Path(path).stat()
        self.verified[str(path)] = (value.st_ino, value.st_size, value.st_mtime_ns)

    def attempt(self, function):
        try:
            return function()
        except (Cancelled, KeyboardInterrupt, EOFError):
            self.notice, self.notice_style = self.term.t('cancelled'), 'warning'
            if self.state.pending:
                self.notice = self.term.t('pending')
        except (Error, OSError, ValueError, tarfile.TarError, KeyError) as exc:
            with self.state.log_path.open('a', encoding='utf-8') as log:
                traceback.print_exc(file=log)
            self.view.error(exc, pending=bool(self.state.pending))
            self.notice, self.notice_style = self.term.t('operation_failed'), 'error'

    def install(self):
        environment = self.pick('environment', ('lan', 'vps'), help_text=self.term.t('environment_help'))
        if environment is None:
            return
        releases = self.view.run('lookup_versions', self.manager.catalog.installation_options)
        versions = [release['version'] for release in releases]
        version = self.view.pick('version', release_rows(self.term, releases),
                                 notice=self.term.t('version_help'))
        if version is None:
            return
        info = host_info()
        address = info['address'] if environment == 'lan' else '127.0.0.1'
        data = dict(version=version, environment=environment, bind=address, address=address, http_port=8080,
                    https_port=8443, timezone='UTC', storage='directory', storage_root=str(self.state.path / 'data'))
        form = Form(self.view, 'configure', data, tuple(data), versions=versions, releases=releases)
        if info['panel_detected']:
            form.notice, form.notice_style = self.term.t('ports_existing'), 'warning'
        while True:
            draft = form.run()
            if draft is None:
                return
            try:
                cfg = dict(draft, id='ui-preflight', project='ui-preflight')
                self.view.run('check_ports', lambda: Engine(self.state, cfg).check_ports())
            except (Error, OSError, ValueError) as exc:
                field = next((key for key in ('http_port', 'https_port') if str(draft[key]) in str(exc)), 'bind')
                form.invalid, form.selected = field, form.fields.index(field)
                form.notice, form.notice_style, form.reviewing = self.view.local(str(exc)), 'error', False
                continue
            version = draft.pop('version')
            self.perform('install', lambda: self.manager.install(version, language=self.language, **draft),
                         context=[('Nextcloud', version), (self.term.t('access_local'), f'http://{draft["address"]}:{draft["http_port"]}')])
            return

    def domains(self):
        while True:
            cfg = self.state.load()
            choices = ('add_local', 'add_direct', 'add_tunnel', 'remove', 'primary', 'local_tls', 'ca', 'network')
            action = self.pick('domains', choices, notice=' · '.join(entry['host'] for entry in cfg['domains']),
                               help_text=self.term.t('domains_help'))
            if action is None:
                return
            self.attempt(lambda: self.domain_action(action, cfg))

    def domain_action(self, action, cfg):
        if action.startswith('add_'):
            mode = action.removeprefix('add_')
            mode = 'local' if mode == 'local' else mode
            fields = ['name'] + (['email', 'external_port'] if mode != 'local' else [])
            if mode == 'direct':
                fields.append('publish')
            data = dict(name='', public=mode != 'local', email='', external_port=443, publish=False)
            draft = Form(self.view, action, data, fields, installed=True).run()
            if draft is None:
                return
            self.view.run(action, lambda: self.manager.add_domain(draft['name'], mode=mode, email=draft['email'],
                          external_port=draft['external_port'], publish_direct=draft['publish']),
                          context=[(self.term.t('name'), draft['name'])])
            name = draft['name']
            url = access_url(self.state.load(), next(entry for entry in self.state.load()['domains'] if entry['host'] == name))
            note = self.term.t('tunnel_help' if mode == 'tunnel' else 'direct_help' if mode == 'direct' else 'domain_added')
            if mode == 'tunnel':
                forward = '127.0.0.1' if cfg['bind'] == '0.0.0.0' else cfg['bind']
                url += f'\nHost: {name}\nForward: http://{forward}:{cfg["http_port"]}'
            self.report(action, url, notice=note)
        elif action == 'network':
            keys = ('bind', 'http_port', 'https_port')
            form = Form(self.view, 'network', {key: cfg[key] for key in keys}, keys, installed=True)
            while True:
                draft = form.run()
                if draft is None:
                    return
                candidate = copy.deepcopy(cfg)
                candidate.update(draft)
                try:
                    self.view.run('check_ports', lambda: Engine(self.state, candidate).check_ports(cfg))
                except (Error, OSError, ValueError) as exc:
                    field = next((key for key in keys[1:] if str(draft[key]) in str(exc)), 'bind')
                    form.invalid, form.selected = field, keys.index(field)
                    form.notice, form.notice_style, form.reviewing = self.view.local(str(exc)), 'error', False
                    continue
                self.perform(action, lambda: self.manager.settings(candidate))
                return
        elif action == 'ca':
            self.perform(action, lambda: Engine(self.state).ca_certificate(), notice=self.term.t('ca_help'))
        else:
            entries = [entry for entry in cfg['domains'] if action != 'local_tls' or entry['kind'] == 'local']
            if not entries:
                return self.report(action, self.term.t('no_addresses'), style='warning')
            name = self.view.pick(action, [(entry['host'], access_url(cfg, entry), '') for entry in entries])
            if name is None:
                return
            if action == 'remove':
                self.perform(action, lambda: self.manager.remove_domain(name), context=[(self.term.t('name'), name)])
            else:
                if action == 'primary':
                    cfg['primary'] = name
                else:
                    entry = next(entry for entry in cfg['domains'] if entry['host'] == name)
                    entry['tls'] = 'internal' if entry['tls'] == 'off' else 'off'
                self.perform(action, lambda: self.manager.settings(cfg), context=[(self.term.t('name'), name)])

    def backups(self):
        focus = ''
        while True:
            paths = sorted((self.state.path / 'backups').glob('*.ndm.tar'), reverse=True)
            rows = [('create', '▶ ' + self.term.t('backup'), 'ok'), ('export', self.term.t('export'), '')]
            rows += [(SEPARATOR, '', '')] + backup_rows(self.term, paths)
            selected = next((i for i, row in enumerate(rows) if row[0] == focus), 0)
            choice = self.view.pick('backups', rows, selected=selected, notice=self.term.t('backup_scope'),
                                    help_text=self.term.t('backup_list_help'), notice_style='warning')
            if choice is None:
                return
            if choice == 'create':
                path = self.attempt(lambda: self.perform('backup', self.manager.backup))
                focus = str(path) if path else ''
            elif choice == 'export':
                path = self.state.path / 'backups' / 'docker-images.tar'
                self.attempt(lambda: self.perform('export', lambda: Engine(self.state).export_images(path)))
            else:
                focus = choice
                self.attempt(lambda: self.backup_detail(Path(choice)))

    def backup_detail(self, path):
        # Read bounded descriptive metadata only. It is not a verification;
        # unpack() still checks the complete archive before verify/restore.
        with tarfile.open(path, 'r:') as source:
            member = source.getmember('manifest.json')
            if not member.isfile() or member.size > 1024 * 1024:
                raise Error('Invalid backup metadata')
            metadata = json.load(source.extractfile(member))
        if not isinstance(metadata, dict) or not isinstance(metadata.get('configuration'), dict):
            raise Error('Invalid backup metadata')
        cfg = metadata['configuration']
        stat = path.stat()
        verified = self.verified.get(str(path)) == (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        detail = self.term.t('backup_verified' if verified else 'backup_unverified')
        created = str(metadata.get('created', '—'))
        try:
            created = dt.datetime.strptime(created, '%Y%m%dT%H%M%SZ').strftime('%Y-%m-%d %H:%M UTC')
        except ValueError:
            pass
        info = pair(self.term, 'version', cfg.get('version', '?'))
        info += pair(self.term, 'database', str(cfg.get('images', {}).get('db', '?')).split('@')[0])
        info += pair(self.term, 'created', created)
        info += pair(self.term, 'kind', metadata.get('kind', '—'))
        info += pair(self.term, 'size', human(stat.st_size))
        info += pair(self.term, 'file', path)
        rows = [('info', label, tone) for label, tone in info] + [(SEPARATOR, '', '')]
        rows += [(key, self.term.t(key), 'warning' if key == 'restore' else 'accent' if key == 'verify' else '')
                 for key in ('verify', 'restore', 'back')]
        old_context = self.term.context
        self.term.context = path.name
        try:
            action = 'info'
            while action == 'info':
                action = self.view.pick('backup_detail', rows, selected=len(info) + 1, notice=detail,
                                        notice_style='ok' if verified else 'warning', help_text=self.term.t('backup_scope'))
            if action == 'verify':
                def verify():
                    with archive.unpack(self.state, path) as (_, manifest):
                        return {'valid': True, 'version': manifest['configuration']['version'], 'excluded': manifest['excluded']}
                self.perform('verify', verify, context=[(self.term.t('file'), path.name)])
                self.mark_verified(path)
            elif action == 'restore':
                self.view.confirm('USER-FILES-UNCHANGED')
                self.perform('restore', lambda: self.manager.restore(path, accept_user_files=True),
                             context=[(self.term.t('file'), path.name)])
                self.mark_verified(path)
        finally:
            self.term.context = old_context

    def updates(self, action):
        cfg = self.state.load()
        available = self.view.run('lookup_updates', self.manager.checks)
        choices = [cfg['version']] if action == 'refresh' else available['next_major' if action == 'major' else 'patches']
        if not choices:
            return self.report(action, available, notice=self.term.t('no_updates'), style='accent')
        target = self.view.pick(action, [(version, f'Nextcloud {cfg["version"]} → {version}', '') for version in choices],
                                notice=self.term.t('backup_scope'), help_text=self.term.t('updates_help'))
        if target is None:
            return
        if action == 'major':
            plan = self.view.run('check_requirements', lambda: self.manager.database_choices(target))
            if plan['required']:
                if not plan['choices']:
                    raise Error(self.term.t('no_database_path'))
                branch = self.view.pick('required_database', [
                    (entry['series'], f'MariaDB {entry["series"]} LTS · {entry["community_until"]}', '')
                    for entry in plan['choices']], notice=self.term.t('database_required'), notice_style='warning')
                if branch is None:
                    return
                self.perform('required_database', lambda: self.manager.migrate_database(branch, for_nextcloud=target),
                             context=[('MariaDB', branch + ' LTS'), ('Nextcloud', target)])
        self.perform(action, lambda: self.manager.update(target, major=action == 'major', refresh=action == 'refresh'),
                     context=[('Nextcloud', cfg['version'] + ' → ' + target)])

    def recover(self):
        pending = self.state.pending or {}
        if not pending:
            return self.report('recover', self.term.t('no_pending'), style='accent')
        action = self.view.pick('recover', [('resume', self.term.t('resume'), 'accent'),
                               ('restore', self.term.t('restore'), 'warning')],
                               notice=' · '.join(str(value) for value in (pending.get('action', pending.get('kind', '')), pending.get('phase', ''))),
                               help_text=self.term.t('pending'), notice_style='warning')
        if action is None:
            return
        acknowledged = action == 'restore' and pending.get('traffic_opened', False)
        if acknowledged:
            self.view.confirm('USER-FILES-UNCHANGED')
        self.perform('recover', lambda: self.manager.recover(action, accept_user_files=acknowledged))

    def action(self, action):
        if action == 'language':
            selected = self.view.pick('language', [('es', 'Español', ''), ('en', 'English', '')],
                                      selected=int(self.language == 'en'))
            if selected:
                self.language = selected
                self.term.msg = messages(selected)
                remember_language(self.state, selected)
            return
        if action in ('install', 'domains', 'backups', 'recover'):
            return getattr(self, action)()
        if action in ('updates', 'major', 'refresh'):
            return self.updates(action)
        if action == 'components':
            component = self.view.pick(action, [(role, SERVICES[role], '') for role in ('db', 'redis', 'proxy')],
                                       notice=self.term.t('components_help'))
            if component:
                self.perform(action, lambda: self.manager.update_component(component))
            return
        if action == 'logs':
            service = self.view.pick(action, [(role, name, '') for role, name in SERVICES.items()])
            if service:
                data = self.view.run(action, lambda: Engine(self.state).dc('logs', '--no-color', '--tail', '200', service).stdout)
                self.view.view(action, [(line, '') for line in data.splitlines()], newest=True, horizontal=True,
                               notice=SERVICES[service], style='accent')
            return
        if action == 'maintenance':
            selected = self.pick(action, ('run_checks', 'enable', 'disable', 'schedule_status'),
                                 help_text=self.term.t('maintenance_help'))
            if selected:
                lookup = {'enable': 'enable', 'disable': 'disable', 'schedule_status': 'status'}
                self.perform(action, self.manager.maintenance if selected == 'run_checks'
                             else lambda: self.manager.schedule(lookup[selected]))
            return
        if action == 'self_update':
            source = self.view.edit('source', '', folder, hint=self.term.t('self_update_help'))
            if source:
                self.perform(action, lambda: self.manager.self_update(source))
            return
        if action == 'setup':
            return self.perform(action, self.manager.setup_check)
        if action == 'backup':
            return self.perform(action, self.manager.backup)
        if action == 'stop':
            choice = self.pick('confirm_stop', ('back', 'stop'), notice=self.term.t('stop_help'), notice_style='warning')
            if choice != 'stop':
                return
        from .cli import dispatch
        from argparse import Namespace
        self.perform(action, lambda: dispatch(self.manager, Namespace(command=action)), style='accent' if action in ('status', 'diagnose') else 'ok')

    def run(self):
        while True:
            if self.state.configured:
                try:
                    self.snapshot = self.view.run('load_status', self.manager.status)
                except (Error, OSError, ValueError):
                    self.snapshot = None
            action = self.term.run(lambda: dashboard(self.term, self.state, last=self.last,
                                   notice=self.notice, notice_style=self.notice_style, status=self.snapshot))
            self.notice = ''
            if action in (None, 'exit'):
                return
            self.last = action
            self.attempt(lambda: self.action(action))
