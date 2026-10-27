"""Small stdlib PTY driver shared by navigation and visual-capture checks."""
import fcntl
import codecs
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import pyte


class Driver:
    def __init__(self, args, *, width=80, height=24, timeout=8, extra_env=None):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
        self.process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
                 'TERM': 'xterm-256color', 'NDM_UI': 'interactive', **(extra_env or {})})
        os.close(slave)
        self.output = b''
        self.screen = pyte.Screen(width, height)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.width, self.height = width, height
        self.timeout = timeout
        self.started, self.events = time.monotonic(), []

    def read(self, seconds=0.08):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if select.select([self.master], [], [], min(0.03, until - time.monotonic()))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.output += chunk
                text = self.decoder.decode(chunk)
                self.stream.feed(text)
                self.events.append([round(time.monotonic() - self.started, 4), 'o', text])

    def wait(self, text, *, start=0, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.read()
            shown = '\n'.join(self.screen.display)
            if len(self.output) > start and text in shown:
                self.read(0.08)
                return
            if self.process.poll() is not None:
                break
        raise AssertionError(f'UI did not show {text!r}; exit={self.process.poll()}\n'
                             + '\n'.join(self.screen.display))

    def send(self, keys, expect=None):
        start = len(self.output)
        os.write(self.master, keys)
        if expect:
            self.wait(expect, start=start)
        else:
            self.read(0.12)
        return self.output[start:]

    def select(self, index, expect):
        return self.send(b'\x1bOH' + b'\x1bOB' * index + b'\r', expect)

    def capture(self, name):
        directory = os.environ.get('NDM_UI_CAPTURE_DIR')
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            (path / (name + '.ansi')).write_bytes(self.output)
            (path / (name + '.size')).write_text(f'{self.width} {self.height}')
            import json
            header = {'version': 2, 'width': self.width, 'height': self.height}
            (path / (name + '.cast')).write_text(
                '\n'.join(json.dumps(event, ensure_ascii=False) for event in [header, *self.events]), encoding='utf-8')

    def resize(self, width, height):
        self.width, self.height = width, height
        self.screen.resize(lines=height, columns=width)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
        self.process.send_signal(signal.SIGWINCH)
        self.read(0.2)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)
