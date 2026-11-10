"""Regression checks for proxy exposure, port probes, backups, updates and host bootstrap."""
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from ndm import archive, system
from ndm.config import caddyfile, initial, validate
from ndm.core import LABEL, Error, Runner
from ndm.docker import Engine
from ndm.maintenance import component_series
from ndm.views import local, operation_summary, update_rows


def configuration():
    images = {r: f'{repo}:{tag}@sha256:' + 'a' * 64 for r, repo, tag in (
        ('app', 'nextcloud', '33.0.9-apache'), ('db', 'mariadb', '11.4.13'),
        ('redis', 'redis', '7.4.12'), ('proxy', 'caddy', '2.11.7'))}
    return initial('33.0.9', images, storage='named')


class FakeRunner:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def run(self, args, **kwargs):
        """Replies map a command prefix to its output, or to (output, return code)."""
        self.calls.append([str(arg) for arg in args])
        for prefix, reply in self.replies.items():
            if ' '.join(map(str, args)).startswith(prefix):
                output, code = reply if isinstance(reply, tuple) else (reply, 0)
                return subprocess.CompletedProcess(args, code, output, '')
        return subprocess.CompletedProcess(args, 0, '', '')


class ProxyExposureTests(unittest.TestCase):
    def test_plain_routes_only_answer_private_clients(self):
        cfg = configuration()
        cfg['domains'] += [dict(host='cloud.example.com', kind='public', mode='tunnel', tls='acme', email=''),
                           dict(host='nube.example.com', kind='public', mode='direct', tls='acme', email='')]
        validate(cfg)
        text = caddyfile(cfg)
        # Local and tunnel origins are plain HTTP: blocked from public clients.
        self.assertIn('@outside0 {\n        host 127.0.0.1\n        not remote_ip private_ranges', text)
        self.assertIn('@outside1 {\n        host cloud.example.com', text)
        # The direct domain only redirects to HTTPS on port 80.
        self.assertNotIn('@outside2', text)
        self.assertIn('trusted_proxies static private_ranges', text)


class PortProbeTests(unittest.TestCase):
    def engine(self, cfg, ports):
        engine = Engine.__new__(Engine)
        engine.cfg = cfg
        container = {'Config': {'Labels': {LABEL + '.installation': cfg['id']}},
                     'NetworkSettings': {'Ports': {f'{port}/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]
                                                   for port in ports}}}
        engine.runner = FakeRunner({'docker ps': 'own\n', 'docker container inspect': json.dumps([container])})
        return engine

    def test_changing_bind_ignores_ports_held_by_our_own_proxy(self):
        old = configuration()
        new = dict(old, bind='0.0.0.0')
        with patch('ndm.docker.socket.socket') as probe:
            probe.return_value.bind.side_effect = OSError('in use')
            self.engine(new, (80, 443)).check_ports(old)
            probe.return_value.bind.assert_not_called()

    def test_ports_not_held_by_us_are_still_probed(self):
        old = configuration()
        new = dict(old, bind='0.0.0.0', https_port=8443)
        with patch('ndm.docker.socket.socket') as probe:
            probe.return_value.bind.side_effect = OSError('in use')
            with self.assertRaisesRegex(Error, '0.0.0.0:8443'):
                self.engine(new, (80, 443)).check_ports(old)


