"""Validated configuration and reproducible Compose/Caddy generation."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import Error, LABEL, version_tuple


DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443


def host(value):
    value = value.strip().lower().rstrip('.')
    if not value or any(c in value for c in '/\\:@*\n\r\t '):
        raise Error('Use an IPv4 address or hostname without protocol/port / IP o dominio sin protocolo ni puerto')
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        pass
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise Error('Dominio inválido / Invalid hostname') from exc
    if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                   for p in value.split('.')):
        raise Error('Dominio inválido / Invalid hostname')
    return value


def public_host(value):
    value = host(value)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if '.' in value and not value.endswith(('.local', '.internal', '.localhost', '.home.arpa')):
            return value
    raise Error('Se necesita un dominio público / A public DNS hostname is required')


def port(value):
    value = int(value)
    if not 1 <= value <= 65535:
        raise Error('Puerto inválido / Invalid port')
    return value


def validate(cfg):
    if cfg.get('format') != 1 or not re.fullmatch('[a-f0-9]{16}', cfg.get('id', '')):
        raise Error('Invalid configuration format')
    if cfg.get('project') != 'ndm_' + cfg['id']:
        raise Error('Invalid project identity')
    version_tuple(cfg['version'])
    for component, repo in (('app', 'nextcloud'), ('db', 'mariadb'), ('redis', 'redis'), ('proxy', 'caddy')):
        ref = cfg['images'][component]
        if not re.fullmatch(re.escape(repo) + r':[0-9]+\.[0-9]+\.[0-9]+(?:-apache)?@sha256:[a-f0-9]{64}', ref):
            raise Error('Images must use exact official tags and digests')
    if not cfg['images']['app'].startswith(f"nextcloud:{cfg['version']}-apache@"):
        raise Error('Image/application version mismatch')
    if cfg['environment'] not in ('lan', 'vps') or cfg['storage'] not in ('named', 'directory'):
        raise Error('Invalid installation environment/storage')
    ipaddress.IPv4Address(cfg['bind'])
    if port(cfg['http_port']) == port(cfg['https_port']):
        raise Error('HTTP and HTTPS ports must differ')
    try:
        ZoneInfo(cfg['timezone'])
    except ZoneInfoNotFoundError as exc:
        raise Error('Unknown timezone or missing tzdata / Zona horaria inválida o falta tzdata') from exc
    network = ipaddress.ip_network(cfg['subnet'])
    if network.version != 4 or network.prefixlen != 28 or not network.is_private:
        raise Error('Invalid private network')
    if cfg['language'] not in ('es', 'en') or not isinstance(cfg['maintenance_gate'], bool):
        raise Error('Invalid language/state')
    if 'db_redo_log_size' in cfg and (type(cfg['db_redo_log_size']) is not int
                                    or not 128 * 1024 ** 2 <= cfg['db_redo_log_size'] <= 1024 ** 3):
        raise Error('Invalid MariaDB redo log size')
    if 'initial_maintenance_pending' in cfg and type(cfg['initial_maintenance_pending']) is not bool:
        raise Error('Invalid initial maintenance state')
    for name in ('app', 'db', 'data', 'proxy_data', 'proxy_config'):
        if not re.fullmatch(r'ndm_[a-f0-9]{16}_[a-z0-9_]+', cfg['volumes'][name]):
            raise Error('Invalid volume identity')
    if cfg['storage'] == 'directory':
        root = Path(cfg['storage_root'])
        if not root.is_absolute() or str(root) == '/' or '..' in root.parts:
            raise Error('Invalid storage root')
    entries = cfg['domains']
    if not entries or len(entries) > 32 or len({e['host'] for e in entries}) != len(entries):
        raise Error('Se necesita al menos una dirección única / Unique access addresses required')
    for entry in entries:
        if entry['host'] != host(entry['host']) or entry['kind'] not in ('local', 'public'):
            raise Error('Invalid access entry')
        if entry['tls'] not in ('off', 'internal', 'acme') or entry['mode'] not in ('local', 'direct', 'tunnel'):
            raise Error('Invalid HTTPS/access mode')
        if entry['kind'] == 'public':
            public_host(entry['host'])
            port(entry.get('external_port', 443))
            if entry['tls'] != 'acme' or entry['mode'] == 'local':
                raise Error('Public access requires HTTPS')
        if entry.get('email') and not re.fullmatch(r'[^\s@{}"\\]+@[^\s@{}"\\]+\.[^\s@{}"\\]+', entry['email']):
            raise Error('Invalid email')
    if cfg['primary'] not in [e['host'] for e in entries]:
        raise Error('Primary address must be allowed')
    for secret in ('db_password', 'root_password', 'redis_password'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}', cfg[secret]):
            raise Error('Invalid generated secret')


def initial(version, images, *, environment='lan', bind='127.0.0.1', address=None,
            http_port=DEFAULT_HTTP_PORT, https_port=DEFAULT_HTTPS_PORT, timezone='UTC', language='es',
            storage='directory', storage_root=None, subnet=None):
    identity = secrets.token_hex(8)
    cfg = dict(format=1, id=identity, project='ndm_' + identity, version=version,
               images=images, environment=environment, bind=bind,
               http_port=port(http_port), https_port=port(https_port), timezone=timezone,
               language=language, storage=storage, storage_root=storage_root or '/opt/nextcloud-docker-manager/data',
               volumes={p: f'ndm_{identity}_{p}' for p in ('app', 'db', 'data', 'proxy_data', 'proxy_config')},
               subnet=subnet or f'172.29.{secrets.randbelow(220) + 10}.0/28',
               db_password=secrets.token_urlsafe(30), root_password=secrets.token_urlsafe(30),
               redis_password=secrets.token_urlsafe(30), maintenance_gate=False,
               primary=host(address or bind),
               domains=[dict(host=host(address or bind), kind='local', tls='off', mode='local', email='')],
               status='awaiting-web', retention=3, data_device=None,
               db_redo_log_size=256 * 1024 ** 2, initial_maintenance_pending=True)
    validate(cfg)
    return cfg


def prefer_first_tunnel(cfg, mode):
    """Promote the first tunnel while retaining an existing public preference."""
    current = next(entry for entry in cfg['domains'] if entry['host'] == cfg['primary'])
    return (mode == 'tunnel' and current['kind'] == 'local'
            and not any(entry['mode'] == 'tunnel' for entry in cfg['domains']))


def access_url(cfg, entry=None):
    entry = entry or next(e for e in cfg['domains'] if e['host'] == cfg['primary'])
    secure = entry['tls'] != 'off'
    p = cfg['https_port'] if secure else cfg['http_port']
    if entry['kind'] == 'public':
        p = entry.get('external_port', 443)
    return f"{'https' if secure else 'http'}://{entry['host']}" + (f':{p}' if p != (443 if secure else 80) else '')


def managed_php(cfg, *, rehearsal=False):
    proxy_ip = str(ipaddress.ip_network(cfg['subnet']).network_address + 2)
    data = {'trusted_domains': [e['host'] for e in cfg['domains']], 'trusted_proxies': [proxy_ip],
            'overwrite.cli.url': access_url(cfg), 'overwritehost': '', 'overwriteprotocol': '',
            'overwritecondaddr': '', 'memcache.local': '\\OC\\Memcache\\APCu',
            'memcache.locking': '\\OC\\Memcache\\Redis',
            'redis': {'host': 'redis', 'port': 6379, 'password': cfg['redis_password']},
            'log_type': 'file', 'logfile': '/var/www/html/data/nextcloud.log',
            'log_rotate_size': 20 * 1024 * 1024, 'maintenance_window_start': 6,
            'default_phone_region': 'EC'}
    if rehearsal:
        data.update(appstoreenabled=False, mail_smtpmode='null')
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return "<?php\n// Managed by Nextcloud Docker Manager. Edit access through the manager.\n$CONFIG = json_decode(base64_decode('" + encoded + "'), true);\n"


def _proxy_route(scheme, gate, *, hsts=False):
    headers = ['header Strict-Transport-Security "max-age=15552000"'] if hsts else []
    if gate:
        return headers + ['respond "Nextcloud maintenance" 503']
    return headers + ['redir /.well-known/carddav /remote.php/dav/ 301',
            'redir /.well-known/caldav /remote.php/dav/ 301',
            'redir /.well-known/webfinger /index.php{uri} 301',
            'redir /.well-known/nodeinfo /index.php{uri} 301',
            'reverse_proxy app:80 {', f'    header_up X-Forwarded-Proto {scheme}', '}']


def caddyfile(cfg):
    lines = ['{', '    admin off', '    auto_https disable_redirects', '}', '', 'http://:80 {',
             '    handle /ndm-health {', '        respond "ok" 200', '    }',
             '    handle /.well-known/acme-challenge/* {', '        respond "Unknown ACME challenge" 404', '    }']
    # Caddy's built-in HTTP-01 handler precedes user routes while a challenge is active.
    for i, entry in enumerate(cfg['domains']):
        lines += [f"    @entry{i} host {entry['host']}", f'    handle @entry{i} {{']
        if entry['tls'] != 'off' and entry['mode'] != 'tunnel':
            external = entry.get('external_port', 443) if entry['kind'] == 'public' else cfg['https_port']
            suffix = '' if external == 443 else ':' + str(external)
            lines += [f'        redir https://{entry["host"]}{suffix}{{uri}} 308']
        else:
            scheme = 'https' if entry['kind'] == 'public' else 'http'
            lines += ['        ' + line for line in _proxy_route(scheme, cfg['maintenance_gate'],
                                                                hsts=entry['kind'] == 'public')]
        lines += ['    }']
    lines += ['    respond "Unknown host" 404', '}']
    for entry in cfg['domains']:
        if entry['tls'] == 'off':
            continue
        lines += ['', f"https://{entry['host']} {{"]
        if entry['tls'] == 'internal':
            lines += ['    tls internal']
        else:
            lines += ['    tls {', '        issuer acme {', '            dir https://acme-v02.api.letsencrypt.org/directory',
                      '            disable_tlsalpn_challenge']
            if entry.get('email'):
                lines += ['            email ' + entry['email']]
            lines += ['        }', '    }']
        lines += ['    handle /.well-known/acme-challenge/* {',
                  '        reverse_proxy 127.0.0.1:80', '    }', '    handle {']
        lines += ['        ' + line for line in _proxy_route('https', cfg['maintenance_gate'],
                                                            hsts=entry['kind'] == 'public')]
        lines += ['    }', '}']
    return '\n'.join(lines) + '\n'


def compose(cfg):
    labels = {LABEL + '.installation': cfg['id']}
    network = ipaddress.ip_network(cfg['subnet'])
    backend = ipaddress.ip_network((int(network.network_address) ^ 16, 28))
    logging = {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}
    env = {'TZ': cfg['timezone'], 'NEXTCLOUD_DATA_DIR': '/var/www/html/data', 'MYSQL_HOST': 'db', 'MYSQL_DATABASE': 'nextcloud',
           'MYSQL_USER': 'nextcloud', 'MYSQL_PASSWORD': cfg['db_password'],
           'REDIS_HOST': 'redis', 'REDIS_HOST_PASSWORD': cfg['redis_password'],
           'APACHE_DISABLE_REWRITE_IP': '1', 'PHP_MEMORY_LIMIT': '512M',
           'PHP_UPLOAD_LIMIT': '16G', 'APACHE_BODY_LIMIT': '0', 'NEXTCLOUD_UPDATE': '0'}
    volumes = ['app:/var/www/html', 'data:/var/www/html/data']
    services = {
        'db': {'image': cfg['images']['db'], 'environment': {
            'MARIADB_ROOT_PASSWORD': cfg['root_password'], 'MARIADB_ROOT_HOST': 'localhost', 'MARIADB_AUTO_UPGRADE': '1',
            'MARIADB_DATABASE': 'nextcloud', 'MARIADB_USER': 'nextcloud', 'MARIADB_PASSWORD': cfg['db_password']},
            'command': ['--transaction-isolation=READ-COMMITTED', '--binlog-format=ROW', '--skip-log-bin']
                       + (['--innodb-log-file-size=' + str(cfg['db_redo_log_size'])]
                          if 'db_redo_log_size' in cfg else []),
            'volumes': ['db:/var/lib/mysql'], 'networks': ['backend'],
            'healthcheck': {'test': ['CMD', 'healthcheck.sh', '--connect', '--innodb_initialized'],
                            'interval': '5s', 'timeout': '5s', 'retries': 60, 'start_period': '30s'}},
        'redis': {'image': cfg['images']['redis'], 'environment': {'NDM_REDIS_PASSWORD': cfg['redis_password']},
            'tmpfs': ['/data:rw,noexec,nosuid,size=64m'],
            'entrypoint': ['/bin/sh', '-ec', 'exec redis-server --save "" --appendonly no --requirepass "$NDM_REDIS_PASSWORD"'],
            'networks': ['backend'], 'healthcheck': {
                'test': ['CMD-SHELL', 'REDISCLI_AUTH="$$NDM_REDIS_PASSWORD" redis-cli ping | grep -q PONG'],
                'interval': '5s', 'timeout': '5s', 'retries': 20}},
        'app': {'image': cfg['images']['app'], 'environment': env, 'volumes': volumes,
            'networks': {'backend': {}, 'proxy': {'ipv4_address': str(network.network_address + 3)}},
            'depends_on': {'db': {'condition': 'service_healthy'}, 'redis': {'condition': 'service_healthy'}},
            'healthcheck': {'test': ['CMD', 'php', '-r', 'exit(@file_get_contents("http://127.0.0.1/status.php") === false ? 1 : 0);'],
                            'interval': '10s', 'timeout': '5s', 'retries': 30, 'start_period': '60s'}},
        'cron': {'image': cfg['images']['app'], 'environment': env, 'volumes': volumes,
            'stop_signal': 'SIGTERM',
            'entrypoint': ['/bin/sh', '-ec', 'child=""; trap \'[ -z "$child" ] || kill -TERM "$child" 2>/dev/null || true; exit 0\' TERM INT; while :; do if su -p www-data -s /bin/sh -c "php /var/www/html/occ status -e" >/dev/null 2>&1; then su -p www-data -s /bin/sh -c "php -f /var/www/html/cron.php" & child=$!; wait "$child"; fi; sleep 300 & child=$!; wait "$child"; done'],
            'networks': ['backend', 'proxy'], 'depends_on': ['app']},
        'proxy': {'image': cfg['images']['proxy'], 'environment': {'NDM_CADDY_CONFIG': caddyfile(cfg)},
            'entrypoint': ['/bin/sh', '-ec', 'printf "%s\\n" "$NDM_CADDY_CONFIG" > /tmp/Caddyfile; exec caddy run --config /tmp/Caddyfile --adapter caddyfile'],
            'volumes': ['proxy_data:/data', 'proxy_config:/config'],
            'networks': {'proxy': {'ipv4_address': str(network.network_address + 2)}},
            'ports': [{'target': 80, 'published': str(cfg['http_port']), 'host_ip': cfg['bind']},
                      {'target': 443, 'published': str(cfg['https_port']), 'host_ip': cfg['bind']}],
            'healthcheck': {'test': ['CMD', 'wget', '-q', '-O', '/dev/null', 'http://127.0.0.1/ndm-health'],
                            'interval': '10s', 'timeout': '5s', 'retries': 12}}}
    for name, service in services.items():
        service.update(restart='unless-stopped', logging=logging,
                       labels={**labels, LABEL + '.role': name})
        # Compose interpolation must not interpret shell variables or generated secrets.
        if isinstance(service.get('entrypoint'), list):
            service['entrypoint'] = [x.replace('$', '$$') for x in service['entrypoint']]
    return {'name': cfg['project'], 'services': services,
            'volumes': {k: {'name': v, 'external': True} for k, v in cfg['volumes'].items()},
            'networks': {'backend': {'internal': True, 'ipam': {'config': [{'subnet': str(backend)}]}, 'labels': labels},
                         'proxy': {'ipam': {'config': [{'subnet': cfg['subnet']}]}, 'labels': labels}}}
