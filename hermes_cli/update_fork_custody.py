"""Fork custody: an update never moves a fork's main onto upstream or a release.

Fork tweak #25 (see the /hermes-sync skill). On 2026-11-24 the Hermes Desktop app ran the
stock `hermes update` against this install. ``origin`` is NousResearch, so the checkout looked
like a plain upstream install: the default ``stable`` channel resolved a release commit and
``_move_checkout_to`` ran ``checkout --detach`` onto it, dropping every fork change.

The custody rule is keyed on what the checkout *is*, not on what ``origin`` looks like: when the
update branch tracks a remote other than ``origin`` (the fork) and carries commits upstream does
not have, the only legitimate update target is that remote's published branch. It is fetched and
deployed (a rebased fork needs a reset, not ``--ff-only``; the diverged-checkout path already
parks a rescue ref first), the channel/release selection and the upstream sync/push are skipped,
and the update says how far the fork trails upstream and where to fix that (/hermes-sync).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

#: ``run(git_cmd, args)`` -> object with ``returncode`` / ``stdout`` (update_cmd._git_run).
GitRun = Callable[..., object]

UPSTREAM_REMOTE = "origin"


@dataclass(frozen=True)
class ForkCustody:
    """The fork remote an update branch is bound to."""

    remote: str
    branch: str
    #: Commits on the local branch that upstream's branch lacks (``None`` when unreadable).
    fork_only: Optional[int]

    @property
    def ref(self) -> str:
        return f"{self.remote}/{self.branch}"


def _out(result) -> str:
    return (getattr(result, "stdout", "") or "").strip()


def tracked_remote(git_cmd, branch: str, run: GitRun) -> str:
    """The remote ``branch`` tracks (``branch.<b>.remote``), or ``""``."""
    return _out(run(git_cmd, ["config", "--get", f"branch.{branch}.remote"]))


def is_custody_ref(git_cmd, branch: str, ref: str, run: GitRun) -> bool:
    """True when *ref* is ``<fork remote>/<branch>`` for the remote *branch* tracks."""
    remote = tracked_remote(git_cmd, branch, run)
    return bool(remote) and remote != UPSTREAM_REMOTE and ref == f"{remote}/{branch}"


def detect(git_cmd, branch: str, run: GitRun) -> Optional[ForkCustody]:
    """Custody for *branch*, or ``None`` when it is an ordinary upstream-tracking branch.

    Applies when the local branch exists, tracks a remote other than ``origin``, and either
    carries commits ``origin/<branch>`` lacks or that count cannot be read (a tracked fork is
    itself the signal; failing open would be the 2026-11-24 detach).
    """
    remote = tracked_remote(git_cmd, branch, run)
    if not remote or remote == UPSTREAM_REMOTE:
        return None
    if getattr(run(git_cmd, ["rev-parse", "-q", "--verify", f"refs/heads/{branch}"]), "returncode", 1) != 0:
        return None
    count = run(git_cmd, ["rev-list", "--count", f"{UPSTREAM_REMOTE}/{branch}..refs/heads/{branch}"])
    fork_only: Optional[int] = None
    if getattr(count, "returncode", 1) == 0:
        try:
            fork_only = int(_out(count))
        except ValueError:
            fork_only = None
    if fork_only == 0:
        return None  # identical to (or behind) upstream: nothing of the fork's to protect
    return ForkCustody(remote=remote, branch=branch, fork_only=fork_only)


def trailing_count(git_cmd, custody: ForkCustody, run: GitRun, *, target: Optional[str] = None) -> Optional[int]:
    """Upstream commits the fork's published branch lacks (``None`` when unreadable)."""
    result = run(git_cmd, ["rev-list", "--count", f"{target or custody.ref}..{UPSTREAM_REMOTE}/{custody.branch}"])
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        return int(_out(result))
    except ValueError:
        return None


def announce(custody: ForkCustody, *, channel_note: str = "") -> None:
    """The custody header printed before the fetch."""
    only = "" if custody.fork_only is None else f" ({custody.fork_only} fork-only commit(s))"
    print(f"→ Fork custody: '{custody.branch}' tracks {custody.ref}{only}.")
    print(f"  Updating to the fork's published {custody.branch}; upstream releases and channels are not applied.{channel_note}")


def report_trailing(git_cmd, custody: ForkCustody, run: GitRun) -> Optional[int]:
    """Print how far the fork trails upstream and where to fix it. Returns the count."""
    behind = trailing_count(git_cmd, custody, run)
    if behind is None:
        print(f"  (could not compare the fork with {UPSTREAM_REMOTE}/{custody.branch})")
    elif behind == 0:
        print(f"  ✓ The fork is not behind {UPSTREAM_REMOTE}/{custody.branch}.")
    else:
        print(f"  ⚠ The fork trails {UPSTREAM_REMOTE}/{custody.branch} by {behind} commit(s).")
        print("    Bring in upstream with /hermes-sync in an agent session (rebase onto upstream, "
              "tests, publish), not with `hermes update`.")
    return behind
