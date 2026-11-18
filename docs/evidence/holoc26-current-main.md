# HOLOC-26 current-main review handoff — 2026-11-18

Neighboring level: default-off outbound audio recovery for every Telegram profile; pillar `belsky-doctrine`.
Native Codex session: `01a1f06c-1996-7d73-bd1e-42ce1340f6de`. Sole source/test/docs carrier; no delegation or ephemeral implementation session.

## Identity and scope
- Review branch: `feat/holoc-26-current-main-20261118` (fork delorenj only).
- Retained review worktree: `/home/delorenj/code/hermes-agent-holoc26-current-main`.
- Preserved recovery clone/branch: `/home/delorenj/code/hermes-agent-holoc26`, `feat/holoc-26-outbound-audio-candybar`; HEAD unchanged `cfec39692c89a3d01f427eb7647851ae82092ad1`.
- Recovery base: `91b809b72cfbe781996e94935124e1bdf6e7f98b`.
- Fetched current fork main/base: `2335d9ef2562262bfd73fd8d151cb3e75f3cc789`. Implementation head: `0f8df936f20a5dbef793b674f7836edc40ff03b1`. Final evidence-only descendant is identified by `git rev-parse HEAD` on the pushed review branch (a report cannot contain its own commit hash).
- `dc84634c793b5bf7005004a02a2cd9ad1306b2a6` → `4a34f79f82aa9f60b690d892753c08d5636381e2` (failover alone, no audio dependency).
- `cfec39692c89a3d01f427eb7647851ae82092ad1` → `0f8df936f20a5dbef793b674f7836edc40ff03b1` (selective audio port plus review-gap fixes). Both preserve Jarad's authorship. Patches checked and applied against fetched main without conflict; no blind branch rewrite, stash, reset or rebase.
- Recovery tag `wip/holoc-22-outbound-audio-autostash` retained at `7758ca610987556a4b72ac17b2ece395b91abcde`: rollback/provenance remains needed until independent review and authorized integration; do not delete now.
- Remotes verified: `delorenj git@github.com:delorenj/hermes-agent.git` is the ONLY authorized push destination. Missing upstream `origin` was added as `git@github.com:NousResearch/hermes-agent.git`; never pushed there.

## AC status (ticket wording verbatim)
1. **Applies cleanly to current delorenj/main. Imports of plugins.platforms.telegram.adapter and the gateway.* hooks work with the spool disabled.** PASS locally on fetched base: patch application, actual imports and disabled adapter construction succeed. Not integrated into main/live.
2. **tests/gateway/test_telegram_readonly_failover.py passes, plus spool and drain tests covering the HOLOC-22 review blockers: no import break without fcntl, and malformed metadata cannot poison queue recovery.** PASS: final committed-source run, 7 files / 108 passed / 0 failed, exit 0. Includes 19 failover, 14 disabled/no-fcntl, 23 spool, 26 drain, 5 cron, 11 egress, 10 added CLI/corrupt-sibling cases. Current main's actual no-fcntl bootstrap passes here; no fake OS or claim of Windows acceptance. No red-on-base run was performed; full repository lint/suite not run within this bounded port.
3. **No systemd timer is committed or installed. The drain is registered in Candybar, candybar doctor --live is clean, and one terminal invocation is proven.** PARTIAL / PENDING: no timer added/installed, canonical runner source committed at intended repo path, inert disabled template offline-loader-valid, direct disabled fixture CLI exit 0. Job remains UNREGISTERED (`show` exit 2); no registered invocation attempted. Doctor exit 2 / unknown: `systemd-user: unavailable inventory`; repo gate passes. Earlier doctor pass is not current evidence. Disabled direct execution is NOT registered execution proof.
4. **The HOLOC-22 privacy gate still holds: no private audio is uploaded until the authenticated phone path is proven and unsigned list/GET is denied. The S3 writer credential is created in DeLoSecrets and referenced only through op://.** PENDING OWNER PROOF: no private audio read/upload/probe; generated profile and delta both OFF. A dedicated reference-only env file exists and names DeLoSecrets item/field references (see scrubbed metadata); existence is NOT proof the item/credential exists, works or is scoped. No op read, credential creation or permission changes. Authenticated phone playback and unsigned GET/list denial cannot be independently proven from producer fixtures. Historical Earshot contract dated 2026-10-20 explicitly says phone private auth was not implemented; it is not current runtime proof. Safe alternative: fake signed-client/S3 transports and synthetic-only phone fixtures; owner must later supply authenticated device and denied unsigned access receipts before any private upload.
5. **The read-only failover can land on its own, ahead of the mirror, if the mirror is still blocked.** PASS for separability: first commit has only transport + 19 tests. Main landing remains prohibited in this carrier.