class LogRedactionTests(unittest.TestCase):
    def test_inspect_output_never_reaches_the_log(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'run.log'
            log.touch()
            state = types.SimpleNamespace(log=log)
            runner = Runner(state)
            runner.run(['sh', '-c', 'echo MYSQL_PASSWORD=secret-value', 'inspect'])
            runner.run(['sh', '-c', 'echo visible-output'])
            text = log.read_text()
            self.assertNotIn('secret-value', text)
            self.assertIn('visible-output', text)


class BackupTests(unittest.TestCase):
    def test_unreadable_archives_are_skipped_and_leftovers_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            state = types.SimpleNamespace(path=Path(folder))
            backups = Path(folder) / 'backups'
            (backups / '.backup-old').mkdir(parents=True)
            (backups / 'x.partial').write_text('interrupted')
            broken = backups / 'broken.ndm.tar'
            broken.write_text('not a tar')
            self.assertIsNone(archive.backup_kind(broken))
            archive.clean_leftovers(state)
            self.assertEqual(sorted(path.name for path in backups.iterdir()), ['broken.ndm.tar'])


class UpdateTests(unittest.TestCase):
    def test_caddy_follows_its_major_while_mariadb_and_redis_keep_their_branch(self):
        self.assertEqual(component_series('proxy', '2.10.4'), '2')
        self.assertEqual(component_series('db', '11.4.13'), '11.4')
        self.assertEqual(component_series('redis', '7.4.12'), '7.4')

    def test_major_is_dimmed_until_the_latest_patch(self):
        from ndm.i18n import messages
        ui = types.SimpleNamespace(msg=messages('es'))
        ui.t = lambda key, *values: ui.msg.get(key, key) % values if values else ui.msg.get(key, key)
        available = {'patches': ['33.0.10'], 'next_major': ['34.0.5'], 'refreshed_image': False,
                     'components': {'proxy': {'current': '2.10.4', 'available': '2.11.7', 'image_changed': True},
                                    'db': {'current': '11.4.13', 'available': '11.4.13', 'image_changed': False}}}
        rows = update_rows(ui, {'version': '33.0.9'}, available)
        self.assertEqual([row[0] for row in rows], ['patch:33.0.10', 'major:34.0.5', 'component:proxy'])
        self.assertEqual(rows[1][1][0][1], 'dim')
        self.assertIn('actualización mayor', operation_summary(ui, {'action': 'migrate', 'phase': 'rehearsed'}))
        self.assertEqual(local(ui, 'uno / one'), 'uno')


class DatabaseSupportTests(unittest.TestCase):
    def manager(self, branches, accepted=('10.11', '11.4', '11.8')):
        from ndm.operations import Manager
        manager = Manager.__new__(Manager)
        manager.catalog = types.SimpleNamespace(
            maintained_databases=lambda: {'source': 'fixture', 'branches': branches},
            requirements=lambda major: {'mariadb': list(accepted)})
        return manager

    def test_supported_branch_offers_nothing(self):
        cfg = configuration()
        support = self.manager({'11.4': '2099-01-01', '11.8': '2099-06-01'}).database_support(cfg)
        self.assertEqual((support['state'], support['choices']), ('supported', []))

    def test_ending_and_ended_branches_offer_newer_accepted_lts_only(self):
        import datetime as dt
        cfg = configuration()
        soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        support = self.manager({'10.11': soon, '11.4': soon, '11.8': '2099-06-01', '12.3': '2099-12-01'}).database_support(cfg)
        self.assertEqual(support['state'], 'ending')
        # 12.3 is maintained but not accepted by this Nextcloud; 10.11 is older.
        self.assertEqual([choice['series'] for choice in support['choices']], ['11.8'])
        ended = self.manager({'11.8': '2099-06-01'}).database_support(cfg)
        self.assertEqual((ended['state'], ended['community_until']), ('eol', None))
        self.assertEqual([choice['series'] for choice in ended['choices']], ['11.8'])


class CleanupTests(unittest.TestCase):
    def test_named_volume_without_options_and_old_revisions_are_removed(self):
        from ndm.operations import Manager
        cfg = configuration()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            revisions = root / 'revisions'
            for index in range(7):
                (revisions / f'2026110{index}T000000Z-r').mkdir(parents=True)
            (root / 'current').symlink_to(revisions / '20261106T000000Z-r')
            manager = Manager.__new__(Manager)
            manager.state = types.SimpleNamespace(pending=None, path=root, load=lambda: cfg)
            orphan = cfg['project'] + '_db_lts1a2b3c4d'
            runner = FakeRunner({'docker volume ls': '\n'.join([*cfg['volumes'].values(), orphan])})
            engine = types.SimpleNamespace(
                cfg=cfg, runner=runner, owned=lambda info, kind: None,
                inspect=lambda kind, name: {'Labels': {}, 'Options': None})
            with patch('ndm.maintenance.Runner', lambda state: runner), \
                    patch('ndm.maintenance.Engine', lambda state: engine):
                result = manager.cleanup(supplied='CLEANUP')
            self.assertEqual(result['removed_volumes'], [orphan])
            self.assertEqual(result['removed_revisions'], 2)
            self.assertEqual(len(list(revisions.iterdir())), 5)
            self.assertIn(['docker', 'volume', 'rm', orphan], runner.calls)


class PhoneRegionTests(unittest.TestCase):
    def test_region_follows_the_timezone_country(self):
        import base64
        from ndm.config import managed_php, phone_region
        if phone_region('America/Guayaquil') is None:
            self.skipTest('System tzdata without zone.tab')
        self.assertEqual(phone_region('America/Guayaquil'), 'EC')
        self.assertEqual(phone_region('Europe/Madrid'), 'ES')
        self.assertIsNone(phone_region('UTC'))

        def settings(timezone):
            cfg = dict(configuration(), timezone=timezone)
            payload = managed_php(cfg).split("base64_decode('")[1].split("'")[0]
            return json.loads(base64.b64decode(payload))

        self.assertEqual(settings('America/Mexico_City')['default_phone_region'], 'MX')
        # Without a country Nextcloud keeps its own warning instead of a wrong region.
        self.assertNotIn('default_phone_region', settings('UTC'))


class AppUpdateTests(unittest.TestCase):
    def test_store_updates_are_read_from_nextcloud(self):
        from ndm.operations import Manager
        output = 'calendar new version available: 5.0.4\ndeck new version available: 1.14.2\nNothing else\n'
        engine = types.SimpleNamespace(occ=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, ''))
        self.assertEqual(Manager.app_updates(engine), [{'app': 'calendar', 'version': '5.0.4'},
                                                       {'app': 'deck', 'version': '1.14.2'}])

    def test_app_updates_appear_as_one_row(self):
        from ndm.i18n import messages
        ui = types.SimpleNamespace(msg=messages('es'))
        ui.t = lambda key, *values: ui.msg.get(key, key) % values if values else ui.msg.get(key, key)
        rows = update_rows(ui, {'version': '33.0.9'}, {'apps': [{'app': 'calendar', 'version': '5.0.4'}]})
        self.assertEqual(rows[0][0], 'apps')
        self.assertIn('1 aplicaciones: calendar', rows[0][1][0][0])


