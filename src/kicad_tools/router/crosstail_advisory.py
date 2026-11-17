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

**Advisory by default.**  Reading a report changes nothing: :func:`emit_advisory`
swallows every error into a stderr diagnostic, on the ``_offboard_preflight``
precedent that report-only surfaces must never block a route.

**Opt-in go/no-go.**  :func:`evaluate_gate` turns that same prediction into a
decision for callers that ask for one (``kct route --census-advisory-gate``):
when the replayed prediction is applicable, trusted, and inert at or above the
threshold, the route aborts with :data:`GATE_EXIT_CODE` *before the first A\\*
expansion*, naming the worst nets and pointing at the layer that can actually
fix them (placement / escape planning).  The gate is deliberately hard to
trigger by accident -- every condition that makes the prediction less than
trustworthy (no report, nothing measured, a stale/suppressed cross-check, a
census that was never enabled, an unknown schema) resolves to **GO**, because
"we could not predict" must never read as "we predict failure".
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
    "GATE_EXIT_CODE",
    "GATE_REASON_BELOW_THRESHOLD",
    "GATE_REASON_CENSUS_DISABLED",
    "GATE_REASON_INERT",
    "GATE_REASON_NOT_APPLICABLE",
    "GATE_REASON_NO_BOARD_CROSSOVERS",
    "GATE_REASON_NO_REPORT",
    "GATE_REASON_SATURATED",
    "GATE_REASON_SCHEMA_MISMATCH",
    "GATE_REASON_STALE",
    "STALE_COVERAGE_PCT_THRESHOLD",
    "WORST_NETS_SHOWN",
    "CensusGateDecision",
    "CensusReportError",
    "CrossingTailAdvisory",
    "LoadedCensusReport",
    "NetPrediction",
    "advisory_path_from_env",
    "board_net_names",
    "build_advisory",
    "emit_advisory",
    "emit_gate_decision",
    "evaluate_gate",
    "parse_gate_threshold_pct",
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

#: Exit code ``kct route`` returns when the opt-in census gate says NO-GO.
#: Distinct from every code already on the route ladder (0-8; see the
#: ``exit codes:`` epilog in ``cli/route_cmd.py``) so CI can tell "refused to
#: start -- the lattice was measured inert" apart from "started and failed",
#: which is the whole point of a pre-route gate.
GATE_EXIT_CODE = 9

#: Why the gate decided what it decided.  The first six are all **GO**: they
#: describe a prediction that does not exist or cannot be trusted, and the gate
#: refuses to convert any of them into a failure.
GATE_REASON_NO_REPORT = "no-report"
GATE_REASON_NOT_APPLICABLE = "not-applicable"
GATE_REASON_STALE = "stale"
GATE_REASON_CENSUS_DISABLED = "census-disabled"
GATE_REASON_SCHEMA_MISMATCH = "schema-mismatch"
GATE_REASON_NO_BOARD_CROSSOVERS = "no-board-crossovers"
GATE_REASON_BELOW_THRESHOLD = "below-threshold"
#: NO-GO reasons.
GATE_REASON_SATURATED = "saturated"
GATE_REASON_INERT = "inert"


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
    #: Schema version the *report* declared.  Kept as a field (rather than
    #: re-derived from the warning text) because the gate has to branch on it:
    #: a document this build only half-understands must not fail a route.
    report_schema_version: int = SCHEMA_VERSION

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

    @property
    def contributing_nets(self) -> tuple[NetPrediction, ...]:
        """Report nets that still exist on the board (worst first).

        ``present_on_board is None`` (cross-check skipped) counts as present --
        the same rule :func:`build_advisory` used to compute the predicted
        counts, kept in one place so the block and the gate cannot disagree.
        """
        return tuple(n for n in self.nets if n.present_on_board is not False)

    @property
    def predicted_inert(self) -> int:
        """Predicted crossovers where no ordering key could change the outcome.

        The union of saturated and single-site-legal, restricted to nets still
        on the board -- the gate's real subject.  ``predicted_saturated`` alone
        would call a lattice healthy when its every legal set offered one site.
        """
        return sum(n.inert for n in self.contributing_nets)

    @property
    def predicted_inert_pct(self) -> float:
        if self.predicted_crossovers <= 0:
            return 0.0
        return round(100.0 * self.predicted_inert / self.predicted_crossovers, 1)

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
            "predicted_inert": self.predicted_inert,
            "predicted_inert_pct": self.predicted_inert_pct,
            "prior_summary": self.summary.to_dict(),
            "nets": [n.to_dict() for n in self.nets],
            "warnings": list(self.warnings),
        }

    def format_human(self, *, gating: bool = False) -> str:
        """Greppable ``[crosstail-advisory]`` block, printed before routing.

        *gating* only changes the trailer: a block that says "ADVISORY ONLY --
        this route is unchanged" and is then followed by an abort would be a
        lie, so an armed :func:`evaluate_gate` gets its own last line.
        """
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
        if gating:
            lines.append(
                f"{tag}   GATE ARMED (--census-advisory-gate) -- see the "
                f"[crosstail-gate] verdict below (#4799)"
            )
        else:
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
        report_schema_version=report.schema_version,
    )


