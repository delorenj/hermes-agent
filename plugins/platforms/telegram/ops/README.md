# Outbound operator audio — HOLOC-26

This is source-only, pending independent review. It is **not safe to land or
deploy against the current holocene-pm configuration without the pin-off below**.
No registration, credential provisioning, private-audio access or live upload is
part of this implementation.

## Required pin-off before landing

Read-only inspection on November 4, 2026 found both switches already `true` in:

`/home/delorenj/.hermes/profiles/holocene-pm/config.delta.yaml`

The owner must change that source delta to:

```yaml
telegram:
  extra:
    outbound_audio_spool:
      enabled: false
    outbound_audio_drain:
      enabled: false
```

Changing only generated `config.yaml` is not sufficient. The renderer authority
inspected for this task is:

`/home/delorenj/code/33GOD/flume/templates/hermes-agent/scripts/hermes-profile-config.py`

Its `expected_config()` applies `deep_merge(base, delta)` and then
`apply_render_pins()`; today its `RENDER_PIN_PATHS` contains only `terminal.cwd`
and `mcp_servers.codegraph`, not audio. Before landing, the owner must add a
holocene-pm-specific, final-render pin setting both paths above to literal
`false`, include those paths in `RENDER_PIN_PATHS`, and propagate that change to
the renderer actually used by the profile. Competing spellings under
`platforms.telegram.extra` or `gateway.platforms.telegram.extra` must be absent
or pinned off too. Do not let a base merge, role projection or regeneration
re-enable either switch.

Read back the delta, renderer's expected result and generated config: **both
false in all three**. Then prove a newly constructed Telegram adapter has no
spool and both shell entrypoints exit disabled without `op`, queue changes or
network activity. These are prerequisites, not work performed by this branch.
The live profile and renderer were not edited here.

## Capture contract

- The spool is absent by default. Only explicit `enabled: true` constructs it.
- Only native Telegram audio actually sent to a positive, allowlisted operator
  private chat is admitted. Native turns require the original inbound event,
  matching author/chat IDs and a trigger ID. Groups, bots, internal events and
  events denied gateway control do not acquire provenance.
- Ordinary, streamed, queued, voice and `/bg` reply paths pass audio-only
  provenance. An uncorrelated queued result fails closed. No inbound-audio hook
  or group/customer collector exists.
- Cron capture is limited to explicit audio directives from a successful agent
  run, delivered back to the same positive Telegram origin. The owner stamps a
  run/job/path-correlated proof on its job copy. `session_cron` returns that
  proof in job flags so the firing scheduler's separate job dictionary can use
  it. Different fires, destinations and attachments do not inherit it.
- Capture brackets the native media send, while its file handle is open and
  before transcoded files are deleted. The spool verifies source identity,
  bytes and metadata before/after the send. This is a source-stability
  guarantee, not protection against a concurrent writer that changes and
  restores bytes entirely inside that window.
- Bot/chat/message/attachment identity makes dedupe stable across processes
  and bot-specific. Spool publication, recovery and consumption use a POSIX
  advisory lock, atomic non-overwriting publication and hash validation.
  Disabled imports work without `fcntl`; enabled storage requires local POSIX
  filesystem semantics. Network filesystems are unsupported.
- This feature never synthesizes speech or enables voice mode. Existing
  voice-first auto-TTS and caption/text dedupe remain in control of the normal
  gateway. A text-only job does not acquire an audio proof.

## Drain and scheduling contract

`.candybars/outbound-audio-drain.candybar.toml` is the only scheduling artifact.
It is **disabled**, uses `* * * * *` in `America/New_York`, and points at the
canonical `/home/delorenj/.hermes/hermes-agent` installation, not this clone.
It has not been registered. No old drain unit, timer, credential file or
installation recipe was recovered from the WIP tag.

The manifest invokes `outbound-audio-drain-runner.sh <profile>`. It parses the
selected `HERMES_HOME/config.yaml` using Hermes's existing safe ruamel YAML
dependency. Missing flags, non-boolean values and conflicting config shapes
cannot enable it. Missing parser or malformed configuration refuses execution.
Only after **spool AND drain** are literally true does it look for `op` and
invoke:

```text
op run --env-file=<selected-profile>/outbound-audio-drain.env.op -- <canonical-install>/plugins/platforms/telegram/ops/outbound-audio-drain.sh <profile>
```

The reference file is deliberately not supplied. A future authorized operator
must provision dedicated 1Password references for
`HOLOC_OUTBOUND_AUDIO_S3_ACCESS_KEY_ID` and
`HOLOC_OUTBOUND_AUDIO_S3_SECRET_ACCESS_KEY`; neither value belongs in source.
The worker repeats the disabled gate and uses the canonical PM environment
launcher. Tests may invoke either script directly in the clone with an
isolated `HERMES_HOME`; there is no upload-enable flag or checkout override.

The Python factory and worker independently require both switches before
client construction or queue access. The only permitted endpoint is exactly
`https://s3.delo.sh`, and the bucket is exactly `earshot-replies`. The public
`earshot` bucket is refused. The client uses explicit dedicated credentials,
signed requests, path addressing, bounded timeouts, zero SDK retries and no
default credential chain or anonymous fallback.

Every PUT is conditional (`IfNoneMatch="*"`). A matching HEAD must prove size,
digest, bot/chat/message identity, original send time and expiry before local
consumption—even after a nominally successful PUT. Ambiguous outcomes keep
the entry unless HEAD verifies it. Authentication refusal ends the pass.
Overlapping passes serialize on the drain lock; network I/O never holds the
spool lock. Expiry is measured from the original send and capped below 30
minutes. Later invocations may retry only while the entry remains unexpired.

## Acceptance still required

Independent review; delta/renderer pin-off and regeneration proof; canonical
install/PM dependency verification; dedicated credential provisioning and
private bucket/endpoint policy verification; authorized synthetic live
PUT/HEAD acceptance; and any later Candybar registration/readback/execution
proof with competing clocks checked by the owner. None is implied by offline
tests or by the manifest loading successfully.
