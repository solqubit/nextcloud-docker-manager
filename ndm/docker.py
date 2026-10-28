"""Docker calls restricted to one labelled installation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import time

from .config import DEFAULT_APPSTORE_TIMEOUT, compose, managed_php
from .core import Error, LABEL, Runner, atomic


class Engine:
    def __init__(self, state, cfg=None, revision=None):
        self.state = state
        self.cfg = cfg or state.load()
        self.runner = Runner(state)
        self.revision = Path(revision) if revision else state.path / 'current'

    def dc(self, *args, **kwargs):
        return self.runner.run(['docker', 'compose', '-p', self.cfg['project'],
                                '-f', self.revision / 'compose.json', *args], **kwargs)

    def inspect(self, kind, name):
        result = self.runner.run(['docker', kind, 'inspect', name], check=False)
        return json.loads(result.stdout)[0] if result.returncode == 0 else None

    def owned(self, info, kind='container'):
        labels = info.get('Config', {}).get('Labels', {}) if kind == 'container' else info.get('Labels', {})
        if labels.get(LABEL + '.installation') != self.cfg['id']:
            raise Error('Recurso ajeno / Resource not owned by this installation')

    def container(self, service, required=True):
        result = self.dc('ps', '--all', '--quiet', service, check=False)
        value = result.stdout.strip()
        if not value:
            if required:
                raise Error(f'Contenedor no disponible: {service} / Container unavailable: {service}')
            return None
        if '\n' in value:
            raise Error('Unexpected replicated service')
        self.owned(self.inspect('container', value) or {})
        return value

    def pull(self, components=None):
        for component in components or self.cfg['images']:
            ref = self.cfg['images'][component]
            if not self.inspect('image', ref):
                self.runner.run(['docker', 'pull', '--quiet', ref], timeout=1800)
            else:
                # Check presence by immutable reference rather than a mutable tag.
                self.runner.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref])

    def volume_path(self, role):
        # A restored SQL/application generation must never reuse the old bind directory.
        suffix = self.cfg['volumes'][role][len(self.cfg['project']) + 1:]
        return Path(self.cfg['storage_root']) / self.cfg['id'] / suffix

    def ensure_volumes(self):
        for role, name in self.cfg['volumes'].items():
            info = self.inspect('volume', name)
            if info:
                self.owned(info, 'volume')
                if self.cfg['storage'] == 'directory' and info.get('Options', {}).get('device') != str(self.volume_path(role)):
                    raise Error('Volume path mismatch')
                continue
            args = ['docker', 'volume', 'create', '--label', LABEL + '.installation=' + self.cfg['id'],
                    '--label', LABEL + '.role=' + role]
            if self.cfg['storage'] == 'directory':
                if snap_docker(self.runner):
                    # Snap confinement cannot bind folders such as /opt/nextcloud-docker-manager/data.
                    raise Error('Docker instalado por Snap no puede usar carpetas del servidor; elige volúmenes Docker '
                                'o instala Docker Engine (docker-ce) / Snap Docker cannot bind server folders; '
                                'choose Docker volumes or install Docker Engine (docker-ce)')
                folder = self.volume_path(role)
                root = Path(self.cfg['storage_root'])
                if any(p.is_symlink() for p in [root, root / self.cfg['id'], folder]):
                    raise Error('Unsafe storage symlink')
                # Initial creation only; later starts must verify the recorded filesystem.
                folder.mkdir(parents=True, exist_ok=True, mode=0o750)
                selinux_label(self.runner, folder)
                args += ['--driver', 'local', '--opt', 'type=none', '--opt', 'o=bind', '--opt', 'device=' + str(folder)]
                if role == 'data':
                    self.cfg['data_device'] = folder.stat().st_dev
                    self.cfg['data_fsid'] = filesystem_id(folder)
            self.runner.run(args + [name])

    def check_mount(self):
        if self.cfg['storage'] == 'directory':
            folder = self.volume_path('data')
            if not folder.is_dir() or folder.is_symlink() or self.cfg.get('data_device') is None:
                raise Error('El almacenamiento de datos no está disponible en su dispositivo original / Data mount unavailable')
            # Device numbers change after reboots on Btrfs subvolumes or renumbered USB/LVM
            # disks; the filesystem id stays with the filesystem itself.
            if self.cfg.get('data_fsid') is not None:
                if filesystem_id(folder) == self.cfg['data_fsid']:
                    return
            elif folder.stat().st_dev == self.cfg['data_device']:
                return
            raise Error('El almacenamiento de datos no está disponible en su dispositivo original / Data mount unavailable')

    def check_ports(self, old=None):
        ids = self.runner.run(['docker', 'ps', '--quiet']).stdout.split()
        containers = json.loads(self.runner.run(['docker', 'container', 'inspect', *ids]).stdout) if ids else []
        own = set()
        for container in containers:
            if container.get('Config', {}).get('Labels', {}).get(LABEL + '.installation') == self.cfg['id']:
                for bindings in container.get('NetworkSettings', {}).get('Ports', {}).values():
                    own.update(int(binding['HostPort']) for binding in bindings or [])
        for key in ('http_port', 'https_port'):
            if old and old['bind'] == self.cfg['bind'] and old[key] == self.cfg[key]:
                continue
            for container in containers:
                if container.get('Config', {}).get('Labels', {}).get(LABEL + '.installation') == self.cfg['id']:
                    continue
                for bindings in container.get('NetworkSettings', {}).get('Ports', {}).values():
                    for binding in bindings or []:
                        if int(binding['HostPort']) == self.cfg[key] and (binding['HostIp'] in ('0.0.0.0', '::', self.cfg['bind']) or self.cfg['bind'] == '0.0.0.0'):
                            raise Error(f"Puerto ocupado por otro contenedor: {self.cfg[key]} / Port owned by another container: {self.cfg[key]}")
            if self.cfg[key] in own:
                # Our own proxy holds this port on the previous IP, so a socket probe on a
                # wildcard/other IP would always collide with it. Compose reports any other
                # conflict when the proxy is recreated, and settings() then restores the old revision.
                continue
            probe = socket.socket()
            try:
                probe.bind((self.cfg['bind'], self.cfg[key]))
            except OSError as exc:
                where = f"{self.cfg['bind']}:{self.cfg[key]}"
                raise Error(f"IP/puerto no disponible: {where} / IP/port unavailable: {where}") from exc
            finally:
                probe.close()

    def validate(self):
        self.dc('config', '--quiet')
        # Validate the real Caddy binary before touching the active proxy.
        self.runner.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'caddy',
                         '--env', 'HOME=/tmp', self.cfg['images']['proxy'], 'adapt', '--adapter', 'caddyfile', '--config', '-'],
                        input=compose(self.cfg)['services']['proxy']['environment']['NDM_CADDY_CONFIG'])

    def put_managed(self, rehearsal=False):
        script = ('mkdir -p /var/www/html/config; '
                  'if [ ! -f /var/www/html/version.php ] && [ ! -f /var/www/html/config/config.php ]; then '
                  'cp -a /usr/src/nextcloud/config/. /var/www/html/config/; fi; '
                  'cat > /var/www/html/config/.ndm-new; '
                  'chown www-data:www-data /var/www/html/config /var/www/html/config/.ndm-new; '
                  'chmod 640 /var/www/html/config/.ndm-new; '
                  'mv /var/www/html/config/.ndm-new /var/www/html/config/zz-ndm.config.php; '
                  'moved=0; for name in nextcloud.log nextcloud.log.1; do '
                  'old="/var/www/html/$name"; '
                  'if [ -f "$old" ] && [ ! -L "$old" ]; then '
                  'saved=$(mktemp "/var/www/html/config/ndm-legacy-$name.XXXXXX"); '
                  'mv "$old" "$saved"; chmod 640 "$saved"; moved=$((moved + 1)); '
                  'fi; done; printf "%s" "$moved"')
        result = self.runner.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', '/bin/sh',
                         '--mount', 'type=volume,src=' + self.cfg['volumes']['app'] + ',dst=/var/www/html',
                         self.cfg['images']['app'], '-ec', script], input=managed_php(self.cfg, rehearsal=rehearsal))
        return int(result.stdout.strip() or '0')

    def up(self, *services):
        self.check_mount()
        self.dc('up', '-d', '--no-build', *services, timeout=1200)

    def stop(self, *services):
        self.dc('stop', '-t', '60', *services, timeout=180)

    def occ(self, *args, check=True, timeout=600):
        return self.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '/var/www/html/occ',
                       *args, check=check, timeout=timeout)

    def status(self):
        cid = self.container('app', required=False)
        if not cid:
            return {'installed': False, 'container': 'missing'}
        info = self.inspect('container', cid)
        if not info['State']['Running']:
            return {'installed': False, 'container': info['State']['Status']}
        result = self.occ('status', '--output=json', check=False, timeout=25)
        start = result.stdout.find('{')
        try:
            return {**json.loads(result.stdout[start:]), 'container': 'running'} if start >= 0 else {'installed': False, 'container': 'starting'}
        except ValueError:
            return {'installed': False, 'container': 'starting'}

    def wait_app(self, installed=True, timeout=600):
        until = time.monotonic() + timeout
        last = {}
        while time.monotonic() < until:
            last = self.status()
            if last.get('container') == 'running' and self.serving():
                if not installed and last.get('version'):
                    return last
                if (installed and last.get('installed') and not last.get('needsDbUpgrade')
                        and not last.get('maintenance')):
                    return last
            time.sleep(3)
        raise Error('Nextcloud no está listo / Not ready: ' + json.dumps(last))

    def serving(self):
        cid = self.container('app', required=False)
        info = self.inspect('container', cid) if cid else None
        return bool(info and info['State'].get('Health', {}).get('Status') == 'healthy')

    def sql(self, statement, root=False):
        user = 'root' if root else 'nextcloud'
        var = 'MARIADB_ROOT_PASSWORD' if root else 'MARIADB_PASSWORD'
        script = f'MYSQL_PWD="${var}" exec mariadb --batch --skip-column-names -u{user} nextcloud'
        return self.dc('exec', '-T', 'db', 'sh', '-ec', script, input=statement + '\n')

    def verify(self, installed=True, configure_cron=False):
        status = self.wait_app(installed=installed)
        if self.sql('SELECT 1;').stdout.strip() != '1':
            raise Error('MariaDB no responde / SQL readiness failed')
        result = self.dc('exec', '-T', 'redis', 'sh', '-ec',
                         'REDISCLI_AUTH="$NDM_REDIS_PASSWORD" redis-cli ping')
        if result.stdout.strip() != 'PONG':
            raise Error('Redis no responde / Redis readiness failed')
        if installed:
            if status.get('versionstring') != self.cfg['version']:
                raise Error('La versión de Nextcloud no coincide con la imagen elegida / Application version differs from selected image')
            if configure_cron:
                self.configure_app_defaults()
                self.occ('background:cron')
        until = time.monotonic() + 90
        while time.monotonic() < until:
            cid = self.container('proxy', required=False)
            info = self.inspect('container', cid) if cid else None
            if info and info['State'].get('Health', {}).get('Status') == 'healthy':
                break
            time.sleep(2)
        else:
            raise Error('El proxy no está listo; revisa los registros de Caddy / Reverse proxy failed its readiness check; inspect proxy logs')
        if installed and not self.cfg['maintenance_gate']:
            entry = next(e for e in self.cfg['domains'] if e['host'] == self.cfg['primary'])
            headers, body = json.loads(self.probe_http(entry))
            code = headers[0].split()[1] if headers else ''
            if code not in ('200', '308') or (code == '200' and not json.loads(body).get('installed')):
                raise Error('Nextcloud no responde a través del proxy configurado / Nextcloud is not reachable through its configured proxy route')
        return status

    def configure_app_defaults(self):
        current = self.occ('config:app:get', 'settings', 'appstore-timeout', '--default-value=').stdout.strip()
        if not current:
            self.occ('config:app:set', 'settings', 'appstore-timeout', f'--value={DEFAULT_APPSTORE_TIMEOUT}')

    def reset_password(self, user, password):
        # Send the password only through stdin, never argv, an environment on
        # the Docker client, a file, or manager command/output logs.
        script = ('putenv("NC_PASS=".stream_get_contents(STDIN)); '
                  '$argv[0]="/var/www/html/occ"; $_SERVER["argv"]=$argv; '
                  'require "/var/www/html/occ";')
        result = self.dc('exec', '-T', '--user', 'www-data', 'app', 'php', '-r', script, '--',
                         'user:resetpassword', '--password-from-env', '--no-interaction', '--', user,
                         input=password, quiet=True, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).replace(password, '[hidden]')[-800:]
            raise Error('No se pudo restablecer la contraseña / Could not reset password: ' + detail)

    def probe_http(self, entry):
        """Probe the proxy from the application network; no public credentials sent."""
        script = '$c=stream_context_create(["http"=>["header"=>"Host: ' + entry['host'] + '\\r\\n","ignore_errors"=>true,"follow_location"=>0]]);'
        script += '$b=@file_get_contents("http://proxy/status.php",false,$c);'
        script += '$h=function_exists("http_get_last_response_headers")?(http_get_last_response_headers()??[]):($http_response_header??[]);'
        script += 'echo json_encode([$h,$b],JSON_THROW_ON_ERROR|JSON_INVALID_UTF8_SUBSTITUTE);'
        return self.dc('exec', '-T', 'app', 'php', '-d', 'display_errors=stderr', '-r', script).stdout

    def db_size(self):
        value = self.sql("SELECT COALESCE(SUM(data_length+index_length),0) FROM information_schema.tables WHERE table_schema='nextcloud';").stdout.strip()
        return int(value)

    def check_space(self, required):
        # The backup filesystem can differ from Docker's SQL/application filesystem.
        # Probe the actual SQL volume without reading any database or user-file data.
        result = self.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'df',
            '--mount', 'type=volume,src=' + self.cfg['volumes']['db'] + ',dst=/ndm-space,readonly',
            self.cfg['images']['app'], '-Pk', '/ndm-space'])
        available = int(result.stdout.splitlines()[-1].split()[3]) * 1024
        if available < required:
            raise Error('Espacio insuficiente en los volúmenes SQL/aplicación / Insufficient SQL/application volume space')
        return available

    def app_size(self):
        # Whitelist the application tree; never enumerate the userdata mount.
        cid = self.container('app')
        result = self.runner.run(['docker', 'run', '--rm', '--network', 'none', '--volumes-from', cid + ':ro',
            '--entrypoint', 'du', self.cfg['images']['app'], '-sk', '--exclude=/var/www/html/data', '/var/www/html'])
        return int(result.stdout.split()[0]) * 1024

    def ca_certificate(self):
        result = self.runner.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat',
            '--mount', 'type=volume,src=' + self.cfg['volumes']['proxy_data'] + ',dst=/data,readonly',
            self.cfg['images']['proxy'], '/data/caddy/pki/authorities/local/root.crt'], check=False)
        if result.returncode:
            raise Error('La CA local aún no se ha creado / Local CA not available yet')
        dest = self.state.path / 'local-ca.crt'
        atomic(dest, result.stdout, 0o644)
        return dest

    def export_images(self, destination):
        self.runner.run(['docker', 'image', 'save', '-o', destination, *set(self.cfg['images'].values())], timeout=1800)
        os.chmod(destination, 0o600)

    def remove_owned(self, delete_volumes=False):
        # Check labels before Compose acts on names selected by its project label.
        containers = self.runner.run(['docker', 'ps', '--all', '--quiet', '--filter',
                                     'label=com.docker.compose.project=' + self.cfg['project']]).stdout.split()
        for cid in containers:
            self.owned(self.inspect('container', cid) or {})
        self.dc('down', '--remove-orphans', *(['--volumes'] if delete_volumes else []), timeout=180)
        if delete_volumes:
            for name in self.cfg['volumes'].values():
                info = self.inspect('volume', name)
                if info:
                    self.owned(info, 'volume')
                    self.runner.run(['docker', 'volume', 'rm', name])


def filesystem_id(path):
    return os.statvfs(path).f_fsid


def selinux_label(runner, folder):
    """Bind-mounted folders need the container label on SELinux hosts (Fedora, RHEL, Rocky, Alma)."""
    enforce = Path('/sys/fs/selinux/enforce')
    if not enforce.exists() or not shutil.which('chcon'):
        return
    # Docker CE confines containers only with selinux-enabled; labelling is harmless otherwise.
    runner.run(['chcon', '-R', '-t', 'container_file_t', folder], check=False)


def snap_docker(runner):
    root = runner.run(['docker', 'info', '--format', '{{.DockerRootDir}}'], check=False).stdout.strip()
    return root.startswith('/var/snap/') or (shutil.which('docker') or '').startswith('/snap/')


def require_engine(runner):
    """Technical requirements, independent of the distribution that provides them."""
    if runner.run(['docker', 'version', '--format', '{{.Server.Os}}']).stdout.strip() != 'linux':
        raise Error('Docker debe usar contenedores Linux / Docker must use Linux containers')
    if runner.run(['docker', 'compose', 'version'], check=False).returncode:
        raise Error('Falta Docker Compose V2 (docker compose) / Docker Compose V2 plugin missing')
    data = runner.run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}']).stdout.strip()
    if os.environ.get('DOCKER_HOST', data).startswith(('tcp:', 'ssh:')):
        raise Error('Solo Docker local / Only a local Docker Engine is supported')
    architecture = runner.run(['docker', 'info', '--format', '{{.Architecture}}'], check=False).stdout.strip()
    if architecture and architecture not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        raise Error(f'Arquitectura de Docker no compatible: {architecture} (amd64/arm64) / '
                    f'Unsupported Docker architecture: {architecture} (amd64/arm64)')
