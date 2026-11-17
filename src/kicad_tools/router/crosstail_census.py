"""Structured capture of the diff-pair crossing-tail legality census (issue #4799).

The census itself is older (#4580, budget-corrected in #4635): with
``KCT_CROSSTAIL_CENSUS=1``, :meth:`DiffPairRouter._synthesize_crossing_tail`
scans its *whole* 225-entry via-site lattice instead of stopping at the first
legal candidate, and prints one ``[crosstail-census]`` header per crossover.
That output answers the question it was built for -- is a crossover's shipped
via site an *ordering* problem (many sites legal, a better key could pick a
kinder one) or a *saturation* one (almost nothing is legal, so no key can
help) -- but only one crossover at a time, in free text, on stdout.

This module turns that already-computed measurement into a **machine-readable
aggregate**: a per-crossover record, a summary with the saturation figures, and
a JSON document written to the path in ``KCT_CROSSTAIL_CENSUS_REPORT``.

**Report-only.**  Nothing here participates in a routing decision.  The
collector is appended to *after* the census has stamped and credited its own
incremental cost (:attr:`DiffPairRouter._census_elapsed_s`, the #4635 budget
credit), so the capture sits in the same uncredited, bounded, per-crossover
tail as the existing ``print`` calls -- it cannot move a deadline, and it
cannot change which candidate ships.

Why an environment variable and not ``kct route --census-report``
----------------------------------------------------------------

The census only fires under ``DiffPairRouter`` with shadow construction on, and
the only caller that does that today is ``boards/06-diffpair-test``'s
``generate_design.py --step route`` -- a board script driving the router API
directly, not ``kct route``.  A CLI flag would therefore be unreachable from
the one run that produces data, whereas an env var composes with the two flags
that already gate this measurement (``KCT_CROSSTAIL_CENSUS=1``,
``KCT_BOARD06_SHADOW=1``).  The written document follows the ``--format json``
conventions (``schema_version`` / ``generated_at``, sorted keys, one document)
so a future CLI surface can emit exactly this payload unchanged.

Interpreting the summary (advisory, non-blocking)
-------------------------------------------------

* ``saturated`` -- crossovers with ``legal == 0``: the lattice offered nothing.
* ``no_ordering_lever`` -- crossovers that *did* have legal candidates but all
  share a single ``v1`` site: every legal route carries the same barrel, so no
  ordering key can move the result either.
* ``inert_pct`` -- the union of the two: the share of crossovers where **no
  ordering key could have changed the outcome**.  This is the leading
  indicator; ``saturated_pct`` alone would call a lattice healthy when its few
  legal sets each offered a single site.
* ``verdict`` -- one of ``not-applicable`` (nothing scanned), ``saturated``
  (``saturated_pct`` at or above :data:`SATURATED_PCT_ADVISORY_THRESHOLD`),
  ``no-ordering-lever`` (``inert_pct`` at or above it, but with real legal
  sets), or ``ordering-levers-available``.  Read the first two as "ordering
  levers are inert here; the constraint lives upstream in placement / escape
  planning", which is the board-06 precedent.  It is **documentation, not a
  gate**: no code branches on it, no exit code reflects it.  Wiring saturation
  into an actual go/no-go or steering decision is deliberate follow-up work.
"""

from __future__ import annotations

import atexit
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "REPORT_ENV_VAR",
    "SATURATED_PCT_ADVISORY_THRESHOLD",
    "SCHEMA_VERSION",
    "VERDICT_NOT_APPLICABLE",
    "VERDICT_NO_ORDERING_LEVER",
    "VERDICT_ORDERING_LEVERS",
    "VERDICT_SATURATED",
    "CrossingTailCensusCollector",
    "CrossingTailCensusRecord",
    "CrossingTailCensusSummary",
    "CENSUS_COLLECTOR",
    "register_atexit_flush",
    "report_path_from_env",
    "write_report",
]

#: Bump only on a breaking change to the document below (additions are free).
SCHEMA_VERSION = 1

#: Set to a filesystem path to have the report written at interpreter exit.
REPORT_ENV_VAR = "KCT_CROSSTAIL_CENSUS_REPORT"