@dataclass(frozen=True)
class CensusGateDecision:
    """The opt-in go/no-go verdict derived from a :class:`CrossingTailAdvisory`.

    ``gated`` is the decision; ``reason`` is the machine-readable *why* (one of
    the ``GATE_REASON_*`` tokens) so a caller never has to parse the prose.
    Every "cannot trust the prediction" path is a **GO** with its own reason --
    the distinction that keeps this gate from failing routes on the strength of
    a file that describes some other board.
    """

    gated: bool
    reason: str
    detail: str
    threshold_pct: float
    predicted_crossovers: int
    predicted_saturated: int
    predicted_saturated_pct: float
    predicted_inert: int
    predicted_inert_pct: float
    worst_nets: tuple[str, ...] = ()
    source: str | None = None

    @property
    def exit_code(self) -> int:
        """:data:`GATE_EXIT_CODE` on NO-GO, 0 on GO."""
        return GATE_EXIT_CODE if self.gated else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": "crosstail-census",
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "gated": self.gated,
            "reason": self.reason,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "threshold_pct": self.threshold_pct,
            "predicted_crossovers": self.predicted_crossovers,
            "predicted_saturated": self.predicted_saturated,
            "predicted_saturated_pct": self.predicted_saturated_pct,
            "predicted_inert": self.predicted_inert,
            "predicted_inert_pct": self.predicted_inert_pct,
            "worst_nets": list(self.worst_nets),
        }

    def format_human(self) -> str:
        """Greppable ``[crosstail-gate]`` block, printed under the advisory."""
        tag = "[crosstail-gate]"
        if not self.gated:
            return "\n".join(
                [
                    f"{tag} GO ({self.reason})",
                    f"{tag}   {self.detail}",
                ]
            )
        lines = [
            f"{tag} NO-GO ({self.reason}) -- refusing to route a lattice this board "
            f"already measured inert",
            f"{tag}   predicted {self.predicted_saturated}/{self.predicted_crossovers} "
            f"saturated ({self.predicted_saturated_pct}%), inert "
            f"{self.predicted_inert_pct}% >= threshold {self.threshold_pct}%",
        ]
        if self.worst_nets:
            lines.append(f"{tag}   worst nets: {', '.join(self.worst_nets)}")
        lines.extend(
            [
                f"{tag}   the diff-pair shadow phase would spend its whole budget "
                f"with no ordering lever to pull: no via-site sort key can create "
                f"legality that the lattice does not have",
                f"{tag}   FIX LAYER: placement / escape planning, not the router -- "
                f"give the nets above more room (re-place their sources/sinks, widen "
                f"the escape channels, or add a layer), then re-measure with "
                f"KCT_CROSSTAIL_CENSUS=1 KCT_CROSSTAIL_CENSUS_REPORT=<path>",
                f"{tag}   aborted before any router work (exit {self.exit_code}); "
                f"drop --census-advisory-gate to route anyway, or raise "
                f"--census-advisory-gate-pct (#4799)",
            ]
        )
        return "\n".join(lines)


