"""Human-readable terminal rows. Machine-readable CLI output stays in cli.py."""
import datetime as dt
from pathlib import Path
import textwrap
import re

from .core import human
from .config import access_url
from .terminal_ui import clean


SERVICES = {'app': 'Nextcloud', 'db': 'MariaDB', 'redis': 'Redis',
            'proxy': 'Caddy / HTTPS', 'cron': 'Cron'}


def domain_summary(ui, cfg):
    def addresses(kind):
        values = [access_url(cfg, entry) for entry in cfg['domains'] if entry['kind'] == kind]
        return ', '.join(values[:2]) + (' …' if len(values) > 2 else '') if values else ui.t('domains_not_configured')
    return '\n'.join((ui.t('domains_summary_primary', access_url(cfg)),
                      ui.t('domains_summary_local', addresses('local')),
                      ui.t('domains_summary_public', addresses('public'))))


def domain_actions(ui):
    rows = []
    for key in ('add_local', 'add_direct', 'add_tunnel', 'remove', 'primary', 'local_tls', 'ca', 'network'):
        tone = {'add_tunnel': 'accent', 'add_direct': 'accent', 'remove': 'warning', 'primary': 'ok'}.get(key, '')
        rows.append((key, [(ui.t(key), tone), ('  ' + ui.t('domains_tag_' + key), 'dim>')], tone))
    return rows


def local(ui, text):
    """Lifecycle messages are 'español / English'; show only the interface language."""
    parts = str(text).split(' / ')
    if len(parts) != 2:
        return str(text)
    return parts[1] if ui.msg.get('locale') == 'en' else parts[0]


def operation_summary(ui, pending):
    action = pending.get('action', pending.get('kind', ''))
    phase = pending.get('phase', '')
    names = [ui.msg.get('op_' + action, action), ui.msg.get('phase_' + phase, phase)]
    return ui.t('pending_summary', *names)


def update_rows(ui, cfg, available):
    current, rows = cfg['version'], []
    for version in available.get('patches') or []:
        rows.append(('patch:' + version, [(f'Nextcloud {current} → {version}', 'ok'),
                                          ('  ' + ui.t('updates_tag_patch'), 'dim>')], ''))
    # A major upgrade starts from the latest patch, so it stays visible but dimmed until then.
    blocked = bool(available.get('patches'))
    for version in available.get('next_major') or []:
        rows.append(('major:' + version, [(f'Nextcloud {current} → {version}', 'dim' if blocked else 'warning'),
                                          ('  ' + ui.t('updates_tag_major'), 'dim>')], ''))
    if available.get('refreshed_image'):
        rows.append(('refresh', [(ui.t('updates_refresh_label', current), 'accent'),
                                 ('  ' + ui.t('updates_tag_refresh'), 'dim>')], ''))
    for role, item in (available.get('components') or {}).items():
        if isinstance(item, dict) and item.get('image_changed'):
            rows.append(('component:' + role, [(f'{SERVICES[role]} {item["current"]} → {item["available"]}', ''),
                                               ('  ' + ui.t('updates_tag_component'), 'dim>')], ''))
    return rows


def support_summary(ui, support):
    state = support.get('state', 'unknown')
    until = support.get('support_until', '')
    date = until[:7] if support.get('precision') == 'month' else until
    if state == 'eol':
        return ui.t('support_eol', support.get('major', '?')), 'error'
    if state == 'ending':
        return ui.t('support_ending', date), 'warning'
    if state == 'supported':
        return ui.t('support_active', date), 'ok'
    return ui.t('support_unknown'), 'warning'


def support_rows(ui, support):
    if not support:
        return []
    text, tone = support_summary(ui, support)
    rows = pair(ui, 'support', text, tone)
    if support.get('support_until'):
        date = support['support_until'][:7] if support.get('precision') == 'month' else support['support_until']
        rows += pair(ui, 'support_until', date, tone)
    if support.get('stale'):
        rows += [(ui.t('support_cached'), 'warning')]
    if support.get('checked'):
        rows += pair(ui, 'support_checked', support['checked'], 'dim')
    return rows


def release_rows(ui, releases):
    result = []
    for release in releases:
        version = release['version']
        until = release['support_until'][:7] if release.get('precision') == 'month' else release['support_until']
        label = f'Nextcloud {version} · {ui.t("release_" + release["recommendation"])} · {ui.t("release_until", until)}'
        result.append((version, label, 'ok' if release['recommendation'] == 'latest' else 'accent'))
    return result


