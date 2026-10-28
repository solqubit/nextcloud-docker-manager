"""The grouped GLPI dashboard, adapted to Nextcloud's actual operations."""
from .config import access_url
from .console import BANNER
from .terminal_ui import curses
from .views import support_summary


GROUPS = [
    ('group_service', ('status', 'setup', 'start', 'stop', 'logs')),
    ('group_data', ('updates', 'backups', 'recover')),
    ('group_settings', ('domains', 'language')),
    ('group_checks', ('maintenance', 'diagnose')),
    ('group_manager', ('self_update', 'exit')),
]
STYLES = {'stop': 'warning', 'recover': 'warning', 'setup': 'ok', 'exit': 'accent'}
# With an interrupted operation only recovery and read-only actions make sense.
PENDING = {'recover', 'status', 'stop', 'logs', 'backups', 'maintenance', 'diagnose', 'language', 'self_update', 'exit'}


def layout(ui, pending=False, configured=True, web_setup=False):
    groups = GROUPS if configured else [('group_install', ('install', 'language', 'exit'))]
    if not configured and pending:
        groups = [('group_install', ('recover', 'install', 'language', 'exit'))]
    columns = [groups[:2], groups[2:]] if ui.width >= 66 and configured else [groups]
    cells, headers = [], []
    for column, entries in enumerate(columns):
        row = 0
        for title, actions in entries:
            visible = [value for value in actions
                       if (value != 'recover' or pending) and (value != 'setup' or web_setup)
                       and (not pending or not configured or value in PENDING)]
            if not visible:
                continue
            headers.append((column, row, ui.t(title)))
            row += 1
            for value in visible:
                cells.append(dict(value=value, label=ui.t(value), column=column, row=row))
                row += 1
    return cells, headers, len(columns)


def move(cells, selected, step):
    options = [i for i, cell in enumerate(cells) if cell['column'] == cells[selected]['column']]
    return options[(options.index(selected) + step) % len(options)]


def switch(cells, selected, columns):
    target = (cells[selected]['column'] + 1) % columns
    options = [i for i, cell in enumerate(cells) if cell['column'] == target]
    return min(options, key=lambda i: abs(cells[i]['row'] - cells[selected]['row'])) if options else selected


def dashboard(ui, state, *, last='', notice='', notice_style='accent', status=None):
    configured, pending = state.configured, bool(state.pending)
    cfg = state.load() if configured else {}
    # «Check web installation» is only offered, and preselected, until it has completed.
    web_setup = configured and cfg['status'] != 'ready'
    if configured:
        ui.context = f'Nextcloud {cfg["version"]} · MariaDB {cfg["images"]["db"].split("@")[0].split(":")[-1]}'
        app = (status or {}).get('status', {})
        services = (status or {}).get('services', [])
        stopped = any(row.get('State') != 'running' for row in services)
        label = ('service_maintenance' if app.get('maintenance') else 'service_stopped' if stopped else
                 'service_ready' if app.get('installed') else 'service_web_setup' if cfg['status'] != 'ready'
                 else 'service_unavailable')
        summary = ui.t(label) + ' · ' + access_url(cfg) + ' · ' + ui.t('environment_' + cfg['environment'])
        tone = 'warning' if label != 'service_ready' or pending else 'accent'
        support = (status or {}).get('support', {})
        if support.get('state', 'unknown') != 'supported' or support.get('stale'):
            support_text, support_tone = support_summary(ui, support)
            summary = support_text + ' · ' + access_url(cfg)
            tone = 'warning' if support_tone == 'ok' else support_tone
    else:
        ui.context = str(state.path)
        summary, tone = ui.t('install_intro'), 'accent'
    if pending:
        summary, tone = ui.t('pending'), 'warning'
        if (status or {}).get('support', {}).get('obsolete'):
            summary = support_summary(ui, status['support'])[0] + ' · ' + summary
            tone = 'error'
    if ui.screen is None:
        ui.width = 76
        cells, _, _ = layout(ui, pending, configured, web_setup)
        return ui.menu(ui.t('menu'), [(cell['value'], cell['label'], STYLES.get(cell['value'], ''))
                                     for cell in cells], notice=notice or summary)
    selected = None
    while True:
        ui.measure()
        top, height = ui.frame(ui.t('menu'), summary[:ui.width - 4], tone)
        cells, headers, columns = layout(ui, pending, configured, web_setup)
        if not configured and ui.width >= 66 and height >= 24:
            for n, line in enumerate(BANNER):
                ui.put(top + n, ui.left, line, 'title')
            top += len(BANNER) + 1
        if selected is None or selected >= len(cells):
            initial = 'recover' if pending else 'setup' if web_setup else last
            selected = next((i for i, cell in enumerate(cells) if cell['value'] == initial), 0)
        width = ui.width // columns
        visible = max(1, height - top - 3)
        offset = max(0, cells[selected]['row'] - visible + 1)
        for column, row, title in headers:
            if offset <= row < offset + visible:
                ui.put(top + row - offset, ui.left + column * width,
                       ('─ ' + title + ' ').ljust(width - 2, '─'), 'title')
        for index, cell in enumerate(cells):
            if offset <= cell['row'] < offset + visible:
                active = index == selected
                text = ('> ' if active else '  ') + cell['label']
                ui.put(top + cell['row'] - offset, ui.left + cell['column'] * width,
                       text[:width - 2].ljust(width - 2),
                       'selected' if active else STYLES.get(cell['value'], ''))
        if notice:
            marker = {'ok': '[+]', 'warning': '[!]', 'error': '[✗]'}.get(notice_style, '[*]')
            ui.put(height - 2, ui.left, marker + ' ' + notice, notice_style)
        else:
            ui.put(height - 2, ui.left, '[*] ' + ui.t('desc_' + cells[selected]['value']), 'dim')
        legend = ui.t('dashboard_keys' if columns > 1 else 'menu_keys')
        ui.put(height - 1, ui.left, legend if len(legend) <= ui.width else ui.t('menu_keys_short'), 'accent')
        ui.screen.refresh()
        key = ui.key()
        notice = ''
        if key in ('\n', '\r', curses.KEY_ENTER):
            return cells[selected]['value']
        if key == '\x1b':
            # Esc means «back» everywhere else; here the first press only moves to Exit,
            # so repeated Esc presses from a submenu never close the manager by accident.
            exit_cell = next(i for i, cell in enumerate(cells) if cell['value'] == 'exit')
            if selected == exit_cell:
                return 'exit'
            selected = exit_cell
            notice, notice_style = ui.t('exit_confirm_hint'), 'accent'
        if key == curses.KEY_DOWN:
            selected = move(cells, selected, 1)
        elif key == curses.KEY_UP:
            selected = move(cells, selected, -1)
        elif key in (curses.KEY_RIGHT, curses.KEY_LEFT):
            selected = switch(cells, selected, columns)
        elif key == '\t':
            selected = (selected + 1) % len(cells)
        elif key == curses.KEY_BTAB:
            selected = (selected - 1) % len(cells)
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = next(i for i, cell in enumerate(cells) if cell['value'] == 'exit')
