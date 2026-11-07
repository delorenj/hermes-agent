"""Opt-in personal skill writes with a required durable Skillex checkpoint.

The marker declares data, never a command. The operator-installed bridge owns
identity, repository and catalog validation; the tool retains its approval and
security gates and confines writes to that identity's learned source.
"""

import contextvars
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class PersonalSource:
    profile: Path
    source: Path


_active_source = contextvars.ContextVar("skill_manage_personal_source", default=None)
_BRIDGE_TIMEOUT = 300


class PersonalCheckpointError(ValueError):
    """A bound personal write cannot be validated or durably saved."""


def active_personal_source():
    return _active_source.get()


def _load_source():
    profile = get_hermes_home().resolve()
    marker = profile / ".skillex-personal-source.json"
    if not marker.exists() and not marker.is_symlink():
        return None
    if marker.is_symlink() or not marker.is_file():
        raise PersonalCheckpointError("personal source binding must be a regular JSON file")
    try:
        binding = json.loads(marker.read_text(encoding="utf-8-sig"))
        if not isinstance(binding, dict) or type(binding.get("schema")) is not int or binding["schema"] != 1:
            raise ValueError("unsupported personal source binding schema")
        identity = binding.get("identity")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", identity):
            raise ValueError("invalid personal source identity")
        paths = {key: Path(binding[key]) for key in ("source", "repository", "definition")}
        if not all(path.is_absolute() for path in paths.values()):
            raise ValueError("personal source paths must be absolute")
        repo = (Path.home() / ".agents").resolve()
        owner = repo / "workforce" / identity
        if (paths["repository"].resolve() != repo
                or paths["source"].resolve() != owner / "learned-skills"
                or paths["definition"].resolve() != owner / "agent.yaml"):
            raise ValueError("personal source paths do not match the declared workforce owner")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise PersonalCheckpointError(f"invalid personal source binding: {exc}") from exc
    return PersonalSource(profile, paths["source"].resolve())


def _bridge(binding, command, name=None):
    from tools.environments.local import served_profile_child_env
    argv = [str(Path.home() / ".local/bin/skillex-personal-checkpoint"),
            command, "--profile", str(binding.profile)]
    if name is not None:
        argv.extend(["--name", name])
    try:
        with subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # detached-spawn: ok — awaited with a process-group-killing timeout below.
                text=True, start_new_session=(os.name == "posix"),
                env=served_profile_child_env(target_home=binding.profile)) as process:
            try:
                stdout, _stderr = process.communicate(timeout=_BRIDGE_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Git/SSH descendants must not keep pushing after a failed tool result.
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only branch.
                    except ProcessLookupError:
                        pass  # The whole group already exited.
                else:
                    process.kill()
                process.wait(timeout=10)
                raise
            returncode = process.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PersonalCheckpointError(f"personal source {command} bridge failed: {exc}") from exc
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise PersonalCheckpointError(f"personal source {command} bridge returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise PersonalCheckpointError(f"personal source {command} bridge returned invalid result")
    if returncode or result.get("ok") is not True:
        raise PersonalCheckpointError(str(result.get("error") or f"{command} exited {returncode}"))
    if command == "checkpoint" and (result.get("pushed") is not True or not result.get("commit")):
        raise PersonalCheckpointError("personal source checkpoint did not confirm a pushed commit")
    return result


def require_personal_target(target):
    binding = active_personal_source()
    if binding and not Path(target).resolve().is_relative_to(binding.source):
        raise PersonalCheckpointError(
            f"Refusing to mutate selected shared/project skill at {target}; "
            f"this profile may author only its personal learned source {binding.source}.")


def check_personal_name(name, skill_dir, *, new_name=True):
    """Reject aliases that could shadow another selected skill before writing."""
    binding = active_personal_source()
    if binding is None:
        return
    from agent.skill_utils import get_skill_search_roots, iter_skill_index_files, parse_frontmatter
    if not isinstance(name, str) or not name.strip():
        raise PersonalCheckpointError("personal skill frontmatter must declare a nonempty string name")
    if new_name:
        _bridge(binding, "check", name)
    own = Path(skill_dir).resolve()
    roots = [root for _tier, root in get_skill_search_roots()] + [binding.source]
    for root in roots:
        for skill_md in iter_skill_index_files(root, "SKILL.md"):
            if skill_md.parent.resolve() == own:
                continue
            fm, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8-sig"))
            if name in (skill_md.parent.name, fm.get("name")):
                raise PersonalCheckpointError(f"Personal skill name '{name}' collides with {skill_md}")


def personal_checkpoint(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return run_personal_write(lambda: fn(*args, **kwargs))
    return wrapped


def run_personal_write(callback):
    """One checkpoint per successful call, including a whole atomic operations batch.

    Nested flat operations share the batch's binding. Checkpoint errors are outside
    the batch rollback path: applied content must remain available for recovery.
    """
    if active_personal_source() is not None:
        try:
            return callback()
        except PersonalCheckpointError as exc:
            return json.dumps({"success": False, "error": str(exc)})
    try:
        binding = _load_source()
        if binding is None:
            return callback()
        _bridge(binding, "check")
    except (PersonalCheckpointError, OSError, RuntimeError) as exc:
        return json.dumps({"success": False, "error": f"Personal source validation failed: {exc}"})
    token = _active_source.set(binding)
    try:
        try:
            raw = callback()
        except PersonalCheckpointError as exc:
            return json.dumps({"success": False, "error": str(exc)})
        result = json.loads(raw)
        if not result.get("success") or result.get("staged"):
            return raw
        try:
            result["checkpoint"] = _bridge(binding, "checkpoint")
        except (PersonalCheckpointError, OSError, RuntimeError) as exc:
            result.update(success=False, content_retained=True,
                          error=f"Durable personal skill checkpoint failed: {exc}. Applied content retained; retry checkpoint before claiming it is saved.")
            if "message" in result:
                result["mutation_message"] = result.pop("message")
        return json.dumps(result, ensure_ascii=False)
    finally:
        _active_source.reset(token)
