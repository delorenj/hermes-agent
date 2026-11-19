"""Finite, symlink-following discovery shared by skill indexes and prompt manifests."""

from collections import deque
from dataclasses import dataclass
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillScanLimits:
    directories: int = 4096
    entries: int = 65536
    results: int = 8192
    path_bytes: int = 8 * 1024 * 1024
    depth: int = 64


DEFAULT_LIMITS = SkillScanLimits()


class _ScanBudget:
    def __init__(self, limits: SkillScanLimits):
        self.limits = limits
        self.directories = self.entries = self.results = self.path_bytes = 0
        self.stopped = False
        self.depth_skipped = False

    def take(self, kind: str, path: Path, amount: int = 1) -> bool:
        if getattr(self, kind) + amount > getattr(self.limits, kind):
            logger.warning("Skill scan stopped at %s: %s budget (%d) exceeded", path, kind, getattr(self.limits, kind))
            self.stopped = True
            return False
        setattr(self, kind, getattr(self, kind) + amount)
        return True

    def retain_path(self, path: Path) -> bool:
        return self.take("path_bytes", path, len(os.fsencode(path)))


def _directory_entries(path: Path, budget: _ScanBudget, names: frozenset[str], excluded, support) -> tuple[list[Path], list[Path]]:
    children, matches = [], []
    has_skill = (path / "SKILL.md").is_file()
    with os.scandir(path) as entries:
        for entry in entries:
            if budget.entries >= budget.limits.entries:
                logger.warning("Skill scan stopped at %s: entries budget (%d) exceeded", path, budget.limits.entries)
                budget.stopped = True
                break
            candidate = path / entry.name
            budget.entries += 1
            try:
                directory = entry.is_dir(follow_symlinks=True)
            except OSError as exc:
                logger.warning("Skill scan skipped %s: cannot classify entry (%s)", candidate, exc)
                continue
            if directory:
                if (entry.name not in excluded
                        and not (has_skill and entry.name in support)
                        and budget.retain_path(candidate)):
                    children.append(candidate)
            if entry.name in names and not directory and budget.take("results", candidate) and budget.retain_path(candidate):
                matches.append(candidate)
            if budget.stopped:
                break
    return children, matches


def walk_skill_directories(skills_dir: Path, filenames: tuple[str, ...], *, limits: SkillScanLimits | None = None):
    """Yield (path, child directories, matching files, stat) within one bounded scan.

    Entries are consumed incrementally, so even one enormous directory cannot allocate an
    unbounded os.walk file list. The shallowest then lexical alias wins.
    """
    from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS

    budget = _ScanBudget(limits if limits is not None else DEFAULT_LIMITS)
    root = Path(skills_dir)
    if not budget.retain_path(root):
        return
    # Breadth first gives the shallowest path priority, so a deep alias cannot
    # consume a physical directory before a later shallow spelling reaches it.
    pending = deque([(root, 0)])
    seen: set[tuple[int, int]] = set()
    while pending and not budget.stopped:
        path, depth = pending.popleft()
        if depth > budget.limits.depth:
            logger.warning("Skill scan skipped %s: depth budget (%d) exceeded", path, budget.limits.depth)
            continue
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                logger.warning("Skill scan skipped %s: cycle or alias (physical directory already visited)", path)
                continue
            if not budget.take("directories", path):
                break
            seen.add(identity)
            children, found = _directory_entries(path, budget, frozenset(filenames),
                                                  EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS)
        except OSError as exc:
            logger.warning("Skill scan skipped %s: directory unavailable (%s)", path, exc)
            continue
        yield path, children, found, stat
        pending.extend((child, depth + 1) for child in sorted(children))


def scan_skill_files(skills_dir: Path, filenames: tuple[str, ...]) -> list[Path]:
    """Sorted lexical matches, once per physical directory; limits apply before retention.

    The prompt manifest tracks two filenames per skill. Its result and path-byte
    allowances are doubled, so DESCRIPTION.md cannot crowd indexed SKILL.md files
    out of snapshot invalidation. Directory/entry/depth budgets stay identical.
    """
    from dataclasses import replace

    limits = (replace(DEFAULT_LIMITS, results=DEFAULT_LIMITS.results * 2,
                      path_bytes=DEFAULT_LIMITS.path_bytes * 2)
              if len(filenames) > 1 else DEFAULT_LIMITS)
    return sorted(file for _, _, files, _ in walk_skill_directories(skills_dir, filenames, limits=limits) for file in files)
