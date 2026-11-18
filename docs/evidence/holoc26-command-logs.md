# HOLOC-26 scrubbed command evidence

## Fresh PM build (exit 0)
Command: `HERMES_HOME=/var/tmp/holoc26-port-home-20261118 HERMES_RUNTIME_DIR=/var/tmp/holoc26-port-runtime-20261118 heavy python3 -m pm.build_env --source . --out /var/tmp/holoc26-port-testenv-20261118 --group dev --group test`
```text
heavy: systemd --user unavailable, falling back to nice/ionice
Preparing the isolated Hermes runtime…
  → Installing Python dependencies…
  ✓ Installing Python dependencies
  → Installing Python dependencies…
  ✓ Installing Python dependencies
/var/tmp/holoc26-port-testenv-20261118/bin/python
```

## Complete PM build (exit 0)
Command: `same isolated vars; heavy python3 -m pm.build_env --source . --out /var/tmp/holoc26-port-testenv-complete-20261118 --group dev --group test --extra bedrock`
```text
heavy: systemd --user unavailable, falling back to nice/ionice
  → Installing Python dependencies…
  ✓ Installing Python dependencies
/var/tmp/holoc26-port-testenv-complete-20261118/bin/python
```

## Initial scoped tests (exit 1)
Command: `scripts/run_tests.sh -j 2 <six test paths below> -q; HERMES_PYTHON=/var/tmp/holoc26-port-testenv-20261118/bin/python`
```text
▶ not activated — using HERMES_PYTHON: /var/tmp/holoc26-port-testenv-20261118/bin/python
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 6 test files (~34 tests) under ['tests/gateway/test_telegram_readonly_failover.py', 'tests/gateway/test_outbound_audio_spool.py', 'tests/gateway/test_outbound_audio_drain.py', 'tests/gateway/test_outbound_audio_disabled.py', 'tests/gateway/test_outbound_audio_cron.py', 'tests/gateway/test_outbound_audio_egress.py']; running with -j 2
[  8.8% |     3/~34 | ✓14 | ✗ 0] ✓ tests/gateway/test_outbound_audio_disabled.py (14✓, 2.6s)
[ 38.2% |    13/~34 | ✓14 | ✗ 0] ✗ tests/gateway/test_outbound_audio_drain.py (1e, 1.0s)

  ╔╍ Failed: tests/gateway/test_outbound_audio_drain.py ╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍
  ║ 
  ║ ==================================== ERRORS ====================================
  ║ _________ ERROR collecting tests/gateway/test_outbound_audio_drain.py __________
  ║ ImportError while importing test module '/home/delorenj/code/hermes-agent-holoc26-current-main/tests/gateway/test_outbound_audio_drain.py'.
  ║ Hint: make sure your test modules/packages have valid Python names.
  ║ Traceback:
  ║ /var/tmp/holoc26-port-runtime-20261118/python-3.14.9+20261021-linux-x64/lib/python3.14/importlib/__init__.py:88: in import_module
  ║     return _bootstrap._gcd_import(name[level:], package, level)
  ║            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  ║ tests/gateway/test_outbound_audio_drain.py:8: in <module>
  ║     from botocore.exceptions import ClientError, EndpointConnectionError
  ║ E   ModuleNotFoundError: No module named 'botocore'
  ║ =========================== short test summary info ============================
  ║ ERROR tests/gateway/test_outbound_audio_drain.py
  ║ !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
  ║ 1 error in 0.13s
  ║ 
  ║ (pytest child exit status 2)
  ║
  ║  Repro: python -m pytest tests/gateway/test_outbound_audio_drain.py -q
  ╚╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍

[ 44.1% |    15/~34 | ✓19 | ✗ 0] ✓ tests/gateway/test_outbound_audio_cron.py (5✓, 3.7s)
[ 64.7% |    22/~34 | ✓42 | ✗ 0] ✓ tests/gateway/test_outbound_audio_spool.py (23✓, 2.0s)
[ 82.4% |    28/~34 | ✓53 | ✗ 0] ✓ tests/gateway/test_outbound_audio_egress.py (11✓, 2.4s)
[100.0% |    34/~34 | ✓72 | ✗ 0] ✓ tests/gateway/test_telegram_readonly_failover.py (19✓, 2.1s)

=== Summary: 6 files, 72 tests passed, 0 failed, 1 error; FAILED: 1 of 6 files (1 at import/collection) (100% complete) in 8.0s (2 workers) ===
  Durations cached to test_durations.json (5 files)

=== Per-file subprocess time distribution ===
  Files:   6
  Total subprocess CPU-wall: 13.9s  (runner wall: 8.0s, parallelism: 2x)
  P50: 2.43s  P90: 3.68s  P95: 3.68s  P99: 3.68s  Max: 3.68s
  <1s: 1 files (17%)  <2s: 1 files (17%)
  Top 10 slowest:
      3.68s  tests/gateway/test_outbound_audio_cron.py
      2.60s  tests/gateway/test_outbound_audio_disabled.py
      2.43s  tests/gateway/test_outbound_audio_egress.py
      2.14s  tests/gateway/test_telegram_readonly_failover.py
      2.05s  tests/gateway/test_outbound_audio_spool.py
      0.97s  tests/gateway/test_outbound_audio_drain.py

=== Failure output ===

--- tests/gateway/test_outbound_audio_drain.py ---

==================================== ERRORS ====================================
_________ ERROR collecting tests/gateway/test_outbound_audio_drain.py __________
ImportError while importing test module '/home/delorenj/code/hermes-agent-holoc26-current-main/tests/gateway/test_outbound_audio_drain.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/var/tmp/holoc26-port-runtime-20261118/python-3.14.9+20261021-linux-x64/lib/python3.14/importlib/__init__.py:88: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/gateway/test_outbound_audio_drain.py:8: in <module>
    from botocore.exceptions import ClientError, EndpointConnectionError
E   ModuleNotFoundError: No module named 'botocore'
=========================== short test summary info ============================
ERROR tests/gateway/test_outbound_audio_drain.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.13s

(pytest child exit status 2)

=== 1 file that FAILED AT IMPORT/COLLECTION (0 tests ran) ===
  tests/gateway/test_outbound_audio_drain.py  ModuleNotFoundError: No module named 'botocore'
```

