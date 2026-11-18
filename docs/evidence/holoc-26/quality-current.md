# Independent QUALITY review — standalone Telegram retrieval failover

**QUALITY PASS — standalone commit only. No actionable findings.**

Reviewed `4a34f79f82aa9f60b690d892753c08d5636381e2` against parent `2335d9ef2562262bfd73fd8d151cb3e75f3cc789`, independently of the separate SPEC verdict. This is not merge approval, mirror review, audio acceptance, or full HOLOC-26 acceptance. Other ticket gates remain outside this review.

## Scope and code inspected

- Actual two-file commit diff, root/plugins/tests AGENTS instructions, existing network tests, adapter transport wiring, and installed HTTPX/PTB request execution.
- `plugins/platforms/telegram/telegram_network.py:143–209`: attempt ordering, seed insertion, response-body read boundary, retry exception types, sticky updates, and pool reset.
- `plugins/platforms/telegram/telegram_network.py:313–346`: request cloning and exact host/method/path/buffered-body classification.
- `tests/gateway/test_telegram_readonly_failover.py`: all added tests.
- The provided export's transport source matches the reviewed commit exactly (SHA-256 `1c144f1a70b3ced652b99d660624795bbc77ebfc2fbba2dddee62263021526dc`). The repository's current HEAD is later (`f6c27c4afddc25e1d2196cafea33de8b94c21fdc`); the primary suite and new probe used the exact failover export, not that later HEAD.

## Execution evidence

Interpreter: `/var/tmp/holoc26-port-testenv-complete-20261118/bin/python` (reused; no installs).

1. Canonical runner from exact export:
   `HERMES_PYTHON=… PYTHONDONTWRITEBYTECODE=1 TMPDIR=<profile scratch> scripts/run_tests.sh tests/gateway/test_telegram_network.py tests/gateway/test_telegram_readonly_failover.py`
   **38 passed, 0 failed**, 2 files, exit 0. The export is not a Git checkout, so the runner printed a non-fatal `fatal: not a git repository` during precompilation; both test subprocesses completed successfully.
2. Supplemental existing pool-release suite, from the current repository checkout: **3 passed, 0 failed**, exit 0. This supplemental run is not represented as exact-commit suite evidence.
3. Private loopback probe through the actual `TelegramFallbackTransport` and real HTTPX/HTTPCore socket transport, with only destination routing redirected to a local fixture:
   - POST `/botTOKEN/getFile` consumed `file_id=opaque` on the first peer.
   - First peer returned 200 headers plus a partial body and stalled; the real socket read timed out.
   - Independent seed peer received the identical POST body and returned `complete`.
   - Actual result: status **200**, body **`complete`**, peer sequence **149.154.166.110 → 149.154.167.220**; assertions passed.
4. Additional transport-fixture probe: complete **429** body `{"retry_after":3}` preserved, with exactly **one** attempt; assertions passed.

## Assessment

- Buffered ordinary PTB form POST bodies survive cloning/reuse across a real partial-response timeout; the retry discards partial response bytes rather than appending them.
- Added ambiguous-read retries are restricted to getFile or file GET on the Telegram host; sends and other Bot API operations do not acquire read-error replay. Streaming request bodies without buffered content are excluded.
- Seed expansion supplies an independent path when discovery contains only the failing seed; normal complete responses are returned rather than status-retried.
- New tests exercise read-header failure, partial body failure, unsafe operations, unbuffered requests, exhaustion, SNI/Host preservation, and failed pool release. Existing connection-error retries remain pre-existing behavior, not a newly widened send retry rule.

## Limits and changes

No source repairs, commits, branch changes, live-service operations, board writes, or secrets changes. Git status remained clean after review. Canonical runner may create ignored test/bytecode/duration caches; no tracked source changes were made. Private probe/report files were created under the active profile scratch directory only.

Durable probe: `/home/delorenj/.hermes/profiles/holocene-pm/cache/scratch/holoc26-quality-probe.py`.

No live Telegram traffic, public-network canary, TLS endpoint availability claim, full mirror review, or whole-ticket acceptance was performed. The local real-socket probe uses HTTP loopback to test request consumption and body timeout/retry behavior; TLS SNI preservation is covered by the bounded tests, not a real Telegram TLS exchange.
