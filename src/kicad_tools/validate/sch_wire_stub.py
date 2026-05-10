"""Wire-stub ERC rule (schematic-level).

Detects wires whose dangling endpoint is exactly *N* grid units short
of a real pin position.  This is the canonical "off-by-one-grid" defect
class found in the chorus-test-revA schematic, where seven wires in
``connectors.kicad_sch`` terminate at ``x=81.28`` while the matching
J2 left-side pins are at ``x=83.82`` -- exactly one 2.54 mm grid step
short.  KiCad's built-in ERC reports these as ``wire_dangling`` but
does NOT diagnose the off-by-grid relationship to a nearby pin, so the
agent loop receives a generic "wire not connected at both ends" with
no actionable suggestion.

Algorithm
---------

For every wire in every sheet of the design:

1. Determine each endpoint's "free" status by checking whether it
   touches any pin, junction, label, or no_connect.  An endpoint that
   already coincides with one of those is connected and is silently
   skipped.
2. For each free endpoint, search for the nearest pin within
   ``max_stub_grids * grid_mm`` mm along an axis-aligned direction
   (horizontal or vertical).  When the offset is an integer multiple
   of the grid (``grid_mm``, default 2.54 mm) and >= 1 grid step, the
   endpoint is classified as a wire stub.  When the offset is below
   one grid step but non-zero, KiCad ERC's ``endpoint_off_grid``
   already flags it -- we skip these to avoid duplicate reporting.

Grid handling
-------------

The default grid is 2.54 mm (KiCad's stock schematic grid).  Some
projects use a 1.27 mm sub-grid; callers can override.  The detector
only flags stubs that are an exact integer multiple of the grid -- a
non-grid-aligned endpoint is a different defect class and is left to
KiCad's built-in ``endpoint_off_grid`` rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Default KiCad schematic grid step in mm.
DEFAULT_GRID_MM = 2.54

# Coordinates within this distance are considered to coincide
# (typically 10 micrometers).  Schematic positions are stored in mm
# with at most 4-5 decimal places, so 0.01 mm is well below any
# meaningful drift.
_COORD_EPS = 0.01

# Maximum number of grid steps to scan for a "missed-by-N-grids"
# match.  Beyond 4 grid steps (10.16 mm) it's unlikely the user
# intended the wire to reach the candidate pin.
DEFAULT_MAX_STUB_GRIDS = 4


@dataclass(frozen=True)
class WireStubFinding:
    """A single wire-stub finding.

    Attributes:
        sheet: Path to the schematic sheet containing the wire.
        wire_start: (x, y) of the wire's start (mm).
        wire_end: (x, y) of the wire's end (mm).
        dangling_endpoint: (x, y) of the free endpoint that is short
            of a pin (mm).
        candidate_pin_ref: Component reference + pin number of the
            nearest pin (e.g., ``"J2.8"``).
        candidate_pin_position: (x, y) of the candidate pin (mm).
        grid_steps_short: How many grid units the endpoint is short
            of the candidate pin.  Always a positive integer.
        axis: ``"x"`` for horizontal stub, ``"y"`` for vertical.
    """

    sheet: str
    wire_start: tuple[float, float]
    wire_end: tuple[float, float]
    dangling_endpoint: tuple[float, float]
    candidate_pin_ref: str
    candidate_pin_position: tuple[float, float]
    grid_steps_short: int
    axis: str


def _approx_eq(a: float, b: float, eps: float = _COORD_EPS) -> bool:
    """True when two floats are within ``eps``."""
    return abs(a - b) <= eps


def _approx_point(
    p: tuple[float, float], q: tuple[float, float], eps: float = _COORD_EPS
) -> bool:
    return _approx_eq(p[0], q[0], eps) and _approx_eq(p[1], q[1], eps)


def _endpoint_is_connected(
    endpoint: tuple[float, float],
    pin_positions: Iterable[tuple[str, str, float, float]],
    junctions: Iterable,
    labels_pos: Iterable[tuple[float, float]],
    no_connects: Iterable[tuple[float, float]],
    other_wires_endpoints: Iterable[tuple[float, float]],
) -> bool:
    """Return True if ``endpoint`` is connected to a real anchor.

    Checks for:
    - Coincident pin position
    - Coincident junction
    - Coincident label of any kind
    - Coincident no-connect flag
    - Coincident endpoint of any other wire (T-junctions through
      proper connections still count -- KiCad does not require a
      junction marker when only two wires meet at a non-T point).
    """
    ex, ey = endpoint
    for _ref, _pin, px, py in pin_positions:
        if _approx_eq(px, ex) and _approx_eq(py, ey):
            return True
    for j in junctions:
        jx, jy = j.position
        if _approx_eq(jx, ex) and _approx_eq(jy, ey):
            return True
    for lx, ly in labels_pos:
        if _approx_eq(lx, ex) and _approx_eq(ly, ey):
            return True
    for nx, ny in no_connects:
        if _approx_eq(nx, ex) and _approx_eq(ny, ey):
            return True
    return any(
        _approx_eq(ox, ex) and _approx_eq(oy, ey) for ox, oy in other_wires_endpoints
    )


def _find_grid_aligned_pin(
    endpoint: tuple[float, float],
    pin_positions: Iterable[tuple[str, str, float, float]],
    grid_mm: float,
    max_grids: int,
) -> tuple[str, str, float, float, int, str] | None:
    """Find the closest pin axis-aligned and an integer grid count away.

    Args:
        endpoint: The wire's free endpoint (x, y) in mm.
        pin_positions: Iterable of (ref, pin_num, x, y) for every pin
            in the design.
        grid_mm: Grid step (typically 2.54 mm).
        max_grids: Maximum search distance in grid units.

    Returns:
        ``(ref, pin_num, px, py, grid_steps, axis)`` for the closest
        matching pin, or ``None`` when no match within
        ``max_grids * grid_mm`` is found.  ``axis`` is ``"x"`` or
        ``"y"``.  Endpoints already coincident with a pin (0 grid
        steps) are skipped -- those are not stubs.
    """
    ex, ey = endpoint
    best: tuple[str, str, float, float, int, str] | None = None
    best_grid_steps: int | None = None

    for ref, pin_num, px, py in pin_positions:
        # Require axis-aligned alignment on the OTHER axis.
        if _approx_eq(py, ey):
            # Same Y -- horizontal stub.
            dx = px - ex
            if abs(dx) < grid_mm - _COORD_EPS:
                # Below 1 grid step.  Could be the same point, could
                # be sub-grid -- either way, skip.
                continue
            # Snap dx to integer grid count.
            grid_count = round(dx / grid_mm)
            if grid_count == 0:
                continue
            # Verify the remainder is small (i.e., dx is an integer
            # multiple of the grid).
            residual = abs(dx - grid_count * grid_mm)
            if residual > _COORD_EPS:
                continue
            if abs(grid_count) > max_grids:
                continue
            steps = abs(grid_count)
            if best_grid_steps is None or steps < best_grid_steps:
                best = (ref, pin_num, px, py, steps, "x")
                best_grid_steps = steps
        elif _approx_eq(px, ex):
            # Same X -- vertical stub.
            dy = py - ey
            if abs(dy) < grid_mm - _COORD_EPS:
                continue
            grid_count = round(dy / grid_mm)
            if grid_count == 0:
                continue
            residual = abs(dy - grid_count * grid_mm)
            if residual > _COORD_EPS:
                continue
            if abs(grid_count) > max_grids:
                continue
            steps = abs(grid_count)
            if best_grid_steps is None or steps < best_grid_steps:
                best = (ref, pin_num, px, py, steps, "y")
                best_grid_steps = steps

    return best


def _resolve_pin_positions(
    schematic, lib_symbols: dict[str, object]
) -> list[tuple[str, str, float, float]]:
    """Resolve every pin position on every placed symbol in a sheet.

    Shared helper duplicated from sch_orphan_label to keep the two
    modules independently testable.
    """
    results: list[tuple[str, str, float, float]] = []
    for sym in schematic.symbols:
        if sym.dnp:
            continue
        lib_sym = lib_symbols.get(sym.lib_id)
        if lib_sym is None:
            continue
        ref = sym.reference or ""
        if not ref:
            continue
        try:
            pin_positions = lib_sym.get_all_pin_positions(
                instance_pos=sym.position,
                instance_rot=sym.rotation,
                mirror=sym.mirror,
            )
        except Exception:
            continue
        for pin_num, (x, y) in pin_positions.items():
            results.append((ref, pin_num, x, y))
    return results


def find_wire_stubs(
    sheets: list[tuple[str, object]],
    lib_symbols: dict[str, object],
    grid_mm: float = DEFAULT_GRID_MM,
    max_stub_grids: int = DEFAULT_MAX_STUB_GRIDS,
) -> list[WireStubFinding]:
    """Find wire endpoints that miss a pin by an integer multiple of grid.

    Args:
        sheets: List of ``(sheet_path, Schematic)`` tuples for every
            sheet in the design.
        lib_symbols: Mapping ``lib_id -> LibrarySymbol`` resolved.
        grid_mm: KiCad schematic grid step (default 2.54 mm).
        max_stub_grids: Maximum number of grid units to search.

    Returns:
        List of :class:`WireStubFinding`.  Empty when no stubs found.
    """
    findings: list[WireStubFinding] = []

    for sheet_path, sch in sheets:
        pin_positions = _resolve_pin_positions(sch, lib_symbols)
        # Pre-compute label and no_connect positions for connection
        # checks.
        labels_pos: list[tuple[float, float]] = []
        for lbl in sch.labels:
            labels_pos.append(lbl.position)
        for lbl in sch.hierarchical_labels:
            labels_pos.append(lbl.position)
        for lbl in sch.global_labels:
            labels_pos.append(lbl.position)
        no_connects = [(nc.position[0], nc.position[1]) for nc in sch.no_connects]
        # Precompute every wire endpoint so we can check whether a
        # given wire endpoint coincides with another wire (and is
        # therefore part of a chain, not dangling).
        all_wire_endpoints: list[tuple[float, float]] = []
        for w in sch.wires:
            all_wire_endpoints.append(w.start)
            all_wire_endpoints.append(w.end)

        for wire in sch.wires:
            for endpoint in (wire.start, wire.end):
                # Exclude this wire's other endpoint from the "other
                # wires" set so a single isolated wire still flags
                # both endpoints if both dangle.
                other_endpoints = [
                    p
                    for p in all_wire_endpoints
                    if not _approx_point(p, endpoint)
                ]
                if _endpoint_is_connected(
                    endpoint,
                    pin_positions,
                    sch.junctions,
                    labels_pos,
                    no_connects,
                    other_endpoints,
                ):
                    continue
                match = _find_grid_aligned_pin(
                    endpoint,
                    pin_positions,
                    grid_mm=grid_mm,
                    max_grids=max_stub_grids,
                )
                if match is None:
                    continue
                ref, pin_num, px, py, steps, axis = match
                findings.append(
                    WireStubFinding(
                        sheet=sheet_path,
                        wire_start=wire.start,
                        wire_end=wire.end,
                        dangling_endpoint=endpoint,
                        candidate_pin_ref=f"{ref}.{pin_num}",
                        candidate_pin_position=(px, py),
                        grid_steps_short=steps,
                        axis=axis,
                    )
                )

    return findings
