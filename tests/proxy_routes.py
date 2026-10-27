"""Offline real-Caddy route check, including WebFinger query preservation."""
import json
import secrets
import subprocess
import sys

sys.path.insert(0, '/workspace/tests')
from test_foundation import configuration
from ndm.config import caddyfile


def main():
    cfg = configuration()
    image = 'caddy:2.11.7@sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489'
    identity = secrets.token_hex(8)
    name = 'ndm-test-routes-' + identity
    script = ('printf "%s\\n" "$NDM_CADDY_CONFIG" >/tmp/Caddyfile; '
              'exec caddy run --config /tmp/Caddyfile --adapter caddyfile')
    subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'none',
        '--label', 'com.solqubit.ndm.test=' + identity, '--env', 'NDM_CADDY_CONFIG=' + caddyfile(cfg),
        '--entrypoint', 'sh', image, '-ec', script], check=True, capture_output=True)
    try:
        for path in ('/.well-known/carddav', '/.well-known/caldav',
                     '/.well-known/webfinger?resource=acct:test@example.com'):
            result = subprocess.run(['docker', 'exec', name, 'wget', '-S', '-O', '/dev/null',
                'http://127.0.0.1' + path], capture_output=True, text=True, timeout=15)
            expected = '/remote.php/dav/' if path.endswith(('carddav', 'caldav')) else '/index.php' + path
            assert 'Location: ' + expected in result.stderr, result.stderr
        print('[PASS] Real offline Caddy DAV discovery and WebFinger query preservation')
    finally:
        info = json.loads(subprocess.check_output(['docker', 'container', 'inspect', name]))[0]
        assert info['Config']['Labels']['com.solqubit.ndm.test'] == identity
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=True, capture_output=True)


if __name__ == '__main__':
    main()
