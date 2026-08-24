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
    "BackendInfo",
    "BenchmarkReport",
    "CompletionMetrics",
    "CopperMetrics",
    "DiffPairCompletion",
    "KctCheckSummary",
    "KicadCliDrcSummary",
    "TimingMetrics",
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
            ARC tracks (KiCad 7+ rounded tracks), which the ``PCB`` schema
            does not model -- they are picked up straight from the
            S-expression so an external board's arcs are never silently
            dropped from the headline number.
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


def _arc_length_mm(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Length of the circular arc through ``start`` -> ``mid`` -> ``end``.

    Falls back to the chord length ``|start-end|`` for a degenerate
    (collinear / zero-radius) arc, which is what KiCad renders in that
    case. Never raises -- a malformed arc must not abort a benchmark run.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end

    # Circumcenter via the perpendicular-bisector determinant.
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return math.dist(start, end)

    s1 = x1 * x1 + y1 * y1
    s2 = x2 * x2 + y2 * y2
    s3 = x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    radius = math.dist((cx, cy), start)
    if radius <= 0:
        return math.dist(start, end)

    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    a3 = math.atan2(y3 - cy, x3 - cx)

    # Sweep start->end the short way round unless ``mid`` says otherwise.
    def _norm(angle: float) -> float:
        return (angle + 2 * math.pi) % (2 * math.pi)

    ccw_mid = _norm(a2 - a1)
    ccw_end = _norm(a3 - a1)
    sweep = ccw_end if ccw_mid <= ccw_end else 2 * math.pi - ccw_end
    return abs(radius * sweep)


def _copper_arcs(pcb_path: Path) -> list[tuple[str, float]]:
    """Return ``(layer, length_mm)`` for every copper ``(arc ...)`` track.

    KiCad 7+ writes curved tracks as top-level ``(arc ...)`` elements.
    ``kicad_tools.schema.pcb.PCB`` models ``segment`` and ``via`` but not
    ``arc``, so measuring wirelength from ``pcb.segments`` alone silently
    under-reports any externally-sourced board that uses rounded tracks.
    This reads them straight from the file (issue #4934).
    """
    from kicad_tools.sexp import parse_file

    try:
        root = parse_file(pcb_path)
    except Exception:  # pragma: no cover - defensive; PCB.load already parsed
        return []

    arcs: list[tuple[str, float]] = []
    for child in root.iter_children():
        if child.tag != "arc":
            continue
        layer_node = child.find("layer")
        layer = (layer_node.get_string(0) or "") if layer_node else ""
        if not _is_copper_layer(layer):
            continue
        start_node = child.find("start")
        mid_node = child.find("mid")
        end_node = child.find("end")
        if not (start_node and mid_node and end_node):
            continue
        start = (start_node.get_float(0) or 0.0, start_node.get_float(1) or 0.0)
        mid = (mid_node.get_float(0) or 0.0, mid_node.get_float(1) or 0.0)
        end = (end_node.get_float(0) or 0.0, end_node.get_float(1) or 0.0)
        arcs.append((layer, _arc_length_mm(start, mid, end)))
    return arcs


def measure_copper(pcb_path: str | Path) -> CopperMetrics:
    """Measure via count and total wirelength from the board file.

    Takes a PATH, not a ``PCB``, on purpose: the acceptance criterion is
    that these numbers come from the board file itself so any router path
    produces comparable figures, and the copper-arc sweep needs the raw
    S-expression the ``PCB`` object does not retain.
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

    arcs = _copper_arcs(path)
    for layer, length in arcs:
        by_layer[layer] = by_layer.get(layer, 0.0) + length

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
class TimingMetrics:
    """Wall-clock runtime of the routing pass, or an explicit refusal.

    ``wall_clock_s`` is ``None`` whenever ``valid`` is ``False``. That is
    deliberate: dropping the number rather than shipping it with a caveat
    flag makes it impossible for a downstream renderer to accidentally
    publish a Python-fallback timing (Epic #4932's stated risk).
    """

    wall_clock_s: float | None
    valid: bool
    refusal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_clock_s": (
                round(self.wall_clock_s, 3) if self.wall_clock_s is not None else None
            ),
            "valid": self.valid,
            "refusal_reason": self.refusal_reason,
        }


def build_timing(wall_clock_s: float | None, backend: BackendInfo) -> TimingMetrics:
    """Accept or refuse a measured wall-clock time given the live backend.

    A timing is published only when the C++ router extension was active.
    Anything else -- Python fallback, a probe that could not run, or no
    measurement at all -- yields ``valid=False`` and no number.
    """
    if wall_clock_s is None:
        return TimingMetrics(
            wall_clock_s=None,
            valid=False,
            refusal_reason="no routing pass was timed for this report",
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
        )
    return TimingMetrics(wall_clock_s=float(wall_clock_s), valid=True, refusal_reason=None)


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
    notes: list[str] = field(default_factory=list)

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
            "completion": self.completion.to_dict(),
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
) -> BenchmarkReport:
    """Measure a routed benchmark board and assemble the full report.

    Args:
        pcb_path: The ROUTED ``.kicad_pcb`` to measure.
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

    return BenchmarkReport(
        board_id=board_id,
        protocol=protocol,
        board_commit=board_commit,
        board_source=board_source,
        board_file=path.name,
        completion=measure_completion(path, strict=strict),
        copper=measure_copper(path),
        timing=build_timing(wall_clock_s, resolved_backend),
        backend=resolved_backend,
        kct_check=run_kct_check(path, manufacturer=manufacturer, layers=layers),
        kicad_cli_drc=cli_drc,
        diff_pairs=measure_diff_pairs(path, pairs=diff_pairs, strict=strict),
        notes=list(notes or []),
    )