## Review-gap remediation and gates
- Optional botocore was absent from the first PM build: initial scoped run had 72 passed, 1 collection error in drain (exit 1). Fresh PM build with declared `--extra bedrock` resolved it without pyproject edits or raw pip/uv mutation. Final run above is dependency-complete.
- `--config` keeps the documented flattened-extra contract. Whole gateway shapes now return 2 before factory/queue/network access instead of misleading disabled success. Omitted `--config` still reads normal owning-home config. Tests cover both flattened flags and whole-config rejection.
- Corrupt JSON and scalar/list/null metadata cannot poison valid sibling uploads; existing wrong-type/overflow tests retained.
- Template at `plugins/platforms/telegram/ops/outbound-audio-drain.candybar.toml.example`, NOT watched `.candybars`. Watchers may auto-register real manifests on save. Never copy/register template before approval.
- Both shell gates return before op lookup, credential resolution, queue I/O or network when disabled; synthetic sentinel tests prove queue untouched. Spool default is absent; no flags changed here.

## Live collision fence and later landing plan
Live checkout untouched at `d6100d8bda1af8c7f1ca121a7f99a29112c62045`; unowned paths:
```
?? agents/
?? plugins/platforms/telegram/ops/
?? plugins/platforms/telegram/outbound_audio_drain.py
?? plugins/platforms/telegram/outbound_audio_provenance.py
?? plugins/platforms/telegram/outbound_audio_spool.py
```
Do not overwrite/remove/stash these paths. Later owner saves these files OUTSIDE the checkout, records hashes, compares each against reviewed source, and resolves ownership before integration. First independent SPEC, then QUALITY. After explicit approval, land failover alone if audio still blocked; otherwise authorized audio landing must remain default-off in delta, final renderer pins and generated config (renderer pin proof is still pending). Verify runtime PM dependencies. Request ONE guarded `hermes-gateway-restart --reason "HOLOC-26 approved default-off landing"`, never a raw restart. Only later with authorization promote/register template disabled, read it back, prove registered disabled execution/no upload and rerun clean live doctor. No enable until authenticated phone/unsigned denial/credential scope owner proofs. Preserve worktree until integration cleanup is authorized.

No main push/merge, live/config/profile/secret writes, restarts, registration/invocation through Candybar, events, timers, ACL changes, billable service calls, board writes or private media operations occurred. Only review-branch Git pushes.

## Changed paths
```
cron/scheduler_delivery.py
gateway/platforms/base.py
gateway/run_background.py
gateway/run_notifications.py
gateway/run_turn.py
gateway/run_voice.py
gateway/session_cron.py
gateway/slash_commands.py
gateway/turn_context.py
plugins/platforms/telegram/adapter.py
plugins/platforms/telegram/ops/README.md
plugins/platforms/telegram/ops/outbound-audio-drain-runner.sh
plugins/platforms/telegram/ops/outbound-audio-drain.candybar.toml.example
plugins/platforms/telegram/ops/outbound-audio-drain.sh
plugins/platforms/telegram/outbound_audio_config.py
plugins/platforms/telegram/outbound_audio_drain.py
plugins/platforms/telegram/outbound_audio_provenance.py
plugins/platforms/telegram/outbound_audio_spool.py
plugins/platforms/telegram/telegram_network.py
tests/gateway/outbound_audio_fixtures.py
tests/gateway/test_outbound_audio_config_contract.py
tests/gateway/test_outbound_audio_cron.py
tests/gateway/test_outbound_audio_disabled.py
tests/gateway/test_outbound_audio_drain.py
tests/gateway/test_outbound_audio_egress.py
tests/gateway/test_outbound_audio_spool.py
tests/gateway/test_telegram_readonly_failover.py
docs/evidence/holoc26-current-main.md
docs/evidence/holoc26-command-logs.md
```

## Verification limitations and checkpoint
Scoped Ruff and diff whitespace checks pass. Initial Ruff flagged two redundant-return rules in a new test helper; fixed, then tests rerun on committed implementation. First manifest probe loaded successfully but its printing assumed attribute access on a dict; corrected probe passes. All failures retained in command logs.
`git unpushed` ran in this touched clone/worktree (same Git common dir), exit 1; host-wide audit reports 328 repositories with unrelated local work, the final audit specifically reports the preserved recovery branch has NO UPSTREAM; it was deliberately not pushed or reconfigured under the preservation mandate. The new review branch is clean and tracks its verified remote head. Those repositories were not touched or landed. Evidence commit must also be pushed and remote head compared before handback.