def parse_gate_threshold_pct(value: str) -> float:
    """argparse ``type=`` for ``--census-advisory-gate-pct``.

    Rejects non-numeric and out-of-range values at parse time (argparse exit 2)
    rather than letting a typo like ``-90`` silently gate every board.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid percentage: {value!r}") from None
    if not (0.0 <= parsed <= 100.0):
        raise ValueError(f"percentage must be between 0 and 100, got {parsed}")
    return parsed


def evaluate_gate(
    advisory: CrossingTailAdvisory | None,
    *,
    threshold_pct: float | None = None,
) -> CensusGateDecision:
    """Turn a replayed prediction into an opt-in go/no-go (#4799).

    NO-GO requires **all** of:

    * a prediction exists (a report was read),
    * the prior run actually measured something (``applicable``; "not
      applicable" is 0 crossovers scanned, which is emphatically not 0%
      saturated and must never be read as either a pass or a failure),
    * the board cross-check accepted it (``not stale``) -- a suppressed
      prediction can never gate, per this module's staleness contract,
    * the report was written with the census enabled and at a schema this
      build reads (otherwise the numbers are not the numbers),
    * at least one predicted crossover survives the board cross-check, and
    * ``predicted_inert_pct >= threshold_pct``.

    *threshold_pct* defaults to the threshold the advisory's own verdict used
    (:data:`SATURATED_PCT_ADVISORY_THRESHOLD`, 90%), so the gate and the
    printed verdict cannot drift apart unless the caller asks them to.
    """
    if advisory is None:
        return CensusGateDecision(
            gated=False,
            reason=GATE_REASON_NO_REPORT,
            detail=(
                "no census report could be read, so there is no prediction to "
                "gate on; routing proceeds unchanged"
            ),
            threshold_pct=(
                SATURATED_PCT_ADVISORY_THRESHOLD if threshold_pct is None else threshold_pct
            ),
            predicted_crossovers=0,
            predicted_saturated=0,
            predicted_saturated_pct=0.0,
            predicted_inert=0,
            predicted_inert_pct=0.0,
        )

    threshold = advisory.summary.saturated_threshold_pct if threshold_pct is None else threshold_pct

    def _decide(gated: bool, reason: str, detail: str) -> CensusGateDecision:
        # Worst offenders by inert count -- the gate's subject is inertness, not
        # saturation alone, so a net whose every legal set has a single site
        # must be nameable here even though it saturates nowhere.
        offenders = sorted(
            (n for n in advisory.contributing_nets if n.inert > 0),
            key=lambda n: (-n.inert, -n.crossovers, n.net_name),
        )
        worst = tuple(
            f"{n.net_name} inert {n.inert}/{n.crossovers}" for n in offenders[:WORST_NETS_SHOWN]
        )
        return CensusGateDecision(
            gated=gated,
            reason=reason,
            detail=detail,
            threshold_pct=threshold,
            predicted_crossovers=advisory.predicted_crossovers,
            predicted_saturated=advisory.predicted_saturated,
            predicted_saturated_pct=advisory.predicted_saturated_pct,
            predicted_inert=advisory.predicted_inert,
            predicted_inert_pct=advisory.predicted_inert_pct,
            worst_nets=worst if gated else (),
            source=advisory.source,
        )

    if not advisory.summary.applicable:
        return _decide(
            False,
            GATE_REASON_NOT_APPLICABLE,
            "the prior run scanned 0 crossover(s) (verdict=not-applicable): "
            "nothing was measured, which is not a 0%-saturated result and not a "
            "failing one either -- never gating on it",
        )
    if advisory.stale:
        return _decide(
            False,
            GATE_REASON_STALE,
            f"the prediction was suppressed as stale ({advisory.coverage_pct}% of "
            f"the report's nets are on this board); a measurement of a different "
            f"design must never fail this route",
        )
    if not advisory.census_enabled:
        return _decide(
            False,
            GATE_REASON_CENSUS_DISABLED,
            "the report was written with the census disabled "
            "(KCT_CROSSTAIL_CENSUS unset), so its lattice figures are not "
            "measurements -- not gating",
        )
    if advisory.summary.crossovers_scanned and advisory.nets_in_report == 0:
        # Defensive: records with no net names at all.
        return _decide(
            False,
            GATE_REASON_NO_BOARD_CROSSOVERS,
            "the report names no nets, so nothing can be matched to this board",
        )
    if advisory.predicted_crossovers <= 0:
        return _decide(
            False,
            GATE_REASON_NO_BOARD_CROSSOVERS,
            "none of the report's measured crossovers belong to a net that is "
            "still on this board -- nothing to predict",
        )
    if advisory.report_schema_version != SCHEMA_VERSION:
        return _decide(
            False,
            GATE_REASON_SCHEMA_MISMATCH,
            f"the report declares schema v{advisory.report_schema_version} and "
            f"this build reads v{SCHEMA_VERSION}; fields may be missing, so its "
            "numbers are not trusted to fail a route",
        )

    if advisory.predicted_inert_pct < threshold:
        return _decide(
            False,
            GATE_REASON_BELOW_THRESHOLD,
            f"predicted inert {advisory.predicted_inert_pct}% < threshold "
            f"{threshold}% ({advisory.predicted_saturated}/"
            f"{advisory.predicted_crossovers} saturated); ordering levers remain, "
            "routing proceeds",
        )
    reason = (
        GATE_REASON_SATURATED
        if advisory.predicted_saturated_pct >= threshold
        else GATE_REASON_INERT
    )
    return _decide(
        True,
        reason,
        f"predicted inert {advisory.predicted_inert_pct}% >= threshold {threshold}%",
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
    gating: bool = False,
) -> CrossingTailAdvisory | None:
    """Print the pre-route advisory for *report_path*.  Never raises.

    Returns the advisory, or ``None`` when the report could not be read (a
    one-line diagnostic goes to *stream*).  A missing or malformed report is a
    missing diagnostic, not a reason to refuse to route -- the
    ``_offboard_preflight`` precedent.

    *gating* only affects the block's trailer (see
    :meth:`CrossingTailAdvisory.format_human`); the decision itself is
    :func:`evaluate_gate`'s, so printing and deciding stay separable.
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
    print(advisory.format_human(gating=gating), file=out)
    return advisory


def emit_gate_decision(
    advisory: CrossingTailAdvisory | None,
    *,
    threshold_pct: float | None = None,
    stream: Any = None,
) -> CensusGateDecision:
    """Print and return the go/no-go for *advisory*.  Never raises.

    A decision that cannot be computed is a **GO**: the caller gets an
    unconditional :class:`CensusGateDecision` and routing proceeds, because an
    exception in a predictor is not evidence about the board.
    """
    import sys

    out = stream if stream is not None else sys.stderr
    try:
        decision = evaluate_gate(advisory, threshold_pct=threshold_pct)
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        print(f"[crosstail-gate] GO (error) -- gate could not run: {exc}", file=out)
        return CensusGateDecision(
            gated=False,
            reason="error",
            detail=f"gate could not run: {exc}",
            threshold_pct=(
                SATURATED_PCT_ADVISORY_THRESHOLD if threshold_pct is None else threshold_pct
            ),
            predicted_crossovers=0,
            predicted_saturated=0,
            predicted_saturated_pct=0.0,
            predicted_inert=0,
            predicted_inert_pct=0.0,
        )
    print(decision.format_human(), file=out)
    return decision
