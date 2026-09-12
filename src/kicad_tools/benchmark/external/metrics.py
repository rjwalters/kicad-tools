"""DeepPCB-comparable benchmark metrics for an externally-sourced board.

Issue #4934 (Epic #4932, Phase 1). This is the *measurement layer* of the
external autorouter benchmark harness: given a routed ``.kicad_pcb`` it
emits the metric tuple DeepPCB publishes, plus the stricter gates that are
this project's differentiator, as a JSON report with a stable schema.

Design rules this module exists to enforce
------------------------------------------

**Every headline number is measured from the board FILE, never from a
router-internal counter.** Wirelength, via count, and completion are all
re-derived from the ``.kicad_pcb`` on disk, so a board routed by *any*
path -- our router, a vendor's, a human -- produces numbers on the same
footing. A router that miscounts its own vias cannot flatter itself here.

**Completion is counted in ratsnest connections, not nets or pads.**
DeepPCB reports "98 of 98 airwires" / "210 of 210 connections", i.e. the
number of ratsnest lines resolved out of the number a fully ripped-up
board would show. That is ``Σ (island_count - 1)`` remaining out of
``Σ (pads - 1)`` required, which is exactly what
:class:`~kicad_tools.analysis.net_status.NetStatusResult` now exposes
(``island_count`` was surfaced for this issue). Counting *pads* instead
would over-report the deficit: three pads stranded together on one island
are ONE missing connection, not two.

**Timing is refused, not fudged, on the Python fallback.** A wall-clock
number produced without the C++ router extension is 10-100x off and would
poison any published comparison, so :func:`build_timing` marks it invalid
and drops the number entirely unless the ``cpp`` backend was active. The
JSON always records which backend was live.

**Two DRC engines, both reported.** ``kct check`` (our internal engine,
which sees diff-pair / match-group rules KiCad cannot express) AND
``kicad-cli pcb drc --refill-zones`` (the fab-accurate cross-gate, which
sees connectivity shorts our engine can miss on stale zone fills). Per
the established process rule, ``kct check`` alone is not sufficient
evidence of a clean board, so the schema has a required slot for both.

Schema documentation: ``docs/benchmark-external-report-schema.md``.

Example::

    from kicad_tools.benchmark.external import collect_report

    report = collect_report(
        "strf.kicad_pcb",
        board_id="strf",
        board_commit="a1b2c3d",
        protocol="zero-touch",
        wall_clock_s=142.7,
    )
    print(report.to_json())
"""

from __future__ import annotations

import math
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "SCHEMA_URL",
    "SCHEMA_VERSION",
    "ARTIFACT_SOURCE_FALLBACK_INPUT",
    "ARTIFACT_SOURCE_ROUTER_OUTPUT",
    "ARTIFACT_SOURCE_UNKNOWN",
    "ROUTE_OUTCOME_COMPLETED",
    "ROUTE_OUTCOME_FAILED",
    "ROUTE_OUTCOME_PARTIAL",
    "ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING",
    "ROUTE_OUTCOME_TIMEOUT",
    "ROUTE_OUTCOME_UNKNOWN",
    "VALID_ARTIFACT_SOURCES",
    "VALID_ROUTE_OUTCOMES",
    "BackendInfo",
    "BenchmarkReport",
    "CompletionMetrics",
    "CopperMetrics",
    "DiffPairCompletion",
    "KctCheckSummary",
    "KicadCliDrcSummary",
    "RouteOutcome",
    "TimingMetrics",
    "build_route_outcome",
    "build_timing",
    "collect_report",
    "measure_completion",
    "measure_copper",
    "measure_diff_pairs",
    "probe_backend",
    "run_kct_check",
    "run_kicad_cli_drc",
]

# Bump when a field is REMOVED or its meaning changes. Purely additive
# fields do not require a bump (same policy as ``docs/board-json-schema.md``).
# Issue #5280 (Epic #5278 Phase 1) added ``route_outcome`` and
# ``pre_route_completion`` -- both purely additive, so v1 stands. A report
# that predates them (schema v1, generated before #5280 landed) simply omits
# those keys; consumers must treat the omission as "unknown", never as
# success -- see "Legacy reports" in
# ``docs/benchmark-external-report-schema.md``.
SCHEMA_VERSION = 1
SCHEMA_URL = "https://kicad-tools.org/schemas/benchmark-external/v1.json"

# Protocol tags mirroring Epic #4932's two comparison protocols. Free-form
# strings are accepted (the harness may add more), but these two are the
# ones the published table is keyed on.
PROTOCOL_ZERO_TOUCH = "zero-touch"
PROTOCOL_TUNED = "tuned"

