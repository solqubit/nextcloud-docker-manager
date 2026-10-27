import json
from pathlib import Path
import sys
import tempfile
import unittest

from ndm.config import initial
from ndm.core import State
from ndm.i18n import remember_language, remembered_language
from pty_driver import Driver
from ui_fixture import images


class StartupLanguageTests(unittest.TestCase):
    def root(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        return Path(work.name) / 'state'

    def launch(self, root, *args):
        ui = Driver([sys.executable, '-u', 'tests/startup_fixture.py', str(root), *args])
        self.addCleanup(ui.close)
        return ui

    def events(self, root):
        path = root / 'cache' / 'startup-events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def finish(self, ui, title):
        ui.send(b'\x1bOB\r', title)
        ui.send(b'\x1b')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 0)

    def test_language_precedes_all_dependency_actions(self):
        root = self.root()
        ui = self.launch(root)
        ui.wait('Idioma / Language')
        self.assertEqual(self.events(root), [])
        self.assertFalse((root / 'language').exists())
        ui.send(b'\x1bOB\r', 'Review and confirm')
        self.assertIn('Install Docker Engine', '\n'.join(ui.screen.display))
        self.assertEqual(self.events(root), [{'event': 'bootstrap', 'language': 'en'}])
        self.finish(ui, 'Manage Nextcloud')
        self.assertEqual([event['event'] for event in self.events(root)], ['bootstrap', 'engine', 'menu'])

    def test_cancel_language_does_not_check_or_install_dependencies(self):
        root = self.root()
        ui = self.launch(root)
        ui.wait('Idioma / Language')
        ui.send(b'\x1b')
        ui.process.wait(timeout=3)
        self.assertEqual(ui.process.returncode, 0)
        self.assertEqual(self.events(root), [])
        self.assertFalse((root / 'language').exists())

    def test_choice_survives_restart_without_another_prompt(self):
        root = self.root()
        first = self.launch(root)
        first.wait('Idioma / Language')
        first.send(b'\x1bOB\r', 'Review and confirm')
        self.finish(first, 'Manage Nextcloud')
        second = self.launch(root)
        second.wait('Review and confirm')
        self.assertNotIn(b'Idioma / Language', second.output)
        self.finish(second, 'Manage Nextcloud')
        self.assertTrue(all(event['language'] == 'en' for event in self.events(root)))

    def test_existing_installation_keeps_its_selected_language(self):
        root = self.root()
        state = State(root).open()
        state.activate(state.revision(initial('33.0.10', images(), language='en', storage='named')))
        state.lock_file.close()
        ui = self.launch(root)
        ui.wait('Review and confirm')
        self.assertNotIn(b'Idioma / Language', ui.output)
        self.finish(ui, 'Manage Nextcloud')
        self.assertEqual((root / 'language').read_text().strip(), 'en')

    def test_explicit_language_updates_the_remembered_preference(self):
        root = self.root()
        state = State(root).open()
        remember_language(state, 'en')
        state.lock_file.close()
        ui = self.launch(root, '--lang', 'es')
        ui.wait('Revisar y confirmar')
        self.assertNotIn(b'Idioma / Language', ui.output)
        self.finish(ui, 'Administrar Nextcloud')
        self.assertEqual((root / 'language').read_text().strip(), 'es')

    def test_language_change_is_retained_after_application_restore(self):
        state = State(self.root()).open()
        self.addCleanup(state.lock_file.close)
        cfg = initial('33.0.10', images(), language='es', storage='named')
        state.activate(state.revision(cfg))
        remember_language(state, 'en')
        self.assertEqual(state.load()['language'], 'en')
        # Application snapshots can predate the manager's language preference.
        state.activate(state.revision(cfg))
        self.assertEqual(remembered_language(state), 'en')
        remember_language(state, remembered_language(state))
        self.assertEqual(state.load()['volumes'], cfg['volumes'])
        self.assertEqual(state.load()['images'], cfg['images'])
        self.assertEqual(state.load()['language'], 'en')

    def test_bootstrap_language_is_forwarded_without_a_second_prompt(self):
        root = self.root()
        ui = Driver([sys.executable, '-u', 'tests/startup_fixture.py', str(root)],
                    extra_env={'NDM_STARTUP_LANGUAGE': 'en'})
        self.addCleanup(ui.close)
        ui.wait('Review and confirm')
        self.assertNotIn(b'Idioma / Language', ui.output)
        self.finish(ui, 'Manage Nextcloud')
        self.assertEqual((root / 'language').read_text().strip(), 'en')