class ImageCleanupTests(unittest.TestCase):
    def test_only_images_from_earlier_revisions_are_removed(self):
        from ndm.operations import Manager
        cfg = configuration()
        old = dict(cfg['images'], app='nextcloud:33.0.8-apache@sha256:' + 'b' * 64)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, images in (('20261101T000000Z-a', old), ('20261102T000000Z-b', cfg['images'])):
                (root / 'revisions' / name).mkdir(parents=True)
                (root / 'revisions' / name / 'config.json').write_text(json.dumps({'images': images}))
            (root / 'current').symlink_to(root / 'revisions' / '20261102T000000Z-b')
            manager = Manager.__new__(Manager)
            manager.state = types.SimpleNamespace(pending=None, path=root, load=lambda: cfg)
            replies = {'docker volume ls': ''}
            replies.update({f'docker image inspect --format {{{{.Id}}}} {ref}': 'sha256:current\n' for ref in cfg['images'].values()})
            replies[f'docker image inspect --format {{{{.Id}}}} {old["app"]}'] = 'sha256:old\n'
            runner = FakeRunner(replies)
            engine = types.SimpleNamespace(cfg=cfg, runner=runner)
            with patch('ndm.maintenance.Runner', lambda state: runner), \
                    patch('ndm.maintenance.Engine', lambda state: engine):
                self.assertEqual(manager.cleanup_candidates()['images'], [old['app']])
                result = manager.cleanup(supplied='CLEANUP')
            self.assertEqual(result['removed_images'], [old['app']])
            self.assertIn(['docker', 'image', 'rm', 'nextcloud@sha256:' + 'b' * 64], runner.calls)


class BootstrapTests(unittest.TestCase):
    def test_derivatives_and_rebuilds_use_their_upstream_repository(self):
        self.assertEqual(system.family({'ID': 'linuxmint', 'ID_LIKE': 'ubuntu debian'}, 'ubuntu', 'debian'), 'ubuntu')
        written = {}
        for info, expected in (({'ID': 'rocky', 'ID_LIKE': 'rhel centos fedora'}, 'centos'),
                               ({'ID': 'rhel', 'ID_LIKE': 'fedora'}, 'rhel'),
                               ({'ID': 'fedora'}, 'fedora')):
            with self.subTest(distro=info['ID']), \
                    patch('ndm.system.fetch', lambda url: url.encode()), \
                    patch('ndm.system.atomic', lambda path, value, mode: written.update({path: value})), \
                    patch('ndm.system.docker_repository', lambda folder: False):
                system.install_dnf(FakeRunner({'rpm -q runc': ('', 1)}), info)
                self.assertIn(f'/linux/{expected}/docker-ce.repo', written['/etc/yum.repos.d/ndm-docker.repo'])

    def test_existing_runc_is_never_replaced_by_containerd(self):
        runner = FakeRunner({'rpm -q runc': ('runc-1.4.2-3.el9_8.x86_64\n', 0)})
        with self.assertRaisesRegex(Error, 'runc'):
            system.install_dnf(runner, {'ID': 'rocky', 'ID_LIKE': 'rhel centos fedora'})
        self.assertFalse(any(call[0] == 'dnf' for call in runner.calls))

    def test_alpine_without_openrc_installs_packages_only(self):
        runner = FakeRunner({})
        with patch('ndm.system.shutil.which', lambda name: None):
            system.install_distribution_packages(runner, 'apk')
        self.assertEqual(runner.calls, [['apk', 'add', 'docker', 'docker-cli-compose']])

    def test_32_bit_userland_is_rejected_before_any_repository_change(self):
        runner = FakeRunner({'dpkg --print-architecture': 'armhf\n'})
        with self.assertRaisesRegex(Error, 'armhf'):
            system.install_apt(runner, {'ID': 'debian', 'VERSION_CODENAME': 'bookworm'})
        self.assertFalse(any(call[0] == 'apt-get' for call in runner.calls))


if __name__ == '__main__':
    unittest.main()
