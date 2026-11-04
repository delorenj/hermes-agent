#!/usr/bin/env bash
# Candybar's one-shot exec entrypoint. Gate before even looking for op or its reference file.
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
command -v op >/dev/null || { echo "outbound-audio-drain: op unavailable" >&2; exit 2; }
references="$HERMES_HOME/outbound-audio-drain.env.op"
[[ -f "$references" && ! -L "$references" ]] || {
    echo "outbound-audio-drain: dedicated credential reference file required" >&2; exit 2;
}
# Only the canonical install is executable after opt-in, never a feature clone.
exec op run --env-file="$references" -- \
    /home/delorenj/.hermes/hermes-agent/plugins/platforms/telegram/ops/outbound-audio-drain.sh "$profile"