## Final committed-source tests (exit 0)
Command: `HERMES_HOME=/var/tmp/holoc26-port-home-20261118 HERMES_PYTHON=/var/tmp/holoc26-port-testenv-complete-20261118/bin/python scripts/run_tests.sh -j 2 tests/gateway/test_telegram_readonly_failover.py tests/gateway/test_outbound_audio_spool.py tests/gateway/test_outbound_audio_drain.py tests/gateway/test_outbound_audio_disabled.py tests/gateway/test_outbound_audio_cron.py tests/gateway/test_outbound_audio_egress.py tests/gateway/test_outbound_audio_config_contract.py -q`
```text
▶ not activated — using HERMES_PYTHON: /var/tmp/holoc26-port-testenv-complete-20261118/bin/python
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 7 test files (~37 tests) under ['tests/gateway/test_telegram_readonly_failover.py', 'tests/gateway/test_outbound_audio_spool.py', 'tests/gateway/test_outbound_audio_drain.py', 'tests/gateway/test_outbound_audio_disabled.py', 'tests/gateway/test_outbound_audio_cron.py', 'tests/gateway/test_outbound_audio_egress.py', 'tests/gateway/test_outbound_audio_config_contract.py']; running with -j 2
[ 16.2% |     6/~37 | ✓11 | ✗ 0] ✓ tests/gateway/test_outbound_audio_egress.py (11✓, 2.5s)
[ 21.6% |     8/~37 | ✓16 | ✗ 0] ✓ tests/gateway/test_outbound_audio_cron.py (5✓, 3.4s)
[ 29.7% |    11/~37 | ✓30 | ✗ 0] ✓ tests/gateway/test_outbound_audio_disabled.py (14✓, 2.2s)
[ 56.8% |    21/~37 | ✓56 | ✗ 0] ✓ tests/gateway/test_outbound_audio_drain.py (26✓, 2.2s)
[ 75.7% |    28/~37 | ✓79 | ✗ 0] ✓ tests/gateway/test_outbound_audio_spool.py (23✓, 2.0s)
[ 91.9% |    34/~37 | ✓98 | ✗ 0] ✓ tests/gateway/test_telegram_readonly_failover.py (19✓, 1.9s)
[100.0% |    37/~37 | ✓108 | ✗  0] ✓ tests/gateway/test_outbound_audio_config_contract.py (10✓, 1.4s)

=== Summary: 7 files, 108 tests passed, 0 failed (100% complete) in 8.3s (2 workers) ===
  Durations cached to test_durations.json (7 files)

=== Per-file subprocess time distribution ===
  Files:   7
  Total subprocess CPU-wall: 15.8s  (runner wall: 8.3s, parallelism: 2x)
  P50: 2.21s  P90: 3.42s  P95: 3.42s  P99: 3.42s  Max: 3.42s
  <1s: 0 files (0%)  <2s: 3 files (43%)
  Top 10 slowest:
      3.42s  tests/gateway/test_outbound_audio_cron.py
      2.55s  tests/gateway/test_outbound_audio_egress.py
      2.24s  tests/gateway/test_outbound_audio_disabled.py
      2.21s  tests/gateway/test_outbound_audio_drain.py
      2.00s  tests/gateway/test_outbound_audio_spool.py
      1.91s  tests/gateway/test_telegram_readonly_failover.py
      1.43s  tests/gateway/test_outbound_audio_config_contract.py
```

