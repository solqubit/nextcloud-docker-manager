"""Exercise the pre-Python Bash UI with a fake package manager in a private PATH."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from pty_driver import Driver


class BootstrapTests(unittest.TestCase):
    def launch(self, *, fail=False, plain=False, choose_language=False):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        binaries, logs = root / 'bin', root / 'tmp'
        binaries.mkdir()
        logs.mkdir()
        for name in ('dirname', 'uname', 'stty', 'sleep', 'mktemp'):
            (binaries / name).symlink_to(shutil.which(name))
        apt = binaries / 'apt-get'
        apt.write_text('#!/bin/bash\n'
                       'echo APT_PRIVATE_OUTPUT\n'
                       '/bin/sleep 0.35\n'
                       + ('exit 17\n' if fail else
                          'if [[ $1 == install ]]; then /bin/ln -s ' + sys.executable + ' "' + str(binaries / 'python3') + '"; fi\n'))
        apt.chmod(0o700)
        # Simulate the privileged entry condition in this private copy only.
        # CI runs as an ordinary user; the real installer retains its root gate.
        launcher = root / 'start.sh'
        source = Path('start.sh').read_text()
        guard = '$EUID == 0'
        self.assertEqual(source.count(guard), 1)
        launcher.write_text(source.replace(guard, '1 == 1', 1))
        arguments = [] if choose_language else ['--lang', 'en', '--version']
        driver = Driver([shutil.which('bash'), str(launcher), *arguments],
                        extra_env={'PATH': str(binaries), 'TMPDIR': str(logs),
                                   'NDM_UI': 'plain' if plain else 'interactive'})
        self.addCleanup(driver.close)
        driver.wait('Idioma / Language' if choose_language else 'Python 3 is required')
        driver.logs = logs
        return driver

    def test_cancel_is_default_without_invoking_package_manager(self):
        ui = self.launch()
        ui.capture('21-python-bootstrap')
        ui.send(b'\r')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 1)
        self.assertEqual(list(ui.logs.iterdir()), [])

    def test_arrow_confirmation_and_package_progress(self):
        ui = self.launch()
        ui.send(b'\x1bOB\r', 'Install Python 3')
        ui.read(0.2)
        ui.capture('22-python-bootstrap-progress')
        ui.process.wait(timeout=5)
        ui.read()
        self.assertEqual(ui.process.returncode, 0)
        self.assertIn(b'Python 3 ready', ui.output)
        self.assertNotIn(b'APT_PRIVATE_OUTPUT', ui.output)

    def test_package_failure_stays_readable_and_preserves_private_log(self):
        ui = self.launch(fail=True)
        ui.send(b'\x1bOB\r', 'Installation failed')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 1)
        logs = list(ui.logs.glob('ndm-bootstrap.*/python.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('APT_PRIVATE_OUTPUT', logs[0].read_text())
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'APT_PRIVATE_OUTPUT', ui.output)

    def test_plain_fallback_can_cancel(self):
        ui = self.launch(plain=True)
        ui.send(b'n\r')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 1)
        self.assertNotIn(b'\x1b[', ui.output)

    def test_language_is_first_even_without_python(self):
        ui = self.launch(choose_language=True)
        self.assertNotIn('Python 3', '\n'.join(ui.screen.display))
        self.assertEqual(list(ui.logs.iterdir()), [])
        ui.send(b'\x1bOB\r', 'Python 3 is required')
        self.assertNotIn('Python 3 es necesario', '\n'.join(ui.screen.display))
        ui.send(b'\x1b')
        ui.process.wait(timeout=3)
        self.assertEqual(list(ui.logs.iterdir()), [])
