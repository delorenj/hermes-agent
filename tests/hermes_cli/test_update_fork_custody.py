"""Fork tweak #25, update custody: `hermes update` never moves the fork onto upstream or a release.

The 2026-11-24 incident: the Hermes Desktop app ran the stock update on this install. ``origin``
is NousResearch, so the checkout looked like an upstream install, the default ``stable`` channel
resolved a release commit, and the checkout was detached onto it with every fork change gone.

A branch that tracks a remote other than ``origin`` and carries commits upstream lacks is the
fork. Its one update target is that remote's published branch: fetched, deployed (a rebased fork
is reset, with a rescue ref, never ``--ff-only``), reported against upstream, and never detached.
"""
import argparse
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import main, source_releases, update_cmd
from hermes_cli import update_fork_custody as custody
from hermes_cli.subcommands.update import build_update_parser

GIT = ["git"]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()


def commit(root, name, text):
    (root / name).write_text(text)
    git(root, "add", name)
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", f"add {name}")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """upstream (NousResearch), a fork one fork-only commit above its base, and the install."""
    home, upstream, fork, install = (tmp_path / n for n in ("home", "upstream", "fork", "install"))
    for d in (home, upstream):
        d.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(install))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    git(upstream, "init", "-q", "-b", "main")
    base = commit(upstream, "base.txt", "base\n")
    git(tmp_path, "clone", "-q", str(upstream), str(fork))
    fork_tip = commit(fork, "fork-tweak.txt", "fork\n")
    upstream_new = commit(upstream, "upstream-new.txt", "upstream\n")  # the "release": fork lacks it

    git(tmp_path, "clone", "-q", str(fork), str(install))
    git(install, "remote", "rename", "origin", "delorenj")
    git(install, "remote", "add", "origin", str(upstream))
    git(install, "fetch", "-q", "origin")
    assert git(install, "config", "branch.main.remote") == "delorenj"

    monkeypatch.setattr(main, "PROJECT_ROOT", install)
    monkeypatch.setattr("hermes_cli.config.get_project_root", lambda: install)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", install)
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=main.cmd_update)
    opts = update_cmd._UpdateOptions(pre_update_version=None, gw_input_fn=None, assume_yes=True,
                                     keep_stash=False, switch_branch=False, discard_local_changes=False)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *_: opts)
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan", lambda *_: None)
    monkeypatch.setattr(main, "_run_pre_update_backup", lambda *_: None)
    monkeypatch.setattr(main, "_pause_windows_gateways_for_update", lambda: None)
    # origin = NousResearch makes the checkout look like a non-fork install: that is the incident.
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda **_: (False, ["git"], False))
    return SimpleNamespace(home=home, upstream=upstream, fork=fork, root=install, base=base,
                           fork_tip=fork_tip, upstream_new=upstream_new, parser=parser)


def run_update(w, *flags):
    update_cmd._cmd_update_impl(w.parser.parse_args(["update", "--yes", *flags]), False)


def run(git_cmd, args):  # update_cmd._git_run shape
    return update_cmd._git_run(git_cmd, args)


# --- detection -----------------------------------------------------------------------------


def test_detect_finds_the_fork_binding(world):
    found = custody.detect(GIT, "main", run)
    assert found == custody.ForkCustody(remote="delorenj", branch="main", fork_only=1)
    assert found.ref == "delorenj/main"
    assert custody.is_custody_ref(GIT, "main", "delorenj/main", run)
    assert not custody.is_custody_ref(GIT, "main", "origin/main", run)
    assert not custody.is_custody_ref(GIT, "main", world.upstream_new, run)  # a release sha


def test_detect_ignores_a_branch_that_tracks_origin(world):
    git(world.root, "config", "branch.main.remote", "origin")
    assert custody.detect(GIT, "main", run) is None
    assert not custody.is_custody_ref(GIT, "main", "origin/main", run)


def test_detect_ignores_a_fork_with_nothing_of_its_own(world):
    git(world.root, "reset", "-q", "--hard", world.base)
    git(world.root, "update-ref", "refs/remotes/origin/main", world.base)
    assert custody.detect(GIT, "main", run) is None


def test_detect_requires_the_local_branch(world):
    git(world.root, "checkout", "-q", "--detach")
    git(world.root, "branch", "-D", "main")
    assert custody.detect(GIT, "main", run) is None


def test_trailing_count_is_how_far_the_fork_is_behind_upstream(world):
    found = custody.detect(GIT, "main", run)
    assert custody.trailing_count(GIT, found, run) == 1
    git(world.root, "fetch", "-q", "origin")
    assert custody.trailing_count(GIT, found, run, target=world.upstream_new) == 0


# --- the incident, through the real update entry point -------------------------------------


def forbid_release_selection(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("custody must not consult the update channel or a release")
    monkeypatch.setattr(source_releases, "resolve_source_target", forbidden)
    from hermes_cli import update_cmd_check
    monkeypatch.setattr(update_cmd_check, "select_apply_target", forbidden)


def test_desktop_update_keeps_the_fork_checkout(world, monkeypatch, capsys):
    """The checkout is detached upstream-ward (as the Desktop update left it); update re-attaches the fork."""
    forbid_release_selection(monkeypatch)
    published = commit(world.fork, "fork-two.txt", "two\n")  # the fork publishes a new commit
    completed = []
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: completed.append(dict(req)))

    run_update(world)

    out = capsys.readouterr().out
    assert git(world.root, "branch", "--show-current") == "main", "never left detached"
    assert git(world.root, "rev-parse", "HEAD") == published
    assert not (world.root / "upstream-new.txt").exists(), "no upstream or release content"
    assert (world.root / "fork-tweak.txt").exists()
    assert "Fork custody: 'main' tracks delorenj/main (1 fork-only commit(s))" in out
    assert "The fork trails origin/main by 1 commit(s)" in out
    assert "/hermes-sync" in out
    assert "Update channel" not in out and "Release:" not in out
    assert completed and completed[0]["branch"] == "main"
    assert completed[0].get("expected_sha") in (None, "", published), "the only pin is the fork's own tip"
    assert completed[0].get("release_tag") is None


def test_update_from_a_detached_checkout_reattaches_main_on_the_fork(world, monkeypatch, capsys):
    forbid_release_selection(monkeypatch)
    published = commit(world.fork, "fork-two.txt", "two\n")
    git(world.root, "checkout", "-q", "--detach", world.base)
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)

    run_update(world)

    assert git(world.root, "branch", "--show-current") == "main"
    assert git(world.root, "rev-parse", "HEAD") == published
    assert not (world.root / "upstream-new.txt").exists()


def test_a_rebased_fork_is_reset_with_a_rescue_ref_not_refused(world, monkeypatch, capsys):
    """/hermes-sync force-pushes the fork: ff-only can't follow, the reset must, and keep the old tip."""
    forbid_release_selection(monkeypatch)
    old_tip = world.fork_tip
    git(world.fork, "fetch", "-q", str(world.upstream), "main")
    git(world.fork, "reset", "-q", "--hard", "FETCH_HEAD")                  # rebase onto the new upstream
    rebased = commit(world.fork, "fork-tweak.txt", "fork\n")                # same tweak, new sha
    assert rebased != old_tip
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)

    run_update(world)

    out = capsys.readouterr().out
    assert git(world.root, "branch", "--show-current") == "main"
    assert git(world.root, "rev-parse", "HEAD") == rebased
    assert (world.root / "upstream-new.txt").exists(), "the fork's own rebased main carries it"
    rescues = git(world.root, "for-each-ref", "--format=%(refname) %(objectname)",
                  "refs/hermes-update-backups/").splitlines()
    assert [r.split()[1] for r in rescues] == [old_tip]
    assert "diverged from delorenj/main" in out and "not on delorenj/main" in out
    assert "origin/main" not in out.split("diverged from", 1)[1].split("\n", 1)[0]


