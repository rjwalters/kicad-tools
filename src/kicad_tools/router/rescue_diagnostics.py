"""Ground-truth instrumentation for the partial-net rescue loop (issue #4469).

Phase 1 of the board-05 unattended-manufacturable epic (#4410).  This module is
**diagnose-only**: it never mutates a board, never re-routes, and never changes a
routing outcome.  It exists to turn the rescue loop's opaque
``Rescue ISENSE_A+: FAILED (no output produced)`` line into a concrete,
actionable reason per stranded net, and to surface *where* the coarse
``--allow-unsafe-grid`` grid cannot faithfully represent sub-cell clearance (the
sites that later become the Phase-2 shorts).

Three independent reports, matching the three deliverables on #4469:

1. :func:`classify_rescue_failure` -- parse the captured ``kct route`` rescue
   subprocess output and emit the concrete failure reason (blocked by
   non-rippable copper / no legal escape / budget exhausted / clearance
   infidelity / no output).  Wired into
   :func:`kicad_tools.router.partial_rescue.rescue_partial_nets` so a fresh
   regen prints a per-stranded-net reason table instead of the opaque message.

2. :func:`grid_fidelity_report` -- flag pad/copper pairs whose routable lane is
   narrower than the grid can represent when ``resolution > clearance/2`` (the
   memory-forced unsafe grid board-05 runs on).  These are the sub-clearance
   sites where a grid-snapped trace risks a DRC short.

3. :func:`format_stranding_report` -- render the existing
   :func:`kicad_tools.router.stuck_classifier.classify_stuck_nets` taxonomy for
   the residual stranded nets (reuse, not re-implement).

None of these functions build a routing grid or attempt a route, so they are
zero-regression by construction and cheap enough to run on every regen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

__all__ = [
    "RescueFailureCategory",
    "RescueFailureReason",
    "classify_rescue_failure",
    "format_rescue_reason_table",
    "GridFidelitySite",
    "GridFidelityReport",
    "grid_fidelity_report",
    "format_grid_fidelity_report",
    "format_stranding_report",
]


# =============================================================================
# Deliverable 1: concrete rescue-failure reasons
# =============================================================================


class RescueFailureCategory(Enum):
    """Why a single-net rescue stage failed to fully connect its target.

    The categories mirror the three actionable buckets named on issue #4469
    (blocked-by-non-rippable-copper / budget-exhausted / no-legal-escape) plus
    the two the router surfaces in practice on the coarse-grid board-05 route
    (clearance infidelity, no output at all) and a catch-all.
    """

    NO_LEGAL_ESCAPE = "no_legal_escape"
    BLOCKED_BY_NON_RIPPABLE = "blocked_by_non_rippable_copper"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CLEARANCE_INFIDELITY = "clearance_infidelity"
    CONGESTION = "congestion"
    NO_OUTPUT = "no_output"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "no_legal_escape": (
                "no legal escape -- the stranded pad(s) could not leave their "
                "footprint on the current grid (all escape candidates failed "
                "clearance validation)"
            ),
            "blocked_by_non_rippable_copper": (
                "blocked by non-rippable copper -- every corridor to the "
                "stranded pad is walled off by preserved (immutable) copper the "
                "solo rescue is not allowed to rip"
            ),
            "budget_exhausted": (
                "budget exhausted -- the per-net / stage wall-clock or "
                "iteration budget ran out before a full path was found"
            ),
            "clearance_infidelity": (
                "clearance infidelity -- a candidate path existed but failed "
                "post-route clearance validation (the coarse grid cannot "
                "represent the required sub-cell clearance)"
            ),
            "congestion": (
                "congestion -- too many competing traces in the corridor for a "
                "single-net solo pass to thread"
            ),
            "no_output": (
                "no output produced -- the rescue subprocess wrote no PCB file "
                "(crash / OOM before save)"
            ),
            "unknown": (
                "unknown -- the rescue subprocess output carried no recognized failure signature"
            ),
        }[self.value]


# Router :class:`~kicad_tools.router.failure_analysis.FailureCause` tokens (as
# printed in the ``Failed nets:`` block, e.g. ``Net 14 "ISENSE_A+":
# blocked_path``) mapped onto the rescue-failure buckets.  In a *solo* rescue
# every other net's copper is preserved and immutable, so a router
# ``routing_order`` / ``layer_conflict`` / ``via_blocked`` verdict is, from the
# rescue's point of view, "blocked by non-rippable copper".
_CAUSE_MAP: dict[str, RescueFailureCategory] = {
    "blocked_path": RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE,
    "routing_order": RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE,
    "layer_conflict": RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE,
    "via_blocked": RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE,
    "keepout": RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE,
    "pin_access": RescueFailureCategory.NO_LEGAL_ESCAPE,
    "clearance": RescueFailureCategory.CLEARANCE_INFIDELITY,
    "congestion": RescueFailureCategory.CONGESTION,
}

# Text signatures (checked only when the per-net FailureCause line is absent).
_BUDGET_SIGNATURES = (
    "deadline reached",
    "per-net timeout",
    "wall-clock budget",
    "budget exhausted",
    "timed out",
    "iteration budget",
)
_ESCAPE_SIGNATURES = (
    "0 pins escaped",
    "no grid point reachable",
    "all escapes failed clearance validation",
    "no open sector",
    "could not escape",
)


@dataclass
class RescueFailureReason:
    """Concrete reason a single-net rescue stage failed (issue #4469)."""

    net: str
    category: RescueFailureCategory
    detail: str = ""
    #: Raw router FailureCause token parsed from the ``Failed nets:`` block.
    router_cause: str = ""
    #: e.g. ``"1/4"`` -- pads connected after the failed rescue attempt.
    pads_connected: str = ""
    #: e.g. ``U10: 0 pins escaped -- all escapes failed clearance validation``.
    escape_note: str = ""
    #: True when the router output shows the coarse grid contributed (clearance
    #: validation failures / the ``grid 0.1mm > clearance/2`` warning).
    grid_infidelity: bool = False

    def one_line(self) -> str:
        bits = [self.category.value.upper()]
        if self.pads_connected:
            bits.append(f"({self.pads_connected} pads)")
        line = f"{self.net}: {' '.join(bits)} -- {self.detail or self.category.label}"
        return line

    def to_dict(self) -> dict:
        return {
            "net": self.net,
            "category": self.category.value,
            "detail": self.detail or self.category.label,
            "router_cause": self.router_cause,
            "pads_connected": self.pads_connected,
            "escape_note": self.escape_note,
            "grid_infidelity": self.grid_infidelity,
        }


def _find_net_cause(net: str, text: str) -> str:
    """Return the last router FailureCause token printed for *net*, or ``""``.

    Matches the ``Failed nets:`` block line format the router emits, e.g.::

        - Net 14 "ISENSE_A+": blocked_path (blocked_path)

    The net name is regex-escaped (board-05 nets carry ``+``/``-`` suffixes).
    The last occurrence wins -- the router reprints the block per escalation
    stage, and the final stage is the authoritative verdict.
    """
    pattern = re.compile(r'Net\s+\d+\s+"' + re.escape(net) + r'"\s*:\s*([a-z_]+)')
    matches = pattern.findall(text)
    return matches[-1] if matches else ""


def _find_pads_connected(net: str, text: str) -> str:
    """Return the ``X/Y`` pad-connectivity ratio the router prints for *net*."""
    m = re.search(re.escape(net) + r":\s*(\d+/\d+)\s+pads connected", text)
    return m.group(1) if m else ""


def _find_escape_note(text: str) -> str:
    """Return the first ``Escape routing for ...: 0 pins escaped ...`` line."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Escape routing for") and "escaped" in stripped:
            # Keep only the ``<ref> (...): <n> pins escaped -- <reason>`` tail.
            return stripped.replace("Escape routing for ", "", 1)
    return ""


def classify_rescue_failure(
    net: str,
    stdout: str,
    stderr: str,
    *,
    output_produced: bool = True,
) -> RescueFailureReason:
    """Turn a failed rescue subprocess's captured output into a concrete reason.

    *stdout* / *stderr* are the captured streams of the single-net ``kct route``
    rescue stage; *output_produced* is False when the subprocess wrote no PCB
    file at all (the opaque ``FAILED (no output produced)`` case).

    Priority of signals (most authoritative first):

    1. The per-net FailureCause the router prints in its ``Failed nets:`` block
       (``blocked_path`` -> blocked-by-non-rippable, ``pin_access`` ->
       no-legal-escape, ``clearance`` -> clearance-infidelity, ...).
    2. Escape-failure lines in stderr (``0 pins escaped -- all escapes failed
       clearance validation``) -> no-legal-escape.
    3. Budget/deadline text signatures -> budget-exhausted.
    4. A bare ``non-rippable`` mention -> blocked-by-non-rippable.

    Always best-effort and never raises: an unrecognized output yields
    :attr:`RescueFailureCategory.UNKNOWN` with the raw tail preserved in
    *detail* so a human can still see what happened.
    """
    text = f"{stdout}\n{stderr}"
    pads_connected = _find_pads_connected(net, text)
    escape_note = _find_escape_note(stderr) or _find_escape_note(stdout)
    grid_infidelity = (
        "failed clearance validation" in text
        or "clearance/2" in text
        or "may cause clearance violations" in text
        or "post-route clearance validation failed" in text
    )

    if not output_produced:
        return RescueFailureReason(
            net=net,
            category=RescueFailureCategory.NO_OUTPUT,
            detail=RescueFailureCategory.NO_OUTPUT.label,
            pads_connected=pads_connected,
            escape_note=escape_note,
            grid_infidelity=grid_infidelity,
        )

    router_cause = _find_net_cause(net, text)
    category: RescueFailureCategory | None = None
    if router_cause:
        category = _CAUSE_MAP.get(router_cause)

    if category is None and escape_note:
        category = RescueFailureCategory.NO_LEGAL_ESCAPE
    if category is None and any(sig in text for sig in _ESCAPE_SIGNATURES):
        category = RescueFailureCategory.NO_LEGAL_ESCAPE
    if category is None and any(sig in text for sig in _BUDGET_SIGNATURES):
        category = RescueFailureCategory.BUDGET_EXHAUSTED
    if category is None and "non-rippable" in text:
        category = RescueFailureCategory.BLOCKED_BY_NON_RIPPABLE
    if category is None:
        category = RescueFailureCategory.UNKNOWN

    # Build a rich one-line detail from whatever evidence was available.
    detail_bits = [category.label]
    if router_cause:
        detail_bits.append(f"[router cause: {router_cause}]")
    if escape_note:
        detail_bits.append(f"[escape: {escape_note}]")
    if grid_infidelity and category is not RescueFailureCategory.CLEARANCE_INFIDELITY:
        detail_bits.append("[coarse-grid clearance validation contributed]")

    return RescueFailureReason(
        net=net,
        category=category,
        detail="; ".join(detail_bits),
        router_cause=router_cause,
        pads_connected=pads_connected,
        escape_note=escape_note,
        grid_infidelity=grid_infidelity,
    )


def format_rescue_reason_table(reasons: list[RescueFailureReason]) -> str:
    """Render the per-stranded-net reason table (issue #4469 AC1).

    Empty input yields an empty string so callers can guard on truthiness.
    """
    if not reasons:
        return ""
    lines = [
        "",
        "=" * 60,
        "Rescue-failure reasons (issue #4469 -- no more opaque 'FAILED')",
        "=" * 60,
    ]
    for r in reasons:
        pads = f" [{r.pads_connected} pads]" if r.pads_connected else ""
        lines.append(f"  {r.net}: {r.category.value}{pads}")
        lines.append(f"      {r.category.label}")
        if r.router_cause:
            lines.append(f"      router cause: {r.router_cause}")
        if r.escape_note:
            lines.append(f"      escape: {r.escape_note}")
        if r.grid_infidelity:
            lines.append("      coarse-grid clearance validation contributed")
    return "\n".join(lines)


# =============================================================================
# Deliverable 2: grid-fidelity report
# =============================================================================
#
# board-05 runs on ``--allow-unsafe-grid``: the memory-forced 0.1mm grid is
# coarser than clearance/2 (=0.075mm for the 0.152mm jlcpcb-tier1 clearance), so
# a trace centreline snapped to the grid can be placed up to a full grid cell
# away from the ideal lane centre.  Where the routable lane between two
# distinct-net copper features is already narrow, that quantization can push a
# grid-snapped trace within the clearance envelope of one side -- a DRC short.
# The report flags exactly those sites.


@dataclass
class GridFidelitySite:
    """One pad pair whose lane the coarse grid cannot represent faithfully."""

    net_a: str
    net_b: str
    ref_a: str
    ref_b: str
    position: tuple[float, float]
    edge_gap_mm: float
    #: The clearance-plus-quantization band this gap fell under.
    band_mm: float

    def to_dict(self) -> dict:
        return {
            "net_a": self.net_a,
            "net_b": self.net_b,
            "ref_a": self.ref_a,
            "ref_b": self.ref_b,
            "position": [round(self.position[0], 4), round(self.position[1], 4)],
            "edge_gap_mm": round(self.edge_gap_mm, 4),
            "band_mm": round(self.band_mm, 4),
        }


@dataclass
class GridFidelityReport:
    """Sub-cell-clearance sites on a coarse routing grid (issue #4469 AC2)."""

    resolution: float
    clearance: float
    unsafe_grid: bool
    band_mm: float
    sites: list[GridFidelitySite] = field(default_factory=list)
    pairs_examined: int = 0

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "clearance": self.clearance,
            "unsafe_grid": self.unsafe_grid,
            "half_clearance": round(self.clearance / 2.0, 4),
            "band_mm": round(self.band_mm, 4),
            "site_count": len(self.sites),
            "pairs_examined": self.pairs_examined,
            "sites": [s.to_dict() for s in self.sites],
        }


def _box_support(size: tuple[float, float], ux: float, uy: float) -> float:
    """Half-extent of an axis-aligned pad box projected onto unit dir ``(ux, uy)``.

    The support function ``h(u) = |ux|*(w/2) + |uy|*(h/2)`` is the distance from
    the pad centre to its projected edge along ``u``.  Using the centre-to-centre
    direction as ``u`` yields the separating-axis gap between two pad boxes:
    ``edge_gap = center_dist - h_a(u) - h_b(u)``.  This is a lower bound on the
    true rectangle-to-rectangle distance (a diagnostic should over-include), and
    -- unlike a bounding half-diagonal -- it does NOT spuriously report thin
    fine-pitch pads (e.g. the DRV8301's long pads at 0.5mm pitch) as overlapping
    when they are merely side-by-side.  Pad rotation is approximated as
    axis-aligned (``_iter_board_pads`` resolves pad *centres* in the board frame
    but not per-pad rotation); documented as an approximation.
    """
    w, h = size
    return abs(ux) * (w / 2.0) + abs(uy) * (h / 2.0)


def grid_fidelity_report(
    pcb_path: str | Path,
    *,
    resolution: float,
    clearance: float,
    max_sites: int = 60,
    excluded_nets: frozenset[str] = frozenset(),
) -> GridFidelityReport:
    """Flag pad pairs whose routable lane the coarse grid cannot represent.

    A pad pair on *distinct* nets is a sub-clearance site when its edge-to-edge
    gap falls below ``clearance + 2 * resolution`` -- the lane is narrower than
    the clearance envelope plus the worst-case grid quantization on each side,
    so a trace threading it (or a via/trace snapped beside either pad) cannot be
    guaranteed to keep clearance once its centreline is rounded to the grid.

    The edge gap is the separating-axis gap between the two axis-aligned pad
    boxes along their centre-to-centre direction (see :func:`_box_support`).

    READ-ONLY: loads the board through :class:`~kicad_tools.schema.pcb.PCB` and
    reuses the frame-correct pad iterator from
    :mod:`kicad_tools.router.stuck_classifier` (so pad centres are in the board
    frame), builds no routing grid, and never mutates the board.
    """
    from kicad_tools.router.stuck_classifier import _iter_board_pads
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    name_by_id = {nid: net.name for nid, net in pcb.nets.items() if net.name}

    band = clearance + 2.0 * resolution
    unsafe = resolution > (clearance / 2.0) + 1e-9

    # Materialize pads once (ref, net_number, centre, size, net-name).
    pads: list[tuple[str, int, tuple[float, float], tuple[float, float], str]] = []
    for ref, net_number, (bx, by), size in _iter_board_pads(pcb):
        name = name_by_id.get(net_number, "")
        if name in excluded_nets:
            continue
        pads.append((ref, net_number, (bx, by), size, name))

    # A generous window on centre distance prunes far pairs before the (cheaper)
    # exact support computation.  Bounded by the largest pad half-diagonal so no
    # genuine in-band pair is skipped.
    max_half_diag = max(
        ((s[0] * s[0] + s[1] * s[1]) ** 0.5 / 2.0 for _r, _n, _p, s, _nm in pads),
        default=0.0,
    )
    window = band + 2.0 * max_half_diag

    sites: list[GridFidelitySite] = []
    pairs_examined = 0
    n = len(pads)
    for i in range(n):
        ref_a, net_a, (ax, ay), size_a, name_a = pads[i]
        for j in range(i + 1, n):
            ref_b, net_b, (bx, by), size_b, name_b = pads[j]
            if net_a == net_b:
                continue  # same net -- not a clearance pair
            dx = bx - ax
            dy = by - ay
            center_dist = (dx * dx + dy * dy) ** 0.5
            if center_dist >= window:
                continue
            pairs_examined += 1
            if center_dist > 1e-9:
                ux, uy = dx / center_dist, dy / center_dist
            else:
                ux, uy = 1.0, 0.0
            edge_gap = center_dist - _box_support(size_a, ux, uy) - _box_support(size_b, ux, uy)
            if edge_gap >= band:
                continue
            sites.append(
                GridFidelitySite(
                    net_a=name_a or f"(net {net_a})",
                    net_b=name_b or f"(net {net_b})",
                    ref_a=ref_a,
                    ref_b=ref_b,
                    position=((ax + bx) / 2.0, (ay + by) / 2.0),
                    edge_gap_mm=edge_gap,
                    band_mm=band,
                )
            )

    sites.sort(key=lambda s: s.edge_gap_mm)
    return GridFidelityReport(
        resolution=resolution,
        clearance=clearance,
        unsafe_grid=unsafe,
        band_mm=band,
        sites=sites[:max_sites],
        pairs_examined=pairs_examined,
    )


def format_grid_fidelity_report(report: GridFidelityReport, *, max_rows: int = 20) -> str:
    """Render a :class:`GridFidelityReport` as a human-readable block."""
    bar = "=" * 60
    half_c = report.clearance / 2.0
    verdict = "UNSAFE" if report.unsafe_grid else "safe"
    lines = [
        "",
        bar,
        "Grid-fidelity report (issue #4469 -- sub-cell clearance sites)",
        bar,
        f"  grid resolution : {report.resolution}mm",
        f"  clearance       : {report.clearance}mm  (clearance/2 = {half_c:.4g}mm)",
        f"  grid safety     : {verdict}"
        + (
            f"  (resolution {report.resolution}mm > clearance/2 {half_c:.4g}mm)"
            if report.unsafe_grid
            else ""
        ),
        f"  danger band     : edge-gap < clearance + 2*grid = {report.band_mm:.4g}mm",
        f"  sub-clearance sites flagged : {len(report.sites)}",
    ]
    if not report.sites:
        lines.append("  (no sub-clearance pad pairs found)")
        return "\n".join(lines)
    lines.append("")
    lines.append("  net A / net B                          refs        edge-gap  @ (x, y)")
    for s in report.sites[:max_rows]:
        pair = f"{s.net_a} / {s.net_b}"
        refs = f"{s.ref_a}/{s.ref_b}"
        lines.append(
            f"    {pair:<38.38} {refs:<11.11} {s.edge_gap_mm:7.4f}  "
            f"({s.position[0]:.2f}, {s.position[1]:.2f})"
        )
    if len(report.sites) > max_rows:
        lines.append(f"    ... and {len(report.sites) - max_rows} more (see JSON for full list)")
    return "\n".join(lines)


# =============================================================================
# Deliverable 3: per-net stranding classification (reuse stuck_classifier)
# =============================================================================


def format_stranding_report(
    pcb_path: str | Path,
    *,
    excluded_nets: frozenset[str] = frozenset(),
) -> str:
    """Render the stuck-net taxonomy for the residual stranded signal nets.

    Thin wrapper over the existing
    :func:`kicad_tools.router.stuck_classifier.classify_stuck_nets` -- reuses
    (does not re-implement) the ESCAPE_BLOCKED / CONGESTION_SATURATED /
    PLACEMENT_BOUND / BUDGET_STARVED / POUR_DISCONTINUOUS taxonomy so the Phase-1
    ground-truth report and ``--why`` agree.  READ-ONLY.
    """
    from kicad_tools.router.stuck_classifier import classify_stuck_nets

    result = classify_stuck_nets(pcb_path, excluded_nets=excluded_nets)
    bar = "=" * 60
    lines = [
        "",
        bar,
        "Per-net stranding classification (issue #4469 -- stuck-net taxonomy)",
        bar,
    ]
    if not result.diagnoses:
        lines.append("  (no stranded signal nets -- board is fully connected)")
        return "\n".join(lines)
    counts = {k: v for k, v in result.counts.items() if v}
    lines.append(f"  stranded signal nets: {len(result.diagnoses)}  counts={counts}")
    lines.append("")
    for diag in result.diagnoses:
        lines.append(f"  {diag.one_line()}")
    return "\n".join(lines)
