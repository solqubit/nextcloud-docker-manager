"""Opt-in real Docker acceptance suite. Uses only its explicitly supplied test state.

Run in the Linux test runner, with a Docker socket and dedicated state volume.
Production code never creates these fixture credentials or fixture user files.
"""
import argparse
import base64
import copy
import html
import http.client
import http.cookiejar
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from ndm import archive
from ndm.config import access_url
from ndm.core import Error, State, atomic, read_json
from ndm.docker import Engine
from ndm.operations import Manager


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Suite:
    def __init__(self, state):
        self.state = state
        self.manager = Manager(state)
        self.engine = Engine(state)
        self.credentials = state.path / 'cache' / 'fixture-credentials.json'
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, method='GET', body=None, authenticated=False, name='127.0.0.1', extra=None):
        headers = {'Host': name + ':' + str(self.engine.cfg['http_port']), **(extra or {})}
        if authenticated:
            credentials = read_json(self.credentials)
            token = base64.b64encode((credentials['user'] + ':' + credentials['password']).encode()).decode()
            headers['Authorization'] = 'Basic ' + token
        if isinstance(body, dict):
            body = urllib.parse.urlencode(body).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(f"http://host.docker.internal:{self.engine.cfg['http_port']}" + path,
                                         method=method, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=300)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, response.headers, response.read()

    def prepare(self, *, complete_setup=True):
        if not self.engine.status().get('installed'):
            credentials = {'user': 'ndm-test-admin', 'password': secrets.token_urlsafe(28)}
            atomic(self.credentials, credentials)
            code, _, page = self.request('/index.php')
            assert code == 200
            token = html.unescape(re.search(r'data-requesttoken="([^"]+)"', page.decode())[1])
            code, _, page = self.request('/index.php', 'POST',
                dict(install='true', adminlogin=credentials['user'], adminpass=credentials['password'], requesttoken=token))
            assert code in (200, 302, 303), f'Web setup HTTP {code}'
        self.engine.wait_app()
        if complete_setup:
            self.manager.setup_check()
        self.opener = urllib.request.build_opener(NoRedirect)
        user = read_json(self.credentials)['user']
        path = f'/remote.php/dav/files/{user}/ndm-preserved.txt'
        code, _, body = self.request(path, 'PUT', b'Nextcloud manager retained user file\n', True)
        assert code in (201, 204), f'DAV PUT {code}: {body[:800]!r}'
        assert self.request(path, authenticated=True)[2] == b'Nextcloud manager retained user file\n'
        assert self.request(f'/remote.php/dav/files/{user}/', 'PROPFIND', authenticated=True,
                            extra={'Depth': '1'})[0] == 207
        code, _, body = self.request('/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json', 'POST',
            {'path': '/ndm-preserved.txt', 'shareType': '3', 'permissions': '1'}, True, extra={'OCS-APIRequest': 'true'})
        assert code == 200 and json.loads(body)['ocs']['meta']['statuscode'] == 200
        atomic(self.state.path / 'cache' / 'fixture-share.json', json.loads(body)['ocs']['data'])
        self.engine.occ('config:app:set', 'ndm_test', 'marker', '--value', 'before-update')
        self.engine.dc('exec', '-T', 'app', 'sh', '-ec',
            'printf "NDM_USER_DATA_EXCLUSION_SENTINEL\\n" > /var/www/html/data/.ndm-exclusion-fixture')
        print('[PASS] Web-admin setup, authenticated WebDAV PUT/GET/PROPFIND and public-share metadata')

    def logging_health(self):
        """Reproduce EXTRA_FILE, preserve both old logs, and keep integrity enabled."""
        assert self.engine.sql('SELECT @@innodb_log_file_size;').stdout.strip() == '268435456'
        assert self.engine.occ('config:system:get', 'logfile').stdout.strip() == '/var/www/html/data/nextcloud.log'
        self.engine.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-r',
            'file_put_contents("/var/www/html/nextcloud.log", "NDM_LEGACY_ROOT_LOG");'
            'file_put_contents("/var/www/html/nextcloud.log.1", "NDM_LEGACY_ROTATED_LOG");')
        before = self.engine.occ('integrity:check-core', check=False)
        assert before.returncode != 0 and 'EXTRA_FILE' in before.stdout and 'nextcloud.log' in before.stdout
        result = self.manager.setup_check()
        assert result['core_integrity_ok']
        check = self.engine.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-r',
            '$found=[]; foreach(glob("/var/www/html/config/ndm-legacy-nextcloud.log*") as $file) {'
            '$found[]=file_get_contents($file); }'
            'echo json_encode(["logs"=>$found,"root_exists"=>file_exists("/var/www/html/nextcloud.log")]);')
        saved = json.loads(check.stdout)
        assert not saved['root_exists']
        assert {'NDM_LEGACY_ROOT_LOG', 'NDM_LEGACY_ROTATED_LOG'} <= set(saved['logs'])
        self.engine.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-r',
            'require "/var/www/html/lib/base.php";'
            '\\OCP\\Server::get(\\Psr\\Log\\LoggerInterface::class)->warning("NDM_ACTIVE_LOG_PROBE");')
        self.engine.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-r',
            'exit(str_contains(file_get_contents("/var/www/html/data/nextcloud.log"), "NDM_ACTIVE_LOG_PROBE") ? 0 : 1);')
        for mode in ('direct', 'tunnel'):
            proposed = copy.deepcopy(self.state.load())
            proposed['domains'].append(dict(host='ndm-fixture.example.com', kind='public',
                                             tls='acme', mode=mode, email=''))
            Engine(self.state, proposed).validate()
        print('[PASS] InnoDB redo 256 MiB, reproduced EXTRA_FILE, legacy logs preserved, integrity clean and active log in data')

    def domains(self):
        if not any(e['host'] == 'nube.test' for e in self.state.load()['domains']):
            self.manager.add_domain('nube.test', supplied='APPLY')
        assert self.request('/status.php', name='nube.test')[0] == 200
        assert self.request('/status.php')[0] == 200
        assert self.request('/status.php', name='untrusted.test')[0] == 404
        if not any(e['host'] == 'nube.internal' for e in self.state.load()['domains']):
            self.manager.add_domain('nube.internal', tls='internal', supplied='APPLY')
        ca = self.engine.ca_certificate()
        connection = http.client.HTTPSConnection('nube.internal', self.engine.cfg['https_port'],
                                                 context=ssl.create_default_context(cafile=str(ca)))
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            ('host.docker.internal', self.engine.cfg['https_port']), timeout=timeout)
        connection.request('GET', '/status.php')
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())['installed']
        connection.request('GET', '/.well-known/carddav')
        response = connection.getresponse()
        assert response.status == 301 and response.getheader('Location') == '/remote.php/dav/'
        response.read()
        connection.request('POST', '/index.php/login/v2', body=b'')
        response = connection.getresponse()
        login = json.loads(response.read())
        assert response.status == 200 and login['login'].startswith('https://nube.internal:')
        connection.close()
        code, _, body = self.request('/index.php/login/v2', 'POST', b'')
        assert code == 200 and json.loads(body)['login'].startswith('http://127.0.0.1:')
        self.manager.remove_domain('nube.test', 'APPLY')
        assert self.request('/status.php', name='nube.test')[0] == 404
        assert self.request('/status.php')[0] == 200
        print('[PASS] Trusted hosts, add/remove, untrusted-host rejection, verified local TLS, DAV discovery and client login v2 HTTPS/HTTP coexistence')

    def failures(self):
        original = self.state.load()
        try:
            self.manager.update('34.0.5', supplied='UPDATE-34.0.5')
            raise AssertionError('Cross-major patch accepted')
        except Error:
            assert self.state.pending is None
        previous = Engine.put_managed
        count = [0]
        def fail_once(engine, *args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                raise Error('Injected application-configuration write failure')
            return previous(engine, *args, **kwargs)
        candidate = copy.deepcopy(original)
        candidate['domains'].append(dict(host='cancelled.test', kind='local', mode='local', tls='off', email=''))
        try:
            with patch.object(Engine, 'put_managed', fail_once):
                self.manager.settings(candidate, supplied='APPLY')
            raise AssertionError('Injected failure was ignored')
        except Error:
            assert self.state.load() == original and self.state.pending is None
            assert self.request('/status.php')[0] == 200
        print('[PASS] Cross-major patch blocked; failed settings compensated and previous access retained')
        if original['version'] == '33.0.9':
            try:
                with patch.object(self.manager, 'rehearse', side_effect=Error('Injected rehearsal failure')):
                    self.manager.update('33.0.10', supplied='UPDATE-33.0.10')
                raise AssertionError('Rehearsal failure ignored')
            except Error:
                # The live SQL was never touched, so the service reopens unchanged by itself.
                assert self.state.pending is None
                assert self.state.load()['version'] == '33.0.9'
                assert self.state.load()['maintenance_gate'] is False
                assert self.request('/status.php')[0] == 200
            self.engine = Engine(self.state)
            assert self.engine.status()['versionstring'] == '33.0.9'
            self.check_file()
            print('[PASS] Failed rehearsal blocks deployment and reopens the original SQL/application without manual recovery')

    def update(self):
        original_db = self.engine.cfg['images']['db']
        path = self.manager.update('33.0.10', supplied='UPDATE-33.0.10')
        self.engine = Engine(self.state)
        assert self.engine.status()['versionstring'] == '33.0.10'
        with archive.unpack(self.state, path) as (work, manifest):
            with tarfile.open(work / 'app.tar') as source:
                assert not any(p.name.lstrip('./').startswith('data/') for p in source)
            assert '/var/www/html/data' in manifest['excluded']
        assert self.engine.cfg['images']['db'] == original_db
        self.check_file()
        atomic(self.state.path / 'cache' / 'fixture-backup.json', {'path': str(path)})
        print('[PASS] Real 33.0.9 -> 33.0.10: consistent backup, isolated actual-SQL rehearsal, migration and user files retained; database image unchanged')

    def check_file(self):
        user = read_json(self.credentials)['user']
        assert self.request(f'/remote.php/dav/files/{user}/ndm-preserved.txt', authenticated=True)[2] == b'Nextcloud manager retained user file\n'
        assert self.engine.dc('exec', '-T', 'app', 'cat', '/var/www/html/data/.ndm-exclusion-fixture').stdout.strip() == 'NDM_USER_DATA_EXCLUSION_SENTINEL'
        share = read_json(self.state.path / 'cache' / 'fixture-share.json')
        code, _, body = self.request('/ocs/v2.php/apps/files_sharing/api/v1/shares/' + str(share['id']) + '?format=json',
                                    authenticated=True, extra={'OCS-APIRequest': 'true'})
        returned = json.loads(body)['ocs']['data']
        returned = returned[0] if isinstance(returned, list) and returned else returned
        assert code == 200 and returned['token'] == share['token'], 'Share metadata not retained'

    def restore(self):
        path = read_json(self.state.path / 'cache' / 'fixture-backup.json')['path']
        data_volume = self.engine.cfg['volumes']['data']
        old_db_volume = self.engine.cfg['volumes']['db']
        self.engine.occ('config:app:set', 'ndm_test', 'marker', '--value', 'after-update')
        self.manager.restore(path, supplied='RESTORE', accept_user_files=True)
        self.engine = Engine(self.state)
        assert self.engine.status()['versionstring'] == '33.0.9'
        assert self.engine.cfg['volumes']['data'] == data_volume
        assert self.engine.cfg['volumes']['db'] != old_db_volume
        assert self.engine.occ('config:app:get', 'ndm_test', 'marker').stdout.strip() == 'before-update'
        self.check_file()
        print('[PASS] Actual restoration of matching SQL + application; fresh SQL volume; live user-file volume and fixture file unchanged')

    def auxiliary(self):
        import shutil
        import subprocess
        cfg = self.state.load()
        occupied = copy.deepcopy(cfg)
        # Reuse the already published proxy port under another identity to emulate a
        # competing stack. This checks actual Docker bindings even inside the runner.
        occupied.update(id='0' * 16, project='ndm_' + '0' * 16)
        try:
            Engine(self.state, occupied).check_ports()
            raise AssertionError('Docker host port conflict accepted')
        except Error:
            pass
        with archive.unpack(self.state, sorted((self.state.path / 'backups').glob('*.ndm.tar'))[-1]):
            pass
        self.engine.occ('background:cron')
        self.engine.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-f', '/var/www/html/cron.php')
        health = self.manager.maintenance()
        assert not health['errors'], health['errors']
        exported = self.state.path / 'cache' / 'sample-caddy-image.tar'
        single = copy.deepcopy(cfg)
        single['images'] = {'proxy': cfg['images']['proxy']}
        Engine(self.state, single).export_images(exported)
        assert exported.stat().st_size > 1024 * 1024
        with tarfile.open(exported) as image:
            assert image.getmembers()
        exported.unlink()
        self.manager.self_update('/workspace', supplied='UPDATE-MANAGER')
        blocked = subprocess.run(['bash', str(self.state.path / 'start.sh'), 'status'], capture_output=True, text=True, cwd='/tmp')
        assert blocked.returncode == 1 and 'operation' in blocked.stderr.lower()
        self.state.lock_file.close()
        try:
            completed = subprocess.run(['bash', str(self.state.path / 'start.sh'), 'status'], capture_output=True, text=True, cwd='/tmp')
            diagnostic = subprocess.run(['bash', str(self.state.path / 'start.sh'), 'diagnose'], capture_output=True, text=True, cwd='/tmp')
        finally:
            self.state.open()
        assert completed.returncode == 0 and 'versionstring' in completed.stdout, completed.stderr
        assert diagnostic.returncode == 0 and json.loads(diagnostic.stdout)['sql_application_free_bytes'] > 0, diagnostic.stderr
        print('[PASS] Docker port conflict, backup parser, cron, daily health/certificate checks, image export and independently installed manager')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'domains', 'failures', 'update', 'restore', 'auxiliary'])
    parser.add_argument('--state', default='/ndm-state')
    args = parser.parse_args()
    if args.state in ('/opt/nextcloud-docker-manager', '/'):
        raise SystemExit('Refusing production/default state')
    state = State(args.state).open()
    try:
        getattr(Suite(state), args.phase)()
    finally:
        state.lock_file.close()


if __name__ == '__main__':
    main()
