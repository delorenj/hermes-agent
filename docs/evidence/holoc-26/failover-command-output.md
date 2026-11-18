# HOLOC-26 failover landing command evidence

Canonical test command (from isolated landing worktree):
```sh
HERMES_PYTHON=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/bin/python HOME=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/home HERMES_HOME=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/hermes-home HERMES_RUNTIME_DIR=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/runtime TMPDIR=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/bytecode scripts/run_tests.sh -j 2 tests/gateway/test_telegram_network.py tests/gateway/test_telegram_readonly_failover.py -q
```

## Actual full test output, exit 0
```text
▶ not activated — using HERMES_PYTHON: /home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/bin/python
▶ running per-file parallel test suite via run_tests_parallel.py
  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)
▶ pre-compiling bytecode cache
▶ launching test runner
Discovered 2 test files (~25 tests) under ['tests/gateway/test_telegram_network.py', 'tests/gateway/test_telegram_readonly_failover.py']; running with -j 2
[ 24.0% |     6/~25 | ✓19 | ✗ 0] ✓ tests/gateway/test_telegram_readonly_failover.py (19✓, 3.4s)
[100.0% |    25/~25 | ✓38 | ✗ 0] ✓ tests/gateway/test_telegram_network.py (19✓, 3.9s)

=== Summary: 2 files, 38 tests passed, 0 failed (100% complete) in 4.0s (2 workers) ===
  Durations cached to /home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/test_durations.json (2 files)

=== Per-file subprocess time distribution ===
  Files:   2
  Total subprocess CPU-wall: 7.3s  (runner wall: 4.0s, parallelism: 2x)
  P50: 3.93s  P90: 3.93s  P95: 3.93s  P99: 3.93s  Max: 3.93s
  <1s: 0 files (0%)  <2s: 0 files (0%)
  Top 10 slowest:
      3.93s  tests/gateway/test_telegram_network.py
      3.38s  tests/gateway/test_telegram_readonly_failover.py
```

## Actual import probe output, exit 0
```text
Actual landing source: TelegramAdapter, TelegramFallbackTransport, all seven gateway modules imported; disabled adapter constructed; no bot/network call.
```

## Recovery push and remote readback
```text
remote:
remote: Create a pull request for 'feat/holoc-26-outbound-audio-candybar' on GitHub by visiting:
remote:      https://github.com/delorenj/hermes-agent/pull/new/feat/holoc-26-outbound-audio-candybar
remote:
To github.com:delorenj/hermes-agent.git
 * [new branch]            feat/holoc-26-outbound-audio-candybar -> feat/holoc-26-outbound-audio-candybar
cfec39692c89a3d01f427eb7647851ae82092ad1	refs/heads/feat/holoc-26-outbound-audio-candybar
```

## Fresh fork fetch
```text
From github.com:delorenj/hermes-agent
 * branch                  main       -> FETCH_HEAD
```

## Exact reviewed-file match, whitespace, preserved remote refs
```text
WHITESPACE_EXIT=0
REVIEWED_MATCH_EXIT=0
f6c27c4afddc25e1d2196cafea33de8b94c21fdc	refs/heads/feat/holoc-26-current-main-20261118
cfec39692c89a3d01f427eb7647851ae82092ad1	refs/heads/feat/holoc-26-outbound-audio-candybar
7758ca610987556a4b72ac17b2ece395b91abcde	refs/tags/wip/holoc-22-outbound-audio-autostash
```

## Live read-only status/remotes
```text
live_head=d6100d8bda1af8c7f1ca121a7f99a29112c62045
live_branch=main
?? agents/hermes/pm/.scripts/momo-wip-lock.py
?? agents/hermes/pm/hermes
?? agents/hermes/pm/runtime/.gitignore
?? plugins/platforms/telegram/ops/hermes-outbound-audio-drain@.service
?? plugins/platforms/telegram/ops/hermes-outbound-audio-drain@.timer
?? plugins/platforms/telegram/ops/outbound-audio-drain.env.op
?? plugins/platforms/telegram/ops/outbound-audio-drain.sh
?? plugins/platforms/telegram/outbound_audio_drain.py
?? plugins/platforms/telegram/outbound_audio_provenance.py
?? plugins/platforms/telegram/outbound_audio_spool.py
delorenj	git@github.com:delorenj/hermes-agent.git (fetch)
delorenj	git@github.com:delorenj/hermes-agent.git (push)
origin	git@github.com:NousResearch/hermes-agent.git (fetch)
origin	git@github.com:NousResearch/hermes-agent.git (push)
```

## ENTIRE live main to landing code diff (no omissions)
```text
LIVE_ANCESTOR_EXIT=0
M	hermes_cli/runtime_state.py
A	hermes_cli/runtime_state_users.py
M	plugins/platforms/telegram/telegram_network.py
M	pm/install_states.py
M	pm/runtime.py
M	tests/conftest.py
A	tests/gateway/test_telegram_readonly_failover.py
A	tests/hermes_cli/test_runtime_state_users.py
M	tests/pm/test_install_states_gc.py
M	tests/pm/test_startup_recovery.py
M	website/docs/developer-guide/pm-audit-status.md
M	website/docs/reference/package-management.md
```