# The only backend under which a timing number is publishable (#4932 risk
# register: "a Python-fallback timing number is invalid and must be
# refused by the harness").
TIMING_VALID_BACKEND = "cpp"

# ---------------------------------------------------------------------------
# Route outcome vocabulary (issue #5280, Epic #5278 Phase 1)
# ---------------------------------------------------------------------------

# The router ran to completion and every required connection is routed.
ROUTE_OUTCOME_COMPLETED = "completed"
# The router ran (and produced output) but the board is not 100% routed --
# normal for the zero-touch protocol on a hard board, NOT a failure.
ROUTE_OUTCOME_PARTIAL = "partial"
# The router refused/exited non-zero before writing any output for this
# attempt. No routing progress was captured.
ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING = "stopped_before_routing"
# The router raised or otherwise failed in a way that is not a clean
# pre-route refusal.
ROUTE_OUTCOME_FAILED = "failed"
# The attempt was abandoned because it exceeded a time budget.
ROUTE_OUTCOME_TIMEOUT = "timeout"
# No route-attempt evidence was supplied (a bare re-measurement via
# ``collect_report``), or the evidence available contradicts itself (e.g.
# exit 0 but no output file). Never collapsed into a success-shaped record.
ROUTE_OUTCOME_UNKNOWN = "unknown"

VALID_ROUTE_OUTCOMES = frozenset(
    {
        ROUTE_OUTCOME_COMPLETED,
        ROUTE_OUTCOME_PARTIAL,
        ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING,
        ROUTE_OUTCOME_FAILED,
        ROUTE_OUTCOME_TIMEOUT,
        ROUTE_OUTCOME_UNKNOWN,
    }
)

# The measured board IS what the router wrote for this attempt.
ARTIFACT_SOURCE_ROUTER_OUTPUT = "router_output"
# The router produced no (usable) output for this attempt; the measured
# board is the pre-route input instead. This must never be allowed to look
# like a routed board downstream.
ARTIFACT_SOURCE_FALLBACK_INPUT = "fallback_input"
# No evidence was supplied about where the measured board came from.
ARTIFACT_SOURCE_UNKNOWN = "unknown"

