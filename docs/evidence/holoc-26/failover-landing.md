# HOLOC-26 authorized standalone failover landing — 2026-11-18

Scope: Telegram read-only retrieval failover for all profiles (pillar `belsky-doctrine`). Standing USER authorization only; no PM approval or full-ticket acceptance implied. All Git/source/docs actions performed by native Codex session `01a1f06c-1996-7d73-bd1e-42ce1340f6de`; no delegation/new reviews.

- Fetched fork main base: `2335d9ef2562262bfd73fd8d151cb3e75f3cc789`.
- Exact reviewed source commit: `4a34f79f82aa9f60b690d892753c08d5636381e2`.
- Cherry-picked landing code commit: `ac128f6f379571f3d3220294f9354012c3cb83e1`; author preserved. Only `plugins/platforms/telegram/telegram_network.py` and `tests/gateway/test_telegram_readonly_failover.py` changed. Both byte-for-byte match reviewed commit (`git diff --exit-code`, exit 0). Everything added after tests is evidence only.
- Existing SPEC and QUALITY standalone PASS reports are copied faithfully here; `cmp` exit 0 for both. Their historical full-audio findings are not refreshed acceptance claims.
- Canonical scoped runner: **38 passed / 2 files / 0 failed / exit 0**. Import/disabled adapter construction: **exit 0**. `git diff --check`: exit 0. No live Telegram/API probe.
- Existing dependency-complete interpreter reused read-only at `/var/tmp/holoc26-port-testenv-complete-20261118/bin/python`; no new /tmp or /var/tmp outputs. Probe/temp/home/bytecode/duration roots are under `/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128`. Scratch-only shim supplies isolated HOME/HERMES_HOME/TMPDIR to the canonical runner, overrides its hardcoded /var/tmp runner root and duration cache, and skips optional compileall precompile. Tracked runner/test files are unmodified. The actual runner and pytest child exit/counts, not wrapper success, establish the result.
- Recovery branch was absent remotely, then normally pushed as `feat/holoc-26-outbound-audio-candybar` at exact `cfec39692c89a3d01f427eb7647851ae82092ad1`; readback matched before setting upstream. Recovery clone/branch kept.
- Audio review branch `feat/holoc-26-current-main-20261118` remains `f6c27c4afddc25e1d2196cafea33de8b94c21fdc`, verified on fork; its worktree remains untouched. No audio commit merged.
- Recovery tag `wip/holoc-22-outbound-audio-autostash` retained unchanged locally/remotely at `7758ca610987556a4b72ac17b2ece395b91abcde` for provenance/recovery.
- Remotes unchanged: only `delorenj git@github.com:delorenj/hermes-agent.git` receives pushes. `origin git@github.com:NousResearch/hermes-agent.git` is upstream; never pushed there.

## Live install blocked, not updated
Live main remains `d6100d8bda1af8c7f1ca121a7f99a29112c62045`. It is an ancestor of landing code, but ENTIRE diff includes these unreviewed paths outside authorized two-file slice (complete diff and commits in command evidence):
```
M	hermes_cli/runtime_state.py
A	hermes_cli/runtime_state_users.py
M	pm/install_states.py
M	pm/runtime.py
M	tests/conftest.py
A	tests/hermes_cli/test_runtime_state_users.py
M	tests/pm/test_install_states_gc.py
M	tests/pm/test_startup_recovery.py
M	website/docs/developer-guide/pm-audit-status.md
M	website/docs/reference/package-management.md
```
Unowned live agents/, audio modules/ops (including old service/timer) remain untouched. Their presence is not a timer installed by this slice. No live Git fetch/merge/config changes, no live source update and therefore no restart request. No raw systemd/kill actions.

## Remaining full-ticket gates
AC5 permits this failover-only landing. AC3 remains blocked: outbound-audio-drain is unregistered and no registered terminal invocation is proven. AC4 remains blocked: HOLOC-22 needs private S26 enrollment/authenticated playback proof, unsigned list/GET denial, and independently proven writer credential scope. No uploads, credential/schedule mutations, mirror enablement, Candybar calls, Holocene/Plane changes or audio acceptance are authorized/performed here.

## Landing/checkpoint policy
Own isolated branch/worktree only; explicit `git pull --no-rebase delorenj main` immediately before normal non-force `HEAD:refs/heads/main` push. Any incoming source invalidates tests and must be inspected/retested. Final remote SHA/receipt and owned-worktree cleanup result are recorded in the final JSON. Only this finished landing worktree/branch may be removed after successful push and clean status; preserved recovery/audio branches/worktrees remain.

## Observed final receipt and cache exception
First main push/readback succeeded at `7e679d3fc7101a0092f5839edc4be1276347ead9`; source tests/imports remain valid, only evidence changed afterwards. Live install remains blocked/untouched; restart NOT requested.

Constraint exception: the first canonical test run created ignored `.pytest-cache/` (gateway conftest adapter guard) and `.pytest_cache/` (pytest provider) inside this OWN landing worktree. These are not /tmp or /var/tmp writes, but violate the scratch-only cache requirement. Both owned directories were moved into the unique profile scratch before cleanup. No unowned artifact was moved/deleted. This is not a full-compliance claim. Reused /var/tmp interpreter was not mutated.

`git unpushed` exit 1: 328 repositories have unrelated local work; no HOLOC-26 matching lines. Recovery and audio branches have exact fork upstreams. Local main is behind fork by 14 commits, not disk-only work. `origin/main` is absent in this full clone (not fetched); live cached HEAD vs NousResearch origin/main is 26 live-only / 7402 upstream-only. Upstream divergence is NOT proof of disk-only work. No unrelated repository was modified.
