#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if ! command -v python3 >/dev/null; then
    if [[ $(uname -s) == Linux && $EUID == 0 ]] && command -v apt-get >/dev/null; then
        # The first screen must work before Python/curses has been installed.
        ndm_animated=false ndm_tone=false ndm_terminal_state='' ndm_spinner=''
        if [[ -t 0 && -t 2 && ${TERM:-dumb} != dumb && ${NDM_UI:-auto} != plain ]] && command -v stty >/dev/null; then
            ndm_animated=true
            [[ -n ${NO_COLOR+x} ]] || ndm_tone=true
        fi
        ndm_color() { if [[ $ndm_tone == true ]]; then printf '\033[%sm' "$1" >&2; fi; }
        ndm_restore() {
            if [[ -n $ndm_spinner ]]; then kill "$ndm_spinner" 2>/dev/null || :; wait "$ndm_spinner" 2>/dev/null || :; ndm_spinner=''; fi
            if [[ -n $ndm_terminal_state ]]; then stty "$ndm_terminal_state"; ndm_terminal_state=''; fi
            [[ $ndm_animated != true ]] || printf '\033[?25h' >&2
        }
        trap ndm_restore EXIT
        trap 'ndm_restore; exit 130' INT TERM
        ndm_header() {
            ndm_color '1;35'; printf '  Nextcloud Docker Manager\n' >&2; ndm_color 0
            printf '  ────────────────────────────────────────────────────────\n\n' >&2
            printf '  [?] Python 3 es necesario / Python 3 is required\n\n' >&2
        }
        ndm_choice=0
        if [[ $ndm_animated == true ]]; then
            ndm_terminal_state=$(stty -g)
            stty -echo -icanon min 1 time 0
            printf '\033[?25l' >&2
            while :; do
                printf '\033[2J\033[H' >&2
                ndm_header
                for ndm_i in 0 1; do
                    if (( ndm_i == ndm_choice )); then ndm_color '1;37;44'; printf '  > ' >&2
                    else printf '    ' >&2; fi
                    if (( ndm_i == 0 )); then printf 'Cancelar / Cancel' >&2
                    else printf 'Instalar Python 3 / Install Python 3' >&2; fi
                    ndm_color 0; printf '\n' >&2
                done
                printf '\n  ↑/↓ Elegir / Select · Enter Confirmar / Confirm · Esc Volver / Back\n' >&2
                IFS= read -rsn 1 ndm_key || exit 1
                case "$ndm_key" in
                    '') break;;
                    $'\t') ndm_choice=$((1-ndm_choice));;
                    $'\e')
                        IFS= read -rsn 1 -t 0.15 ndm_next || exit 1
                        [[ $ndm_next == '[' || $ndm_next == O ]] || continue
                        IFS= read -rsn 1 -t 0.15 ndm_next || continue
                        [[ $ndm_next != A && $ndm_next != B ]] || ndm_choice=$((1-ndm_choice));;
                    $'\003'|$'\004') exit 130;;
                esac
            done
            ndm_restore
        else
            ndm_header
            printf '  ¿Instalar? / Install? [y/N] ' >&2
            read -r ndm_answer || exit 1
            case "$ndm_answer" in y|Y|s|S) ndm_choice=1;; esac
        fi
        (( ndm_choice == 1 )) || exit 1
        ndm_log_dir=$(mktemp -d "${TMPDIR:-/tmp}/ndm-bootstrap.XXXXXXXX")
        ndm_log="$ndm_log_dir/python.log"
        if [[ $ndm_animated == true ]]; then
            printf '\033[2J\033[H' >&2
            ndm_header
            (
                trap - EXIT INT TERM
                ndm_frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
                ndm_i=0 ndm_started=$SECONDS
                while :; do
                    ndm_elapsed=$((SECONDS-ndm_started))
                    printf '\r\033[2K  ' >&2; ndm_color '1;34'
                    printf '[%s]' "${ndm_frames[ndm_i]}" >&2; ndm_color 0
                    printf ' Instalar Python 3 / Install Python 3 · %02d:%02d' "$((ndm_elapsed/60))" "$((ndm_elapsed%60))" >&2
                    ndm_i=$(((ndm_i+1)%10))
                    sleep 0.1
                done
            ) &
            ndm_spinner=$!
        else
            printf '  [*] Instalar Python 3 / Install Python 3\n' >&2
        fi
        if { apt-get update && apt-get install -y --no-remove python3 ca-certificates; } > "$ndm_log" 2>&1; then
            ndm_restore
            printf '\n  [+] Python 3 listo / Python 3 ready\n' >&2
        else
            ndm_restore
            printf '\n  [✗] Error al instalar / Installation failed\n  Log: %s\n' "$ndm_log" >&2
            exit 1
        fi
        trap - EXIT INT TERM
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