VALID_ARTIFACT_SOURCES = frozenset(
    {
        ARTIFACT_SOURCE_ROUTER_OUTPUT,
        ARTIFACT_SOURCE_FALLBACK_INPUT,
        ARTIFACT_SOURCE_UNKNOWN,
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_commit() -> str:
    """Short git SHA of the kicad-tools checkout producing this report."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


# ---------------------------------------------------------------------------
# Completion (DeepPCB headline metric #1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionMetrics:
    """Routing completion in DeepPCB's own unit: ratsnest connections.

    Attributes:
        connections_routed: Numerator -- connections satisfied by copper.
        connections_total: Denominator -- connections a fully ripped-up
            copy of this board would show (``Σ max(pads - 1, 0)``).
        nets_total: Nets considered (net 0 and unnamed nets excluded, as
            :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer`
            does).
        nets_complete / nets_incomplete / nets_unrouted: the per-net
            rollup, kept alongside the connection counts because a board
            at 99% connections with one wholly-unrouted net is a
            materially different result from one with a single stranded
            pad.
        nets_blocking_incomplete: incomplete nets after plane/pour
            stitching residuals are reclassified as advisory -- the count
            the ship-ready gate actually uses.
    """

    connections_routed: int
    connections_total: int
    nets_total: int
    nets_complete: int
    nets_incomplete: int
    nets_unrouted: int
    nets_blocking_incomplete: int

    @property
    def completion_pct(self) -> float:
        """Routed share of required connections, 0-100.

        ``100.0`` when there is nothing to route, so an empty board is not
        reported as a routing failure.
        """
        if self.connections_total == 0:
            return 100.0
        return (self.connections_routed / self.connections_total) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "connections_routed": self.connections_routed,
            "connections_total": self.connections_total,
            "completion_pct": round(self.completion_pct, 2),
            "nets_total": self.nets_total,
            "nets_complete": self.nets_complete,
            "nets_incomplete": self.nets_incomplete,
            "nets_unrouted": self.nets_unrouted,
            "nets_blocking_incomplete": self.nets_blocking_incomplete,
        }


def measure_completion(board: str | Path | PCB, *, strict: bool = True) -> CompletionMetrics:
    """Measure routing completion from the board itself.

    Reuses :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` --
    the same connectivity model ``kct net-status`` and the CI plane-net
    gate trust -- rather than re-deriving connectivity here.

    Args:
        board: Path to a ``.kicad_pcb`` or an already-loaded ``PCB``.
        strict: Real-geometry (shapely) copper contact, matching KiCad's
            connectivity semantics. Leave ``True`` for published numbers;
            the legacy proximity model diverges from KiCad in BOTH
            directions and would make the comparison dishonest.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    result = NetStatusAnalyzer(board, strict=strict).analyze()
    return CompletionMetrics(
        connections_routed=result.routed_connections,
        connections_total=result.total_connections,
        nets_total=result.total_nets,
        nets_complete=result.complete_count,
        nets_incomplete=result.incomplete_count,
        nets_unrouted=result.unrouted_count,
        nets_blocking_incomplete=result.blocking_incomplete_count,
    )


# ---------------------------------------------------------------------------
# Copper: vias + wirelength (DeepPCB headline metrics #2 and #3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopperMetrics:
    """Via count and total wirelength, measured from the board file.

    Attributes:
        via_count: Every ``(via ...)`` element on the board. On a
            ripped-up benchmark input every via is router-placed, which is
            what makes this directly comparable to DeepPCB's "68 vias".
        wirelength_mm: Total copper track length. Segments plus copper
            arc tracks (KiCad 7+ rounded tracks), both read through the PCB
            schema. Curved tracks contribute their swept centerline length,
            exactly once, rather than the straight chord between endpoints.
        segment_count / arc_count: the populations behind ``wirelength_mm``.
        wirelength_by_layer_mm: per-copper-layer breakdown, for the
            per-board annotations the published table carries.
    """

    via_count: int
    wirelength_mm: float
    segment_count: int
    arc_count: int
    wirelength_by_layer_mm: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "via_count": self.via_count,
            "wirelength_mm": round(self.wirelength_mm, 2),
            "segment_count": self.segment_count,
            "arc_count": self.arc_count,
            "wirelength_by_layer_mm": {
                layer: round(length, 2)
                for layer, length in sorted(self.wirelength_by_layer_mm.items())
            },
        }


def _is_copper_layer(layer: str) -> bool:
    return layer.endswith(".Cu")


def measure_copper(pcb_path: str | Path) -> CopperMetrics:
    """Measure via count and total wirelength from the board file.

    Takes a PATH, not a ``PCB``, on purpose: the acceptance criterion is
    that these numbers come from the board file itself so any router path
    produces comparable figures, including curved tracks exposed by the PCB schema.
    """
    from kicad_tools.schema.pcb import PCB

    path = Path(pcb_path)
    pcb = PCB.load(str(path))

    by_layer: dict[str, float] = {}
    segment_count = 0
    for seg in pcb.segments:
        if not _is_copper_layer(seg.layer):
            continue
        segment_count += 1
        by_layer[seg.layer] = by_layer.get(seg.layer, 0.0) + math.dist(seg.start, seg.end)

    arcs = pcb.arcs
    for arc in arcs:
        by_layer[arc.layer] = by_layer.get(arc.layer, 0.0) + arc.length

    return CopperMetrics(
        via_count=len(pcb.vias),
        wirelength_mm=sum(by_layer.values()),
        segment_count=segment_count,
        arc_count=len(arcs),
        wirelength_by_layer_mm=by_layer,
    )


# ---------------------------------------------------------------------------
# Environment validity: which backend was live (#4932 timing risk)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendInfo:
    """Which router backend was active when the routing pass ran.

    Populated from the SAME probe ``kct build-native --check`` uses
    (:func:`kicad_tools.router.cpp_backend.probe_backend_info`), so a
    report and the CLI can never disagree about the environment.
    """

    backend: str  # "cpp" | "python" | "unknown"
    available: bool
    version: str | None = None
    build_version: int | None = None
    unavailable_reason: str | None = None

    @property
    def timing_valid(self) -> bool:
        """Whether a wall-clock number taken under this backend is publishable."""
        return self.available and self.backend == TIMING_VALID_BACKEND

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "available": self.available,
            "version": self.version,
            "build_version": self.build_version,
            "unavailable_reason": self.unavailable_reason,
        }


def probe_backend() -> BackendInfo:
    """Probe the C++ router extension exactly as ``kct build-native --check``."""
    from kicad_tools.router.cpp_backend import probe_backend_info

    info = probe_backend_info()
    build_version = info.get("build_version")
    return BackendInfo(
        backend=str(info.get("backend", "unknown")),
        available=bool(info.get("available", False)),
        version=info.get("version"),
        build_version=build_version if isinstance(build_version, int) else None,
        unavailable_reason=info.get("unavailable_reason"),
    )


@dataclass(frozen=True)
class RouteOutcome:
    """What actually happened on this attempt, and where the measured board came from.

    This is the structured answer to Epic #5278 Phase 1's core complaint: a
    report that only carries ``connections_routed`` + a completion % +
    a valid-looking timing cannot be told apart from a genuinely completed
    route. ``outcome`` (one of the ``ROUTE_OUTCOME_*`` constants) and
    ``artifact_source`` (one of the ``ARTIFACT_SOURCE_*`` constants) are
    reported as SEPARATE axes -- a router can exit 0 without finishing
    (``partial`` + ``router_output``), or refuse before writing anything
    (``stopped_before_routing`` + ``fallback_input``) -- so no downstream
    renderer can collapse them into one success-shaped cell.

    ``None`` (not this type at all) on :class:`BenchmarkReport` means no
    route-attempt evidence was supplied to :func:`collect_report` -- e.g. a
    bare re-measurement of an already-produced board, or (far more common in
    practice) a report generated before this issue existed. Consumers MUST
    treat that absence as unknown, never as an implicit success.
    """

    outcome: str
    artifact_source: str
    exit_code: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "artifact_source": self.artifact_source,
            "exit_code": self.exit_code,
            "reason": self.reason,
        }


