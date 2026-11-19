# PJAN-74 review and execution receipts

Source: independent read-only Hermes one-shots, logs in the active pjangler-pm profile scratch (`pjan74-{spec,quality}-{review,rereview}.log`). These excerpts exclude credential-loader banners. Reviewers did not implement code or modify the board.

## SPEC — first review: FAIL

> High: manifest and index spend separate 8,192-result budgets; DESCRIPTION.md can crowd indexed skills out of cache invalidation.
>
> Medium: a lexically earlier deep alias can consume a physical identity before a shallower path, losing a skill past the depth limit.
>
> Medium: the gateway test calls the reload handler directly rather than message dispatch and does not measure watchdog probe latency.

Repair and independent re-review verdict: **PASS on these three findings**. The re-review cites `agent/skill_walk.py:83-122`, `agent/prompt_builder.py:1100-1109`, `tests/agent/test_skill_walk.py:158-167`, and `tests/gateway/test_skill_discovery_liveness.py:48-115,132-152`. It explicitly did **not** approve upstream publication, which remains blocked.

## QUALITY — first review: FAIL

> High: starting a real hard-exit watchdog in the pytest process risks killing the worker; its dump-path assertion pointed to the wrong home.
>
> Medium: pruned support dirs spend retained path bytes before pruning and can omit eligible skills.
>
> Medium: manifest test checked count, not paths/signatures or actual disk-snapshot reuse.

Repair and distinct independent re-review verdict: **PASS on these three findings**. The re-review cites `tests/gateway/test_skill_discovery_liveness.py:64-69,107-108,132-152`, `agent/skill_walk.py:59-65`, and `tests/agent/test_skill_walk.py:23-65,170-179`. It did not run the tests and explicitly noted separate full-repository/publication blockers. Tests were run by the implementation worker under the isolated runner.

## Test runner outputs

- Nine related scanner/prompt/tool test files: `=== Summary: 9 files, 199 tests passed, 0 failed (100% complete) in 9.3s (4 workers) ===` (`pjan74-related-final.log`). Includes 13 scanner regressions: self/ancestor/two-back-link cycles under 8s/320-MiB child limits, external aliases/pruning, each finite budget, deep-alias and support-budget edges, and a 4,096-directory acyclic child bound.
- Gateway child-process test: `=== Summary: 1 files, 2 tests passed, 0 failed (100% complete) in 96.0s (1 workers) ===` (`pjan74-gateway-child.log`). The generated JUnit `pjan74-gateway-child.xml` recorded:
  - Accelerated cadence: scan blocked `3.0259079019306228s`; four heartbeat writes, max gap `1.0051992200314999s`; three watchdog probes, max latency `0.00020743103232234716s`.
  - Production 30s cadence: scan blocked `90.02317601302639s`; four writes, max gap `30.00498971599154s`; three probes, max latency `0.00009951798710972071s`. Unchanged watchdog timeout `10.0s`, three strikes, PID unchanged, restart false.
- `python scripts/check --only health --base d6100d8bda1af8c7f1ca121a7f99a29112c62045`: `code health: 6 files ... 0 blocking, 0 advisory`; `scripts/check: 1 checks, ok`.
- Ruff over six owned Python files: `All checks passed!`. Whole-repository `scripts/check`: 14 checks, failures limited to out-of-scope `cron/economics.py` footgun and 19 Ruff findings in unrelated paths; see [results](results.md) for boundary.

## Publication

Commit [`263453421c456ed59d45f209cc9cb72596be0088`](https://github.com/delorenj/hermes-agent/commit/263453421c456ed59d45f209cc9cb72596be0088) was pushed to [`delorenj/fix/PJAN-74-bounded-skill-discovery`](https://github.com/delorenj/hermes-agent/tree/fix/PJAN-74-bounded-skill-discovery). `git ls-remote delorenj refs/heads/fix/PJAN-74-bounded-skill-discovery` returned the exact commit hash. `origin/main` was `8d70b88abd68fd58833ba0fc1f78ab209d213ed8` at readback. **This is a fallback branch, not a main landing.**
