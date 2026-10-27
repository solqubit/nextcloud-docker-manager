#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ndm_python_found=false
if command -v python3 >/dev/null; then ndm_python_found=true; fi
if [[ $ndm_python_found == false ]] || ! python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
    # Language selection must also work before Python/curses is available.
    ndm_language=${NDM_STARTUP_LANGUAGE:-}
    ndm_state_dir=${NDM_STATE_DIR:-/opt/nextcloud-docker-manager}
    ndm_menu=true
    ndm_options() {
        while (( $# )); do
            case "$1" in
                --lang) (( $# >= 2 )) || return; ndm_language=$2; shift 2;;
                --lang=*) ndm_language=${1#*=}; shift;;
                --state-dir) (( $# >= 2 )) || return; ndm_state_dir=$2; shift 2;;
                --state-dir=*) ndm_state_dir=${1#*=}; shift;;
                --plain) export NDM_UI=plain; shift;;
                menu) return;;
                *) ndm_menu=false; return;;
            esac
        done
    }
    ndm_options "$@"
    if [[ -z $ndm_language && -f $ndm_state_dir/language ]]; then
        IFS= read -r ndm_language < "$ndm_state_dir/language" || :
        case "$ndm_language" in es|en) ;; *) ndm_language='';; esac
    fi
    case "$ndm_language" in ''|es|en) ;; *) printf 'Idioma no válido / Invalid language: es, en\n' >&2; exit 1;; esac
    ndm_animated=false ndm_tone=false ndm_terminal_state='' ndm_spinner=''
    if [[ -t 0 && -t 2 && ${TERM:-dumb} != dumb && ${NDM_UI:-auto} != plain ]] && command -v stty >/dev/null; then
        ndm_animated=true
        [[ -n ${NO_COLOR+x} ]] || ndm_tone=true
    fi
    ndm_t() {
        case "$1" in
            python) ndm_es='Python 3 es necesario'; ndm_en='Python 3 is required';;
            cancel) ndm_es='Cancelar'; ndm_en='Cancel';;
            install) ndm_es='Instalar Python 3'; ndm_en='Install Python 3';;
            ready) ndm_es='Python 3 listo'; ndm_en='Python 3 ready';;
            failed) ndm_es='Error al instalar'; ndm_en='Installation failed';;
            manual) ndm_es='Instala Python 3 primero.'; ndm_en='Install Python 3 first.';;
            minimum) ndm_es='Se necesita Python 3.11 o posterior.'; ndm_en='Python 3.11 or later is required.';;
            keys) ndm_es='↑/↓ Elegir · Enter Confirmar · Esc Volver'; ndm_en='↑/↓ Select · Enter Confirm · Esc Back';;
            plain) ndm_es='Opción (Enter = seleccionada, b = volver): '; ndm_en='Choice (Enter = selected, b = back): ';;
        esac
        if [[ $ndm_language == en ]]; then printf '%s' "$ndm_en"
        elif [[ -z $ndm_language ]]; then printf '%s / %s' "$ndm_es" "$ndm_en"
        else printf '%s' "$ndm_es"; fi
    }
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
        printf '  [?] %s\n\n' "$1" >&2
    }
    ndm_choose() {
        ndm_choice=0
        if [[ $ndm_animated == true ]]; then
            ndm_terminal_state=$(stty -g)
            stty -echo -icanon min 1 time 0
            printf '\033[?25l' >&2
            while :; do
                printf '\033[2J\033[H' >&2
                ndm_header "$1"
                for ndm_i in 0 1; do
                    if (( ndm_i == ndm_choice )); then ndm_color '1;37;44'; printf '  > ' >&2
                    else printf '    ' >&2; fi
                    if (( ndm_i == 0 )); then printf '%s' "$2" >&2; else printf '%s' "$3" >&2; fi
                    ndm_color 0; printf '\n' >&2
                done
                printf '\n  %s\n' "$(ndm_t keys)" >&2
                IFS= read -rsn 1 ndm_key || return 1
                case "$ndm_key" in
                    '') break;;
                    $'\t') ndm_choice=$((1-ndm_choice));;
                    $'\e')
                        IFS= read -rsn 1 -t 0.15 ndm_next || return 1
                        [[ $ndm_next == '[' || $ndm_next == O ]] || continue
                        IFS= read -rsn 1 -t 0.15 ndm_next || continue
                        [[ $ndm_next != A && $ndm_next != B ]] || ndm_choice=$((1-ndm_choice));;
                    $'\003'|$'\004') return 1;;
                esac
            done
            ndm_restore
        else
            ndm_header "$1"
            printf '  1) %s\n  2) %s\n' "$2" "$3" >&2
            while :; do
                printf '  %s' "$(ndm_t plain)" >&2
                IFS= read -r ndm_answer || return 1
                case "${ndm_answer,,}" in
                    ''|1|n|no|es) ndm_choice=0; break;;
                    2|y|yes|s|si|sí|en|english) ndm_choice=1; break;;
                    b|back|volver) return 1;;
                esac
            done
        fi
    }
    if [[ $ndm_menu == true && -z $ndm_language ]]; then
        ndm_choose 'Idioma / Language' 'Español' 'English' || exit 1
        if (( ndm_choice == 0 )); then ndm_language=es; else ndm_language=en; fi
    fi
    ndm_language=${ndm_language:-es}
    export NDM_STARTUP_LANGUAGE=$ndm_language
    if [[ $ndm_python_found == true ]]; then
        ndm_header "$(ndm_t minimum)"
        exit 1
    fi
    if [[ $(uname -s) == Linux && $EUID == 0 ]] && command -v apt-get >/dev/null; then
        ndm_choose "$(ndm_t python)" "$(ndm_t cancel)" "$(ndm_t install)" || exit 1
        (( ndm_choice == 1 )) || exit 1
        ndm_log_dir=$(mktemp -d "${TMPDIR:-/tmp}/ndm-bootstrap.XXXXXXXX")
        ndm_log="$ndm_log_dir/python.log"
        if [[ $ndm_animated == true ]]; then
            printf '\033[2J\033[H' >&2
            ndm_header "$(ndm_t python)"
            (
                trap - EXIT INT TERM
                ndm_frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
                ndm_i=0 ndm_started=$SECONDS
                while :; do
                    ndm_elapsed=$((SECONDS-ndm_started))
                    printf '\r\033[2K  ' >&2; ndm_color '1;34'
                    printf '[%s]' "${ndm_frames[ndm_i]}" >&2; ndm_color 0
                    printf ' %s · %02d:%02d' "$(ndm_t install)" "$((ndm_elapsed/60))" "$((ndm_elapsed%60))" >&2
                    ndm_i=$(((ndm_i+1)%10))
                    sleep 0.1
                done
            ) &
            ndm_spinner=$!
        else
            printf '  [*] %s\n' "$(ndm_t install)" >&2
        fi
        if { apt-get update && apt-get install -y --no-remove python3 ca-certificates; } > "$ndm_log" 2>&1; then
            ndm_restore
            printf '\n  [+] %s\n' "$(ndm_t ready)" >&2
        else
            ndm_restore
            printf '\n  [✗] %s\n  Log: %s\n' "$(ndm_t failed)" "$ndm_log" >&2
            exit 1
        fi
    else
        printf '  [!] %s\n' "$(ndm_t manual)" >&2
        exit 1
    fi
    trap - EXIT INT TERM
fi
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
if ! python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    printf '%s\n' 'Python 3.11+ required / Se necesita Python 3.11+.' >&2
    exit 1
fi
exec python3 -P -m ndm "$@"