def test_update_never_syncs_or_pushes_upstream_into_the_fork(world, monkeypatch):
    forbid_release_selection(monkeypatch)
    for name in ("_sync_with_upstream_if_needed", "_push_synced_fork", "_sync_fork_with_upstream"):
        monkeypatch.setattr(update_cmd, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} ran under custody"))
        monkeypatch.setattr(update_cmd._m(), name, lambda *a, _n=name, **k: pytest.fail(f"{_n} ran under custody"),
                            raising=False)
    # An already-current fork on a fork-flagged checkout used to reach the upstream sync with count 0.
    monkeypatch.setattr(update_cmd, "_prepare_git_command", lambda **_: (False, ["git"], True))
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)

    run_update(world)

    assert git(world.root, "rev-parse", "HEAD") == world.fork_tip
    assert not (world.root / "upstream-new.txt").exists()


def test_an_explicit_channel_does_not_pull_the_fork_onto_a_release(world, monkeypatch):
    forbid_release_selection(monkeypatch)
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)

    run_update(world, "--channel", "stable")

    assert git(world.root, "branch", "--show-current") == "main"
    assert git(world.root, "rev-parse", "HEAD") == world.fork_tip


def test_explicit_branch_main_is_still_the_fork(world, monkeypatch):
    forbid_release_selection(monkeypatch)
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)

    run_update(world, "--branch", "main")

    assert git(world.root, "rev-parse", "HEAD") == world.fork_tip
    assert not (world.root / "upstream-new.txt").exists()