#: ``saturated_pct`` at or above which the summary's verdict flips to
#: :data:`VERDICT_SATURATED`.  Advisory only -- see the module docstring.
#: Chosen from the board-06 precedent, whose measured saturation sits in the
#: 90-95% band while a healthy (open) lattice measures near 0%.
SATURATED_PCT_ADVISORY_THRESHOLD = 90.0

VERDICT_NOT_APPLICABLE = "not-applicable"
VERDICT_SATURATED = "saturated"
VERDICT_NO_ORDERING_LEVER = "no-ordering-lever"
VERDICT_ORDERING_LEVERS = "ordering-levers-available"


def _pct(numerator: int, denominator: int) -> float:
    """Percentage rounded to one decimal; ``0.0`` when the denominator is 0."""
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


@dataclass(frozen=True)
class CrossingTailCensusRecord:
    """One crossover's census result -- the structured twin of the header line.

    Field names mirror the ``[crosstail-census]`` header exactly
    (``legal``/``total``/``distinct_v1``/``census_s``) so a reader of one can
    read the other.
    """

    net_name: str
    head: tuple[float, float]
    goal: tuple[float, float]
    legal: int
    total: int
    distinct_v1: int
    census_s: float = 0.0

    @property
    def saturated(self) -> bool:
        """No candidate in the whole lattice was legal."""
        return self.legal == 0

    @property
    def no_ordering_lever(self) -> bool:
        """Legal candidates exist, but they all land on ONE ``v1`` site.

        Deliberately excludes saturated crossovers: those have no ordering
        lever either, but for the stronger reason that they have no candidate
        at all, and conflating the two hides which one a board is hitting.
        """
        return self.legal > 0 and self.distinct_v1 <= 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_name": self.net_name,
            "head": [self.head[0], self.head[1]],
            "goal": [self.goal[0], self.goal[1]],
            "legal": self.legal,
            "total": self.total,
            "distinct_v1": self.distinct_v1,
            "census_s": round(self.census_s, 6),
            "saturated": self.saturated,
            "no_ordering_lever": self.no_ordering_lever,
        }


