#!/usr/bin/env python3
"""Migrate conventional repo Hermes agents to inherited profile config.

Run from a repository root, or pass the repository path explicitly:

    scripts/migrate-repo-agents-inherited-config.py ~/code/drumjangler
    scripts/migrate-repo-agents-inherited-config.py --apply ~/code/drumjangler

The migrator intentionally handles only the conventional PM and
scrum-master/Ticket Sentinel roles:

    agents/hermes/pm              -> <repo>-pm
    agents/hermes/scrum-master    -> <repo>-scrum-master

It does not recurse into ad hoc nested profiles.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised only on broken envs
    raise SystemExit("PyYAML is required to run this migration script.") from exc


DEFAULT_HERMES_BIN = str(Path.home() / ".hermes" / "hermes-agent" / ".venv" / "bin" / "hermes")
ROLE_ORDER = ("pm", "scrum-master")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class RolePlan:
    role: str
    role_dir: Path
    runtime_dir: Path
    profile_name: str
    profile_home: Path
    agent_id: str


@dataclass
class Context:
    repo_root: Path
    repo_slug: str
    fleet_home: Path
    apply: bool
    restart: bool
    prune_model: bool
    systemd_user_dir: Path
    changed_files: set[Path]
    modified_units: set[str]


def info(message: str) -> None:
    print(message)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            tmp.chmod(mode)
        elif path.exists():
            tmp.chmod(stat.S_IMODE(path.stat().st_mode))
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def write_if_changed(ctx: Context, path: Path, text: str, *, mode: int | None = None) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    prefix = "PATCH" if path.exists() else "CREATE"
    info(f"  {prefix} {path}")
    ctx.changed_files.add(path)
    if ctx.apply:
        atomic_write_text(path, text, mode=mode)
    return True


def scalar_from_yaml_line(line: str) -> str:
    _, _, value = line.partition(":")
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def set_top_level_scalar(ctx: Context, path: Path, key: str, value: str) -> None:
    if not path.exists():
        write_if_changed(ctx, path, f"{key}: {value}\n")
        return

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}:"):
            newline = "\n" if line.endswith("\n") else ""
            replacement = f"{key}: {value}{newline}"
            if line != replacement:
                changed = True
            new_lines.append(replacement)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = f"{new_lines[-1]}\n"
        new_lines.append(f"{key}: {value}\n")
        changed = True
    if changed:
        write_if_changed(ctx, path, "".join(new_lines))


def remove_top_level_block(ctx: Context, path: Path, key: str) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines: list[str] = []
    removing = False
    changed = False
    for line in lines:
        is_top_level = bool(line.strip()) and not line.startswith((" ", "\t"))
        if is_top_level and line.startswith(f"{key}:"):
            removing = True
            changed = True
            continue
        if removing and is_top_level:
            removing = False
        if not removing:
            new_lines.append(line)
    if changed:
        info(f"  PRUNE {key} override from {path}")
        write_if_changed(ctx, path, "".join(new_lines))


def normalize_repo_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    if not slug or not PROFILE_RE.match(f"{slug}-pm"):
        raise SystemExit(f"Cannot derive a valid profile name from repository name {name!r}.")
    return slug


def discover_repo_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if root.is_file():
        root = root.parent
    if not root.exists():
        raise SystemExit(f"Repository path does not exist: {root}")
    return root


def read_role_agent_id(role_dir: Path, fallback: str) -> str:
    role_yaml = load_yaml(role_dir / "role.yaml")
    agent_id = role_yaml.get("agent_id")
    return str(agent_id).strip() if agent_id else fallback


def role_profile_name(repo_slug: str, role: str) -> str:
    suffix = "pm" if role == "pm" else "scrum-master"
    return f"{repo_slug}-{suffix}"


def discover_roles(ctx: Context) -> list[RolePlan]:
    roles: list[RolePlan] = []
    for role in ROLE_ORDER:
        role_dir = ctx.repo_root / "agents" / "hermes" / role
        runtime_dir = role_dir / "runtime"
        if not role_dir.exists():
            info(f"  SKIP  agents/hermes/{role} does not exist")
            continue
        if not runtime_dir.exists():
            info(f"  SKIP  {runtime_dir} does not exist")
            continue
        profile_name = role_profile_name(ctx.repo_slug, role)
        agent_id = read_role_agent_id(role_dir, profile_name)
        roles.append(
            RolePlan(
                role=role,
                role_dir=role_dir,
                runtime_dir=runtime_dir,
                profile_name=profile_name,
                profile_home=ctx.fleet_home / "profiles" / profile_name,
                agent_id=agent_id,
            )
        )
    return roles


def update_profile_meta(ctx: Context, role: RolePlan) -> None:
    path = role.runtime_dir / "profile.yaml"
    data = load_yaml(path)
    config = data.get("config")
    if not isinstance(config, dict):
        config = {}
    config["inherit_from"] = "default"
    config["save_mode"] = "delta"
    data["config"] = config
    write_if_changed(ctx, path, dump_yaml(data))


def prune_runtime_config(ctx: Context, role: RolePlan) -> None:
    if not ctx.prune_model:
        return
    path = role.runtime_dir / "config.yaml"
    remove_top_level_block(ctx, path, "model")


def ensure_profile_symlink(ctx: Context, role: RolePlan) -> None:
    target = role.runtime_dir
    link = role.profile_home
    if link.exists() or link.is_symlink():
        try:
            existing = link.resolve(strict=True)
            expected = target.resolve(strict=True)
        except FileNotFoundError:
            existing = link.resolve()
            expected = target.resolve()
        if existing == expected:
            info(f"  OK    {link} -> {target}")
            return
        raise SystemExit(f"Refusing to replace existing profile path {link} -> {existing}")

    info(f"  LINK  {link} -> {target}")
    ctx.changed_files.add(link)
    if ctx.apply:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)


def ensure_alias(ctx: Context, role: RolePlan) -> None:
    alias_path = Path.home() / ".local" / "bin" / role.profile_name
    text = f'#!/bin/sh\nexec hermes -p {role.profile_name} "$@"\n'
    write_if_changed(ctx, alias_path, text, mode=0o755)


def update_role_yaml(ctx: Context, role: RolePlan) -> None:
    path = role.role_dir / "role.yaml"
    if not path.exists():
        return
    data = load_yaml(path)
    if data.get("profile") == role.profile_name:
        return
    set_top_level_scalar(ctx, path, "profile", role.profile_name)


def render_launcher(role: RolePlan) -> str:
    return f'''#!/usr/bin/env bash
# Launcher for {role.agent_id}. Resolves HERMES_HOME through the fleet profile
# when available, then falls back to the local runtime submodule.

set -euo pipefail

ROLE_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_HOME="$ROLE_DIR/runtime"

FLEET_ENV="${{HERMES_FLEET_ENV:-$HOME/.hermes/fleet.env}}"
if [[ -f "$FLEET_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$FLEET_ENV"
fi

CONFIG="${{HERMES_TEMPLATE_CONFIG:-${{XDG_CONFIG_HOME:-$HOME/.config}}/hermes-agent-template/config.toml}}"
config_get() {{
  local key="$1" def="${{2:-}}"
  if [[ ! -f "$CONFIG" ]] || ! command -v python3 >/dev/null 2>&1; then
    printf '%s' "$def"; return 0
  fi
  python3 - "$CONFIG" "$key" "$def" <<'PYEOF' || printf '%s' "$def"
import os
import sys
try:
    import tomllib
except ModuleNotFoundError:
    print(sys.argv[3], end=""); sys.exit(0)
try:
    cur = tomllib.load(open(sys.argv[1], "rb"))
except Exception:
    print(sys.argv[3], end=""); sys.exit(0)
for part in sys.argv[2].split("."):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print(sys.argv[3], end=""); sys.exit(0)
print(os.path.expanduser(str(cur)), end="")
PYEOF
}}

FLEET_HOME="${{HERMES_FLEET_HOME:-$HOME/.hermes}}"
PROFILE_NAME="${{HERMES_PROFILE_NAME:-{role.profile_name}}}"
PROFILE_HOME="$FLEET_HOME/profiles/$PROFILE_NAME"
if [[ -d "$PROFILE_HOME" ]]; then
  HERMES_HOME="$PROFILE_HOME"
else
  HERMES_HOME="$RUNTIME_HOME"
fi

HERMES_BIN="${{HERMES_BIN:-${{HERMES_FLEET_BIN:-}}}}"
if [[ -z "$HERMES_BIN" ]]; then
  HERMES_BIN="$(config_get fleet.hermes_bin "")"
fi
HERMES_BIN="${{HERMES_BIN:-{DEFAULT_HERMES_BIN}}}"
HERMES_OAUTH_FILE="${{HERMES_OAUTH_FILE:-${{HERMES_FLEET_OAUTH_FILE:-$(config_get fleet.oauth_file "$HOME/.hermes/auth.json")}}}}"
CODEX_HOME="${{CODEX_HOME:-${{HERMES_FLEET_CODEX_HOME:-$(config_get fleet.codex_home "$HOME/.codex")}}}}"

if [[ ! -d "$RUNTIME_HOME" ]]; then
  echo "hermes: runtime submodule not initialized at $RUNTIME_HOME" >&2
  echo "  fix: git submodule update --init --recursive" >&2
  exit 1
fi

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "hermes: binary not executable at $HERMES_BIN" >&2
  echo "  set HERMES_BIN or HERMES_FLEET_BIN (in $FLEET_ENV) to the shared Hermes binary." >&2
  exit 1
fi

exec env HERMES_HOME="$HERMES_HOME" HERMES_FLEET_ENV="$FLEET_ENV" \\
  HERMES_OAUTH_FILE="$HERMES_OAUTH_FILE" CODEX_HOME="$CODEX_HOME" \\
  "$HERMES_BIN" "$@"
'''


def update_launcher(ctx: Context, role: RolePlan) -> None:
    path = role.role_dir / "hermes"
    write_if_changed(ctx, path, render_launcher(role), mode=0o755)


def expected_systemd_units(role: RolePlan) -> set[str]:
    """Return conventional systemd units owned by this repo role.

    Some legacy role manifests have stale ``agent_id`` values from another
    repo. Using those as selectors can patch unrelated services, so migration
    only updates units named for the profile this script is currently repairing.
    Split-brain unit names need a deliberate manual reconciliation.
    """
    return {
        f"hermes-{role.profile_name}-consumer.service",
        f"hermes-{role.profile_name}-gateway.service",
        f"hermes-{role.profile_name}-continuous-ticket-sentinel.service",
    }


def patch_systemd_units(ctx: Context, role: RolePlan) -> None:
    if not ctx.systemd_user_dir.exists():
        return
    expected_units = expected_systemd_units(role)
    for unit in sorted(ctx.systemd_user_dir.glob("hermes-*.service")):
        if unit.name not in expected_units:
            continue
        text = unit.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        changed = False
        new_lines: list[str] = []
        for line in lines:
            if line.startswith("Environment=HERMES_HOME="):
                newline = "\n" if line.endswith("\n") else ""
                replacement = f"Environment=HERMES_HOME={role.profile_home}{newline}"
                if replacement != line:
                    line = replacement
                    changed = True
            new_lines.append(line)
        if not changed:
            continue
        info(f"  PATCH {unit}")
        ctx.changed_files.add(unit)
        ctx.modified_units.add(unit.name)
        if ctx.apply:
            atomic_write_text(unit, "".join(new_lines))


def update_registry(ctx: Context, role: RolePlan) -> None:
    path = ctx.fleet_home / "agents-registry.yaml"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    output = list(lines)

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not re.match(r"^  [^#\s][^:]*:\s*$", line):
            idx += 1
            continue
        start = idx
        idx += 1
        while idx < len(lines) and not re.match(r"^  [^#\s][^:]*:\s*$", lines[idx]):
            idx += 1
        end = idx
        block = lines[start:end]

        block_role = None
        block_project = None
        profile_line_index = None
        for offset, block_line in enumerate(block):
            if block_line.startswith("    role:"):
                block_role = scalar_from_yaml_line(block_line)
            elif block_line.startswith("    project_path:"):
                block_project = scalar_from_yaml_line(block_line)
            elif block_line.startswith("    profile_name:"):
                profile_line_index = start + offset

        if block_role != role.role:
            continue
        if not block_project:
            continue
        if Path(block_project).expanduser() != ctx.repo_root:
            continue

        replacement = f"    profile_name: {role.profile_name}\n"
        if profile_line_index is not None:
            if output[profile_line_index] != replacement:
                output[profile_line_index] = replacement
                changed = True
        else:
            insert_at = start + 1
            output.insert(insert_at, replacement)
            lines.insert(insert_at, replacement)
            idx += 1
            changed = True

    if changed:
        write_if_changed(ctx, path, "".join(output))


def restart_modified_units(ctx: Context) -> None:
    if not ctx.apply or not ctx.modified_units:
        return
    if not ctx.restart:
        info("  SKIP  systemd restart (--no-restart)")
        return

    systemctl = shutil.which("systemctl")
    if not systemctl:
        info("  WARN  systemctl not found; reload/restart skipped")
        return

    env = os.environ.copy()
    get_uid = getattr(os, "get" + "uid", None)
    if "XDG_RUNTIME_DIR" not in env and get_uid is not None:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{get_uid()}"
    units = sorted(ctx.modified_units)
    info("  RUN   systemctl --user daemon-reload")
    try:
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True, env=env)
        info(f"  RUN   systemctl --user try-restart {' '.join(units)}")
        subprocess.run([systemctl, "--user", "try-restart", *units], check=True, env=env)
    except subprocess.CalledProcessError as exc:
        info(f"  WARN  systemd reload/restart failed: {exc}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate conventional repo PM and scrum-master Hermes agents to inherited profile config."
    )
    parser.add_argument("repo", nargs="?", default=".", help="Repository root to migrate. Defaults to cwd.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script only prints a dry-run plan.",
    )
    parser.add_argument(
        "--fleet-home",
        default=os.environ.get("HERMES_FLEET_HOME", str(Path.home() / ".hermes")),
        help="Fleet Hermes root containing config.yaml and profiles/. Defaults to ~/.hermes.",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not daemon-reload or try-restart modified user systemd units after --apply.",
    )
    parser.add_argument(
        "--keep-model",
        action="store_true",
        help="Keep local model blocks instead of pruning them so model.default inherits from the fleet default.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = discover_repo_root(Path(args.repo))
    repo_slug = normalize_repo_slug(repo_root.name)
    ctx = Context(
        repo_root=repo_root,
        repo_slug=repo_slug,
        fleet_home=Path(args.fleet_home).expanduser().resolve(),
        apply=args.apply,
        restart=not args.no_restart,
        prune_model=not args.keep_model,
        systemd_user_dir=Path.home() / ".config" / "systemd" / "user",
        changed_files=set(),
        modified_units=set(),
    )

    mode = "APPLY" if ctx.apply else "DRY-RUN"
    info(f"[{mode}] repo={ctx.repo_root} slug={ctx.repo_slug} fleet={ctx.fleet_home}")
    roles = discover_roles(ctx)
    if not roles:
        info("No conventional PM or scrum-master roles found.")
        return 0

    for role in roles:
        info(f"\n== {role.role}: {role.profile_name}")
        update_profile_meta(ctx, role)
        prune_runtime_config(ctx, role)
        ensure_profile_symlink(ctx, role)
        ensure_alias(ctx, role)
        update_role_yaml(ctx, role)
        update_launcher(ctx, role)
        patch_systemd_units(ctx, role)
        update_registry(ctx, role)

    restart_modified_units(ctx)

    info("\nsummary:")
    info(f"  changed paths: {len(ctx.changed_files)}")
    for path in sorted(ctx.changed_files):
        info(f"  - {path}")
    if not ctx.apply:
        info("\ndry-run only. re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
