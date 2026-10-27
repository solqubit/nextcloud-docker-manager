import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest

from ndm.i18n import messages


class TerminalTests(unittest.TestCase):
    def pty_run(self, expression, keys):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        code = ('from ndm.terminal_ui import Terminal; from ndm.i18n import messages; '
                'import json; t=Terminal(messages("es")); result=t.run(lambda: ' + expression + '); '
                'print("RESULT="+json.dumps(result))')
        process = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, 'TERM': 'xterm-256color', 'NDM_UI': 'interactive'})
        os.close(slave)
        output = b''
        try:
            until = time.monotonic() + 8
            while time.monotonic() < until and b'Alpha' not in output:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b'Alpha', output)
            os.write(master, keys)
            while time.monotonic() < until:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
                if process.poll() is not None:
                    break
            process.wait(timeout=2)
            self.assertEqual(process.returncode, 0, output.decode(errors='replace'))
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_arrow_navigation(self):
        output = self.pty_run('t.menu("Alpha", [("a","Alpha",""),("b","Beta","")])', b'\x1bOB\r')
        self.assertIn(b'RESULT="b"', output)

    def test_escape_cancels_edit(self):
        output = self.pty_run('t.edit("Alpha", "original", "", lambda s:s)', b'changed\x1b')
        self.assertIn(b'RESULT=null', output)

    def test_plain_back(self):
        code = ('from ndm.terminal_ui import Terminal; from ndm.i18n import messages; '
                't=Terminal(messages("en")); print("RESULT="+str(t.run(lambda:t.menu("Menu",[("a","Alpha","")]))))')
        result = subprocess.run([sys.executable, '-c', code], input='back\n', text=True, capture_output=True,
                                env={**os.environ, 'NDM_UI': 'plain'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('RESULT=None', result.stdout)
