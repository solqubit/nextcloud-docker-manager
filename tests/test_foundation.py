import copy
import io
import gzip
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from ndm.archive import members, extract_checked
from ndm.config import access_url, caddyfile, compose, host, initial, managed_php, public_host, validate
from ndm.core import Error, State, atomic, read_json, version_tuple, sha
from ndm.docker import Engine


def configuration():
    images = {r: f'{repo}:{tag}@sha256:' + 'a' * 64 for r, repo, tag in (
        ('app', 'nextcloud', '33.0.9-apache'), ('db', 'mariadb', '11.4.13'),
        ('redis', 'redis', '7.4.12'), ('proxy', 'caddy', '2.11.7'))}
    return initial('33.0.9', images, storage='named')


class ConfigurationTests(unittest.TestCase):
    def test_no_automatic_web_credentials(self):
        cfg = configuration()
        app = compose(cfg)['services']['app']
        self.assertNotIn('NEXTCLOUD_ADMIN_USER', app['environment'])
        self.assertNotIn('NEXTCLOUD_ADMIN_PASSWORD', app['environment'])

    def test_database_and_redis_are_private(self):
        services = compose(configuration())['services']
        self.assertNotIn('ports', services['db'])
        self.assertNotIn('ports', services['redis'])
        self.assertEqual(services['app']['image'], services['cron']['image'])

    def test_backend_address_pool_cannot_consume_the_planned_proxy_subnet(self):
        import ipaddress
        networks = compose(configuration())['networks']
        proxy = ipaddress.ip_network(networks['proxy']['ipam']['config'][0]['subnet'])
        backend = ipaddress.ip_network(networks['backend']['ipam']['config'][0]['subnet'])
        self.assertFalse(proxy.overlaps(backend))
        self.assertEqual(proxy.supernet(), backend.supernet())

    def test_subnet_selection_reserves_both_halves(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from ndm.operations import Manager
        from ndm.core import Runner
        with tempfile.TemporaryDirectory() as folder:
            state = State(folder).open()
            try:
                occupied = [{'IPAM': {'Config': [{'Subnet': '172.19.0.16/28'}]}}]
                results = [SimpleNamespace(stdout='existing-network'), SimpleNamespace(stdout=json.dumps(occupied))]
                with patch.object(Runner, 'run', side_effect=results), patch('ndm.operations.secrets.randbelow', side_effect=[0, 0, 1, 0]):
                    self.assertEqual(Manager(state).new_subnet(), '172.20.0.0/28')
            finally:
                state.lock_file.close()

    def test_user_files_have_separate_mount(self):
        cfg = configuration()
        volumes = compose(cfg)['services']['app']['volumes']
        self.assertIn('data:/var/www/html/data', volumes)
        self.assertNotEqual(cfg['volumes']['data'], cfg['volumes']['app'])

    def test_restored_directory_generation_is_fresh(self):
        cfg = configuration()
        cfg['storage'] = 'directory'
        before = Engine(None, cfg, '/unused')
        saved = before.volume_path('db')
        data = before.volume_path('data')
        restored = copy.deepcopy(cfg)
        restored['volumes']['db'] += '_r1234'
        after = Engine(None, restored, '/unused')
        self.assertNotEqual(saved, after.volume_path('db'))
        self.assertEqual(data, after.volume_path('data'))

    def test_domains_reject_configuration_injection(self):
        for value in ('foo\nrespond hacked', 'x {', '*.example.com', 'https://a.example', 'a/b', 'x:80', '../x'):
            with self.subTest(value=value), self.assertRaises(Error):
                host(value)
        self.assertEqual(host('NUBE.EXAMPLE.COM'), 'nube.example.com')

    def test_public_domain_rejects_private_names(self):
        for value in ('127.0.0.1', '192.168.1.2', 'nube.local', 'nube.home.arpa'):
            with self.subTest(value=value), self.assertRaises(Error):
                public_host(value)

    def test_local_and_public_protocols_do_not_override_each_other(self):
        cfg = configuration()
        cfg['domains'].append(dict(host='nube.example.com', kind='public', mode='tunnel', tls='acme', email=''))
        validate(cfg)
        text = caddyfile(cfg)
        self.assertIn('header_up X-Forwarded-Proto http', text)
        self.assertIn('header_up X-Forwarded-Proto https', text)
        self.assertIn('redir /.well-known/carddav /remote.php/dav/ 301', text)
        self.assertNotIn('cloudflared', json.dumps(compose(cfg)))
        import base64
        payload = managed_php(cfg).split("base64_decode('")[1].split("'")[0]
        settings = json.loads(base64.b64decode(payload))
        self.assertEqual(settings['overwriteprotocol'], '')
        self.assertEqual(len(settings['trusted_proxies']), 1)

    def test_unpinned_images_and_duplicate_domains_fail_closed(self):
        cfg = configuration()
        cfg['images']['app'] = 'nextcloud:latest'
        with self.assertRaises(Error):
            validate(cfg)
        cfg = configuration()
        cfg['domains'] *= 2
        with self.assertRaises(Error):
            validate(cfg)

    def test_stable_version_policy(self):
        for value in ('latest', 'stable', '33', '33.0.1-rc1', '33.0.1;id'):
            with self.subTest(value=value), self.assertRaises(Error):
                version_tuple(value)
        self.assertGreater(version_tuple('33.0.10'), version_tuple('33.0.9'))

    def test_public_redirect_and_url_use_client_port_independently_of_docker_forward(self):
        cfg = configuration()
        entry = dict(host='nube.example.com', kind='public', mode='direct', tls='acme', email='')
        cfg['domains'].append(entry)
        self.assertEqual(access_url(cfg, entry), 'https://nube.example.com')
        self.assertIn('redir https://nube.example.com{uri} 308', caddyfile(cfg))
        entry['external_port'] = 9443
        self.assertEqual(access_url(cfg, entry), 'https://nube.example.com:9443')

    def test_locale_keys_and_placeholders_match(self):
        root = Path(__file__).resolve().parents[1] / 'locales'
        es, en = (json.loads((root / (lang + '.json')).read_text()) for lang in ('es', 'en'))
        self.assertEqual(set(es), set(en))
        for key in es:
            self.assertEqual(es[key].count('%s'), en[key].count('%s'), key)


class ArchiveTests(unittest.TestCase):
    def check_entry(self, name, kind=None, application=True):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as output:
            info = tarfile.TarInfo(name)
            if kind:
                info.type = kind
                info.linkname = '/etc/shadow'
            output.addfile(info)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as source:
            return members(source, application=application)

    def test_rejects_path_traversal_and_absolute_files(self):
        for name in ('../passwd', '/etc/passwd', 'config/../../passwd', 'config\\passwd'):
            with self.subTest(name=name), self.assertRaises(Error):
                self.check_entry(name)

    def test_rejects_user_data_inside_application_archive(self):
        for name in ('data/alice/files/private.txt', './data/alice/files/private.txt'):
            with self.subTest(name=name), self.assertRaises(Error):
                self.check_entry(name)

    def test_rejects_links_and_devices(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), self.assertRaises(Error):
                self.check_entry('config/evil', kind)

    def test_accepts_regular_application_file(self):
        self.assertEqual(len(self.check_entry('./config/config.php')), 1)

    def test_safe_symlink_to_hardlink_preserves_official_image_licenses(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as output:
            output.addfile(tarfile.TarInfo('./apps/example/license.txt'))
            hardlink = tarfile.TarInfo('./apps/example/other.txt')
            hardlink.type = tarfile.LNKTYPE
            hardlink.linkname = './apps/example/license.txt'
            output.addfile(hardlink)
            symlink = tarfile.TarInfo('./apps/example/script.js.license')
            symlink.type = tarfile.SYMTYPE
            symlink.linkname = 'other.txt'
            output.addfile(symlink)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as source:
            self.assertEqual(len(members(source, application=True)), 3)

    def test_cyclic_links_are_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as output:
            for name, target in (('first', 'second'), ('second', 'first')):
                link = tarfile.TarInfo(name)
                link.type = tarfile.SYMTYPE
                link.linkname = target
                output.addfile(link)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as source, self.assertRaises(Error):
            members(source)


class BackupIntegrityTests(unittest.TestCase):
    def sample(self, root, *, corrupt=False, user_files=False, broken_gzip=False):
        root = Path(root)
        for name in ('app.tar', 'proxy.tar'):
            with tarfile.open(root / name, 'w') as output:
                output.addfile(tarfile.TarInfo('data/private.txt' if user_files and name == 'app.tar' else 'config/config.php'))
        (root / 'db.sql.gz').write_bytes(b'not-gzip' if broken_gzip else gzip.compress(b'-- consistent SQL fixture\nSELECT 1;'))
        metadata = {'format': 1, 'configuration': configuration(),
                    'files': {name: {'bytes': (root / name).stat().st_size, 'sha256': sha(root / name)}
                              for name in ('app.tar', 'proxy.tar', 'db.sql.gz')}}
        atomic(root / 'manifest.json', metadata)
        if corrupt:
            (root / 'app.tar').write_bytes(b'changed-after-hashing')
        archive = root / 'example.ndm.tar'
        with tarfile.open(archive, 'w') as output:
            for name in ('app.tar', 'proxy.tar', 'db.sql.gz', 'manifest.json'):
                output.add(root / name, arcname=name)
        destination = root / 'extracted'
        destination.mkdir()
        return archive, destination

    def test_valid_package(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, destination = self.sample(folder)
            self.assertEqual(extract_checked(archive, destination)['format'], 1)

    def test_headless_verification_does_not_add_terminal_output(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from ndm.archive import unpack
        with tempfile.TemporaryDirectory() as folder:
            path, _ = self.sample(folder)
            (Path(folder) / 'backups').mkdir()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), unpack(SimpleNamespace(path=Path(folder)), path) as (_, manifest):
                self.assertEqual(manifest['format'], 1)
            self.assertEqual(output.getvalue(), '')

    def test_checksum_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, destination = self.sample(folder, corrupt=True)
            with self.assertRaises(Error):
                extract_checked(archive, destination)

    def test_user_data_cannot_be_smuggled_in_a_correctly_hashed_package(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, destination = self.sample(folder, user_files=True)
            with self.assertRaises(Error):
                extract_checked(archive, destination)

    def test_invalid_sql_gzip_is_rejected_even_with_matching_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, destination = self.sample(folder, broken_gzip=True)
            with self.assertRaises((Error, OSError)):
                extract_checked(archive, destination)


class StateTests(unittest.TestCase):
    def test_root_aliases_and_shared_system_directories_are_rejected(self):
        for path in ('/', '/tmp/..', '/opt', '/var/lib/docker'):
            with self.subTest(path=path), self.assertRaises(Error):
                State(path)

    def test_nonempty_unrelated_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            original_mode = path.stat().st_mode
            (path / 'other-app.txt').write_text('keep')
            with self.assertRaises(Error):
                State(path).open()
            self.assertEqual((path / 'other-app.txt').read_text(), 'keep')
            self.assertEqual(path.stat().st_mode, original_mode)

    def test_corrupt_configuration_is_not_replaced_with_empty_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.json'
            path.write_text('{corrupted')
            with self.assertRaises(Error):
                read_json(path)
            self.assertEqual(path.read_text(), '{corrupted')

    def test_atomic_revision_and_recoverable_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            state = State(folder).open()
            cfg = configuration()
            revision = state.revision(cfg)
            state.activate(revision)
            state.begin('update', source=str(revision))
            state.phase('backed-up', backup='example')
            self.assertEqual(state.pending['phase'], 'backed-up')
            with self.assertRaises(Error):
                state.begin('second')
            self.assertEqual(state.load()['id'], cfg['id'])
            state.done()
            self.assertIsNone(state.pending)
            state.lock_file.close()


if __name__ == '__main__':
    unittest.main()