def pair(ui, label, value, tone='accent', width=76):
    """Aligned dim labels and colored values, wrapping paths rather than hiding them."""
    label = ui.msg.get('label_' + label, ui.t(label))
    column = min(25, width // 2)
    label = label if len(label) < column - 1 else label[:column - 3] + '…'
    parts = textwrap.wrap(clean(value), max(12, width - column - 6)) or ['—']
    return [([(label.ljust(column) if i == 0 else ' ' * column, 'dim'), (part, tone)], '')
            for i, part in enumerate(parts)]


def section(ui, name):
    return [('', ''), ('§ ' + ui.t(name), 'title')]


def status_rows(ui, data, cfg):
    rows = support_rows(ui, data.get('support'))
    for entry in data.get('access', []):
        rows += pair(ui, 'access_' + entry.get('kind', 'local'), entry.get('url', entry.get('host', '')))
    state = data.get('status', {})
    rows += pair(ui, 'web_setup', ui.t('yes' if state.get('installed') else 'pending_value'),
                 'ok' if state.get('installed') else 'warning')
    rows += pair(ui, 'maintenance_mode', ui.t('yes' if state.get('maintenance') else 'no'),
                 'warning' if state.get('maintenance') else 'ok')
    if state.get('needsDbUpgrade'):
        rows += pair(ui, 'database_schema', ui.t('schema_pending'), 'warning')
    if data.get('operation'):
        rows += pair(ui, 'pending_operation', data['operation'].get('action', data['operation'].get('kind', '—')), 'warning')
    rows += section(ui, 'group_service')
    by_service = {row.get('Service'): row for row in data.get('services', [])}
    for role, name in SERVICES.items():
        item = by_service.get(role, {})
        active, health = item.get('State') == 'running', item.get('Health')
        tone = 'warning' if health == 'starting' else 'ok' if active and health != 'unhealthy' else 'error'
        marker = {'ok': '[+]', 'warning': '[!]'}.get(tone, '[✗]')
        version = cfg.get('version', '') if role in ('app', 'cron') else cfg.get('images', {}).get(role, '').split('@')[0].split(':')[-1]
        state_key = 'service_' + ('starting' if health == 'starting' else 'unhealthy' if health == 'unhealthy'
                                else 'running' if active else 'stopped' if item else 'missing')
        rows.append((f'{marker} {name} {version} · {ui.t(state_key)}', tone))
        if item.get('Ports'):
            rows += pair(ui, 'ports', item['Ports'])
    rows += section(ui, 'paths')
    rows += pair(ui, 'storage', ui.t(cfg.get('storage', 'directory')))
    if cfg.get('storage') == 'directory':
        rows += pair(ui, 'storage_root', cfg.get('storage_root', ''))
    return rows


def result_rows(ui, action, result, state):
    if action in ('status', 'install', 'setup', 'setup-check') and isinstance(result, dict) and 'services' in result:
        return status_rows(ui, result, state.load())
    if action == 'diagnose':
        host = result.get('host', {})
        rows = pair(ui, 'os', host.get('os', {}).get('PRETTY_NAME', '—'))
        rows += pair(ui, 'architecture', host.get('architecture', '—'))
        rows += pair(ui, 'address', host.get('address', '—'))
        rows += pair(ui, 'disk_free', human(result.get('disk_free', 0)))
        rows += pair(ui, 'sql_application_free_bytes', human(result.get('sql_application_free_bytes', 0)))
        rows += section(ui, 'group_service')
        rows += status_rows(ui, result.get('state', {}), state.load())
        if result.get('last_maintenance'):
            rows += section(ui, 'last_maintenance')
            rows += [(clean(str(result['last_maintenance'].get('time', ''))), '')]
            rows += [(clean(local(ui, error)), 'warning') for error in result['last_maintenance'].get('errors', [])]
        return rows
    if isinstance(result, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', result):
        return pair(ui, 'version', 'Nextcloud ' + result, 'ok')
    if isinstance(result, Path) or isinstance(result, str) and result.startswith('/') and '\n' not in result:
        rows = pair(ui, 'file', result)
        path = Path(result)
        if path.is_file():
            rows += pair(ui, 'size', human(path.stat().st_size))
        if action == 'backup':
            rows += [(ui.t('backup_scope'), 'warning')]
        return rows
    if isinstance(result, str):
        return [(clean(line), '') for paragraph in result.splitlines() for line in textwrap.wrap(paragraph, 68)]
    # Structured results use translated labels and readable sections, never JSON
    # or a dump of config/environment. Explicitly omit all credential fields.
    blocked = {'passwords', 'Env', 'Command', 'configuration', 'Config'}
    def expand(value, depth=0):
        rows = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in blocked or key == 'support' or any(word in key.casefold() for word in ('password', 'secret', 'token')):
                    continue
                if key == 'services':
                    continue
                if isinstance(item, (dict, list)) and item:
                    rows += section(ui, key)
                    rows += expand(item, depth + 1)
                elif isinstance(item, bool):
                    rows += pair(ui, key, ui.t('yes' if item else 'no'), 'ok' if item else 'dim')
                elif item not in (None, '', [], {}):
                    shown = human(item) if key.endswith(('_free', '_free_bytes', '_bytes')) and isinstance(item, int) else str(item)
                    rows += pair(ui, key, shown)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    rows += expand(item, depth + 1)
                else:
                    for line in textwrap.wrap(clean(local(ui, item)), 68):
                        rows.append(('· ' + line, 'warning' if depth == 1 and action == 'maintenance' else ''))
        else:
            rows += [(clean(value), '')]
        return rows
    rows = support_rows(ui, result.get('support')) if isinstance(result, dict) else []
    return rows + expand(result) or [(ui.t('operation_done'), 'ok')]


def backup_rows(ui, paths):
    rows = []
    for path in paths:
        moment = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        rows.append((str(path), [(moment + ' · ', 'dim'), (path.name, 'accent'),
                                ('  ' + human(path.stat().st_size), 'dim>')], ''))
    return rows