def build_route_outcome(
    *,
    exit_code: int | None,
    output_exists: bool,
    completion_pct: float | None = None,
    connections_total: int | None = None,
    exception: BaseException | None = None,
    timed_out: bool = False,
    stopped_before_routing: bool = False,
) -> RouteOutcome:
    """Classify a routing attempt from the evidence actually captured.

    Only classifies what the caller can actually attest to -- an exit code,
    whether an output file exists FOR THIS ATTEMPT (never a stale artifact
    from a previous run; see :func:`kicad_tools.cli.commands.bench._run_one_board`'s
    per-attempt output ownership), and whether the call raised. Nothing here
    infers intent the caller did not supply.

    Args:
        exit_code: The router's reported exit code, or ``None`` when the
            call raised before returning one.
        output_exists: Whether THIS attempt wrote an output file (the
            caller is responsible for ensuring this reflects only the
            current attempt, not a leftover from a prior run).
        completion_pct / connections_total: The measured completion of the
            artifact actually being reported on, used only to distinguish
            ``completed`` from ``partial`` when the router claims success.
        exception: The exception the route call raised, if any.
        timed_out: Whether the attempt was abandoned for exceeding a time
            budget (reported as :data:`ROUTE_OUTCOME_TIMEOUT` rather than
            :data:`ROUTE_OUTCOME_FAILED`).
        stopped_before_routing: Explicit preflight refusal evidence; never
            infer this from output absence alone.
    """
    artifact_source = (
        ARTIFACT_SOURCE_ROUTER_OUTPUT if output_exists else ARTIFACT_SOURCE_FALLBACK_INPUT
    )

    if timed_out:
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_TIMEOUT,
            artifact_source=artifact_source,
            exit_code=exit_code,
            reason=(
                "the routing attempt exceeded its time budget"
                + (
                    " -- measuring the partial output it wrote before being stopped"
                    if output_exists
                    else " -- no output was written before it was stopped, "
                    "measuring the pre-route input as fallback"
                )
            ),
        )

    if stopped_before_routing and not output_exists and exit_code not in (None, 0):
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_STOPPED_BEFORE_ROUTING,
            artifact_source=artifact_source,
            exit_code=exit_code,
            reason="an explicit preflight gate refused the attempt before routing",
        )

    if exception is not None:
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_FAILED,
            artifact_source=artifact_source,
            exit_code=exit_code,
            reason=(
                f"the router raised {type(exception).__name__}: {exception} -- "
                + (
                    "measuring the partial output it wrote before failing"
                    if output_exists
                    else "no output was written, measuring the pre-route input as fallback"
                )
            ),
        )

    if exit_code is None:
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_UNKNOWN,
            artifact_source=ARTIFACT_SOURCE_UNKNOWN,
            exit_code=None,
            reason="no route attempt was recorded for this report",
        )

    if exit_code == 0:
        if not output_exists:
            return RouteOutcome(
                outcome=ROUTE_OUTCOME_UNKNOWN,
                artifact_source=ARTIFACT_SOURCE_FALLBACK_INPUT,
                exit_code=exit_code,
                reason=(
                    "the router reported success (exit 0) but produced no output "
                    "file for this attempt -- this contradiction cannot be "
                    "resolved from available evidence, so the outcome is unknown "
                    "rather than assumed successful; measuring the pre-route "
                    "input as fallback"
                ),
            )
        is_complete = connections_total == 0 or (
            completion_pct is not None and completion_pct >= 100.0 - 1e-9
        )
        if is_complete:
            return RouteOutcome(
                outcome=ROUTE_OUTCOME_COMPLETED,
                artifact_source=artifact_source,
                exit_code=exit_code,
                reason=None,
            )
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_PARTIAL,
            artifact_source=artifact_source,
            exit_code=exit_code,
            reason=(
                "the router exited 0 but the measured board is not at 100% "
                "connection completion -- a normal zero-touch/tuned outcome on "
                "a hard board, not a failure"
            ),
        )

    # Non-zero exit, no exception, no timeout.
    if output_exists:
        return RouteOutcome(
            outcome=ROUTE_OUTCOME_PARTIAL,
            artifact_source=artifact_source,
            exit_code=exit_code,
            reason=(
                f"the router exited {exit_code} (non-zero) but wrote an output "
                "file for this attempt -- measuring the partial progress it "
                "made, not treating this as success"
            ),
        )
    return RouteOutcome(
        outcome=ROUTE_OUTCOME_FAILED,
        artifact_source=artifact_source,
        exit_code=exit_code,
        reason=(
            f"the router exited {exit_code} (non-zero) and produced no output "
            "file for this attempt -- no routing progress was captured; "
            "measuring the pre-route input as fallback"
        ),
    )