def test_an_origin_tracking_install_still_follows_its_release(world, monkeypatch):
    """Custody is only for the fork: an ordinary upstream-tracking branch keeps the stock behaviour."""
    git(world.root, "config", "branch.main.remote", "origin")
    called = []
    from hermes_cli import update_cmd_check
    real = update_cmd_check.select_apply_target
    monkeypatch.setattr(update_cmd_check, "select_apply_target",
                        lambda *a, **k: called.append(1) or real(*a, **k))
    monkeypatch.setattr(update_cmd, "_complete_source_update", lambda req: None)
    try:
        run_update(world)
    except SystemExit:
        pass  # the stock path may refuse in this fixture; it must have been taken
    assert called


# --- the pull primitives -------------------------------------------------------------------


def test_move_checkout_never_detaches_for_the_fork_ref(world, monkeypatch):
    commit(world.fork, "fork-two.txt", "two\n")
    git(world.root, "fetch", "-q", "delorenj")
    target = git(world.root, "rev-parse", "delorenj/main")
    pre = git(world.root, "rev-parse", "HEAD")
    update_cmd._move_checkout_to(GIT, "main", "delorenj/main", target, pre)
    assert git(world.root, "branch", "--show-current") == "main"
    assert git(world.root, "rev-parse", "HEAD") == target


def test_move_checkout_still_detaches_for_a_release_ref(world):
    """The stock release behaviour is untouched for non-fork targets."""
    git(world.root, "config", "branch.main.remote", "origin")
    pre = git(world.root, "rev-parse", "HEAD")
    update_cmd._move_checkout_to(GIT, "main", world.upstream_new, world.upstream_new, pre)
    assert git(world.root, "branch", "--show-current") == ""
    assert git(world.root, "rev-parse", "HEAD") == world.upstream_new


def test_report_says_where_to_fix_the_trail(world, capsys):
    found = custody.detect(GIT, "main", run)
    assert custody.report_trailing(GIT, found, run) == 1
    out = capsys.readouterr().out
    assert "trails origin/main by 1 commit(s)" in out and "/hermes-sync" in out
    git(world.root, "update-ref", "refs/remotes/origin/main", world.base)
    custody.report_trailing(GIT, found, run)
    assert "not behind origin/main" in capsys.readouterr().out