@dataclass(frozen=True)
class CrossingTailCensusSummary:
    """Aggregate of every :class:`CrossingTailCensusRecord` in one process."""

    crossovers_scanned: int
    saturated: int
    saturated_pct: float
    no_ordering_lever: int
    no_ordering_lever_pct: float
    inert_pct: float
    distinct_v1_max: int
    census_s_total: float
    verdict: str
    saturated_threshold_pct: float

    @property
    def applicable(self) -> bool:
        """Did anything actually exercise the census in this process?

        ``False`` is the honest answer for a board that never routes a
        differential pair through the shadow constructor (boards 05 and 07
        today) -- distinct from "0% saturated", which would be a fabricated
        clean bill of health.
        """
        return self.crossovers_scanned > 0

    @classmethod
    def from_records(
        cls,
        records: Sequence[CrossingTailCensusRecord],
        *,
        saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
    ) -> CrossingTailCensusSummary:
        scanned = len(records)
        saturated = sum(1 for r in records if r.saturated)
        unsaturated = scanned - saturated
        no_lever = sum(1 for r in records if r.no_ordering_lever)
        saturated_pct = _pct(saturated, scanned)
        # "Inert" = no ordering key could have changed the outcome, whether
        # because nothing was legal or because every legal candidate sat on the
        # same barrel.  This is the union the verdict is really about; keying
        # only on ``saturated_pct`` would call a lattice "ordering levers
        # available" when in fact none of its legal sets offered a choice.
        inert_pct = _pct(saturated + no_lever, scanned)
        if scanned == 0:
            verdict = VERDICT_NOT_APPLICABLE
        elif saturated_pct >= saturated_threshold_pct:
            verdict = VERDICT_SATURATED
        elif inert_pct >= saturated_threshold_pct:
            verdict = VERDICT_NO_ORDERING_LEVER
        else:
            verdict = VERDICT_ORDERING_LEVERS
        return cls(
            crossovers_scanned=scanned,
            saturated=saturated,
            saturated_pct=saturated_pct,
            no_ordering_lever=no_lever,
            # Denominator is the UNSATURATED set: "of the crossovers that had a
            # choice, how many had no real choice".  Against ``scanned`` the
            # figure would be diluted by the saturated ones and mean nothing.
            no_ordering_lever_pct=_pct(no_lever, unsaturated),
            inert_pct=inert_pct,
            distinct_v1_max=max((r.distinct_v1 for r in records), default=0),
            census_s_total=round(float(sum(r.census_s for r in records)), 6),
            verdict=verdict,
            saturated_threshold_pct=saturated_threshold_pct,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "crossovers_scanned": self.crossovers_scanned,
            "saturated": self.saturated,
            "saturated_pct": self.saturated_pct,
            "unsaturated": self.crossovers_scanned - self.saturated,
            "no_ordering_lever": self.no_ordering_lever,
            "no_ordering_lever_pct": self.no_ordering_lever_pct,
            "inert_pct": self.inert_pct,
            "distinct_v1_max": self.distinct_v1_max,
            "census_s_total": self.census_s_total,
            "verdict": self.verdict,
            "saturated_threshold_pct": self.saturated_threshold_pct,
        }

    def format_human(self) -> str:
        """Multi-line, greppable text form of this summary.

        Every line is prefixed ``[crosstail-census-summary]`` so it survives
        interleaving with the router's own chatter.
        """
        tag = "[crosstail-census-summary]"
        if not self.applicable:
            return (
                f"{tag} 0 crossovers scanned -- NOT APPLICABLE\n"
                f"{tag}   nothing in this run exercised the diff-pair "
                f"crossing-tail census (no shadow-constructed crossover was "
                f"synthesized); this is not a 0% saturation result"
            )
        lines = [
            f"{tag} {self.crossovers_scanned} crossover(s) scanned, verdict={self.verdict}",
            f"{tag}   saturated (legal=0): {self.saturated}/"
            f"{self.crossovers_scanned} ({self.saturated_pct}%)",
            f"{tag}   no ordering lever (legal>0, distinct_v1<=1): "
            f"{self.no_ordering_lever}/{self.crossovers_scanned - self.saturated} "
            f"({self.no_ordering_lever_pct}% of unsaturated)",
            f"{tag}   inert (no ordering key could change the outcome): {self.inert_pct}%",
            f"{tag}   distinct_v1 max={self.distinct_v1_max} "
            f"credited census_s total={self.census_s_total:.4f}",
        ]
        if self.verdict in (VERDICT_SATURATED, VERDICT_NO_ORDERING_LEVER):
            lines.append(
                f"{tag}   advisory: inert >= {self.saturated_threshold_pct}% "
                f"-- ordering levers are inert; the constraint is upstream in "
                f"placement / escape planning (non-blocking)"
            )
        return "\n".join(lines)


class CrossingTailCensusCollector:
    """Append-only sink for :class:`CrossingTailCensusRecord`s.

    One instance is shared per process (:data:`CENSUS_COLLECTOR`) so a report
    can be produced even from entry points that never reach
    ``route_all_with_diffpairs``; :class:`DiffPairRouter` also keeps its own
    per-instance list for tests that want one router's records in isolation.
    """

    def __init__(self) -> None:
        self._records: list[CrossingTailCensusRecord] = []

    def add(self, record: CrossingTailCensusRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[CrossingTailCensusRecord, ...]:
        return tuple(self._records)

    def reset(self) -> None:
        self._records.clear()

    def summary(
        self,
        *,
        saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
    ) -> CrossingTailCensusSummary:
        return CrossingTailCensusSummary.from_records(
            self._records, saturated_threshold_pct=saturated_threshold_pct
        )

    def to_report_dict(
        self,
        *,
        census_enabled: bool | None = None,
        saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
    ) -> dict[str, Any]:
        """The JSON document: envelope + summary + per-crossover detail."""
        if census_enabled is None:
            census_enabled = census_is_enabled()
        summary = self.summary(saturated_threshold_pct=saturated_threshold_pct)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report": "crosstail-census",
            "census_enabled": bool(census_enabled),
            "summary": summary.to_dict(),
            "crossovers": [r.to_dict() for r in self._records],
        }

    def format_human(
        self,
        *,
        saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
    ) -> str:
        return self.summary(saturated_threshold_pct=saturated_threshold_pct).format_human()


#: Process-wide collector.  Reset it in tests rather than rebinding the name.
CENSUS_COLLECTOR = CrossingTailCensusCollector()


