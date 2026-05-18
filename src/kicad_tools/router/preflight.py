"""Preflight checks performed before invoking the autorouter.

These checks surface routability problems early — at PCB write time, or via
``kct check`` — instead of deep inside the routing pipeline where they are
hard to diagnose.

The primary check at the moment is :func:`check_pad_grid_alignment`, which
detects pads that are not aligned to the configured router grid (within
``resolution / 10`` tolerance, matching the router-side check at
:mod:`kicad_tools.router.core`).

See issue #2497 for background.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .io import (
    auto_select_grid_resolution,
    load_pads_for_analysis,
)
from .primitives import Pad


@dataclass(frozen=True)
class PreflightOffGridPad:
    """A single pad whose absolute position does not align with the router grid.

    Attributes:
        ref: Component reference (e.g. ``"U1"``).
        pin: Pad number/pin name (e.g. ``"9"``).
        x: Absolute X coordinate in mm.
        y: Absolute Y coordinate in mm.
        offset_mm: Euclidean (L2) distance from the pad to the nearest grid point.
        footprint_name: Library footprint name
            (e.g. ``"Package_QFP:TQFP-32_7x7mm_P0.8mm"``).
    """

    ref: str
    pin: str
    x: float
    y: float
    offset_mm: float
    footprint_name: str

    @property
    def label(self) -> str:
        """Return ``"<ref>.<pin>"`` (or just ``"<ref>"`` if pin is empty)."""
        if self.ref and self.pin:
            return f"{self.ref}.{self.pin}"
        if self.ref:
            return self.ref
        return f"({self.x:.3f}, {self.y:.3f})"

    def message(self, grid_resolution: float, suggested_grid: float | None) -> str:
        """Format the user-facing error message for this off-grid pad.

        Mirrors the format proposed in issue #2497 and the PADS_OFF_GRID
        text emitted by :mod:`kicad_tools.router.core`::

            Pad U1.9 at (123.456, 78.910) is off-grid by 0.036mm (grid 0.1mm).
            Footprint: Package_QFP:TQFP-32_7x7mm_P0.8mm
            Suggested fix: round pad position OR set finer router grid (0.05mm would align all pads).
        """
        lines = [
            f"Pad {self.label} at ({self.x:.3f}, {self.y:.3f}) "
            f"is off-grid by {self.offset_mm:.3f}mm (grid {grid_resolution}mm).",
        ]
        if self.footprint_name:
            lines.append(f"Footprint: {self.footprint_name}")
        if suggested_grid is not None:
            lines.append(
                "Suggested fix: round pad position OR set finer router "
                f"grid ({suggested_grid}mm would align all pads)."
            )
        else:
            lines.append("Suggested fix: round pad position to the router grid.")
        return "\n".join(lines)


@dataclass
class OffGridReport:
    """Result of :func:`check_pad_grid_alignment`.

    Attributes:
        grid_resolution: Router grid resolution that was checked, in mm.
        threshold: Maximum L2 deviation considered "on-grid", in mm
            (defaults to :data:`DEFAULT_PAD_GRID_TOLERANCE_MM` = 0.05 mm).
        grid_origin: ``(x_offset, y_offset)`` in mm; grid points are at
            ``offset + k * resolution`` for integer ``k``.
        off_grid_pads: List of :class:`PreflightOffGridPad` records, one per pad
            whose position exceeds the threshold.
        suggested_grid: Finer grid resolution (in mm) that would clear all
            violations, or ``None`` if no improvement is available.
        total_pads: Total number of pads inspected.
    """

    grid_resolution: float
    threshold: float
    grid_origin: tuple[float, float]
    off_grid_pads: list[PreflightOffGridPad] = field(default_factory=list)
    suggested_grid: float | None = None
    total_pads: int = 0

    @property
    def passed(self) -> bool:
        """True when no off-grid pads were found."""
        return not self.off_grid_pads

    def summary(self) -> str:
        """Human-readable, multi-line summary suitable for CLI output."""
        if self.passed:
            return (
                f"Pad grid alignment OK: {self.total_pads} pads checked "
                f"against grid {self.grid_resolution}mm "
                f"(threshold {self.threshold:.4f}mm)."
            )
        lines = [
            f"Off-grid pads detected ({len(self.off_grid_pads)} of "
            f"{self.total_pads}) for grid {self.grid_resolution}mm "
            f"(threshold {self.threshold:.4f}mm):",
        ]
        for pad in self.off_grid_pads:
            lines.append("")
            lines.append(pad.message(self.grid_resolution, self.suggested_grid))
        return "\n".join(lines)


def _axis_distance_to_grid(value: float, resolution: float, offset: float) -> float:
    """Distance from ``value`` to the nearest grid point, FP-stable.

    Uses ``round()`` to find the nearest grid index instead of ``%``, which
    avoids floating-point round-off near exact grid points (e.g.
    ``124.49 % 0.1 == 0.09000000000000002``).
    """
    nearest = round((value - offset) / resolution) * resolution + offset
    return abs(value - nearest)


def _l2_distance_to_grid(
    x: float,
    y: float,
    resolution: float,
    x_offset: float,
    y_offset: float,
) -> float:
    """Euclidean distance from ``(x, y)`` to the nearest grid point.

    Matches the L2 form used by the router's PADS_OFF_GRID check
    (see :mod:`kicad_tools.router.core` ~line 1271-1281).
    """
    dx = _axis_distance_to_grid(x, resolution, x_offset)
    dy = _axis_distance_to_grid(y, resolution, y_offset)
    return float((dx * dx + dy * dy) ** 0.5)


def _suggest_finer_grid(
    pads: list[Pad],
    current_resolution: float,
    grid_origin: tuple[float, float],
    clearance: float = 0.15,
) -> float | None:
    """Return a finer router grid that would put every pad on-grid, or ``None``.

    Uses :func:`auto_select_grid_resolution` to enumerate candidate
    resolutions and picks the coarsest one (still finer than
    ``current_resolution``) that yields zero off-grid pads.
    """
    if not pads:
        return None

    auto_result = auto_select_grid_resolution(pads, clearance=clearance)

    # Inspect each candidate the auto-selector tried.  Pick the coarsest
    # resolution that is strictly finer than current_resolution AND clears
    # all violations.
    candidates = sorted(
        {res for res, _ in auto_result.candidates_tried if res < current_resolution},
        reverse=True,
    )

    x_off, y_off = grid_origin
    for res in candidates:
        threshold = res / 10
        fp_eps = max(1e-9, threshold * 1e-6)
        all_clear = True
        for pad in pads:
            dist = _l2_distance_to_grid(pad.x, pad.y, res, x_off, y_off)
            if dist > threshold + fp_eps:
                all_clear = False
                break
        if all_clear:
            return res
    return None


#: Default L2 tolerance for the pad-grid preflight rule, in millimeters.
#:
#: Set to ``0.05`` mm to accommodate stock KiCad library footprints whose
#: pads sit 0.03-0.05 mm off the 0.1 mm router grid by design (metric
#: rounding of imperial parts such as ``Connector_PinHeader_2.54mm`` and
#: ``USB_C_Receptacle``).  Genuine placement errors at >= 0.06 mm still
#: flag.  See issue #3042 for the fleet audit (341 false-positive warnings
#: across 9 boards) that motivated raising the default from the original
#: ``grid_resolution / 10`` = 0.01 mm.
DEFAULT_PAD_GRID_TOLERANCE_MM: float = 0.05


#: Absolute hard cap on auto-derived tolerance, in millimeters.
#:
#: The auto-derive logic builds the tolerance from each board's pad-offset
#: histogram (``p99(offsets) + safety margin``).  This cap prevents truly
#: pathological boards from pushing the threshold so high that real placement
#: errors slip through.  ``0.15`` mm is the smallest clearance routed boards
#: typically run with, so anything above this would be a routing problem
#: irrespective of grid alignment.
AUTO_DERIVED_TOLERANCE_HARD_CAP_MM: float = 0.15


#: Floor for the auto-derived tolerance, in millimeters.
#:
#: Even on a perfectly on-grid board the auto-derive logic must not return
#: a tolerance below the post-#3057 default.  This guarantees that
#: ``auto_derive_threshold=True`` is always at least as permissive as
#: ``auto_derive_threshold=False`` -- no surprise regressions for boards
#: where every pad happens to land within 0.001 mm of grid.
AUTO_DERIVED_TOLERANCE_FLOOR_MM: float = DEFAULT_PAD_GRID_TOLERANCE_MM


#: Safety margin added to the p99 of the per-pad offsets when auto-deriving
#: the tolerance, in millimeters.  Pads naturally cluster around their
#: intrinsic metric/imperial-rounding offset; the margin gives the auto-derived
#: threshold a little headroom over the highest observed legitimate offset
#: so that a single placement error 0.005 mm above the cluster still flags.
AUTO_DERIVED_TOLERANCE_MARGIN_MM: float = 0.005


#: Percentile used to summarise the per-pad L2 offset distribution when
#: auto-deriving the tolerance.  ``0.99`` means "absorb the noise floor of
#: the 99% of pads that are clearly intrinsic, then add a small safety
#: margin"; the top 1% (which empirically corresponds to the off-grid tail
#: introduced by genuine placement errors and gross library bugs) still
#: shows up as a violation.
AUTO_DERIVED_TOLERANCE_PERCENTILE: float = 0.99


def _percentile(values: list[float], percentile: float) -> float:
    """Compute the (linearly-interpolated) percentile of a non-empty list.

    Implemented locally to avoid pulling in NumPy as a hot-path
    dependency for a one-shot DRC rule that the rest of the preflight
    module never needs.  The signature matches ``numpy.percentile`` with
    ``interpolation="linear"`` and ``percentile`` expressed as a fraction
    (``0.99``) rather than a percent (``99``).
    """
    if not values:
        raise ValueError("Cannot compute percentile of empty list")
    sorted_values = sorted(values)
    if percentile <= 0:
        return sorted_values[0]
    if percentile >= 1:
        return sorted_values[-1]
    # Linear interpolation between adjacent ranks (numpy.percentile default).
    rank = percentile * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def compute_pad_grid_tolerance(
    pcb_path: str | Path,
    grid_resolution: float = 0.1,
    grid_origin: tuple[float, float] = (0.0, 0.0),
    percentile: float = AUTO_DERIVED_TOLERANCE_PERCENTILE,
    margin: float = AUTO_DERIVED_TOLERANCE_MARGIN_MM,
    floor: float = AUTO_DERIVED_TOLERANCE_FLOOR_MM,
    cap: float = AUTO_DERIVED_TOLERANCE_HARD_CAP_MM,
) -> float:
    """Auto-derive the pad_grid tolerance from a board's offset histogram.

    Builds the per-pad L2-distance-to-grid distribution for ``pcb_path``
    and returns ``max(floor, min(cap, p99(offsets) + margin))``.  The
    result is the smallest tolerance that absorbs ~99% of the intrinsic
    library/metric-rounding noise on the board while leaving the top
    ~1% of pads (genuine placement errors, gross library bugs) above
    the threshold.

    The threshold is *board-specific*: a board with only metric-on-metric
    footprints lands near :data:`AUTO_DERIVED_TOLERANCE_FLOOR_MM`; a
    board with fine-pitch QFN/BGA whose pad lattice naturally sits 0.07
    mm off the 0.1 mm grid lands near 0.08 mm; both still flag a 0.15 mm
    placement error.  See issue #3061 for context.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file (or the file contents).
        grid_resolution: Router grid resolution in mm.  Defaults to the
            same ``0.1`` mm used by :func:`check_pad_grid_alignment`.
        grid_origin: Grid origin offset ``(x, y)`` in mm.
        percentile: Fraction (0..1) of the offset distribution to absorb
            below the threshold.  Defaults to
            :data:`AUTO_DERIVED_TOLERANCE_PERCENTILE` (``0.99``).
        margin: Safety margin added on top of the percentile, in mm.
            Defaults to :data:`AUTO_DERIVED_TOLERANCE_MARGIN_MM`
            (``0.005``).
        floor: Minimum tolerance to return, in mm.  Defaults to
            :data:`AUTO_DERIVED_TOLERANCE_FLOOR_MM` (``0.05``) so the
            auto-derived rule is never stricter than the post-#3057
            default.
        cap: Maximum tolerance to return, in mm.  Defaults to
            :data:`AUTO_DERIVED_TOLERANCE_HARD_CAP_MM` (``0.15``) so a
            pathological board cannot push the threshold above the
            typical minimum trace clearance.

    Returns:
        The derived tolerance in mm.  Falls back to ``floor`` when the
        board has no pads (the answer is vacuous either way).
    """
    pads = load_pads_for_analysis(pcb_path)
    if not pads:
        return floor

    x_off, y_off = grid_origin
    offsets = [
        _l2_distance_to_grid(pad.x, pad.y, grid_resolution, x_off, y_off)
        for pad in pads
    ]

    raw = _percentile(offsets, percentile) + margin
    bounded = min(cap, max(floor, raw))
    return float(bounded)


def check_pad_grid_alignment(
    pcb_path: str | Path,
    grid_resolution: float = 0.1,
    threshold: float | None = None,
    grid_origin: tuple[float, float] = (0.0, 0.0),
    clearance: float = 0.15,
    auto_derive_threshold: bool = False,
) -> OffGridReport:
    """Check that every pad in the PCB aligns with the router grid.

    Off-grid pads cause routing failures (``PADS_OFF_GRID``) deep inside
    the autorouter.  Running this check at PCB-write time produces a
    much earlier and more actionable error.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file (or the file contents).
        grid_resolution: Router grid resolution in mm (default ``0.1``,
            matching ``Autorouter`` defaults and ``KCT_ROUTE_GRID``).
        threshold: Maximum L2 deviation considered on-grid, in mm.
            When omitted (and ``auto_derive_threshold`` is ``False``)
            defaults to :data:`DEFAULT_PAD_GRID_TOLERANCE_MM` (``0.05``
            mm) to clear stock KiCad library footprints whose pads sit
            0.03-0.05 mm off the 0.1 mm grid by design.  Pass an explicit
            value (e.g. ``grid_resolution / 10``) to enforce the
            stricter router-side check.  An explicit ``threshold``
            always wins over ``auto_derive_threshold``.
        grid_origin: Optional grid origin offset ``(x, y)`` in mm.
            Grid points are at ``offset + k * resolution``.  Defaults to
            ``(0.0, 0.0)``.
        clearance: Default trace clearance in mm, used by the
            :func:`auto_select_grid_resolution` analysis when computing a
            "finer grid" suggestion.
        auto_derive_threshold: When ``True`` and ``threshold`` is
            ``None``, derive the threshold from the board's pad-offset
            histogram via :func:`compute_pad_grid_tolerance`.  Defaults
            to ``False`` to preserve backwards-compatibility with the
            constant-default behaviour introduced in PR #3057.  The CLI
            (``kct check``) overrides this to ``True`` by default.

    Returns:
        :class:`OffGridReport` with the list of off-grid pads (possibly
        empty) and a suggested finer grid when one would resolve every
        violation.

    Example:
        >>> report = check_pad_grid_alignment("board.kicad_pcb")
        >>> if not report.passed:
        ...     print(report.summary())
    """
    if threshold is None:
        if auto_derive_threshold:
            threshold = compute_pad_grid_tolerance(
                pcb_path,
                grid_resolution=grid_resolution,
                grid_origin=grid_origin,
            )
        else:
            threshold = DEFAULT_PAD_GRID_TOLERANCE_MM

    pads = load_pads_for_analysis(pcb_path)

    x_off, y_off = grid_origin

    # Add a small floating-point tolerance to the threshold so pads exactly
    # on the boundary (e.g. ``124.49 mm`` on a ``0.1 mm`` grid -- the FP
    # round-off makes the residue ``0.0100000000005``) are not falsely
    # flagged.  ~1 nm is well below any meaningful PCB tolerance.
    fp_eps = max(1e-9, threshold * 1e-6)

    off_grid: list[PreflightOffGridPad] = []
    for pad in pads:
        dist = _l2_distance_to_grid(pad.x, pad.y, grid_resolution, x_off, y_off)
        if dist <= threshold + fp_eps:
            continue

        off_grid.append(
            PreflightOffGridPad(
                ref=pad.ref,
                pin=pad.pin,
                x=pad.x,
                y=pad.y,
                offset_mm=dist,
                footprint_name=pad.footprint_name,
            )
        )

    suggested = (
        _suggest_finer_grid(pads, grid_resolution, grid_origin, clearance) if off_grid else None
    )

    return OffGridReport(
        grid_resolution=grid_resolution,
        threshold=threshold,
        grid_origin=grid_origin,
        off_grid_pads=off_grid,
        suggested_grid=suggested,
        total_pads=len(pads),
    )


__all__ = [
    "AUTO_DERIVED_TOLERANCE_FLOOR_MM",
    "AUTO_DERIVED_TOLERANCE_HARD_CAP_MM",
    "AUTO_DERIVED_TOLERANCE_MARGIN_MM",
    "AUTO_DERIVED_TOLERANCE_PERCENTILE",
    "DEFAULT_PAD_GRID_TOLERANCE_MM",
    "PreflightOffGridPad",
    "OffGridReport",
    "check_pad_grid_alignment",
    "compute_pad_grid_tolerance",
]
