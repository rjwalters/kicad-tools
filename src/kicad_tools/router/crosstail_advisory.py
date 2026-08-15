"""Pre-route consumption of a crossing-tail census report (issue #4799).

Slice 1 (#4852, :mod:`kicad_tools.router.crosstail_census`) turned the #4580
crossing-tail census into a structured JSON document.  That document is still a
**post-mortem**: it is written at the end of the run that produced it, long
after the router has already spent its shadow-construction budget.

This module closes the loop and makes the census a **leading indicator**: a
report written by run *N* is loaded and interpreted *before the first A\\*
expansion* of run *N+1*, so the operator sees "this board's crossover lattice
measured 90% saturated last time; ordering levers are inert" up front rather
than 24 minutes in.

Why a prior run, and not a placement-only computation
-----------------------------------------------------

The census's legality test is genuinely order-dependent -- it consults drills
placed by *earlier crossovers in the same pass*, an escape-channel registry
keyed on which nets are still unrouted, and the pair's own guide route -- so
the measurement cannot simply be hoisted ahead of routing (see
``docs/reference/crosstail-census-report.md``).  Replaying the *previous*
measurement sidesteps that open design question entirely, the way
profile-guided optimization sidesteps predicting a program's hot paths: the
prediction is a measurement, just one taken earlier.

Its accuracy therefore decays with staleness, which is why every advisory
carries an explicit board cross-check (how many of the report's nets still
exist on the board being routed) and refuses to look confident when the
overlap is poor.

**Advisory only.**  Nothing here changes routing, and nothing here can fail a
run: :func:`emit_advisory` swallows every error into a stderr diagnostic, on
the ``_offboard_preflight`` precedent that report-only surfaces must never
block a route.  Wiring saturation into an actual go/no-go or steering decision
remains deliberate follow-up work on #4799.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from kicad_tools.router.crosstail_census import (
    SATURATED_PCT_ADVISORY_THRESHOLD,
    SCHEMA_VERSION,
    VERDICT_NO_ORDERING_LEVER,
    VERDICT_SATURATED,
    CrossingTailCensusRecord,
    CrossingTailCensusSummary,
)

__all__ = [
    "ADVISORY_ENV_VAR",
    "STALE_COVERAGE_PCT_THRESHOLD",
    "WORST_NETS_SHOWN",
    "CensusReportError",
    "CrossingTailAdvisory",
    "LoadedCensusReport",
    "NetPrediction",
    "advisory_path_from_env",
    "board_net_names",
    "build_advisory",
    "emit_advisory",
]

#: Fallback surface for callers that do not go through ``kct route`` -- board
#: scripts drive the router API directly (``boards/06-diffpair-test``), and the
#: capture side of this feature is already env-gated
#: (``KCT_CROSSTAIL_CENSUS_REPORT``), so the replay side matches.
ADVISORY_ENV_VAR = "KCT_CROSSTAIL_CENSUS_ADVISORY"

#: Below this share of the report's nets still present on the board, the
#: advisory declares itself **stale**: too little of what was measured is still
#: here for the replay to predict anything.  50% is deliberately generous --
#: a report from the same board with a handful of nets renamed stays usable,
#: while a report from a *different* design (near-zero overlap) is rejected.
STALE_COVERAGE_PCT_THRESHOLD = 50.0

#: How many worst-offender nets the human block names before eliding.
WORST_NETS_SHOWN = 5


class CensusReportError(ValueError):
    """A census report could not be read or is not a census report.

    Raised by the loader so callers that *want* to fail loudly (tests, future
    tooling) can.  :func:`emit_advisory` catches it -- the preflight never
    blocks a route because a diagnostic file was missing or malformed.
    """


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_xy(value: Any) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return (_as_float(value[0]), _as_float(value[1]))
    return (0.0, 0.0)


@dataclass(frozen=True)
class LoadedCensusReport:
    """A parsed ``crosstail-census`` document (schema v1).

    The per-crossover entries are rehydrated into the very
    :class:`CrossingTailCensusRecord` type that wrote them, so the advisory can
    re-aggregate them through :meth:`CrossingTailCensusSummary.from_records` --
    the same code path slice 1 used -- instead of trusting (or duplicating) the
    stored summary block.
    """

    path: Path | None
    schema_version: int
    generated_at: str | None
    census_enabled: bool
    records: tuple[CrossingTailCensusRecord, ...]
    stored_summary: Mapping[str, Any]

    @classmethod
    def from_payload(cls, payload: Any, *, path: Path | None = None) -> LoadedCensusReport:
        """Validate and rehydrate an already-decoded JSON document."""
        if not isinstance(payload, Mapping):
            raise CensusReportError(f"census report {path or '<payload>'} is not a JSON object")
        report_kind = payload.get("report")
        if report_kind != "crosstail-census":
            raise CensusReportError(
                f"census report {path or '<payload>'} has report={report_kind!r}, "
                "expected 'crosstail-census'"
            )
        crossovers = payload.get("crossovers", [])
        if not isinstance(crossovers, Sequence) or isinstance(crossovers, (str, bytes)):
            raise CensusReportError(
                f"census report {path or '<payload>'} has a non-list 'crossovers'"
            )
        records = tuple(
            CrossingTailCensusRecord(
                net_name=str(entry.get("net_name", "")),
                head=_as_xy(entry.get("head")),
                goal=_as_xy(entry.get("goal")),
                legal=_as_int(entry.get("legal")),
                total=_as_int(entry.get("total")),
                distinct_v1=_as_int(entry.get("distinct_v1")),
                census_s=_as_float(entry.get("census_s")),
            )
            for entry in crossovers
            if isinstance(entry, Mapping)
        )
        stored = payload.get("summary")
        return cls(
            path=path,
            schema_version=_as_int(payload.get("schema_version"), SCHEMA_VERSION),
            generated_at=(str(payload["generated_at"]) if payload.get("generated_at") else None),
            census_enabled=bool(payload.get("census_enabled", False)),
            records=records,
            stored_summary=dict(stored) if isinstance(stored, Mapping) else {},
        )

    @classmethod
    def from_path(cls, path: Path | str) -> LoadedCensusReport:
        target = Path(path)
        try:
            raw = target.read_text()
        except OSError as exc:
            raise CensusReportError(f"cannot read census report {target}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise CensusReportError(f"census report {target} is not valid JSON: {exc}") from exc
        return cls.from_payload(payload, path=target)

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        """Seconds between ``generated_at`` and *now*, or ``None`` if unknown.

        Negative ages (a report stamped in the future -- clock skew across
        machines) are clamped to 0 rather than reported as a negative age.
        """
        if not self.generated_at:
            return None
        try:
            stamped = datetime.fromisoformat(self.generated_at)
        except ValueError:
            return None
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - stamped).total_seconds())


@dataclass(frozen=True)
class NetPrediction:
    """Per-net roll-up of a report's crossovers -- the prediction's unit.

    ``present_on_board`` is ``None`` when the caller could not enumerate the
    board's nets (no PCB path, unparseable file); it is *not* a synonym for
    ``False``, which would silently drop every net from the prediction.
    """

    net_name: str
    crossovers: int
    saturated: int
    no_ordering_lever: int
    present_on_board: bool | None = None

    @property
    def inert(self) -> int:
        """Crossovers where no ordering key could have changed the outcome."""
        return self.saturated + self.no_ordering_lever

    @property
    def saturated_pct(self) -> float:
        if self.crossovers <= 0:
            return 0.0
        return round(100.0 * self.saturated / self.crossovers, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_name": self.net_name,
            "crossovers": self.crossovers,
            "saturated": self.saturated,
            "saturated_pct": self.saturated_pct,
            "no_ordering_lever": self.no_ordering_lever,
            "inert": self.inert,
            "present_on_board": self.present_on_board,
        }


@dataclass(frozen=True)
class CrossingTailAdvisory:
    """What a prior census report predicts about the route about to start."""

    source: str
    generated_at: str | None
    age_seconds: float | None
    census_enabled: bool
    summary: CrossingTailCensusSummary
    nets: tuple[NetPrediction, ...]
    board_nets_known: bool
    nets_in_report: int
    nets_on_board: int
    coverage_pct: float
    predicted_crossovers: int
    predicted_saturated: int
    stale: bool
    warnings: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        """Is there a prediction at all?

        ``False`` when the report measured nothing (``not-applicable``: boards
        05 and 07 today) or when the board cross-check rejected it as stale.
        Kept distinct from "predicts 0% saturation", which would read as a
        clean bill of health for a board nobody measured.
        """
        return self.summary.applicable and not self.stale

    @property
    def predicted_saturated_pct(self) -> float:
        if self.predicted_crossovers <= 0:
            return 0.0
        return round(100.0 * self.predicted_saturated / self.predicted_crossovers, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisory": "crosstail-census",
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "generated_at": self.generated_at,
            "age_seconds": (None if self.age_seconds is None else round(self.age_seconds, 3)),
            "census_enabled": self.census_enabled,
            "applicable": self.applicable,
            "stale": self.stale,
            "board_nets_known": self.board_nets_known,
            "nets_in_report": self.nets_in_report,
            "nets_on_board": self.nets_on_board,
            "coverage_pct": self.coverage_pct,
            "predicted_crossovers": self.predicted_crossovers,
            "predicted_saturated": self.predicted_saturated,
            "predicted_saturated_pct": self.predicted_saturated_pct,
            "prior_summary": self.summary.to_dict(),
            "nets": [n.to_dict() for n in self.nets],
            "warnings": list(self.warnings),
        }

    def format_human(self) -> str:
        """Greppable ``[crosstail-advisory]`` block, printed before routing."""
        tag = "[crosstail-advisory]"
        age = ""
        if self.age_seconds is not None:
            age = f", {_format_age(self.age_seconds)} old"
        lines = [f"{tag} pre-route prediction from {self.source}{age}"]
        s = self.summary
        if not s.applicable:
            lines.append(
                f"{tag}   prior run scanned 0 crossover(s) -- NOT APPLICABLE, "
                f"no prediction (this is not a 0%-saturated result)"
            )
        else:
            lines.append(
                f"{tag}   prior run: {s.crossovers_scanned} crossover(s), "
                f"{s.saturated} saturated ({s.saturated_pct}%), "
                f"inert {s.inert_pct}%, verdict={s.verdict}"
            )
            if self.board_nets_known:
                lines.append(
                    f"{tag}   board cross-check: {self.nets_on_board}/"
                    f"{self.nets_in_report} report net(s) still on this board "
                    f"({self.coverage_pct}% coverage)"
                )
            else:
                lines.append(
                    f"{tag}   board cross-check: skipped (board nets unavailable); "
                    f"predicting from all {self.nets_in_report} report net(s)"
                )
        if s.applicable and self.stale:
            # A block that says "suppressed" and then prints the numbers has
            # not suppressed anything -- a reader is entitled to use whatever
            # is on screen.  The rejected counts stay in to_dict() for tooling
            # that wants to inspect what was thrown away.
            lines.append(
                f"{tag}   prediction SUPPRESSED -- this report does not "
                f"describe the board being routed (see WARNING below)"
            )
        elif s.applicable:
            lines.append(
                f"{tag}   predicted this run: {self.predicted_saturated}/"
                f"{self.predicted_crossovers} saturated crossover(s) "
                f"({self.predicted_saturated_pct}%)"
            )
            # Only nets that actually contribute to the prediction are named:
            # listing a net the board no longer has, next to a predicted count
            # that deliberately excludes it, reads as an inconsistency.  The
            # full per-net roll-up (absent nets included) stays in to_dict().
            saturated_nets = [
                n for n in self.nets if n.saturated > 0 and n.present_on_board is not False
            ]
            worst = saturated_nets[:WORST_NETS_SHOWN]
            if worst:
                rendered = ", ".join(f"{n.net_name} {n.saturated}/{n.crossovers}" for n in worst)
                elided = len(saturated_nets) - len(worst)
                suffix = f" (+{elided} more)" if elided > 0 else ""
                lines.append(f"{tag}   worst nets: {rendered}{suffix}")
        for warning in self.warnings:
            lines.append(f"{tag}   WARNING: {warning}")
        if self.applicable and s.verdict in (VERDICT_SATURATED, VERDICT_NO_ORDERING_LEVER):
            lines.append(
                f"{tag}   reading: ordering levers are inert here -- the "
                f"diff-pair shadow phase will spend its budget without a lever "
                f"to pull; the constraint is upstream in placement / escape planning"
            )
        lines.append(f"{tag}   ADVISORY ONLY -- this route is unchanged by the above (#4799)")
        return "\n".join(lines)


def _format_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def build_advisory(
    report: LoadedCensusReport,
    board_nets: Iterable[str] | None = None,
    *,
    saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
    stale_coverage_pct: float = STALE_COVERAGE_PCT_THRESHOLD,
    now: datetime | None = None,
) -> CrossingTailAdvisory:
    """Interpret *report* as a prediction for a route about to start.

    *board_nets* is the net-name set of the board being routed.  When given,
    only the report's nets that still exist there contribute to the predicted
    counts, and their overlap decides staleness; when ``None`` (unknown, not
    empty) the whole report contributes and the block says so.
    """
    known_nets: set[str] | None = None if board_nets is None else set(board_nets)
    summary = CrossingTailCensusSummary.from_records(
        report.records, saturated_threshold_pct=saturated_threshold_pct
    )

    per_net: dict[str, list[int]] = {}
    for record in report.records:
        bucket = per_net.setdefault(record.net_name, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += 1 if record.saturated else 0
        bucket[2] += 1 if record.no_ordering_lever else 0

    predictions = [
        NetPrediction(
            net_name=name,
            crossovers=counts[0],
            saturated=counts[1],
            no_ordering_lever=counts[2],
            present_on_board=None if known_nets is None else name in known_nets,
        )
        for name, counts in per_net.items()
    ]
    # Worst first, then alphabetical: the head of this list is what the human
    # block names, and a tie must not depend on dict insertion order.
    predictions.sort(key=lambda n: (-n.saturated, -n.crossovers, n.net_name))

    contributing = [n for n in predictions if n.present_on_board is not False]
    predicted_crossovers = sum(n.crossovers for n in contributing)
    predicted_saturated = sum(n.saturated for n in contributing)

    nets_in_report = len(predictions)
    nets_on_board = (
        nets_in_report if known_nets is None else sum(1 for n in predictions if n.present_on_board)
    )
    coverage_pct = _pct(nets_on_board, nets_in_report)

    warnings: list[str] = []
    if report.schema_version != SCHEMA_VERSION:
        warnings.append(
            f"report schema_version={report.schema_version} but this build "
            f"reads v{SCHEMA_VERSION}; fields may be missing"
        )
    if not report.census_enabled:
        warnings.append(
            "report was written with the census disabled "
            "(KCT_CROSSTAIL_CENSUS unset) -- nothing was measured"
        )
    stored_scanned = report.stored_summary.get("crossovers_scanned")
    if stored_scanned is not None and _as_int(stored_scanned, -1) != summary.crossovers_scanned:
        warnings.append(
            f"report's stored summary claims {stored_scanned} crossover(s) but "
            f"{summary.crossovers_scanned} record(s) are present; re-aggregated "
            "from the records"
        )

    stale = False
    if known_nets is not None and nets_in_report > 0 and coverage_pct < stale_coverage_pct:
        stale = True
        warnings.append(
            f"only {coverage_pct}% of the report's nets exist on this board "
            f"(< {stale_coverage_pct}%); the report was probably measured on a "
            "different design -- prediction suppressed"
        )

    return CrossingTailAdvisory(
        source=str(report.path) if report.path else "<payload>",
        generated_at=report.generated_at,
        age_seconds=report.age_seconds(now=now),
        census_enabled=report.census_enabled,
        summary=summary,
        nets=tuple(predictions),
        board_nets_known=known_nets is not None,
        nets_in_report=nets_in_report,
        nets_on_board=nets_on_board,
        coverage_pct=coverage_pct,
        predicted_crossovers=predicted_crossovers,
        predicted_saturated=predicted_saturated,
        stale=stale,
        warnings=tuple(warnings),
    )


def advisory_path_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    """Path from :data:`ADVISORY_ENV_VAR`, or ``None`` when unset/blank."""
    raw = (env if env is not None else os.environ).get(ADVISORY_ENV_VAR, "")
    raw = raw.strip()
    return Path(raw) if raw else None


def board_net_names(pcb_path: Path | str) -> set[str] | None:
    """Net names declared by *pcb_path*, or ``None`` when they cannot be read.

    ``None`` means *unknown* and switches the cross-check off; it is returned
    for a missing file, an unparseable one, and for a board that declares no
    nets at all -- the last because an empty set is indistinguishable from a
    failed parse here, and taking it literally would mark every net in the
    report "absent" and wrongly condemn the report as stale.

    Net 0 (the empty name) is dropped: it is the unconnected sentinel, never a
    crossover's net.
    """
    try:
        target = Path(pcb_path)
        if not target.is_file():
            return None
        from kicad_tools.pcb.editor import PCBEditor

        editor = PCBEditor(str(target))
        names = {str(name) for name in editor.nets if str(name)}
    except Exception:
        return None
    return names or None


def emit_advisory(
    report_path: Path | str,
    pcb_path: Path | str | None = None,
    *,
    stream: Any = None,
    saturated_threshold_pct: float = SATURATED_PCT_ADVISORY_THRESHOLD,
) -> CrossingTailAdvisory | None:
    """Print the pre-route advisory for *report_path*.  Never raises.

    Returns the advisory, or ``None`` when the report could not be read (a
    one-line diagnostic goes to *stream*).  A missing or malformed report is a
    missing diagnostic, not a reason to refuse to route -- the
    ``_offboard_preflight`` precedent.
    """
    import sys

    out = stream if stream is not None else sys.stderr
    try:
        report = LoadedCensusReport.from_path(report_path)
    except CensusReportError as exc:
        print(f"[crosstail-advisory] no prediction: {exc}", file=out)
        return None
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        print(
            f"[crosstail-advisory] no prediction from {report_path}: {exc}",
            file=out,
        )
        return None
    board_nets = board_net_names(pcb_path) if pcb_path is not None else None
    advisory = build_advisory(
        report,
        board_nets,
        saturated_threshold_pct=saturated_threshold_pct,
    )
    print(advisory.format_human(), file=out)
    return advisory
