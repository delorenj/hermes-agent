---
title: PJAN-74 bounded cycle-safe skill discovery
type: bugfix
created: '2026-11-19'
status: in-progress
baseline_commit: d6100d8bda1af8c7f1ca121a7f99a29112c62045
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="operator supplied ticket criteria and autonomous implementation authorization">

## Intent

**Problem:** Following symlink directories without tracking physical identity makes skill discovery and prompt manifest construction revisit the same directories indefinitely. Large acyclic catalogs also retain unbounded traversal and result state. Skill discovery must not starve the gateway owner loop.

**Approach:** Share an import-light bounded traversal across index and prompt manifests. Keep legitimate external symlinks and deterministic discovery; prove production scanner, cache and gateway scheduling behavior with isolated fixtures.

## Boundaries & Constraints

**Always:** Preserve external symlinks, physical alias deduplication, existing excluded and skill-support directories, lexical output ordering, profile scope and conversation prompt stability. Operate in an isolated PJAN-74 worktree. Tests use private homes, timeout/memory limits and actual imports. Commit only explicit owned paths. Require independent SPEC and QUALITY review; repair/review findings. Integrate with `git pull --no-rebase origin main` only when unrelated conflicts can be avoided; preserve a pushed fork branch if upstream permissions or integration block publication.

**Ask First:** Scope expansion or resolving unrelated merge conflicts.

**Never:** Change live services, profiles, catalog links, clocks, rollout, MCP work or board state. Never stash/reset/rebase/switch shared branches. Never discard another worker's files.

## I/O & Edge-Case Matrix

| Scenario | State | Expected behavior | Handling |
|---|---|---|---|
| Cycles | Self, ancestor, mutually linked directories | Visit each physical directory at most once; complete | Path and repeat/cycle reason warned |
| External alias | Two links to external skill | Discover once, stable alias and sorted output | Duplicate path warning |
| Pruning | Excluded dir; support dir inside skill; support-named category | First two pruned, category retained | Existing policy preserved |
| Acyclic oversized tree | Flat files, directory fanout, excessive results or path storage | Stop during scan at finite published budget | Path and budget reason warned |
| Prompt | Cold, memory cached, disk snapshot | All walks terminate using same rules | Snapshot remains deterministic |
| Gateway | Actual discovery blocked for multiple heartbeat periods | Owner loop heartbeat stays below existing watchdog threshold, no restart | Capture scan/heartbeat timings |

</frozen-after-approval>

## Code Map

- `agent/skill_utils.py:878` — `iter_skill_index_files` currently uses raw following `os.walk`, retaining every match before sorting. Existing exclusion/support sets at lines 28–36 define policy.
- `agent/prompt_builder.py:1100` — `_build_skills_manifest` duplicates unsafe walk for SKILL.md and DESCRIPTION.md. Inner prompt builder validates manifest even on cache hits; cold snapshot write walks again. Route all through shared walker.
- `agent/skill_commands.py` — actual command discovery and freshness signatures use the index helper. Preserve registry/cache behavior.
- `gateway/run_inbound.py:1153` and `gateway/run_busy.py:1329` — idle and busy skill rewrite already use `_run_in_executor_with_context`; prove rather than replace this off-loop protection.
- `gateway/run.py:4007` — executor hop copies contextvars. `gateway/shutdown_watchdog.py` — heartbeat 30 seconds, watchdog probe timeout 10 seconds, interval 30 seconds, three strikes. Do not relax these defaults.
- `tests/AGENTS.md` — canonical runner, no source-shape assertions, no live profile writes/network.
- Upstream `origin` is NousResearch (account push=false); `delorenj` fork push=true and main's configured upstream. Required origin pull was attempted and aborted because unrelated files conflicted. Preserve the isolated branch if that blocker remains.

## Tasks & Acceptance

**Execution:**
- [ ] `agent/skill_walk.py` — implement shared finite traversal with physical identity, deterministic alias selection, bounded directory/entry/result/path state and warning reasons.
- [ ] `agent/skill_utils.py`, `agent/prompt_builder.py` — route index and manifests through that scanner; document numerical policy in developer docs.
- [ ] `tests/agent/test_skill_walk.py` — exercise real index and prompt entrypoints in private resource-limited child; assert cycles, pruning, external aliases, cold/cache paths and acyclic limits.
- [ ] `tests/gateway/test_skill_discovery_liveness.py` — invoke production dispatch and real skill discovery deliberately slowed at filesystem boundary; run production heartbeat/watchdog with safe private effects and record timings.
- [ ] `docs/evidence/pjan-74/` — retain commands, red/green evidence, reviewer receipts and publication blockers with linked paths.

**Acceptance Criteria:**
- Given each cycle fixture, when real index runs under enforced timeout and address-space limit, then it completes and each physical directory is opened at most once per scan.
- Given the same fixtures, when prompt builds cold, cached and after clearing memory cache, then manifest/snapshot paths all complete consistently.
- Given oversized acyclic fixtures, when scan budgets are crossed, then traversal and retained results stop at the documented finite boundary and warn with path/reason before unbounded allocation.
- Given a scan lasting several heartbeat intervals, when gateway dispatch performs actual discovery, then loop heartbeat/probe latency stays within unchanged watchdog threshold and no kill/restart occurs.
- Given external skill symlinks, duplicate aliases and pruning fixtures, when discovery runs, then legitimate skills survive once and output stays sorted.
- Given final diff, when independent SPEC and QUALITY reviewers inspect real behavior and checks, then actionable findings are repaired and re-reviewed; changes and evidence are committed/pushed to an authorized ref.

## Spec Change Log

## Verification

- `scripts/run_tests.sh tests/agent/test_skill_walk.py tests/gateway/test_skill_discovery_liveness.py` — targeted regressions green, no skips.
- `scripts/run_tests.sh tests/agent/test_skill_utils.py tests/agent/test_prompt_builder.py tests/agent/test_skill_commands.py tests/agent/test_skill_commands_reload.py tests/agent/test_skill_app_snapshot.py tests/gateway/test_stacked_skill_platform_disabled.py tests/gateway/test_messaging_slash_command_fifo.py` — related behavior stays green.
- `python scripts/check --base d6100d8bda1af8c7f1ca121a7f99a29112c62045` — repository lint gates, distinguish infrastructure failures.
- `git ls-remote delorenj refs/heads/fix/PJAN-74-bounded-skill-discovery`; `git unpushed` — published hash readback and host-wide final audit; unrelated findings preserved.