@dataclass(frozen=True)
class TimingMetrics:
    """Wall-clock runtime of the routing pass, or an explicit refusal.

    ``wall_clock_s`` is ``None`` whenever ``valid`` is ``False``. That is
    deliberate: dropping the number rather than shipping it with a caveat
    flag makes it impossible for a downstream renderer to accidentally
    publish a Python-fallback timing (Epic #4932's stated risk).

    ``measured_phase`` (issue #5280) labels what the elapsed time actually
    measures -- one of the ``ROUTE_OUTCOME_*`` constants, or ``"unknown"``
    when no route-attempt evidence was supplied. A valid, backend-eligible
    timing on a ``stopped_before_routing`` or ``failed`` attempt is real
    elapsed time, but it is time-to-refusal, NOT a completed-routing
    performance number -- native backend availability alone does not make
    it one, so every renderer must show this label whenever the phase is
    not ``completed``.
    """

    wall_clock_s: float | None
    valid: bool
    refusal_reason: str | None = None
    measured_phase: str = ROUTE_OUTCOME_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_clock_s": (
                round(self.wall_clock_s, 3) if self.wall_clock_s is not None else None
            ),
            "valid": self.valid,
            "refusal_reason": self.refusal_reason,
            "measured_phase": self.measured_phase,
        }


def build_timing(
    wall_clock_s: float | None,
    backend: BackendInfo,
    *,
    route_outcome: RouteOutcome | None = None,
) -> TimingMetrics:
    """Accept or refuse a measured wall-clock time given the live backend.

    A timing is published only when the C++ router extension was active.
    Anything else -- Python fallback, a probe that could not run, or no
    measurement at all -- yields ``valid=False`` and no number.

    ``route_outcome``, when supplied, labels ``measured_phase`` so a
    published number can never be read as a completed-routing performance
    figure when the attempt was actually stopped before routing, partial,
    or failed (#5280).
    """
    phase = route_outcome.outcome if route_outcome is not None else ROUTE_OUTCOME_UNKNOWN
    if wall_clock_s is None:
        return TimingMetrics(
            wall_clock_s=None,
            valid=False,
            refusal_reason="no routing pass was timed for this report",
            measured_phase=phase,
        )
    if not backend.timing_valid:
        detail = backend.unavailable_reason or f"active backend is {backend.backend!r}"
        return TimingMetrics(
            wall_clock_s=None,
            valid=False,
            refusal_reason=(
                "timing refused: the C++ router backend was not active "
                f"({detail}). A Python-fallback runtime is 10-100x off and is "
                "not comparable to a published vendor number."
            ),
            measured_phase=phase,
        )
    return TimingMetrics(
        wall_clock_s=float(wall_clock_s), valid=True, refusal_reason=None, measured_phase=phase
    )


# ---------------------------------------------------------------------------
# Strict gates: kct check + the mandatory kicad-cli cross-gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KctCheckSummary:
    """Summary of the internal ``kct check`` engine's verdict."""

    ran: bool
    passed: bool | None = None
    error_count: int | None = None
    warning_count: int | None = None
    errors_by_rule: dict[str, int] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "errors_by_rule": dict(sorted(self.errors_by_rule.items())),
            "note": self.note,
        }


