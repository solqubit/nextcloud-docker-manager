"""Requirement-based Docker bootstrap and a non-destructive host inventory."""
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import time

from .catalog import fetch
from .core import Error, atomic, confirm

APT = {'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'a'}
PACKAGES = ['docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-compose-plugin']


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


def family(info, *names):
    """First of names matching ID or ID_LIKE, so derivatives use their upstream repository."""
    ids = [info.get('ID', '')] + info.get('ID_LIKE', '').split()
    return next((name for value in ids for name in names if value == name), None)


def docker_repository(folder):
    """True when the host already lists Docker's repository (apt sources or yum repos)."""
    return any('download.docker.com' in item.read_text(errors='replace')
               for item in Path(folder).glob('*') if item.is_file() and not item.name.startswith('ndm-docker'))


def install_apt(runner, info):
    # Docker publishes Debian/Ubuntu/Raspbian suites; derivatives (Mint, Pop!_OS, Zorin…)
    # declare their upstream release in ID_LIKE and UBUNTU_CODENAME/DEBIAN_CODENAME.
    distro = info.get('ID') if info.get('ID') in ('ubuntu', 'debian', 'raspbian') else family(info, 'ubuntu', 'debian')
    codename = ((info.get('UBUNTU_CODENAME') if distro == 'ubuntu' else info.get('DEBIAN_CODENAME'))
                or info.get('VERSION_CODENAME', ''))
    if not distro or not re.fullmatch('[a-z0-9]+', codename):
        raise Error('No se reconoce la versión base Debian/Ubuntu; instala Docker Engine y Compose manualmente / '
                    'Unknown Debian/Ubuntu base release; install Docker Engine and Compose manually')
    # The userland decides the packages: a 64-bit kernel may run a 32-bit armhf system.
    arch = runner.run(['dpkg', '--print-architecture']).stdout.strip()
    if arch not in ('amd64', 'arm64'):
        raise Error(f'Se necesita un sistema de 64 bits (amd64/arm64): {arch} / 64-bit userland required (amd64/arm64): {arch}')
    conflicts = runner.run(['dpkg-query', '-W', '-f=${db:Status-Status}', 'podman-docker'], check=False)
    if conflicts.returncode == 0 and conflicts.stdout.strip() == 'installed':
        raise Error('Hay otro motor de contenedores (podman-docker); revísalo manualmente / '
                    'Existing container runtime requires manual review; no packages removed')
    runner.run(['apt-get', 'update'], timeout=900, env=APT)
    runner.run(['apt-get', 'install', '-y', '--no-remove', 'ca-certificates'], timeout=900, env=APT)
    listed = Path('/etc/apt/sources.list')
    # Reuse a Docker repository configured earlier: a second entry with another Signed-By breaks apt.
    if not docker_repository('/etc/apt/sources.list.d') and not (
            listed.exists() and 'download.docker.com' in listed.read_text(errors='replace')):
        fetch(f'https://download.docker.com/linux/{distro}/dists/{codename}/Release')
        Path('/etc/apt/keyrings').mkdir(exist_ok=True, mode=0o755)
        atomic('/etc/apt/keyrings/ndm-docker.asc', fetch(f'https://download.docker.com/linux/{distro}/gpg').decode(), 0o644)
        atomic('/etc/apt/sources.list.d/ndm-docker.sources',
               f'Types: deb\nURIs: https://download.docker.com/linux/{distro}\nSuites: {codename}\nComponents: stable\n'
               f'Architectures: {arch}\nSigned-By: /etc/apt/keyrings/ndm-docker.asc\n', 0o644)
        runner.run(['apt-get', 'update'], timeout=900, env=APT)
    runner.run(['apt-get', 'install', '-y', '--no-remove', *PACKAGES], timeout=1200, env=APT)


def install_dnf(runner, info):
    # Fedora, RHEL and its rebuilds (Rocky, AlmaLinux, CentOS Stream, Oracle Linux).
    if info.get('ID') == 'rhel':
        distro = 'rhel'
    elif family(info, 'rhel', 'centos'):
        distro = 'centos'
    elif family(info, 'fedora'):
        distro = 'fedora'
    else:
        raise Error('Distribución RPM no reconocida; instala Docker Engine y Compose manualmente / '
                    'Unknown RPM distribution; install Docker Engine and Compose manually')
    # containerd.io obsoletes the distribution's runc, so dnf would replace it without
    # an error or --allowerasing. An existing runtime is never swapped silently.
    if runner.run(['rpm', '-q', 'runc'], check=False).returncode == 0:
        raise Error('runc ya está instalado (Podman/Buildah); Docker lo reemplazaría. Revísalo manualmente o instala '
                    'Docker Engine antes de continuar / runc is installed (Podman/Buildah) and Docker would replace it; '
                    'review it manually or install Docker Engine first')
    if not docker_repository('/etc/yum.repos.d'):
        atomic('/etc/yum.repos.d/ndm-docker.repo',
               fetch(f'https://download.docker.com/linux/{distro}/docker-ce.repo').decode(), 0o644)
    if runner.run(['dnf', '-y', 'install', *PACKAGES], timeout=1200, check=False).returncode:
        raise Error('dnf no pudo instalar Docker; si hay Podman o runc instalados, revísalos manualmente / '
                    'dnf could not install Docker; review Podman/runc conflicts manually')


def install_distribution_packages(runner, manager):
    """Distributions whose own Docker packages already include Compose V2."""
    commands = {'zypper': [['zypper', '--non-interactive', 'install', 'docker', 'docker-compose']],
                'apk': [['apk', 'add', 'docker', 'docker-cli-compose']]}
    for command in commands[manager]:
        runner.run(command, timeout=1200)
    # Alpine boots Docker through OpenRC; minimal images and containers may lack it.
    if manager == 'apk' and shutil.which('rc-update'):
        runner.run(['rc-update', 'add', 'docker', 'default'])


def bootstrap(runner):
    """Requirements rather than distribution names: Linux, root and a local Docker Engine
    with Compose V2. A missing engine is installed with the host's own package manager."""
    if platform.system() != 'Linux':
        raise Error('Runtime requires Linux; Windows is supported as a Docker test host')
    if os.geteuid() != 0:
        raise Error('Ejecuta con sudo / Run with sudo: sudo ./start.sh')
    if not shutil.which('docker'):
        info = host_info()['os']
        manager = next((name for name in ('apt-get', 'dnf', 'zypper', 'apk') if shutil.which(name)), None)
        if not manager:
            raise Error('Instala Docker Engine y Compose desde la documentación oficial / Install Docker first')
        if platform.machine() not in ('x86_64', 'aarch64'):
            raise Error('Arquitecturas compatibles: amd64 / arm64 / Supported architectures: amd64 / arm64')
        source = 'download.docker.com' if manager in ('apt-get', 'dnf') else info.get('NAME', manager)
        confirm('INSTALL-DOCKER', message=f'Instalar Docker y Compose desde {source} / Install Docker and Compose from {source}')
        if manager == 'apt-get':
            install_apt(runner, info)
        elif manager == 'dnf':
            install_dnf(runner, info)
        else:
            install_distribution_packages(runner, manager)
    if runner.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=False).returncode:
        confirm('START-DOCKER', message='Iniciar el servicio Docker / Start the Docker service')
        if Path('/run/systemd/system').exists():
            # RPM and distribution packages do not enable the daemon on installation.
            runner.run(['systemctl', 'enable', '--now', 'docker'])
        elif shutil.which('rc-service'):
            runner.run(['rc-service', 'docker', 'start'])
        elif shutil.which('service'):
            runner.run(['service', 'docker', 'start'])
        else:
            raise Error('Inicia Docker manualmente y vuelve a intentarlo / Start Docker manually and retry')
        # The socket can appear a few seconds after the service reports started.
        for _ in range(30):
            if not runner.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=False).returncode:
                break
            time.sleep(1)
    if runner.run(['docker', 'compose', 'version'], check=False).returncode:
        raise Error('Instala el plugin Compose V2 desde el repositorio del motor existente y vuelve a ejecutar / Compose plugin missing')
