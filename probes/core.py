"""Probe types: outcomes, part results, the Probe record, and how part outcomes combine (S0 spec §5.1, §5.4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from probes.context import ProbeContext


class Outcome(str, Enum):
    CONFIRMED = "CONFIRMED"
    DIFFERENT = "DIFFERENT"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"


class ProbeBlocked(Exception):
    """A part can't continue: timeout, Tally unreachable, missing prerequisite, or a manual step in non-interactive mode."""


@dataclass
class PartResult:
    outcome: Outcome
    summary: str
    observations: dict[str, Any] = field(default_factory=dict)
    spec_impact: str = ""

    def __post_init__(self) -> None:
        if self.outcome is Outcome.PARTIAL:
            raise ValueError("PARTIAL is a probe-level outcome, never a part outcome")
        if self.outcome in (Outcome.DIFFERENT, Outcome.FAILED) and not self.spec_impact.strip():
            raise ValueError(f"{self.outcome.value} needs a spec_impact sentence")


PartFn = Callable[["ProbeContext"], Awaitable[PartResult]]


@dataclass(frozen=True, eq=False)
class Probe:
    id: int
    name: str
    question: str
    feeds: tuple[str, ...]
    parts: dict[str, PartFn]
    requires: tuple[int, ...] = ()
    mutating: bool = False
    guard: bool = True
    educational_sensitive: bool = False
    planned_parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.planned_parts and not set(self.parts) <= set(self.planned_parts):
            raise ValueError(f"Probe {self.id}: built parts {sorted(self.parts)} must be among {list(self.planned_parts)}")

    @property
    def part_labels(self) -> tuple[str, ...]:
        """Every company part of this probe, built now or in a later plan part (S0 spec §5.1, §5.4 PARTIAL)."""
        return self.planned_parts or tuple(self.parts)


_PRECEDENCE = (Outcome.BLOCKED, Outcome.FAILED, Outcome.DIFFERENT)


def combine(part_outcomes: dict[str, Outcome | None]) -> Outcome:
    """Probe outcome from its parts; first match wins: PARTIAL, BLOCKED, FAILED, DIFFERENT, else CONFIRMED."""
    if not part_outcomes or any(outcome is None for outcome in part_outcomes.values()):
        return Outcome.PARTIAL
    present = set(part_outcomes.values())
    for outcome in _PRECEDENCE:
        if outcome in present:
            return outcome
    return Outcome.CONFIRMED


HALF_RANK = {"CONFIRMED": 0, "DIFFERENT": 1, "FAILED": 2}


def worst_verdict(verdicts) -> str:
    """The worst of several half verdicts (CONFIRMED < DIFFERENT < FAILED) — one part judged by independent halves."""
    return max(verdicts, key=HALF_RANK.__getitem__)


def judge_halves(halves: dict[str, tuple[str, str, str]]) -> tuple[Outcome, str, list[str]]:
    """A part judged half by half (probes 11, 24, 25): `halves` = name → (verdict, text, spec impact).

    Returns the worst verdict, a summary naming EVERY half with its verdict (C46: a FAILED half must not hide a
    DIFFERENT one), and EVERY half's non-empty impact -- worst verdict first (stable within a verdict),
    de-duplicated. Ruling 2026-09-25: a CONFIRMED half's impact (e.g. R9 in 25 B) is a real spec input and is never
    dropped because another half is worse -- so every half's impact must be scoped to that half, never a claim about
    the whole part (C47 review I1: probe 24's whole-probe CONFIRMED text beside a FAILED vault half contradicted
    itself). The caller may still choose its own impact text."""
    worst = worst_verdict(verdict for verdict, _, _ in halves.values())
    summary = "; ".join(f"{name.replace('_', ' ')}: {text} ({verdict})" for name, (verdict, text, _) in halves.items())
    ranked = sorted(halves.values(), key=lambda half: -HALF_RANK[half[0]])
    impacts = list(dict.fromkeys(impact for _, _, impact in ranked if impact))
    return Outcome(worst), summary, impacts
