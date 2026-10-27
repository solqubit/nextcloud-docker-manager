"""Run the real entry point with dependency operations replaced by test probes."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

from ndm.cli import main
from ndm.core import confirm


root = Path(sys.argv[1])


def record(event):
    language = (root / 'language').read_text().strip()
    with (root / 'cache' / 'startup-events.jsonl').open('a') as out:
        out.write(json.dumps({'event': event, 'language': language}) + '\n')


def bootstrap(runner):
    record('bootstrap')
    confirm('INSTALL-DOCKER')


def menu(manager, language=None, console=None):
    record('menu')
    assert language == console.language
    console.pick('menu', [('exit', 'Exit / Salir', '')])


with patch('ndm.cli.bootstrap', bootstrap), patch('ndm.cli.require_engine', lambda runner: record('engine')), patch('ndm.cli.menu', menu):
    raise SystemExit(main(['--state-dir', str(root), *sys.argv[2:]]))