def census_is_enabled() -> bool:
    """Is the underlying #4580 census switched on for this process?

    Read through the module that owns the flag so a ``monkeypatch`` of
    ``_CROSSTAIL_CENSUS`` is honoured, and tolerate the import cycle during
    ``diffpair_routing``'s own import by falling back to the environment.
    """
    try:
        from kicad_tools.router import diffpair_routing as _dpr

        return bool(_dpr._CROSSTAIL_CENSUS)
    except Exception:  # pragma: no cover - import-time / partial-import safety
        return os.environ.get("KCT_CROSSTAIL_CENSUS", "0") == "1"


def report_path_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    """Path from ``KCT_CROSSTAIL_CENSUS_REPORT``, or ``None`` when unset/blank."""
    raw = (env if env is not None else os.environ).get(REPORT_ENV_VAR, "")
    raw = raw.strip()
    return Path(raw) if raw else None


def write_report(
    path: Path | str,
    collector: CrossingTailCensusCollector | None = None,
    *,
    census_enabled: bool | None = None,
    saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
) -> Path:
    """Write the JSON report for *collector* to *path* and return the path.

    Keys are sorted and the document is a single JSON object, matching
    ``kct``'s machine-output contract (``docs/reference/machine-output.md``).
    """
    target = Path(path)
    payload = (collector or CENSUS_COLLECTOR).to_report_dict(
        census_enabled=census_enabled,
        saturated_threshold_pct=saturated_threshold_pct,
    )
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return target


def _existing_report_has_records(target: Path) -> bool:
    """Does *target* already hold a report that measured something?

    Used by :func:`flush_report` for the no-clobber rule below.  Any read or
    parse problem answers ``False`` -- an unreadable or foreign file is not a
    measurement worth protecting.
    """
    try:
        payload = json.loads(target.read_text())
        return int(payload["summary"]["crossovers_scanned"]) > 0
    except Exception:
        return False


def flush_report(
    collector: CrossingTailCensusCollector | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stream: Any = None,
) -> Path | None:
    """Write the report iff :data:`REPORT_ENV_VAR` names a path.

    **An empty report never clobbers a non-empty one.**  Board scripts shell
    out (``kicad-cli``, helper ``python`` invocations), and every child
    inherits the environment -- so each child would otherwise flush its own
    "0 crossovers scanned" document over the parent's real measurement,
    silently, since the child's stderr is usually captured.  A flush with no
    records therefore stands down when the target already holds a report that
    measured something.  A run that DID measure always writes.

    Never raises: a diagnostic that can abort a 25-minute route because its
    output directory is read-only would be worse than no diagnostic (the
    ``_offboard_preflight`` precedent -- report-only surfaces do not block).
    Returns the written path, or ``None`` when nothing was written.
    """
    import sys

    target = report_path_from_env(env)
    if target is None:
        return None
    sink = collector or CENSUS_COLLECTOR
    out = stream if stream is not None else sys.stderr
    if not sink.records and target.exists() and _existing_report_has_records(target):
        print(
            f"[crosstail-census] report at {target} kept: this process scanned "
            f"0 crossover(s) and will not overwrite a report that has records",
            file=out,
        )
        return None
    try:
        written = write_report(target, sink)
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        print(f"[crosstail-census] report NOT written to {target}: {exc}", file=out)
        return None
    summary = sink.summary()
    print(
        f"[crosstail-census] report written to {written} "
        f"({summary.crossovers_scanned} crossover(s) scanned, "
        f"verdict={summary.verdict})",
        file=out,
    )
    return written


_ATEXIT_REGISTERED = False


def register_atexit_flush() -> None:
    """Arrange for :func:`flush_report` to run at interpreter exit (once).

    Registered at import time so *any* entry point that pulls in the router --
    ``kct route``, a board's ``generate_design.py``, a bare script -- produces
    a report when the env var is set, including the honest "0 crossovers
    scanned / not applicable" one for a board that never routes a coupled
    differential pair.  The hook re-reads the environment when it fires and is
    a no-op when :data:`REPORT_ENV_VAR` is unset, so importing this module has
    no observable effect on a normal run.
    """
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(flush_report)
    _ATEXIT_REGISTERED = True


register_atexit_flush()