## Ruff initial (exit 1)
Command: `/var/tmp/holoc26-port-testenv-complete-20261118/bin/ruff check plugins/platforms/telegram/outbound_audio*.py tests/gateway/test_outbound_audio*.py tests/gateway/outbound_audio_fixtures.py tests/gateway/test_telegram_readonly_failover.py`
```text
warning: Selection `FURB192` has no effect because preview is not enabled.
warning: Selection `PLE0304` has no effect because preview is not enabled.
warning: Selection `PLR1708` has no effect because preview is not enabled.
warning: Selection `RUF063` has no effect because preview is not enabled.
warning: Selection `RUF068` has no effect because preview is not enabled.
RET501 [*] Do not explicitly `return None` in function if it is the only possible return value
  --> tests/gateway/test_outbound_audio_config_contract.py:28:9
   |
26 |     def factory(config, home):
27 |         seen.append(config)
28 |         return None
   |         ^^^^^^^^^^^
29 |
30 |     monkeypatch.setattr(drain, "build_drain", factory)
   |
help: Remove explicit `return None`

PLR1711 [*] Useless `return` statement at end of function
  --> tests/gateway/test_outbound_audio_config_contract.py:28:9
   |
26 |     def factory(config, home):
27 |         seen.append(config)
28 |         return None
   |         ^^^^^^^^^^^
29 |
30 |     monkeypatch.setattr(drain, "build_drain", factory)
   |
help: Remove useless `return` statement

Found 2 errors.
[*] 2 fixable with the `--fix` option.
```

## Ruff final (exit 0)
Command: `same scoped Ruff command after removing redundant return`
```text
warning: Selection `FURB192` has no effect because preview is not enabled.
warning: Selection `PLE0304` has no effect because preview is not enabled.
warning: Selection `PLR1708` has no effect because preview is not enabled.
warning: Selection `RUF063` has no effect because preview is not enabled.
warning: Selection `RUF068` has no effect because preview is not enabled.
All checks passed!
```

## Real imports + disabled runner (exit 0 each)
Command: `HERMES_HOME=/var/tmp/holoc26-port-home-20261118; complete interpreter: import gateway.run, run_voice, run_turn, run_background, run_notifications, session_cron; TelegramAdapter(PlatformConfig(extra={})); drain.main([]). Then PATH=<complete-env>/bin:$PATH plugins/platforms/telegram/ops/outbound-audio-drain-runner.sh holocene-pm`
```text
INFO plugins.platforms.telegram.outbound_audio_drain: outbound audio drain disabled; nothing to do
Current-main gateway hooks and disabled adapter imports: PASS

IMPORT_EXIT=0
outbound-audio-drain: disabled; no credentials, network or queue access
DISABLED_FIXTURE_RUNNER_EXIT=0
```

