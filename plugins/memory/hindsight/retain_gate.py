"""Decide whether a conversation turn is worth paying an LLM to remember.

Why this exists, measured on 2026-09-04:

The ``agent-33god-pm`` bank was 98.4% of all Hindsight LLM spend — 6,918,531
tokens on 2026-09-03, against 115,899 for the next busiest bank. Each cron
tick retained a document of 80,153 characters:

===========================  ======  ==============================
segment                       chars   changes between ticks
===========================  ======  ==============================
six inlined skill documents   66,805  no (byte-identical)
the cron instruction          12,264  no (byte-identical)
the assistant's status block    1,084  yes
===========================  ======  ==============================

So 99% of every retain repeated the one before it. Of the 116 facts
extracted from one such tick, 110 were ``world`` facts restating the skill
documentation that the harness had pasted into the prompt — "Momo triggers
include 'be Momo'", "Momo must not be used for hands-on coding". The bank
had become a memory of its own instruction manual, re-derived every eight
minutes, and consolidation then reasoned over roughly 4,000 such units a
day on a premium model.

Hindsight already skips re-extraction when a document's content hash is
unchanged, which is why the 5-minute write probe costs only ~17 LLM calls a
day. Cron agents defeat that: every tick opens a new session, so every
retain lands under a new document id and nothing can dedupe.

This module answers "is there anything new here?" before any LLM is paid.
Three layers, cheapest first:

1. :func:`strip_injected_blocks` removes harness-injected scaffolding. The
   blocks are self-delimiting and announce themselves, so this is exact
   rather than heuristic.
2. :func:`RetainGate.decide` drops any paragraph seen in ``threshold`` of
   the last ``window`` retains for this bank. Boilerplate is *defined* by
   repetition, so this needs no knowledge of any particular harness and
   keeps working when the prompt changes.
3. The same call refuses the retain outright when what survives is shorter
   than ``min_novel_chars``, repeats the previous residue, or is the
   harness's own ``[SILENT]`` no-op marker. A tick reporting the state the
   last tick reported is a heartbeat, not a memory.

Every cron tick is a fresh process, so the rolling window lives on disk,
one small file per bank, written atomically. Every entry point fails open:
a gate that cannot decide retains the turn unchanged and unfiltered. Losing
a memory is worse than paying for a duplicate one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set

__all__ = [
    "DEFAULT_MIN_NOVEL_CHARS",
    "DEFAULT_THRESHOLD",
    "DEFAULT_WINDOW",
    "Decision",
    "RetainGate",
    "filter_paragraphs",
    "paragraph_key",
    "split_paragraphs",
    "state_dir_default",
    "strip_injected_blocks",
]

STATE_VERSION = 1

DEFAULT_WINDOW = 20            # how many past retains the gate remembers
DEFAULT_THRESHOLD = 3          # a paragraph in this many of them is boilerplate
DEFAULT_MIN_NOVEL_CHARS = 64   # below this the residue cannot carry a fact.
# Deliberately low. Once layers 1 and 2 have run, whatever survives is small by
# construction and costs little to extract, so the floor only has to refuse the
# genuinely empty: an acknowledgement, a leftover warning line, nothing at all.
# A real one-line transition ("STATUS: INTEGRATED. Ticket 33GOD-54 merged into
# the main line after the suite ran green.") is 99 characters and must survive.
# A tick whose only change is a clock is caught by paragraph_key normalising
# timestamps away, not by this floor.

# Bounds so a runaway turn cannot grow the state file without limit.
MAX_PARAGRAPHS_PER_WINDOW = 500
MAX_PARAGRAPH_CHARS = 4096

# The harness wraps its own instructions in a bracketed IMPORTANT block. A
# block that says it has pasted a skill below owns everything up to the next
# block; any other block is scaffolding and owns only itself.
#
# The blocks nest: the cron block quotes the literal "[SILENT]" twice inside
# itself, so a non-greedy regex ends the marker in the wrong place and leaks
# the tail. _important_spans tracks bracket depth instead.
_IMPORTANT_OPEN = re.compile(r"\[IMPORTANT:", re.I)
_LOADS_SKILL = re.compile(
    r"full skill content is loaded below|the following skill\(s\) were listed",
    re.I,
)


def _important_spans(text: str) -> List[tuple[int, int, str]]:
    """Every ``[IMPORTANT: ...]`` block as ``(start, end, body)``.

    Bracket-depth aware, so a block quoting ``[SILENT]`` inside itself ends
    at its own closing bracket and not at the quoted one. An unterminated
    block runs to the end of the text.
    """
    spans: List[tuple[int, int, str]] = []
    pos = 0
    while True:
        match = _IMPORTANT_OPEN.search(text, pos)
        if not match:
            return spans
        depth, i, n = 0, match.start(), len(text)
        while i < n:
            char = text[i]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        spans.append((match.start(), i, text[match.start():i]))
        pos = max(i, match.start() + 1)

# The harness's explicit "nothing happened" reply. Retaining it is pure cost.
_SILENT = re.compile(r"^(?:\w+:\s*)?\W*\[SILENT\]\W*$", re.I)

_BLANK_RUN = re.compile(r"\n{3,}")

# Volatile spans are normalised away before hashing, so a paragraph whose only
# change is the clock counts as the same paragraph. This affects the hash only;
# the retained text keeps its real values.
#
# The list is deliberately short. An earlier version also folded away bare
# integers and hex, which made "TICKET: 33GOD-53" and "TICKET: 33GOD-54"
# identical — a real state transition would have been dropped as a duplicate.
# Only spans that are timestamps *by construction* belong here. Everything
# else, including commit hashes, counts and ticket numbers, is content: paying
# to extract a near-duplicate is cheap once the boilerplate is gone, and losing
# a transition is not.
_VOLATILE: Sequence[tuple[re.Pattern[str], str]] = (
    # ISO-8601, e.g. 2026-09-04T06:21:48.123Z or 2026-09-04 06:21+00:00
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    # compact stamp used in generated filenames, e.g. 20260904T062148Z
    (re.compile(r"\b\d{8}T\d{6}Z?\b"), "<ts>"),
    # a bare wall clock, e.g. 06:21 or 06:21:48
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"), "<clock>"),
    (re.compile(r"\s+"), " "),
)


def state_dir_default() -> Path:
    """Where the rolling windows live. Honours XDG_STATE_HOME."""
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "hermes" / "hindsight-retain-gate"


def strip_injected_blocks(text: str) -> str:
    """Remove harness-injected instruction blocks from a turn.

    A block announcing that it has loaded a skill takes its body with it, up
    to the *next* block. A skill block with nothing after it to bound its body
    surrenders only its marker: the text that follows an unbounded block is
    the user's real message, and dropping that is the one unrecoverable
    mistake this module can make. Every other bracketed IMPORTANT block is
    dropped on its own, leaving the prose around it — that prose is the actual
    prompt, and layer 2 decides whether it repeats.
    """
    if not text:
        return text
    spans = _important_spans(text)
    if not spans:
        return text
    out: List[str] = []
    cursor = 0
    for i, (start, end, body) in enumerate(spans):
        if start >= cursor:
            out.append(text[cursor:start])
        if _LOADS_SKILL.search(body) and i + 1 < len(spans):
            # The body runs to the next block. Only a *bounded* body is dropped:
            # with no following block the remaining text is the user's actual
            # message, and eating it is the one unrecoverable mistake here.
            cursor = spans[i + 1][0]
        else:
            cursor = end
    out.append(text[cursor:])
    return _BLANK_RUN.sub("\n\n", "".join(out)).strip()


def split_paragraphs(text: str) -> List[str]:
    """Split into blank-line-separated paragraphs, dropping empties."""
    if not text:
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def paragraph_key(paragraph: str) -> str:
    """A stable key for a paragraph, insensitive to clocks only.

    Ticket numbers, counts and commit hashes deliberately change the key: they
    are the difference between a heartbeat and a state transition.
    """
    norm = paragraph[:MAX_PARAGRAPH_CHARS]
    for pattern, repl in _VOLATILE:
        norm = pattern.sub(repl, norm)
    return hashlib.sha256(norm.strip().lower().encode("utf-8")).hexdigest()[:16]


def filter_paragraphs(text: str, dropped: Set[str]) -> str:
    """Rebuild `text` without the paragraphs whose keys are in `dropped`."""
    if not dropped or not text:
        return text
    kept = [p for p in split_paragraphs(text) if paragraph_key(p) not in dropped]
    return "\n\n".join(kept)


@dataclass
class Decision:
    """What the gate concluded, and everything needed to act on it."""

    retain: bool
    reason: str
    text: str = ""
    original_chars: int = 0
    kept_chars: int = 0
    dropped_keys: Set[str] = field(default_factory=set)
    seen_keys: List[str] = field(default_factory=list)
    residue_key: str = ""

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.kept_chars)

    def summary(self) -> str:
        if not self.retain:
            return f"skipped ({self.reason}); {self.original_chars} chars not retained"
        return (
            f"retained ({self.reason}); {self.kept_chars} of {self.original_chars} chars"
            f", {len(self.dropped_keys)} repeated paragraph(s) dropped"
        )


class RetainGate:
    """Per-bank novelty gate. Construct once per retain, it is cheap."""

    def __init__(
        self,
        bank: str,
        *,
        state_dir: Path | str | None = None,
        window: int = DEFAULT_WINDOW,
        threshold: int = DEFAULT_THRESHOLD,
        min_novel_chars: int = DEFAULT_MIN_NOVEL_CHARS,
        strip_injected: bool = True,
        drop_repeated: bool = True,
        skip_unchanged: bool = True,
    ) -> None:
        self.bank = bank or "default"
        self.state_dir = Path(state_dir) if state_dir is not None else state_dir_default()
        self.window = max(1, int(window))
        self.threshold = max(1, int(threshold))
        self.min_novel_chars = max(0, int(min_novel_chars))
        self.strip_injected = bool(strip_injected)
        self.drop_repeated = bool(drop_repeated)
        self.skip_unchanged = bool(skip_unchanged)

    # ------------------------------------------------------------------ state

    @property
    def state_path(self) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.bank)[:120] or "default"
        return self.state_dir / f"{safe}.json"

    def _load(self) -> Dict[str, object]:
        try:
            with self.state_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and int(data.get("version", 0)) == STATE_VERSION:
                return data
        except Exception:
            pass
        return {"version": STATE_VERSION, "windows": [], "last_residue": ""}

    def _save(self, data: Dict[str, object]) -> None:
        """Atomic replace. A failure here must never fail the retain."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.state_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, self.state_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            pass

    def reset(self) -> None:
        try:
            self.state_path.unlink()
        except Exception:
            pass

    # ----------------------------------------------------------------- decide

    def evaluate(self, text: str, state: Dict[str, object] | None = None) -> Decision:
        """Pure decision against `state`; nothing is written."""
        original = text or ""
        if state is None:
            state = self._load()

        body = strip_injected_blocks(original) if self.strip_injected else original

        paragraphs = split_paragraphs(body)
        seen_keys = [paragraph_key(p) for p in paragraphs][:MAX_PARAGRAPHS_PER_WINDOW]

        dropped: Set[str] = set()
        if self.drop_repeated:
            windows = state.get("windows") or []
            counts: Dict[str, int] = {}
            for past in windows:
                for key in set(past if isinstance(past, list) else []):
                    counts[key] = counts.get(key, 0) + 1
            dropped = {k for k in seen_keys if counts.get(k, 0) >= self.threshold}

        kept = [p for p in paragraphs if paragraph_key(p) not in dropped]
        residue = "\n\n".join(kept).strip()
        residue_key = hashlib.sha256(
            "".join(paragraph_key(p) for p in kept).encode("utf-8")
        ).hexdigest()[:16]

        base = Decision(
            retain=True,
            reason="novel",
            text=residue,
            original_chars=len(original),
            kept_chars=len(residue),
            dropped_keys=dropped,
            seen_keys=seen_keys,
            residue_key=residue_key,
        )

        if _SILENT.match(residue):
            base.retain, base.reason = False, "harness reported [SILENT]"
        elif len(residue) < self.min_novel_chars:
            base.retain, base.reason = False, (
                f"only {len(residue)} novel chars, floor is {self.min_novel_chars}"
            )
        elif self.skip_unchanged and residue_key and residue_key == state.get("last_residue"):
            base.retain, base.reason = False, "identical to the previous retain"
        return base

    def commit(self, decision: Decision) -> None:
        """Record this turn as seen, whether or not it was retained."""
        state = self._load()
        windows = list(state.get("windows") or [])
        windows.append(decision.seen_keys)
        state["windows"] = windows[-self.window:]
        state["last_residue"] = decision.residue_key
        state["version"] = STATE_VERSION
        self._save(state)

    def decide(self, text: str) -> Decision:
        """Evaluate and record in one call. Fails open on any error."""
        try:
            state = self._load()
            decision = self.evaluate(text, state)
            self.commit(decision)
            return decision
        except Exception as exc:  # never block the host agent
            return Decision(
                retain=True,
                reason=f"gate error, failing open: {exc}",
                text=text or "",
                original_chars=len(text or ""),
                kept_chars=len(text or ""),
            )
