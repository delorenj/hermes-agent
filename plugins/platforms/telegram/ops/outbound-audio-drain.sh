#!/usr/bin/env bash
# One foreground pass of the private outbound-audio drain for ONE Hermes profile (default holocene-pm).
# Never daemonizes; overlapping runs serialize on <spool>/.drain.lock. Credentials come only from the
# process environment (HOLOC_OUTBOUND_AUDIO_S3_ACCESS_KEY_ID / _SECRET_ACCESS_KEY), normally injected by
# `op run --env-file=<profile>/outbound-audio-drain.env.op`; this script never reads or prints them.
set -euo pipefail

profile="${1:-holocene-pm}"
checkout="${HERMES_CHECKOUT:-$HOME/.hermes/hermes-agent}"
python="${DRAIN_PYTHON:-$checkout/.venv/bin/python}"
profile_home="$HOME/.hermes/profiles/$profile"

[[ "$profile" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "outbound-audio-drain: bad profile name" >&2; exit 2; }
[[ -d "$profile_home" && ! -L "$profile_home" ]] || { echo "outbound-audio-drain: $profile_home must be a real profile dir" >&2; exit 2; }
"$python" -c 'import botocore' 2>/dev/null || { echo "outbound-audio-drain: $python lacks botocore; drain unavailable" >&2; exit 2; }

export HERMES_HOME="$profile_home"
cd "$checkout"
exec "$python" -m plugins.platforms.telegram.outbound_audio_drain --once
