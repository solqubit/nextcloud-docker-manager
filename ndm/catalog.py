"""Official image discovery, manifest pinning and version-specific requirements."""
from __future__ import annotations

import html
import json
from pathlib import Path
import platform
import re
import time
import datetime as dt
import urllib.error
import urllib.request

from .core import Error, atomic, read_json, version_tuple


def fetch(url, headers=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'Nextcloud-Docker-Manager/0.1', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise Error(f'No se pudo consultar / Cannot retrieve: {url.split("?")[0]}') from exc


class Catalog:
    def maintained_databases(self):
        """Read Community LTS dates, independently of Nextcloud's compatibility matrix."""
        path = self.cache / 'mariadb-lts.json'
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            value = read_json(path)
        else:
            url = 'https://mariadb.org/about/'
            document = fetch(url).decode()
            months = {name: index + 1 for index, name in enumerate(
                ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}
            branches = {}
            for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', document, re.S | re.I):
                cells = [html.unescape(re.sub('<[^>]+>', ' ', cell)).strip()
                         for cell in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.S | re.I)]
                if len(cells) < 4 or not re.fullmatch(r'[0-9]+\.[0-9]+', cells[0]):
                    continue
                date = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', cells[2])
                if date and date[2][:3].lower() in months:
                    branches[cells[0]] = dt.date(int(date[3]), months[date[2][:3].lower()], int(date[1])).isoformat()
            if not branches:
                raise Error('Cannot verify official MariaDB Community LTS maintenance dates')
            value = {'source': url, 'branches': branches}
            atomic(path, value)
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        return {'source': value['source'], 'branches': {v: end for v, end in value['branches'].items() if end >= today}}

    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, key, url, ttl=900):
        path = self.cache / (key + '.json')
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return read_json(path)
        value = json.loads(fetch(url))
        atomic(path, value)
        return value

    def branches(self):
        value = self.get('branches', 'https://raw.githubusercontent.com/nextcloud/docker/master/versions.json')
        return sorted([v['version'] for v in value.values()
                       if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', v.get('version', ''))
                       and 'apache' in v.get('variants', {})], key=version_tuple, reverse=True)

    def patches(self, major):
        url = f'https://hub.docker.com/v2/repositories/library/nextcloud/tags?page_size=100&name={int(major)}.'
        values = []
        # Bounded pagination; never interpret an HTTP failure as "no update".
        for page in range(12):
            data = self.get(f'app-{major}-{page}', url)
            values += [v['name'][:-7] for v in data['results']
                       if re.fullmatch(rf'{int(major)}\.[0-9]+\.[0-9]+-apache', v['name'])]
            url = data.get('next')
            if not url:
                break
        return sorted(set(values), key=version_tuple, reverse=True)

    def latest(self, repo, series):
        if repo not in ('mariadb', 'redis', 'caddy') or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', series):
            raise Error('Invalid image series')
        data = self.get(f'{repo}-{series}', f'https://hub.docker.com/v2/repositories/library/{repo}/tags?page_size=100&name={series}.')
        versions = [r['name'] for r in data['results'] if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', r['name'])
                    and r['name'].startswith(series + '.')]
        if not versions:
            raise Error(f'No stable images: {repo}:{series}')
        return max(versions, key=version_tuple)

    def resolve(self, repo, tag, arch=None):
        if repo not in ('nextcloud', 'mariadb', 'redis', 'caddy') or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-apache)?', tag):
            raise Error('Only exact stable official images are allowed')
        arch = arch or {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
        if arch not in ('amd64', 'arm64'):
            raise Error('Supported architectures: amd64, arm64')
        # The official tag endpoint supplies a manifest digest plus platform inventory.
        data = json.loads(fetch(f'https://hub.docker.com/v2/repositories/library/{repo}/tags/{tag}'))
        if not any(i.get('architecture') == arch and i.get('os') == 'linux' for i in data.get('images', [])):
            raise Error(f'Image unavailable for Linux/{arch}: {repo}:{tag}')
        digest = data.get('digest', '')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            raise Error('Registry did not supply a valid digest')
        return f'{repo}:{tag}@{digest}'

    def requirements(self, major):
        path = self.cache / f'requirements-{int(major)}.json'
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            return read_json(path)
        url = f'https://docs.nextcloud.com/server/{int(major)}/admin_manual/installation/system_requirements.html'
        text = html.unescape(re.sub('<[^>]+>', ' ', fetch(url).decode()))
        text = re.sub(r'\s+', ' ', text)
        match = re.search(r'MariaDB ((?:[0-9]+\.[0-9]+\s*(?:/\s*)?)+)', text)
        php = re.search(r'PHP Runtime (.+?)See ', text)
        if not match or not php:
            raise Error(f'Cannot verify requirements: {url}')
        value = {'source': url, 'mariadb': re.findall(r'[0-9]+\.[0-9]+', match[1]),
                 'php': re.findall(r'[0-9]+\.[0-9]+', php[1])}
        atomic(path, value)
        return value

    def compatible(self, version, db_ref):
        required = self.requirements(version_tuple(version)[0])
        db = db_ref.split(':', 1)[1].split('@')[0]
        if '.'.join(db.split('.')[:2]) not in required['mariadb']:
            raise Error(f"MariaDB {db} is not supported by Nextcloud {version}. {required['source']}")
        return required
