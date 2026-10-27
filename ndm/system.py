"""Assisted Ubuntu/Debian bootstrap and a non-destructive host inventory."""
import os
from pathlib import Path
import platform
import re
import shutil
import socket

from .catalog import fetch
from .core import Error, atomic, confirm


def host_info():
    values = {}
    if Path('/etc/os-release').exists():
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                values[key] = value.strip('"\'')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(('1.1.1.1', 80))
            address = probe.getsockname()[0]
    except OSError:
        address = '127.0.0.1'
    return {'os': values, 'architecture': platform.machine(), 'address': address,
            'panel_detected': Path('/usr/local/hestia').exists() or Path('/usr/local/psa').exists()}


def bootstrap(runner):
    if platform.system() != 'Linux':
        raise Error('Runtime requires Linux; Windows is supported as a Docker test host')
    if os.geteuid() != 0:
        raise Error('Ejecuta con sudo / Run with sudo: sudo ./start.sh')
    if not shutil.which('docker'):
        info = host_info()['os']
        distro, codename = info.get('ID'), info.get('VERSION_CODENAME', '')
        if distro not in ('ubuntu', 'debian') or not re.fullmatch('[a-z0-9]+', codename):
            raise Error('Instala Docker Engine y Compose desde la documentación oficial / Install Docker first')
        confirm('INSTALL-DOCKER', message='Instalar Docker desde su repositorio oficial / Install Docker from its official repository')
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
        if not arch:
            raise Error('Supported architectures: amd64 / arm64')
        # Do not remove Podman/containerd or replace a pre-existing engine.
        conflicts = runner.run(['dpkg-query', '-W', '-f=${db:Status-Status}', 'podman-docker'], check=False)
        if conflicts.returncode == 0 and 'installed' in conflicts.stdout:
            raise Error('Existing container runtime requires manual review; no packages removed')
        fetch(f'https://download.docker.com/linux/{distro}/dists/{codename}/Release')
        runner.run(['apt-get', 'update'], timeout=900)
        runner.run(['apt-get', 'install', '-y', '--no-remove', 'ca-certificates', 'curl'], timeout=900)
        Path('/etc/apt/keyrings').mkdir(exist_ok=True, mode=0o755)
        atomic('/etc/apt/keyrings/ndm-docker.asc', fetch(f'https://download.docker.com/linux/{distro}/gpg').decode(), 0o644)
        atomic('/etc/apt/sources.list.d/ndm-docker.sources',
               f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {codename}\nComponents: stable\nArchitectures: {arch}\nSigned-By: /etc/apt/keyrings/ndm-docker.asc\n', 0o644)
        runner.run(['apt-get', 'update'], timeout=900)
        runner.run(['apt-get', 'install', '-y', '--no-remove', 'docker-ce', 'docker-ce-cli',
                    'containerd.io', 'docker-compose-plugin'], timeout=1200)
    result = runner.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=False)
    if result.returncode:
        confirm('START-DOCKER', message='Iniciar el servicio Docker existente / Start the existing Docker service')
        if Path('/run/systemd/system').exists():
            runner.run(['systemctl', 'start', 'docker'])
        elif shutil.which('service'):
            runner.run(['service', 'docker', 'start'])
        else:
            raise Error('Start Docker manually and retry')
    if runner.run(['docker', 'compose', 'version'], check=False).returncode:
        raise Error('Instala el plugin Compose V2 desde el repositorio del motor existente y vuelve a ejecutar / Compose plugin missing')