## Candybar readback (exit 2)
Command: `cd /home/delorenj/code/33GOD/candybar; uv run candybar show outbound-audio-drain -n 1`
```text
no candybar named 'outbound-audio-drain'
```

## Manifest first probe (exit 1: printing error after validation)
Command: `uv run python: load_manifest(template); assert not m.enabled; print(m.runner.argv[0])`
```text
Traceback (most recent call last):
  File "<stdin>", line 4, in <module>
AttributeError: 'dict' object has no attribute 'argv'
```

## Manifest corrected probe (exit 0)
Command: `uv run python: load_manifest(template); assert m.name == "outbound-audio-drain" and m.enabled is False; assert m.runner["kind"] == "exec"; print(m.runner["argv"][0])`
```text
Offline canonical loader: PASS; enabled=False; canonical runner=/home/delorenj/.hermes/hermes-agent/plugins/platforms/telegram/ops/outbound-audio-drain-runner.sh
```

## Read-only config/reference metadata (no values resolved)
Command: `Safe ruamel read: output only audio enable booleans; reference file: output only op:// names/field metadata`
```text
{"profile": "holocene-pm", "source": "config.yaml", "outbound_audio_spool": "OFF", "outbound_audio_drain": "OFF"}
{"profile": "holocene-pm", "source": "config.delta.yaml", "outbound_audio_spool": "OFF", "outbound_audio_drain": "OFF"}
reference_file_exists=True
{"variable": "HOLOC_OUTBOUND_AUDIO_S3_ACCESS_KEY_ID", "op_reference_only": true, "reference": "op://DeLoSecrets/f3q77itadfpfzeqkun7ehgqasu/access_key_id"}
{"variable": "HOLOC_OUTBOUND_AUDIO_S3_SECRET_ACCESS_KEY", "op_reference_only": true, "reference": "op://DeLoSecrets/f3q77itadfpfzeqkun7ehgqasu/secret_access_key"}
```

## Candybar doctor (exit 2, not green)
Command: `cd /home/delorenj/code/33GOD/candybar; uv run candybar doctor --repo . --live --json`
Approved identity inventories omitted; status/findings retained.
```json
{
  "status": "unknown",
  "checks": {
    "repo": {
      "status": "pass",
      "violations": [],
      "unknown": []
    },
    "machine": {
      "status": "unknown",
      "violations": [],
      "unknown": [
        "systemd-user: unavailable inventory"
      ],
      "counts": {
        "registry": 152,
        "systemd-system": 24,
        "crontab": 11,
        "hermes": 22
      },
      "not_audited": [
        "remote hosts",
        "other users' private crontabs",
        "n8n"
      ]
    }
  }
}
```

## Git evidence
`git fetch delorenj main`: fetched 2335d9ef2562262bfd73fd8d151cb3e75f3cc789

`git diff --check`: exit 0. No .timer paths in port.

`git push --set-upstream delorenj HEAD:refs/heads/feat/holoc-26-current-main-20261118` and `git ls-remote delorenj refs/heads/feat/holoc-26-current-main-20261118`: first push/readback 0f8df936f20a5dbef793b674f7836edc40ff03b1; final evidence descendant pushed separately.

`git unpushed`: exit 1, both pre/post first push captures; global audit relevant lines:
```text
holoc26-unpushed-before.log
code/hermes-agent-holoc26-current-main/feat/holoc-26-current-main-20261118 1 commit(s) UNPUSHED
code/hermes-agent-holoc26/feat/holoc-26-outbound-audio-candybar NO UPSTREAM — nothing is tracking this branch
329 repo(s) hold work that exists only on this disk.
holoc26-unpushed-after.log
code/hermes-agent-holoc26/feat/holoc-26-outbound-audio-candybar NO UPSTREAM — nothing is tracking this branch
328 repo(s) hold work that exists only on this disk.
```
No unrelated audit findings were modified. No main integration authorized.

## Final post-evidence push audit
`git unpushed`: exit 1.
```text
code/hermes-agent-holoc26/feat/holoc-26-outbound-audio-candybar NO UPSTREAM — nothing is tracking this branch
328 repo(s) hold work that exists only on this disk.
```
Review worktree status empty; HEAD equals upstream. Recovery NO UPSTREAM remains intentionally unchanged; all unrelated findings preserved.
