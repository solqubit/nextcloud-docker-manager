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
from .config import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT, access_url, host, port, prefer_first_tunnel, public_host
from .console import Console
from .core import Cancelled, Error, human
from .dashboard import dashboard
from .docker import Engine
from .i18n import messages, remember_language, remembered_language
from .system import host_info
from .terminal_ui import INFO, SEPARATOR, Terminal
from .views import (SERVICES, backup_rows, domain_actions, domain_summary, operation_summary, pair, release_rows,
                    result_rows, support_summary, update_rows)


def folder(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or any(ord(char) < 32 for char in value):
        raise Error('Use an absolute folder / Usa una carpeta absoluta')
    return str(path)


class Form:
    """In-memory drafts. Escape never writes configuration or changes services."""
    def __init__(self, console, title, data, fields, *, installed=False, versions=(), releases=(), submit=None):
        self.view, self.ui, self.title = console, console.term, title
        self.data, self.original = copy.deepcopy(data), copy.deepcopy(data)
        self._fields, self.installed, self.versions = fields, installed, versions
        self.releases = releases
        self.submit = submit
        self.selected, self.invalid = 0, ''
        self.notice, self.notice_style = self.ui.t('install_form_intro' if submit == 'install' else 'form_intro'), 'accent'
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
            if not self.installed and (address.is_unspecified or address.is_global
                                       or self.data.get('environment') == 'vps' and not address.is_loopback):
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
            # [+] marks only what this draft changed; untouched values stay neutral.
            marker = '[!]' if pending else '[✗]' if invalid else '[+]' if changed else '[·]'
            value = self.display(key)
            room = max(4, width - column - len(state) - 10)
            if len(value) > room:
                value = value[:room - 1] + '…'
            label = [(marker + ' ', 'warning' if pending else 'error' if invalid else 'ok' if changed else 'dim'),
                     (names[key][:column - 2].ljust(column), ''),
                     (value, 'warning' if pending else 'error' if invalid else 'accent'),
                     ('  ' + state, tone + '>')]
            rows.append((key, label, 'error' if invalid and not pending else ''))
        rows += [(SEPARATOR, '', '')]
        action = 'apply' if review or self.submit else 'review'
        label = self.submit or ('apply_changes' if self.installed else 'deploy' if review else 'review')
        rows += [(action, '▶ ' + self.ui.t(label), 'ok')]
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
                        number = port(text)
                        return self.ui.t('https_reserved', text) if key == 'https_port' else self.ui.t(
                            'access_preview', f'http://{data.get("address", data.get("bind", "127.0.0.1"))}' +
                            (f':{number}' if number != 80 else ''))
                    except (Error, ValueError):
                        return ''
            value = self.view.edit(key, data[key], lambda text: self.normalize(key, text),
                                   hint=self.ui.t('field_help_' + key), default=self.original[key], preview=preview)
        if value is not None:
            self.data[key] = value
            self.invalid = ''
            self.notice, self.notice_style = self.ui.t('field_saved', self.ui.t(key)), 'ok'
            if key == 'environment' and value == 'vps' and (data.get('bind'), data.get('address')) != ('127.0.0.1',) * 2:
                self.data.update(bind='127.0.0.1', address='127.0.0.1')
                self.notice = self.ui.t('vps_loopback_set')

    def field_help(self, key):
        if key in self.fields:
            full = self.display(key)
            hint = self.ui.msg.get('field_help_' + key, self.ui.t('form_edit_hint'))
            return full + ' · ' + hint
        if key == 'apply' and self.submit == 'install':
            return self.ui.t('install_submit_help')
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

    def menu(self, title, rows, *, help_text='', **kwargs):
        """Submenus end with an explicit Back row; Back and Esc both return None."""
        rows = list(rows) + [(SEPARATOR, '', ''), ('back', '← ' + self.term.t('back'), '')]
        def describe(key):
            if key == 'back':
                return self.term.t('back_help')
            return help_text(key) if callable(help_text) else help_text
        choice = self.view.pick(title, rows, help_text=describe, **kwargs)
        return None if choice == 'back' else choice

    def notice_for(self, text, style='accent'):
        """Show the last result inside the submenu where it happened, then forget it,
        so the dashboard never repeats a stale «Cancelled» or «Completed»."""
        if not self.notice:
            return text, style
        notice, tone = self.notice + ('\n' + text if text else ''), self.notice_style
        self.notice = ''
        return notice, tone

    def report(self, action, data=None, *, notice='', style='ok'):
        if isinstance(data, dict) and (data.get('errors') or data.get('warnings')):
            style = 'warning'
            notice = self.term.t('checks_warning')
        if isinstance(data, dict) and data.get('support', {}).get('state') in ('eol', 'ending', 'unknown'):
            notice, style = support_summary(self.term, data['support'])
        if isinstance(data, dict) and data.get('initial_maintenance_deferred'):
            notice, style = self.term.t('setup_repairs_deferred'), 'warning'
        if isinstance(data, dict) and data.get('core_integrity_ok') is False:
            notice, style = self.term.t('setup_integrity_warning'), 'warning'
        rows = result_rows(self.term, action, data, self.state)
        if action in ('install', 'setup') and self.state.configured:
            cfg = self.state.load()
            rows = pair(self.term, 'access_local', access_url(cfg)) + rows
            if not (data or {}).get('installed'):
                notice, style = self.term.t('web_setup_help'), 'warning'
                if cfg['environment'] == 'vps':
                    rows += pair(self.term, 'ssh_forward', f'ssh -L 8080:{cfg["bind"]}:{cfg["http_port"]} usuario@servidor')
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
        data = dict(version=version, environment=environment, bind=address, address=address, http_port=DEFAULT_HTTP_PORT,
                    https_port=DEFAULT_HTTPS_PORT, timezone='UTC', storage='directory', storage_root=str(self.state.path / 'data'))
        form = Form(self.view, 'configure', data, tuple(data), versions=versions, releases=releases, submit='install')
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
            local_url = f'http://{draft["address"]}' + (f':{draft["http_port"]}' if draft['http_port'] != 80 else '')
            try:
                self.perform('install', lambda: self.manager.install(version, language=self.language, **draft),
                             context=[('Nextcloud', version), (self.term.t('access_local'), local_url)])
            except Cancelled:
                if self.state.configured or self.state.pending:
                    raise
                form.notice, form.notice_style = self.term.t('install_cancelled'), 'accent'
                continue
            return

    def domains(self):
        while True:
            cfg = self.state.load()
            notice, tone = self.notice_for(domain_summary(self.term, cfg))
            action = self.menu('domains', domain_actions(self.term), notice=notice, notice_style=tone,
                               help_text=lambda key: self.term.t('domains_help_' + key))
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
            form = Form(self.view, action, data, fields, installed=True, submit='save_access')
            if prefer_first_tunnel(cfg, mode):
                form.notice = self.term.t('tunnel_first_primary')
            draft = form.run()
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
                if self.state.load()['primary'] == name:
                    note = self.term.t('tunnel_primary_saved')
            self.report(action, url, notice=note)
        elif action == 'network':
            keys = ('bind', 'http_port', 'https_port')
            # One review only: the confirmation screen after the form already lists the change.
            form = Form(self.view, 'network', {key: cfg[key] for key in keys}, keys, installed=True, submit='apply_changes')
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
            def label(entry):
                text = access_url(cfg, entry)
                if action == 'local_tls':
                    # Selecting toggles: say what each address has now.
                    text += ' · ' + self.term.t('tls_on' if entry['tls'] != 'off' else 'tls_off')
                if entry['host'] == cfg['primary']:
                    text += ' · ' + self.term.t('domains_current_primary')
                return text
            name = self.view.pick(action, [
                (entry['host'], label(entry), 'ok' if entry['host'] == cfg['primary'] else '') for entry in entries],
                selected=next((i for i, entry in enumerate(entries) if entry['host'] == cfg['primary']), 0))
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
            rows += [(SEPARATOR, '', '')] + (backup_rows(self.term, paths) or [(INFO, self.term.t('no_backups'), 'dim')])
            selected = next((i for i, row in enumerate(rows) if row[0] == focus), 0)
            notice, tone = self.notice_for(self.term.t('backup_scope'), 'warning')
            choice = self.menu('backups', rows, selected=selected, notice=notice, notice_style=tone,
                               help_text=lambda key: self.term.t('export_help') if key == 'export'
                               else self.term.t('backup_list_help'))
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
        rows = [(INFO, label, tone) for label, tone in info] + [(SEPARATOR, '', '')]
        rows += [(key, ('← ' if key == 'back' else '') + self.term.t(key),
                  'warning' if key in ('restore', 'delete_backup') else 'accent' if key == 'verify' else '')
                 for key in ('verify', 'restore', 'delete_backup', 'back')]
        old_context = self.term.context
        self.term.context = path.name
        try:
            action = self.view.pick('backup_detail', rows, selected=len(info) + 1, notice=detail,
                                    notice_style='ok' if verified else 'warning',
                                    help_text=lambda key: self.term.t('delete_help' if key == 'delete_backup' else 'backup_scope'))
            if action == 'delete_backup':
                choice = self.view.pick('confirm_delete_backup', [
                    ('back', '← ' + self.term.t('cancel'), 'accent'),
                    ('delete', self.term.t('delete_backup'), 'warning')],
                    notice=path.name, notice_style='warning', help_text=self.term.t('delete_help'))
                if choice == 'delete':
                    path.unlink()
                    self.verified.pop(str(path), None)
                    self.notice, self.notice_style = self.term.t('backup_deleted', path.name), 'ok'
            elif action == 'verify':
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

    def updates(self):
        """One screen for every update: patches, next major, image refresh and components.
        The catalog is consulted once per visit and again after each applied change."""
        while True:
            cfg = self.state.load()
            available = self.view.run('lookup_updates', self.manager.checks)
            rows = update_rows(self.term, cfg, available)
            if not rows:
                self.notice = ''
                return self.report('updates', available, notice=self.term.t('updates_none'), style='ok')
            text = self.term.t('backup_scope')
            if available.get('patches') and available.get('next_major'):
                text = self.term.t('major_needs_patch') + '\n' + text
            notice, tone = self.notice_for(text)
            def describe(key):
                kind = key.partition(':')[0]
                if kind == 'major' and available.get('patches'):
                    return self.term.t('major_needs_patch')
                return self.term.t('components_help' if kind == 'component' else 'updates_help')
            choice = self.menu('updates', rows, notice=notice, notice_style=tone, help_text=describe)
            if choice is None:
                self.notice = ''
                return
            self.attempt(lambda: self.apply_update(choice, cfg, available))

    def apply_update(self, choice, cfg, available):
        kind, _, target = choice.partition(':')
        if kind == 'component':
            return self.perform('components', lambda: self.manager.update_component(target),
                                context=[(SERVICES[target], available['components'][target]['available'])])
        if kind == 'major' and available.get('patches'):
            self.notice, self.notice_style = self.term.t('major_needs_patch'), 'warning'
            return
        if kind == 'refresh':
            target = cfg['version']
        action = {'patch': 'updates', 'major': 'major', 'refresh': 'refresh'}[kind]
        if kind == 'major':
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
        self.perform(action, lambda: self.manager.update(target, major=kind == 'major', refresh=kind == 'refresh'),
                     context=[('Nextcloud', cfg['version'] + ' → ' + target)])

    def recover(self):
        pending = self.state.pending or {}
        if not pending:
            return self.report('recover', self.term.t('no_pending'), style='accent')
        rows = [('resume', self.term.t('resume'), 'accent')]
        # Restore needs the operation's own backup (or the original SQL volume of an LTS change).
        if pending.get('backup') or pending.get('action') == 'db-migrate':
            rows.append(('restore', self.term.t('restore'), 'warning'))
        action = self.menu('recover', rows, notice=operation_summary(self.term, pending),
                           help_text=self.term.t('pending'), notice_style='warning')
        if action is None:
            return
        # Operations whose journal proves the user files untouched restore without the acknowledgement.
        guarded = ('update', 'migrate', 'db-update', 'component-update', 'db-migrate', 'restore')
        acknowledged = action == 'restore' and (pending.get('traffic_opened', False) or pending.get('action') not in guarded)
        if acknowledged:
            self.view.confirm('USER-FILES-UNCHANGED')
        self.perform('recover', lambda: self.manager.recover(action, accept_user_files=acknowledged))

    def logs(self):
        service = None
        while True:
            roles = list(SERVICES)
            selected = roles.index(service) if service in roles else 0
            service = self.menu('logs', [(role, name, '') for role, name in SERVICES.items()],
                                selected=selected, help_text=self.term.t('logs_help'))
            if not service:
                return
            def show(role=service):
                data = self.view.run('logs', lambda: Engine(self.state).dc('logs', '--no-color', '--tail', '200', role).stdout)
                self.view.view('logs', [(line, '') for line in data.splitlines()], newest=True, horizontal=True,
                               notice=SERVICES[role], style='accent')
            self.attempt(show)

    def maintenance(self):
        while True:
            enabled = self.manager.schedule_enabled()
            rows = [('run_checks', self.term.t('run_checks'), 'ok')]
            if enabled is not None:
                rows += [('disable', self.term.t('disable'), 'warning') if enabled else ('enable', self.term.t('enable'), 'accent'),
                         ('schedule_status', self.term.t('schedule_status'), '')]
            state = self.term.t('schedule_unavailable' if enabled is None else 'schedule_on' if enabled else 'schedule_off')
            notice, tone = self.notice_for(state)
            selected = self.menu('maintenance', rows, notice=notice, notice_style=tone,
                                 help_text=self.term.t('maintenance_help'))
            if not selected:
                return
            lookup = {'enable': 'enable', 'disable': 'disable', 'schedule_status': 'status'}
            self.attempt(lambda: self.perform('maintenance', self.manager.maintenance if selected == 'run_checks'
                                              else lambda: self.manager.schedule(lookup[selected])))

    def action(self, action):
        if action == 'language':
            selected = self.view.pick('language', [('es', 'Español', ''), ('en', 'English', '')],
                                      selected=int(self.language == 'en'))
            if selected:
                self.language = selected
                self.term.msg = messages(selected)
                remember_language(self.state, selected)
                self.notice, self.notice_style = self.term.t('language_changed'), 'ok'
            return
        if action in ('install', 'domains', 'backups', 'recover', 'updates', 'logs', 'maintenance'):
            return getattr(self, action)()
        if action == 'self_update':
            source = self.view.edit('source', '', folder, hint=self.term.t('self_update_help'))
            if source:
                self.perform(action, lambda: self.manager.self_update(source))
            return
        if action == 'setup':
            return self.perform(action, self.manager.setup_check)
        if action == 'stop':
            choice = self.view.pick('confirm_stop', [('back', '← ' + self.term.t('back'), 'accent'),
                                                     ('stop', self.term.t('stop'), 'warning')],
                                    notice=self.term.t('stop_help'), notice_style='warning')
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
