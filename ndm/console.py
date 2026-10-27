"""GLPI's terminal presentation: cards, marked messages and timed Braille steps.

Lifecycle functions remain synchronous. Only the spinner runs in a thread;
command output stays in the private log and never becomes a terminal command.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import textwrap
import threading
import time

from .core import Cancelled, VERSION, presentation
from .terminal_ui import clean


FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
BANNER = (
    r'    _   __          __       __                __',
    r'   / | / /__  _  __/ /______/ /___  __  _______/ /',
    r'  /  |/ / _ \| |/_/ __/ ___/ / __ \/ / / / __  /',
    r' / /|  /  __/>  </ /_/ /__/ / /_/ / /_/ / /_/ /',
    r'/_/ |_/\___/_/|_|\__/\___/_/\____/\__,_/\__,_/',
)
TONES = {'title': '1;35', 'accent': '1;36', 'info': '1;34', 'ok': '1;32',
         'warning': '1;33', 'error': '1;31', 'dim': '2', 'bold': '1'}


def clock(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


class OutputLog(io.TextIOBase):
    """Capture Python notices too; Docker output is already logged by Runner."""
    def __init__(self, target, original):
        self.target, self.original = target, original

    def write(self, value):
        if self.target:
            with self.target.open('a', encoding='utf-8') as out:
                out.write(value)
        return len(value)

    def flush(self):
        pass

    def isatty(self):
        return self.original.isatty()

    def fileno(self):
        return self.original.fileno()


class Console:
    def __init__(self, terminal, state=None):
        self.term, self.state = terminal, state
        self.stdout, self.stream = sys.stdout, sys.stderr
        self.animated = (sys.stdin.isatty() and self.stream.isatty()
                         and os.environ.get('TERM', 'dumb') != 'dumb'
                         and os.environ.get('NDM_UI') != 'plain')
        self.colors = self.animated and 'NO_COLOR' not in os.environ
        self.running = False
        self.completed = []
        self.context = []
        self.label = self.detail = ''
        self.started = 0
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()

    def t(self, key, *values):
        return self.term.t(key, *values)

    @property
    def language(self):
        return self.term.msg.get('locale', 'es')

    def local(self, text):
        parts = str(text).split(' / ')
        return parts[-1] if self.language == 'en' and len(parts) == 2 else parts[0]

    def style(self, text, tone=''):
        text = clean(text)
        return f'\033[{TONES[tone]}m{text}\033[0m' if self.colors and tone in TONES else text

    def dimensions(self):
        try:
            columns, rows = os.get_terminal_size(self.stream.fileno())
        except (OSError, AttributeError, ValueError):
            columns, rows = shutil.get_terminal_size((80, 24))
        return max(20, min(100, columns - 8)), rows

    def header(self):
        width, _ = self.dimensions()
        title = 'Nextcloud Docker Manager'
        subtitle = f' v{VERSION} · {self.t("ui_subtitle")}'
        if len(title + subtitle) > width + 4:
            subtitle = f' v{VERSION}'
        return ('  ' + self.style(title, 'title') + self.style(subtitle, 'dim') + '\n  '
                + self.style('─' * (width + 4), 'title') + '\n\n')

    def card(self, title, rows, tone='title'):
        width, _ = self.dimensions()
        labels = [len(str(row[0])) for row in rows if isinstance(row, tuple)]
        column = min(width // 2, max(12, max(labels, default=0) + 3))
        lines = ['  ' + self.style('╭' + '─' * (width + 2) + '╮', tone)]
        def line(value, style=''):
            padding = max(0, width - len(clean(value)))
            lines.append('  ' + self.style('│ ', 'title') + self.style(value, style)
                         + ' ' * padding + self.style(' │', 'title'))
        line(('[+] ' if tone == 'ok' else '[*] ') + title, tone)
        line('')
        for row in rows:
            if isinstance(row, tuple):
                label, value = map(str, row)
                label = label[:column - 2].ljust(column)
                pieces = textwrap.wrap(clean(value), max(8, width - column)) or ['']
                for i, piece in enumerate(pieces):
                    prefix = label if i == 0 else ' ' * column
                    body = self.style(prefix, 'dim') + self.style(piece, 'accent')
                    padding = width - len(prefix + piece)
                    lines.append('  ' + self.style('│ ', 'title') + body + ' ' * padding
                                 + self.style(' │', 'title'))
            else:
                for part in textwrap.wrap(clean(row), width) or ['']:
                    line(part)
        lines.append('  ' + self.style('╰' + '─' * (width + 2) + '╯', tone))
        return '\n'.join(lines) + '\n\n'

    def welcome(self):
        if not sys.stdin.isatty():
            return
        if self.animated:
            self.stream.write('\033[2J\033[H')
        width, _ = self.dimensions()
        if width >= 52:
            self.stream.write('\n' + '\n'.join('  ' + self.style(row, 'title') for row in BANNER) + '\n')
        else:
            self.stream.write('\n  ' + self.style('Nextcloud', 'title') + '\n')
        self.stream.write('  ' + self.style('Docker Manager', 'bold')
                          + self.style(f' v{VERSION} · {self.t("ui_subtitle")}', 'dim') + '\n\n')
        self.stream.flush()

    def _page(self):
        width, height = self.dimensions()
        body = '\033[2J\033[H' + self.header()
        if self.context:
            body += self.card(self.t('operation_context'), self.context)
        visible = max(1, height - 12 - min(8, len(self.context)))
        for label, duration, ok in self.completed[-visible:]:
            marker, tone = ('[+]', 'ok') if ok else ('[✗]', 'error')
            body += ('  ' + self.style(marker, tone) + ' ' + label[:width - 10] + ' '
                     + self.style('· ' + clock(duration), 'dim') + '\n')
        self.stream.write(body + '\n')
        self.stream.flush()

    def _spin(self):
        index = 0
        while not self._stop.is_set():
            with self._lock:
                width, _ = self.dimensions()
                # Keep the elapsed clock visible even with long stage names.
                label = clean(self.label)[:max(8, width - 14)]
                tail = f' · {clock(time.monotonic() - self.started)}'
                room = max(0, width - len(label) - len(tail) - 5)
                detail = (' · ' + self.detail)[:room] if room > 5 and self.detail else ''
                self.stream.write('\r\033[2K  ' + self.style('[' + FRAMES[index] + ']', 'info')
                                  + ' ' + label + self.style(tail + detail, 'dim'))
                self.stream.flush()
                index = (index + 1) % len(FRAMES)
            self._stop.wait(0.1)

    def _start(self):
        if not self.animated or not self.running:
            return
        self._stop.clear()
        self.stream.write('\033[?25l')
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _pause(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
            self.stream.write('\r\033[2K\033[?25h')
            self.stream.flush()

    @contextlib.contextmanager
    def prompt(self):
        """Suspend animation and output capture before any keyboard widget."""
        self._pause()
        try:
            with contextlib.redirect_stdout(self.stdout):
                yield
        finally:
            if self.running and self.animated:
                self._page()
                self._start()

    def pick(self, title, rows, **kwargs):
        with self.prompt():
            return self.term.run(lambda: self.term.menu(self.t(title), rows, **kwargs))

    def edit(self, key, value, normalize=str, hint='', default=None, preview=None):
        def validate(text):
            try:
                normalize(text)
            except (ValueError, OSError, RuntimeError) as exc:
                return clean(exc) or self.t('invalid_value')
            return ''
        with self.prompt():
            result = self.term.run(lambda: self.term.edit(self.t(key), value, hint, validate,
                                  default=value if default is None else default, preview=preview))
        return None if result is None else normalize(result)

    def view(self, title, rows, **kwargs):
        with self.prompt():
            self.term.run(lambda: self.term.view(self.t(title), rows, **kwargs))

    def error(self, message, *, pending=False):
        from .views import pair
        rows = [(line, 'error') for line in textwrap.wrap(self.local(str(message)), 68)]
        if self.state:
            rows += pair(self.term, 'private_log', self.state.log_path)
        if pending:
            rows += [(self.t('pending'), 'warning')]
        self.view('error', rows, style='error', notice=self.t('error_help'))

    def confirm(self, expected, message=''):
        if expected == 'RESTORE':
            with self.prompt():
                value = self.term.run(lambda: self.term.edit(self.t('confirm_restore'), '',
                    self.t('restore_word'), lambda text: '' if text == 'RESTORE' else self.t('restore_word'),
                    keys='confirm_edit_keys'))
            if value is None:
                raise Cancelled(self.t('cancelled'))
            return
        if expected == 'USER-FILES-UNCHANGED':
            key, tone = 'confirm_files', 'warning'
        elif expected.startswith('MIGRATE-DB-'):
            key, tone = 'confirm_database', 'warning'
        elif expected.startswith('MIGRATE-'):
            key, tone = 'confirm_major', 'warning'
        elif expected.startswith('UPDATE-'):
            key, tone = 'confirm_update', 'accent'
        else:
            key, tone = {
                'INSTALL': ('confirm_install', 'accent'),
                'INSTALL-DOCKER': ('confirm_docker', 'accent'),
                'START-DOCKER': ('confirm_start_docker', 'accent'),
                'BACKUP': ('backup_scope', 'accent'),
                'APPLY': ('confirm_settings', 'accent'),
                'RESUME': ('confirm_resume', 'warning'),
                'SCHEDULE-ENABLE': ('confirm_schedule', 'accent'),
                'SCHEDULE-DISABLE': ('confirm_disable_schedule', 'warning'),
            }.get(expected, ('confirm_operation', 'accent'))
        # The token remains in the review for precise identification, without
        # requiring it to be typed for ordinary reversible actions.
        note = self.t(key) + '\n' + expected
        if message and ' / ' not in message and message != expected:
            note += ' · ' + message
        choice = self.pick('confirm_operation', [
            ('back', '← ' + self.t('cancel'), 'accent'),
            ('apply', '▶ ' + self.t('confirm_continue'), tone)],
            notice=note, notice_style=tone, help_text=self.t('confirmation_help'))
        if choice != 'apply':
            raise Cancelled(self.t('cancelled'))

    def command(self, args):
        """Only trusted command categories appear live, never argv/env/SQL."""
        if not self.running:
            return
        values = [str(arg) for arg in args]
        if values and values[0] == 'docker':
            if len(values) > 1 and values[1] == 'pull':
                self.detail = self.t('activity_download')
            elif 'compose' in values:
                self.detail = self.t('activity_containers')
            else:
                self.detail = self.t('activity_docker')
        elif values and values[0] in ('apt-get', 'systemctl', 'service'):
            self.detail = self.t('activity_host')

    def values(self, values):
        if not self.running:
            return
        self._pause()
        self.context += [(self.t(key), value) for key, value in values.items()]
        if self.animated:
            self._page()
            self._start()
        else:
            for key, value in values.items():
                print('  ' + self.t(key) + ': ' + str(value), file=self.stream)

    @contextlib.contextmanager
    def stage(self, message):
        label = self.local(message)
        start = time.monotonic()
        previous = self.label, self.started
        self._pause()
        self.label, self.started, self.detail = label, start, ''
        if self.animated and self.running:
            self._page()
            self._start()
        else:
            print('[*] ' + label, file=self.stream, flush=True)
        ok = False
        try:
            yield
            ok = True
        finally:
            self._pause()
            elapsed = time.monotonic() - start
            self.completed.append((label, elapsed, ok))
            self.label, self.started = previous
            if self.animated and self.running:
                self._page()
                self._start()
            else:
                print(('[+] ' if ok else '[✗] ') + label + ' · ' + clock(elapsed), file=self.stream)

    def run(self, label, function, *, context=None):
        if self.running:
            return function()
        self.running, self.completed = True, []
        self.label, self.started, self.detail = self.t(label), time.monotonic(), ''
        self.context = list(context or [])
        capture = contextlib.redirect_stdout(OutputLog(getattr(self.state, 'log_path', None), self.stdout))
        if self.animated:
            self._page()
            self._start()
        else:
            print('[*] ' + self.label, file=self.stream, flush=True)
        try:
            with presentation(self), capture:
                return function()
        finally:
            self._pause()
            self.running = False
            self.context = []
            self.stream.flush()
