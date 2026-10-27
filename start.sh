#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if ! command -v python3 >/dev/null; then
    if [[ $(uname -s) == Linux && $EUID == 0 ]] && command -v apt-get >/dev/null; then
        printf '%s\n' 'Se necesita Python 3. ¿Instalarlo? / Python 3 required. Install? [y/N]'
        read -r answer
        case "$answer" in y|Y|s|S) apt-get update; apt-get install -y --no-remove python3 ca-certificates;; *) exit 1;; esac
    else
        printf '%s\n' 'Install Python 3 first / Instala Python 3 primero.' >&2
        exit 1
    fi
fi
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
if ! python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    printf '%s\n' 'Python 3.11+ required / Se necesita Python 3.11+.' >&2
    exit 1
fi
exec python3 -P -m ndm "$@"
