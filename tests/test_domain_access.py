"""First tunnel promotion updates generated URLs without replacing preferences."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ndm.config import access_url, managed_php
from ndm.core import Cancelled, State
from ndm.operations import Manager
from test_foundation import configuration


class DomainAccessTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.state = State(Path(work.name) / 'state').open()
        self.addCleanup(self.state.lock_file.close)
        cfg = configuration()
        cfg['status'] = 'ready'
        self.state.activate(self.state.revision(cfg))
        self.manager = Manager(self.state)

    def add(self, name, **kwargs):
        def save(cfg, supplied=None):
            self.state.activate(self.state.revision(cfg))
        with patch.object(self.manager, 'settings', side_effect=save):
            return self.manager.add_domain(name, **kwargs)

    def test_first_tunnel_becomes_primary_and_updates_nextcloud_cli_url(self):
        cfg = self.add('cloud.example.com', mode='tunnel')
        self.assertEqual(cfg['primary'], 'cloud.example.com')
        self.assertEqual(access_url(cfg), 'https://cloud.example.com')
        self.assertEqual(cfg['domains'][0]['host'], '127.0.0.1')
        self.assertEqual(cfg['domains'][0]['tls'], 'off')
        payload = managed_php(cfg).split("base64_decode('")[1].split("'")[0]
        values = json.loads(base64.b64decode(payload))
        self.assertEqual(values['overwrite.cli.url'], 'https://cloud.example.com')
        self.assertEqual(values['overwriteprotocol'], '')

    def test_later_tunnel_does_not_replace_public_primary(self):
        self.add('first.example.com', mode='tunnel')
        cfg = self.add('second.example.com', mode='tunnel')
        self.assertEqual(cfg['primary'], 'first.example.com')

    def test_first_direct_domain_becomes_primary_without_replacing_local_access(self):
        cfg = self.add('vps.example.com', mode='direct')
        self.assertEqual(cfg['primary'], 'vps.example.com')
        self.assertEqual(cfg['domains'][0]['host'], '127.0.0.1')
        self.assertEqual(access_url(cfg), 'https://vps.example.com')

    def test_additional_direct_domain_preserves_public_and_manual_local_preferences(self):
        self.add('first.example.com', mode='direct')
        cfg = self.add('second.example.com', mode='direct')
        self.assertEqual(cfg['primary'], 'first.example.com')
        cfg['primary'] = '127.0.0.1'
        self.state.activate(self.state.revision(cfg))
        cfg = self.add('third.example.com', mode='tunnel')
        self.assertEqual(cfg['primary'], '127.0.0.1')

    def test_later_tunnel_keeps_a_manually_restored_local_preference(self):
        cfg = self.add('first.example.com', mode='tunnel')
        cfg['primary'] = '127.0.0.1'
        self.state.activate(self.state.revision(cfg))
        cfg = self.add('second.example.com', mode='tunnel')
        self.assertEqual(cfg['primary'], '127.0.0.1')

    def test_existing_direct_public_preference_is_retained(self):
        self.add('direct.example.com', mode='direct', primary=True)
        cfg = self.add('tunnel.example.com', mode='tunnel')
        self.assertEqual(cfg['primary'], 'direct.example.com')

    def test_local_addresses_do_not_replace_primary_and_explicit_override_works(self):
        self.add('first.example.com', mode='tunnel')
        cfg = self.add('nube.internal')
        self.assertEqual(cfg['primary'], 'first.example.com')
        cfg = self.add('second.example.com', mode='tunnel', primary=True)
        self.assertEqual(cfg['primary'], 'second.example.com')

    def test_cancelled_settings_do_not_save_domain_or_change_primary(self):
        with patch.object(self.manager, 'settings', side_effect=Cancelled('cancelled')):
            with self.assertRaises(Cancelled):
                self.manager.add_domain('cloud.example.com', mode='tunnel')
        cfg = self.state.load()
        self.assertEqual(cfg['primary'], '127.0.0.1')
        self.assertEqual(len(cfg['domains']), 1)