## Incoming live-to-landing commits
```text
ac128f6f37 fix(telegram): port read-only retrieval failover to current main
2335d9ef25 fix(pm): a Windows process that hides its start time is still read
76d6b3f2af fix(pm): one probe budget, fresh psutil processes, no settling on an empty table
059bf74c27 fix(pm): a process forked while the process table is read is read too
2f6da84e83 fix(pm): a set-aside name is one nothing has, and a put-back proves the move happened
2a4ba2eb17 fix(pm): record the set-aside target before the move, qualify what a probe cannot see
a4cbab6820 fix(pm): answer round two of the set-aside review
ce86d43dce fix(pm): answer the second-look review of the generation set-aside
b210a3658b fix(pm): close the scan-to-delete window in the generation collectors
43c7d23784 fix(pm): keep a dependency generation a live process still runs from
```

## Scratch-only execution wrapper and import probe (exact)

### bin/python
```text
#!/bin/sh
exec /var/tmp/holoc26-port-testenv-complete-20261118/bin/python -B /home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128/launch.py "$@"
```

### launch.py
```text
import importlib.util, os, pathlib, runpy, sys, tempfile
S=pathlib.Path('/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-failover-landing-20261118-161128')
os.environ.update(HOME=str(S/'home'), HERMES_HOME=str(S/'hermes-home'), PYTHONDONTWRITEBYTECODE='1', PYTHONPYCACHEPREFIX=str(S/'bytecode'))
os.environ.setdefault('TMPDIR',str(S/'tmp'))
os.environ['TMP']=os.environ['TEMP']=os.environ['TMPDIR']
tempfile.tempdir=os.environ['TMPDIR']
sys.dont_write_bytecode=True
sys.executable=str(S/'bin/python')
sys.path.insert(0,os.getcwd())
a=sys.argv[1:]
if a[:2]==['-m','compileall']:
    raise SystemExit(0)  # precompile is optional; don't fan out or write checkout caches
if a and a[0].endswith('scripts/run_tests_parallel.py'):
    sys.argv=a
    spec=importlib.util.spec_from_file_location('holoc26_runner',a[0])
    runner=importlib.util.module_from_spec(spec); sys.modules[spec.name]=runner; spec.loader.exec_module(runner)
    runner._runner_scratch_root=lambda: str(S/'tmp')
    runner._DURATIONS_FILE=str(S/'test_durations.json')
    raise SystemExit(runner.main())
if a[:1]==['-m']:
    sys.argv=[a[1],*a[2:]]; runpy.run_module(a[1],run_name='__main__',alter_sys=True)
elif a[:1]==['-c']:
    sys.argv=['-c',*a[2:]]; exec(compile(a[1],'<string>','exec'),{'__name__':'__main__'})
else:
    sys.argv=a; runpy.run_path(a[0],run_name='__main__')
```

### import-probe.py
```text
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.telegram_network import TelegramFallbackTransport
from gateway.config import PlatformConfig
import gateway.run_turn, gateway.run_notifications, gateway.run_voice, gateway.run_background
import gateway.session_cron, gateway.slash_commands, gateway.turn_context
adapter=TelegramAdapter(PlatformConfig(extra={}))
assert getattr(adapter,'_outbound_audio_spool',None) is None
print('Actual landing source: TelegramAdapter, TelegramFallbackTransport, all seven gateway modules imported; disabled adapter constructed; no bot/network call.')
```

## Required pull before first main push
```text
From github.com:delorenj/hermes-agent
 * branch                  main       -> FETCH_HEAD
Already up to date.
```

## Successful main push
```text
To github.com:delorenj/hermes-agent.git
   2335d9ef25..7e679d3fc7  HEAD -> main
```

## Exact main remote readback
```text
7e679d3fc7101a0092f5839edc4be1276347ead9	refs/heads/main
```

## Owned status and branch/upstream divergence
```text
!! .pytest-cache/
!! .pytest_cache/
feat/holoc-26-current-main-20261118 f6c27c4afddc25e1d2196cafea33de8b94c21fdc upstream=delorenj/feat/holoc-26-current-main-20261118
feat/holoc-26-outbound-audio-candybar cfec39692c89a3d01f427eb7647851ae82092ad1 upstream=delorenj/feat/holoc-26-outbound-audio-candybar
land/holoc26-failover-20261118-161128 7e679d3fc7101a0092f5839edc4be1276347ead9 upstream=delorenj/main
main 91b809b72cfbe781996e94935124e1bdf6e7f98b upstream=delorenj/main [behind 14]
landing HEAD vs delorenj/main (left=HEAD-only,right=remote-only): 0	0
origin/main absent locally; upstream NousResearch not fetched or changed
live cached HEAD vs origin/main (left=live-only,right=upstream-only): 26	7402
```

## Host scanner (exit 1)
```text
No matching HOLOC-26 entries.
328 repo(s) hold work that exists only on this disk.
```
Ignored caches in prior status were owned run artifacts, subsequently moved to profile scratch as disclosed.
