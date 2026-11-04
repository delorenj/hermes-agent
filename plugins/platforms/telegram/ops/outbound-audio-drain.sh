#!/usr/bin/env bash
# Foreground worker; direct invocation must pass the same gate as the Candybar entrypoint.
set -euo pipefail
profile="${1:-holocene-pm}"
[[ $# -le 1 && "$profile" =~ ^[a-z0-9][a-z0-9-]*$ ]] || exit 2
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/$profile}"
status=0
python3 "$script_dir/../outbound_audio_config.py" "$HERMES_HOME/config.yaml" || status=$?
if [[ "$status" == 3 ]]; then
    echo "outbound-audio-drain: disabled; no credentials, network or queue access"
    exit 0
fi
[[ "$status" == 0 ]] || exit 2
checkout="$(cd -- "$script_dir/../../../.." && pwd)"
cd "$checkout"
exec "$checkout/scripts/run-in-hermes-env" python -m plugins.platforms.telegram.outbound_audio_drain --once