def run_kct_check(
    board: str | Path | PCB,
    *,
    manufacturer: str = "jlcpcb",
    layers: int = 4,
) -> KctCheckSummary:
    """Run the ``kct check`` DRC engine in-process and summarize it.

    Uses :class:`kicad_tools.validate.DRCChecker` -- the exact engine
    behind the ``kct check`` CLI -- rather than shelling out, so the
    benchmark harness does not depend on an installed console script.

    Never raises: an engine failure is reported as ``ran=False`` with the
    real error text, so a benchmark run can never silently overstate
    cleanliness.
    """
    try:
        from kicad_tools.schema.pcb import PCB as PCBClass
        from kicad_tools.validate import DRCChecker

        pcb = PCBClass.load(str(board)) if isinstance(board, (str, Path)) else board
        results = DRCChecker(pcb, manufacturer=manufacturer, layers=layers).check_all(
            pad_grid_auto_derive=True
        )
    except Exception as exc:  # pragma: no cover - defensive
        return KctCheckSummary(ran=False, note=f"kct check failed: {type(exc).__name__}: {exc}")

    errors_by_rule: dict[str, int] = {}
    for violation in results.errors:
        rule = violation.rule_id or "unknown"
        errors_by_rule[rule] = errors_by_rule.get(rule, 0) + 1

    return KctCheckSummary(
        ran=True,
        passed=results.passed,
        error_count=results.error_count,
        warning_count=results.warning_count,
        errors_by_rule=errors_by_rule,
    )


@dataclass(frozen=True)
class KicadCliDrcSummary:
    """Summary of ``kicad-cli pcb drc --refill-zones``.

    This is the MANDATORY cross-gate: ``kct check`` alone is structurally
    blind to connectivity shorts that only appear once the copper pours
    are re-filled from scratch, so a benchmark report that omits it is not
    evidence of a clean board.

    ``violation_count`` is ``None`` (not ``0``) when kicad-cli could not
    run -- "we did not check" must never render as "clean".
    """

    ran: bool
    violation_count: int | None = None
    by_type: dict[str, int] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "violation_count": self.violation_count,
            "by_type": dict(sorted(self.by_type.items())),
            "note": self.note,
        }


def run_kicad_cli_drc(pcb_path: str | Path, *, timeout: int = 300) -> KicadCliDrcSummary:
    """Run the ``kicad-cli pcb drc --refill-zones`` cross-gate.

    Delegates to :func:`kicad_tools.drc.geometric.run_geometric_drc`, the
    single in-repo implementation of that invocation, so the benchmark and
    the board-recipe pipeline gate reason over identical geometry.
    """
    from kicad_tools.drc.geometric import run_geometric_drc

    result = run_geometric_drc(pcb_path, timeout=timeout)
    if not result.ran:
        return KicadCliDrcSummary(ran=False, note=result.note)
    return KicadCliDrcSummary(
        ran=True,
        violation_count=result.error_count,
        by_type=dict(result.by_type),
        note=result.note,
    )


# ---------------------------------------------------------------------------
# Diff-pair completion (strict gate, only where pairs are defined)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffPairCompletion:
    """Completion of the board's differential pairs.

    Only emitted when pairs are actually defined for the board -- an
    absent ``diff_pairs`` block in the JSON means "this board declares no
    pairs", which is different from "its pairs are unrouted".
    """

    pairs_total: int
    pairs_complete: int
    pairs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completion_pct(self) -> float:
        if self.pairs_total == 0:
            return 100.0
        return (self.pairs_complete / self.pairs_total) * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs_total": self.pairs_total,
            "pairs_complete": self.pairs_complete,
            "completion_pct": round(self.completion_pct, 2),
            "pairs": list(self.pairs),
        }


