"""Production background jobs need outbound TLS; rehearsal networks must block it."""
import tempfile

from ndm.config import compose
from ndm.core import State, atomic
from ndm.docker import Engine
from ndm.operations import Manager


def main():
    from test_foundation import configuration
    with tempfile.TemporaryDirectory() as folder:
        state = State(folder).open()
        engine = None
        try:
            cfg = configuration()
            cfg['images']['app'] = 'nextcloud:33.0.9-apache@sha256:8f46dc79e9abf4f1e330ca9a7d9d9b8a8811ecc28f18018c1951281e522499e8'
            cfg['subnet'] = Manager(state).new_subnet()
            revision = state.revision(cfg)
            state.activate(revision)
            engine = Engine(state)
            engine.ensure_volumes()
            probe = '$s=@stream_socket_client("tls://docs.nextcloud.com:443",$e,$m,8);exit($s===false?1:0);'
            result = engine.dc('run', '--rm', '--no-deps', '--entrypoint', 'php', 'cron', '-r', probe, check=False)
            assert result.returncode == 0, result.stderr
            engine.remove_owned()
            definition = compose(cfg)
            definition['networks']['proxy']['internal'] = True
            atomic(revision / 'compose.json', definition)
            result = engine.dc('run', '--rm', '--no-deps', '--entrypoint', 'php', 'cron', '-r', probe, check=False)
            assert result.returncode != 0, 'Rehearsal unexpectedly reached the external TLS server'
            print('[PASS] Production cron reaches verified external TLS; isolated rehearsal networks block outbound access')
        finally:
            if engine:
                engine.remove_owned(delete_volumes=True)
            state.lock_file.close()


if __name__ == '__main__':
    main()
