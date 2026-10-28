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
