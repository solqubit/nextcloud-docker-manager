#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
run_id=$(python3 -c 'import secrets;print(secrets.token_hex(6))')
state_volume="ndm-test-state-$run_id"
bind_volume="ndm-test-bind-$run_id"
docker build -t ndm-test-runner:local -f "$ROOT/tests/Dockerfile" "$ROOT"
docker volume create --label "com.solqubit.ndm.test=$run_id" "$state_volume" >/dev/null
docker volume create --label "com.solqubit.ndm.test=$run_id" "$bind_volume" >/dev/null
cleanup() {
    for volume in "$state_volume" "$bind_volume"; do
        owner=$(docker volume inspect --format '{{index .Labels "com.solqubit.ndm.test"}}' "$volume") || continue
        if [[ $owner == "$run_id" ]]; then docker volume rm "$volume" >/dev/null || true; fi
    done
}
trap cleanup EXIT
bind_mount=$(docker volume inspect --format '{{.Mountpoint}}' "$bind_volume")
[[ $bind_mount == /var/lib/docker/volumes/ndm-test-bind-* ]] || exit 1
suite_args=(--major --bind --latest)
if [[ ${1:-} == --lts-only ]]; then suite_args=(--lts-only); fi
docker run --rm --name "ndm-test-runner-$run_id" --label "com.solqubit.ndm.test=$run_id" \
    --network host --add-host host.docker.internal:127.0.0.1 \
    -v "$ROOT:/workspace:ro" -v "$state_volume:/ndm-state" -v "$bind_volume:$bind_mount" \
    -v /var/run/docker.sock:/var/run/docker.sock -e "NDM_BIND_FIXTURE=$bind_mount" \
    -w /workspace ndm-test-runner:local python3 -u tests/run_integration.py "${suite_args[@]}"