def measure_diff_pairs(
    board: str | Path | PCB,
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    strict: bool = True,
) -> DiffPairCompletion | None:
    """Measure per-pair completion, or ``None`` when no pairs are defined.

    Args:
        board: Path to a ``.kicad_pcb`` or a loaded ``PCB``.
        pairs: Explicit ``(net_p, net_n)`` pairs (e.g. from a net-class
            map sidecar). When ``None``, pairs are discovered by
            :meth:`~kicad_tools.analysis.trace_length.TraceLengthAnalyzer.find_differential_pairs`
            (the ``_P``/``_N``, ``+``/``-`` naming conventions).
        strict: Passed through to the connectivity model.

    Returns:
        ``None`` when the board defines no pairs at all; otherwise a
        :class:`DiffPairCompletion` where a pair counts as complete only
        when BOTH members are fully connected.
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer
    from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
    from kicad_tools.schema.pcb import PCB as PCBClass

    pcb = PCBClass.load(str(board)) if isinstance(board, (str, Path)) else board

    resolved: list[tuple[str, str]]
    if pairs is None:
        resolved = TraceLengthAnalyzer().find_differential_pairs(pcb)
    else:
        resolved = [(str(p), str(n)) for p, n in pairs]

    if not resolved:
        return None

    wanted: set[str] = set()
    for net_p, net_n in resolved:
        wanted.add(net_p)
        wanted.add(net_n)

    status = NetStatusAnalyzer(pcb, strict=strict).analyze_nets(wanted)
    by_name = {net.net_name: net for net in status.nets}

    rows: list[dict[str, Any]] = []
    complete = 0
    for net_p, net_n in resolved:
        sp = by_name.get(net_p)
        sn = by_name.get(net_n)
        p_ok = sp is not None and sp.status == "complete"
        n_ok = sn is not None and sn.status == "complete"
        pair_ok = bool(p_ok and n_ok)
        if pair_ok:
            complete += 1
        rows.append(
            {
                "net_positive": net_p,
                "net_negative": net_n,
                "positive_complete": p_ok,
                "negative_complete": n_ok,
                "complete": pair_ok,
            }
        )

    return DiffPairCompletion(pairs_total=len(resolved), pairs_complete=complete, pairs=rows)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkReport:
    """One board's benchmark result, in the schema v1 contract.

    Attributes:
        board_id: Stable board slug from the fetch manifest (e.g. ``strf``).
        board_commit: Upstream commit the board file was pinned to. The
            reproduction key -- without it the numbers are unfalsifiable.
        protocol: ``"zero-touch"`` or ``"tuned"`` (Epic #4932's two
            protocols), or another harness-defined tag.
        tool_commit: kicad-tools commit that produced the report.
        completion / copper / timing / backend: the DeepPCB-comparable
            tuple plus its environment-validity evidence.
        kct_check / kicad_cli_drc: the two DRC engines. Both are required
            slots; a report with only one is not evidence of a clean board.
        diff_pairs: ``None`` when the board defines no pairs.
        route_outcome: what happened on this attempt and where the measured
            artifact came from (:class:`RouteOutcome`). ``None`` when no
            route-attempt evidence was supplied to :func:`collect_report` --
            including every report generated before issue #5280 landed.
            Consumers MUST treat ``None`` as unknown, never as success.
        pre_route_completion: :class:`CompletionMetrics` measured on the
            pre-route (post rip-up, pre-routing) input, when the caller
            supplied it. This is the baseline the router started from --
            comparing it against ``completion`` (measured on the final
            artifact) separates newly-routed progress from connectivity
            that was already present (or trivially satisfied) before this
            attempt ran. ``None`` when not measured -- never fabricated as
            zero.
        notes: free-form annotations (e.g. "PocketBeagle: 3 nets left
            unrouted, see #NNNN"), rendered under the markdown table.
    """

    board_id: str
    protocol: str
    completion: CompletionMetrics
    copper: CopperMetrics
    timing: TimingMetrics
    backend: BackendInfo
    kct_check: KctCheckSummary
    kicad_cli_drc: KicadCliDrcSummary
    board_commit: str | None = None
    board_source: str | None = None
    board_file: str | None = None
    tool_commit: str = field(default_factory=_tool_commit)
    generated_at: str = field(default_factory=_utc_now_iso)
    diff_pairs: DiffPairCompletion | None = None
    route_outcome: RouteOutcome | None = None
    pre_route_completion: CompletionMetrics | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def newly_routed_connections(self) -> int | None:
        """``completion`` minus ``pre_route_completion``, or ``None`` when unmeasured.

        Never assumes a zero baseline: the delta is only meaningful (and
        only ever computed) when ``pre_route_completion`` was actually
        measured.
        """
        if self.pre_route_completion is None:
            return None
        return self.completion.connections_routed - self.pre_route_completion.connections_routed

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable schema-v1 JSON contract."""
        return {
            "$schema": SCHEMA_URL,
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "board_id": self.board_id,
            "board_commit": self.board_commit,
            "board_source": self.board_source,
            "board_file": self.board_file,
            "protocol": self.protocol,
            "tool_commit": self.tool_commit,
            "route_outcome": (self.route_outcome.to_dict() if self.route_outcome else None),
            "completion": self.completion.to_dict(),
            "pre_route_completion": (
                self.pre_route_completion.to_dict() if self.pre_route_completion else None
            ),
            "newly_routed_connections": self.newly_routed_connections,
            "copper": self.copper.to_dict(),
            "timing": self.timing.to_dict(),
            "backend": self.backend.to_dict(),
            "kct_check": self.kct_check.to_dict(),
            "kicad_cli_drc": self.kicad_cli_drc.to_dict(),
            "diff_pairs": (self.diff_pairs.to_dict() if self.diff_pairs else None),
            "notes": list(self.notes),
        }

    def to_json(self, indent: int = 2) -> str:
        """Render the report as JSON text."""
        import json

        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    def write_json(self, path: str | Path, indent: int = 2) -> Path:
        """Write :meth:`to_json` to ``path`` (parents created)."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_json(indent=indent) + "\n", encoding="utf-8")
        return out


def collect_report(
    pcb_path: str | Path,
    *,
    board_id: str,
    protocol: str = PROTOCOL_ZERO_TOUCH,
    board_commit: str | None = None,
    board_source: str | None = None,
    wall_clock_s: float | None = None,
    diff_pairs: Sequence[tuple[str, str]] | None = None,
    manufacturer: str = "jlcpcb",
    layers: int = 4,
    strict: bool = True,
    run_kicad_cli: bool = True,
    kicad_cli_timeout: int = 300,
    notes: Iterable[str] | None = None,
    backend: BackendInfo | None = None,
    route_exit_code: int | None = None,
    route_output_exists: bool | None = None,
    route_exception: BaseException | None = None,
    route_timed_out: bool = False,
    route_stopped_before_routing: bool = False,
    pre_route_path: str | Path | None = None,
) -> BenchmarkReport:
    """Measure a routed benchmark board and assemble the full report.

    Args:
        pcb_path: The MEASURED ``.kicad_pcb`` -- router output when the
            attempt produced any, otherwise the caller's fallback input
            (never inferred here; the caller picks the path and reports
            which it was via ``route_output_exists``).
        board_id: Manifest slug for the board.
        protocol: ``"zero-touch"`` / ``"tuned"`` (see Epic #4932).
        board_commit: Pinned upstream commit of the board source.
        board_source: Upstream URL, recorded for reproduction.
        wall_clock_s: Measured runtime of the routing pass, or ``None``
            when this report is a pure re-measurement of an existing
            board. Refused (dropped) unless the C++ backend is live.
        diff_pairs: Explicit pairs; ``None`` auto-detects by naming
            convention and omits the block when none exist.
        manufacturer / layers: design-rule context for ``kct check``.
        strict: real-geometry connectivity (leave ``True``).
        run_kicad_cli: set ``False`` only to skip the cross-gate in a unit
            test; the resulting report records ``ran=False`` with an
            explicit note, never a clean verdict.
        kicad_cli_timeout: seconds before the kicad-cli DRC is abandoned.
        notes: free-form annotations for the rendered table.
        backend: pre-probed backend info (the harness probes once BEFORE
            routing); probed here when omitted.
        route_exit_code: The router's exit code for this attempt, or
            ``None`` when ``route_exception`` (or no attempt at all) is why
            there isn't one.
        route_output_exists: Whether THIS attempt's own output file exists
            (never a stale artifact from a previous run -- see
            :func:`kicad_tools.cli.commands.bench._run_one_board`'s
            per-attempt output ownership). Required (with the other
            ``route_*`` args) to populate :attr:`BenchmarkReport.route_outcome`;
            omitting all of them leaves it ``None`` (unknown provenance).
        route_exception: The exception the route call raised, if any.
        route_timed_out: Whether the attempt was abandoned for exceeding a
            time budget.
        route_stopped_before_routing: Explicit preflight refusal evidence.
        pre_route_path: The pre-route (post rip-up) input board, used to
            populate :attr:`BenchmarkReport.pre_route_completion` -- the
            baseline this attempt started from. ``None`` when not supplied
            (never fabricated as zero).
    """
    path = Path(pcb_path)
    resolved_backend = backend if backend is not None else probe_backend()

    if run_kicad_cli:
        cli_drc = run_kicad_cli_drc(path, timeout=kicad_cli_timeout)
    else:
        cli_drc = KicadCliDrcSummary(
            ran=False,
            note="kicad-cli cross-gate skipped by caller (run_kicad_cli=False)",
        )

    completion = measure_completion(path, strict=strict)

    route_attempted = (
        route_exit_code is not None
        or route_output_exists is not None
        or route_exception is not None
    )
    route_outcome = (
        build_route_outcome(
            exit_code=route_exit_code,
            output_exists=bool(route_output_exists),
            completion_pct=completion.completion_pct,
            connections_total=completion.connections_total,
            exception=route_exception,
            timed_out=route_timed_out,
            stopped_before_routing=route_stopped_before_routing,
        )
        if route_attempted
        else None
    )

    pre_route_completion = (
        measure_completion(pre_route_path, strict=strict) if pre_route_path is not None else None
    )

    return BenchmarkReport(
        board_id=board_id,
        protocol=protocol,
        board_commit=board_commit,
        board_source=board_source,
        board_file=path.name,
        route_outcome=route_outcome,
        completion=completion,
        pre_route_completion=pre_route_completion,
        copper=measure_copper(path),
        timing=build_timing(wall_clock_s, resolved_backend, route_outcome=route_outcome),
        backend=resolved_backend,
        kct_check=run_kct_check(path, manufacturer=manufacturer, layers=layers),
        kicad_cli_drc=cli_drc,
        diff_pairs=measure_diff_pairs(path, pairs=diff_pairs, strict=strict),
        notes=list(notes or []),
    )
