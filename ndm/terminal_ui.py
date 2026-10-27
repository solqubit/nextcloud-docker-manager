#!/usr/bin/env python3
"""Small terminal widgets using Python's optional standard-library curses module."""
import os
import getpass
from pathlib import Path
import re
import sys
import textwrap

try:
    import curses
except ImportError:
    curses = None


MARKERS = {'ok': '[+]', 'warning': '[!]', 'error': '[✗]', 'accent': '[*]'}
SEPARATOR = '\0separator'


def plain(label):
    """Text of a row label given as a string or as [(text, style)] segments."""
    return ''.join(text for text, _ in label) if isinstance(label, (list, tuple)) else label


def translations(root, language):
    return dict(line.split('\t', 1) for line in
                (root / 'locales' / (language + '.tsv')).read_text(encoding='utf-8').splitlines())


def version(root):
    try:
        return (root / 'VERSION').read_text(encoding='utf-8').strip()
    except OSError:
        return 'dev'


def clean(value):
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    return ''.join(char for char in value if char.isprintable())


class Terminal:
    def __init__(self, messages, context=''):
        self.msg = messages
        self.context = context
        self.screen = None
        self.styles = {}
        self.version = version(Path(__file__).resolve().parents[1])

    def t(self, key, *values):
        value = self.msg.get(key, key)
        return value % values if values else value

    def run(self, action):
        capable = (curses is not None and sys.stdin.isatty() and sys.stdout.isatty()
                   and os.environ.get('TERM', 'dumb') != 'dumb'
                   and os.environ.get('NDM_UI') != 'plain')
        if capable:
            size = os.get_terminal_size()
            capable = size.columns >= 50 and size.lines >= 18
        if not capable:
            return action()
        os.environ.setdefault('ESCDELAY', '35')
        def start(screen):
            self.screen = screen
            screen.keypad(True)
            if hasattr(curses, 'set_escdelay'):
                curses.set_escdelay(35)
            self.cursor(False)
            if curses.has_colors() and 'NO_COLOR' not in os.environ:
                curses.start_color()
                try:
                    curses.use_default_colors()
                    background = -1
                except curses.error:
                    background = curses.COLOR_BLACK
                # Same palette as lib/ui.sh: magenta titles, blue information,
                # green success, yellow warnings, red errors, blue selection bar.
                colors = [('title', curses.COLOR_MAGENTA, background),
                          ('accent', curses.COLOR_CYAN, background),
                          ('ok', curses.COLOR_GREEN, background),
                          ('warning', curses.COLOR_YELLOW, background),
                          ('error', curses.COLOR_RED, background),
                          ('selected', curses.COLOR_WHITE, curses.COLOR_BLUE),
                          ('info', curses.COLOR_BLUE, background)]
                for number, (name, foreground, back) in enumerate(colors, 1):
                    curses.init_pair(number, foreground, back)
                    self.styles[name] = curses.color_pair(number) | curses.A_BOLD
            self.styles['dim'] = curses.A_DIM
            self.styles['bold'] = curses.A_BOLD
            return action()
        try:
            result = curses.wrapper(start)
            # Leaving curses restores the previous screen, which still shows the
            # last Bash prompt; clear it so no stale, inactive menu reappears.
            sys.stderr.write('\033[2J\033[H')
            sys.stderr.flush()
            return result
        except curses.error:
            # Missing terminfo or a limited curses build still permits setup.
            self.screen = None
            return action()
        finally:
            self.screen = None

    def cursor(self, visible):
        if self.screen is not None:
            try:
                curses.curs_set(int(visible))
            except curses.error:
                pass

    def put(self, y, x, text, style=''):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height and 0 <= x < width - 1:
            try:
                self.screen.addnstr(y, x, clean(text), width - x - 1,
                                   self.styles.get(style, 0))
            except curses.error:
                pass

    def measure(self):
        """Set left/width for the current terminal size; returns (height, width).
        Callers that size content before frame() must call this first."""
        height, width = self.screen.getmaxyx()
        self.left = max(2, (width - 104) // 2)
        self.width = max(12, min(100, width - self.left - 2))
        return height, width

    def frame(self, title, notice='', notice_style='accent'):
        """Header shared with lib/ui.sh; returns the first free row and height."""
        self.screen.erase()
        height, width = self.measure()
        name = 'Nextcloud Docker Manager'
        tagline = f' v{self.version} · {self.t("ui_subtitle")}'
        if len(name) + len(tagline) > self.width:
            tagline = f' v{self.version}'
        self.put(0, self.left, name, 'title')
        self.put(0, self.left + len(name), tagline, 'dim')
        self.put(1, self.left, '─' * self.width, 'title')
        self.put(2, self.left, self.context, 'accent')
        self.put(3, self.left, '[?]', 'title')
        self.put(3, self.left + 4, title, 'bold')
        marker = MARKERS.get(notice_style, '[*]')
        notice = clean(notice)
        if notice.startswith('✓ '):
            notice = notice[2:]
        lines = textwrap.wrap(notice, self.width - 4)[:max(2, min(5, height - 17))]
        for row, line in enumerate(lines, 4):
            if row == 4:
                self.put(row, self.left, marker, notice_style)
            self.put(row, self.left + 4, line, notice_style if notice_style != 'accent' else '')
        return 5 + len(lines), height

    def key(self):
        key = self.screen.get_wch()
        if key == '\x03':
            raise KeyboardInterrupt
        # Some SSH clients send normal cursor keys even in application mode.
        if key == '\x1b':
            sequence = ''
            self.screen.timeout(35)
            try:
                first = self.screen.get_wch()
                if first not in ('[', 'O'):
                    curses.unget_wch(first)
                    return '\x1b'
                sequence = first
                while len(sequence) < 5:
                    part = self.screen.get_wch()
                    if isinstance(part, str):
                        sequence += part
                    else:
                        break
                    if part.isalpha() or part == '~':
                        break
            except curses.error:
                pass
            finally:
                self.screen.timeout(-1)
            key = {'[A': curses.KEY_UP, 'OA': curses.KEY_UP,
                   '[B': curses.KEY_DOWN, 'OB': curses.KEY_DOWN,
                   '[C': curses.KEY_RIGHT, 'OC': curses.KEY_RIGHT,
                   '[D': curses.KEY_LEFT, 'OD': curses.KEY_LEFT,
                   '[H': curses.KEY_HOME, 'OH': curses.KEY_HOME,
                   '[F': curses.KEY_END, 'OF': curses.KEY_END,
                   '[1~': curses.KEY_HOME, '[4~': curses.KEY_END,
                   '[3~': curses.KEY_DC,
                   '[5~': curses.KEY_PPAGE, '[6~': curses.KEY_NPAGE}.get(sequence, '\x1b')
        return key

    def draw_label(self, y, label, style, active):
        """A row is plain text or segments [(text, style)]; a style ending in
        '>' right-aligns that segment. The active row is one selection bar."""
        if active:
            self.put(y, self.left, ('> ' + plain(label)).ljust(self.width),
                     'error' if style == 'error' else 'selected')
            return
        if not isinstance(label, (list, tuple)):
            self.put(y, self.left, '  ' + label, style)
            return
        x = self.left + 2
        for text, part_style in label:
            if part_style.endswith('>'):
                self.put(y, max(x, self.left + self.width - len(text)), text, part_style[:-1])
            else:
                self.put(y, x, text, part_style)
                x += len(text)

    def menu(self, title, rows, selected=0, notice='', help_text='', search=False,
             notice_style='accent'):
        """Rows are (value, label, semantic color); return a value or None on Back.
        Rows whose value is SEPARATOR are drawn as a rule and never selected."""
        selected = min(selected, len(rows) - 1)
        if self.screen is None:
            choices = [row for row in rows if row[0] != SEPARATOR]
            current = rows[selected][0]
            print('\n[?] ' + title, file=sys.stderr)
            if notice:
                print(notice, file=sys.stderr)
            for number, (_, label, _) in enumerate(choices, 1):
                print(f'  {number}) {plain(label)}', file=sys.stderr)
            while True:
                answer = input(self.t('setup_plain_choice'))
                if answer.lower() in ('b', 'back', 'volver'):
                    return None
                if answer == '':
                    return current
                if answer.isdigit() and 1 <= int(answer) <= len(choices):
                    return choices[int(answer) - 1][0]
                matches = [row for row in choices if answer.casefold() in plain(row[1]).casefold()]
                if len(matches) == 1:
                    return matches[0][0]
                print(self.t('ui_invalid_choice'), file=sys.stderr)
        query = ''
        step = 1
        while True:
            filtered = [row for row in rows if row[0] == SEPARATOR and not query
                        or row[0] != SEPARATOR and query.casefold() in plain(row[1]).casefold()]
            selected = max(0, min(selected, len(filtered) - 1))
            if any(row[0] != SEPARATOR for row in filtered):
                while filtered[selected][0] == SEPARATOR:
                    selected = (selected + step) % len(filtered)
            note = self.t('setup_search', query) if search and query else notice
            top, height = self.frame(title, note, notice_style)
            visible = max(1, height - top - 5)
            offset = max(0, min(selected - visible // 2, len(filtered) - visible))
            for index in range(offset, min(offset + visible, len(filtered))):
                value, label, style = filtered[index]
                if value == SEPARATOR:
                    self.put(top + index - offset, self.left + 2, '┄' * (self.width - 4), 'dim')
                    continue
                self.draw_label(top + index - offset, label, style, index == selected)
            if not filtered:
                self.put(top, self.left, self.t('setup_no_matches'), 'warning')
            self.put(height - 4, self.left, '─' * self.width, 'accent')
            for row, line in enumerate(textwrap.wrap(help_text, self.width)[:2], height - 3):
                self.put(row, self.left, line)
            choices = [i for i, row in enumerate(filtered) if row[0] != SEPARATOR]
            position = choices.index(selected) + 1 if selected in choices else 0
            self.put(height - 1, self.left,
                     f'{position}/{len(choices)} · ' + self.t('setup_search_keys' if search else 'setup_keys'), 'accent')
            self.screen.refresh()
            key = self.key()
            step = 1
            if key in ('\n', '\r', curses.KEY_ENTER) and choices:
                return filtered[selected][0]
            if key == '\x1b':
                return None
            if key in (curses.KEY_DOWN, '\t'):
                selected = (selected + 1) % max(1, len(filtered))
            elif key in (curses.KEY_UP, curses.KEY_BTAB):
                selected = (selected - 1) % max(1, len(filtered)); step = -1
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(filtered) - 1; step = -1
            elif key == curses.KEY_NPAGE:
                selected = min(len(filtered) - 1, selected + visible)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - visible); step = -1
            elif search and key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                query = query[:-1]
            elif search and isinstance(key, str) and key.isprintable():
                query += key
                selected = 0

    def edit(self, title, value, hint, validator, secret=False, default=None, preview=None, keys='setup_edit_keys'):
        """Framed text field. default is shown for reference; preview(text)
        returns a live line such as the resulting URL."""
        visible = os.environ.get('NDM_PASSWORD_VISIBILITY') != 'hidden'
        if self.screen is None:
            # Without curses there is no Esc: ":back" goes back.
            print('\n' + title + '\n' + hint + ' ' + self.t('plain_back_hint'), file=sys.stderr)
            while True:
                text = getpass.getpass('> ') if secret and not visible else input('> ')
                if text == ':back':
                    return None
                text = text if text or secret else str(value)
                error = validator(text)
                if not error:
                    return text
                print(error, file=sys.stderr)
        text = str(value)
        position = len(text)
        error = ''
        while True:
            top, height = self.frame(title, error or hint, 'error' if error else 'accent')
            display = text if not secret or visible else '*' * len(text)
            inner = max(8, self.width - 6)
            start = max(0, position - inner + 1)
            # ╭──╮ / │ > value │ / ╰──╯ input box, then reference lines.
            self.put(top, self.left, '╭' + '─' * (inner + 4) + '╮', 'title')
            self.put(top + 1, self.left, '│', 'title')
            self.put(top + 1, self.left + 2, '>', 'accent')
            self.put(top + 1, self.left + 4, display[start:start + inner], 'bold')
            self.put(top + 1, self.left + inner + 5, '│', 'title')
            self.put(top + 2, self.left, '╰' + '─' * (inner + 4) + '╯', 'title')
            row = top + 3
            if secret:
                marker = self.t('ui_password_visible' if visible else 'ui_password_hidden')
                self.put(row, self.left, f'[{marker}] ' + self.t('ui_password_keys'), 'dim')
                row += 1
            elif default not in (None, ''):
                self.put(row, self.left, self.t('setup_field_values', value, default), 'dim')
                row += 1
            if preview is not None:
                line = preview(text)
                if line:
                    self.put(row, self.left, '[*] ' + line, 'accent')
            self.put(height - 2, self.left, self.t(keys), 'accent')
            self.cursor(True)
            self.screen.move(min(top + 1, height - 1), min(self.left + 4 + position - start,
                                                           self.screen.getmaxyx()[1] - 2))
            self.screen.refresh()
            key = self.key()
            self.cursor(False)
            if key == '\x1b':
                return None
            if key in ('\n', '\r', curses.KEY_ENTER):
                error = validator(text)
                if not error:
                    self.screen.erase()
                    self.screen.refresh()
                    return text
            elif secret and key == '\t':
                visible = not visible
            elif key in (curses.KEY_BACKSPACE, '\x7f', '\b') and position:
                text = text[:position - 1] + text[position:]
                position -= 1
            elif key == curses.KEY_DC:
                text = text[:position] + text[position + 1:]
            elif key in (curses.KEY_HOME, '\x01'):
                position = 0
            elif key in (curses.KEY_END, '\x05'):
                position = len(text)
            elif key == curses.KEY_LEFT:
                position = max(0, position - 1)
            elif key == curses.KEY_RIGHT:
                position = min(len(text), position + 1)
            elif key == '\x15':
                text = ''; position = 0
            elif isinstance(key, str) and key.isprintable():
                text = text[:position] + key + text[position:]
                position += 1
