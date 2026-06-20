"""Regression tests for board-outline containment in placement optimization.

Issue #3804: ``kct placement optimize`` let components escape the Edge.Cuts
because (1) ``PlacementOptimizer.from_pcb`` silently swallowed the real
Edge.Cuts parse error and estimated a (wrong) outline from current component
positions, and (2) the only hard containment was an AABB clamp keyed off that
(possibly wrong) outline.

These tests build PCBs with *known small* Edge.Cuts outlines, seed footprints
off-board, run the optimizer, and assert that every non-fixed component center
ends up inside the real outline (minus boundary margin).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.workflow import OptimizationWorkflow, WorkflowConfig
from kicad_tools.schema.pcb import PCB

# A 40 x 30 mm board outline anchored at (100, 100). Deliberately small so that
# components seeded far away (e.g. at ~250, 200) are unambiguously off-board and
# must be pulled back inside.
BOARD_MIN_X = 100.0
BOARD_MIN_Y = 100.0
BOARD_W = 40.0
BOARD_H = 30.0
BOARD_MAX_X = BOARD_MIN_X + BOARD_W
BOARD_MAX_Y = BOARD_MIN_Y + BOARD_H


_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
"""

_GR_RECT_OUTLINE = f"""  (gr_rect (start {BOARD_MIN_X} {BOARD_MIN_Y}) (end {BOARD_MAX_X} {BOARD_MAX_Y})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
"""

# Same rectangle expressed as four chained gr_line segments (the alternate
# outline representation the reporter also tried).
_GR_LINE_OUTLINE = f"""  (gr_line (start {BOARD_MIN_X} {BOARD_MIN_Y}) (end {BOARD_MAX_X} {BOARD_MIN_Y})
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start {BOARD_MAX_X} {BOARD_MIN_Y}) (end {BOARD_MAX_X} {BOARD_MAX_Y})
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start {BOARD_MAX_X} {BOARD_MAX_Y}) (end {BOARD_MIN_X} {BOARD_MAX_Y})
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (gr_line (start {BOARD_MIN_X} {BOARD_MAX_Y}) (end {BOARD_MIN_X} {BOARD_MIN_Y})
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
"""


def _footprint(ref: str, x: float, y: float, net1: int, net2: int, uuid_n: int) -> str:
    """A small two-pad resistor footprint at (x, y) on two nets."""
    return f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uuid_n:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-0000000001{uuid_n:02d}"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-0000000002{uuid_n:02d}"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1} "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2} "+3.3V"))
  )
"""


def _build_pcb_text(outline: str, *, off_board: bool) -> str:
    """Assemble a .kicad_pcb with the given outline and several footprints.

    When ``off_board`` is True several footprints are seeded well outside the
    Edge.Cuts to exercise the containment fix (and to ensure the off-board
    seeds are *not* used to inflate the outline estimate).
    """
    if off_board:
        seeds = [
            ("R1", 250.0, 200.0),
            ("R2", 260.0, 210.0),
            ("R3", 50.0, 50.0),
            ("R4", 300.0, 105.0),
            ("R5", 115.0, 115.0),  # one in-board for good measure
        ]
    else:
        seeds = [
            ("R1", 110.0, 110.0),
            ("R2", 120.0, 115.0),
            ("R3", 130.0, 120.0),
            ("R4", 115.0, 125.0),
            ("R5", 125.0, 110.0),
        ]
    # Alternate the spring nets so the optimizer has connectivity to act on.
    fps = []
    for i, (ref, x, y) in enumerate(seeds):
        net1 = 1 if i % 2 == 0 else 2
        net2 = 2 if i % 2 == 0 else 1
        fps.append(_footprint(ref, x, y, net1, net2, uuid_n=10 + i))
    return _HEADER + outline + "".join(fps) + ")\n"


def _write_pcb(tmp_path: Path, text: str, name: str) -> PCB:
    pcb_file = tmp_path / name
    pcb_file.write_text(text)
    return PCB.load(str(pcb_file))


def _assert_all_in_bounds(pcb: PCB, margin: float) -> None:
    """Assert every footprint center is within the real Edge.Cuts (minus margin)."""
    for fp in pcb.footprints:
        x, y = fp.position
        assert BOARD_MIN_X + margin - 1e-6 <= x <= BOARD_MAX_X - margin + 1e-6, (
            f"{fp.reference} x={x} escaped board [{BOARD_MIN_X + margin}, {BOARD_MAX_X - margin}]"
        )
        assert BOARD_MIN_Y + margin - 1e-6 <= y <= BOARD_MAX_Y - margin + 1e-6, (
            f"{fp.reference} y={y} escaped board [{BOARD_MIN_Y + margin}, {BOARD_MAX_Y - margin}]"
        )


# ---------------------------------------------------------------------------
# Containment after optimize (the core regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outline", [_GR_RECT_OUTLINE, _GR_LINE_OUTLINE], ids=["gr_rect", "gr_line"]
)
def test_force_directed_keeps_components_in_bounds(tmp_path: Path, outline: str) -> None:
    """Force-directed optimize must keep all centers inside a small outline."""
    pcb = _write_pcb(tmp_path, _build_pcb_text(outline, off_board=True), "board.kicad_pcb")

    workflow = OptimizationWorkflow(pcb, WorkflowConfig(strategy="force-directed", iterations=400))
    result = workflow.run()
    workflow.write_to_pcb()

    assert result.success
    # End-of-run containment check reports zero escapes.
    assert result.out_of_bounds_components == []
    # And the actual written-back positions are inside the real outline.
    margin = workflow.optimizer.config.boundary_margin
    _assert_all_in_bounds(pcb, margin)


def test_hybrid_keeps_components_in_bounds(tmp_path: Path) -> None:
    """Hybrid (evolutionary + physics) optimize must also contain components."""
    pcb = _write_pcb(tmp_path, _build_pcb_text(_GR_RECT_OUTLINE, off_board=True), "board.kicad_pcb")

    workflow = OptimizationWorkflow(
        pcb,
        WorkflowConfig(strategy="hybrid", iterations=200, generations=10, population=12),
    )
    result = workflow.run()
    workflow.write_to_pcb()

    assert result.success
    assert result.out_of_bounds_components == []
    margin = workflow.optimizer.config.boundary_margin
    _assert_all_in_bounds(pcb, margin)


def test_offboard_seed_does_not_inflate_outline(tmp_path: Path) -> None:
    """A footprint seeded far off-board is pulled in, not used to grow the board.

    Regression for the smoking-gun fallback: the optimizer must resolve the real
    Edge.Cuts (40x30) rather than an outline estimated from the off-board seeds
    (which would span >150mm and "contain" the escapees).
    """
    pcb = _write_pcb(tmp_path, _build_pcb_text(_GR_RECT_OUTLINE, off_board=True), "board.kicad_pcb")
    workflow = OptimizationWorkflow(pcb, WorkflowConfig(strategy="force-directed", iterations=400))
    workflow.run()

    # The resolved board outline must be the real 40x30 rect, not an inflated
    # box derived from the off-board seeds.
    verts = workflow.optimizer.board_outline.vertices
    width = max(v.x for v in verts) - min(v.x for v in verts)
    height = max(v.y for v in verts) - min(v.y for v in verts)
    assert width == pytest.approx(BOARD_W, abs=1e-6)
    assert height == pytest.approx(BOARD_H, abs=1e-6)


# ---------------------------------------------------------------------------
# --fixed pinning
# ---------------------------------------------------------------------------


def test_fixed_refs_are_pinned(tmp_path: Path) -> None:
    """Components passed via fixed_refs must not move (within float tolerance)."""
    pcb = _write_pcb(
        tmp_path, _build_pcb_text(_GR_RECT_OUTLINE, off_board=False), "board.kicad_pcb"
    )

    # Record R1's starting position before optimize.
    r1_before = next(fp.position for fp in pcb.footprints if fp.reference == "R1")

    workflow = OptimizationWorkflow(
        pcb,
        WorkflowConfig(strategy="force-directed", iterations=300, fixed_refs=["R1"]),
    )
    workflow.run()

    comp = workflow.optimizer._component_map["R1"]
    assert comp.fixed is True
    assert comp.x == pytest.approx(r1_before[0], abs=1e-6)
    assert comp.y == pytest.approx(r1_before[1], abs=1e-6)


def test_connector_prefix_autofixed_and_others_contained(tmp_path: Path) -> None:
    """A ``J``-prefixed ref is auto-fixed; non-fixed parts still contain."""
    # Build a board with one connector (J1) seeded in-board and resistors
    # seeded off-board.
    text = _HEADER + _GR_RECT_OUTLINE
    text += _footprint("J1", 110.0, 110.0, 1, 2, uuid_n=10)
    text += _footprint("R2", 250.0, 200.0, 2, 1, uuid_n=11)
    text += _footprint("R3", 300.0, 150.0, 1, 2, uuid_n=12)
    text += ")\n"
    pcb = _write_pcb(tmp_path, text, "board.kicad_pcb")

    j1_before = next(fp.position for fp in pcb.footprints if fp.reference == "J1")

    workflow = OptimizationWorkflow(pcb, WorkflowConfig(strategy="force-directed", iterations=400))
    result = workflow.run()
    workflow.write_to_pcb()

    # J1 auto-fixed by the J/H/MH prefix rule -> unchanged.
    j1 = workflow.optimizer._component_map["J1"]
    assert j1.fixed is True
    assert j1.x == pytest.approx(j1_before[0], abs=1e-6)
    assert j1.y == pytest.approx(j1_before[1], abs=1e-6)

    # Non-fixed resistors contained.
    assert result.out_of_bounds_components == []


# ---------------------------------------------------------------------------
# Fail-loud outline resolution
# ---------------------------------------------------------------------------


def test_unparseable_outline_raises_by_default(tmp_path: Path) -> None:
    """When Edge.Cuts cannot be closed, from_pcb raises rather than estimating."""
    from kicad_tools.optim.placement import PlacementOptimizer

    # An Edge.Cuts board with a single dangling segment cannot form a closed
    # polygon. The optimizer must refuse to estimate from component positions.
    text = _HEADER
    text += """  (gr_line (start 100 100) (end 140 100)
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
"""
    text += _footprint("R1", 250.0, 200.0, 1, 2, uuid_n=10)
    text += ")\n"
    pcb = _write_pcb(tmp_path, text, "board.kicad_pcb")

    with pytest.raises(ValueError, match="closed board outline"):
        PlacementOptimizer.from_pcb(pcb)


def test_unparseable_outline_opt_in_estimates_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With allow_estimated_outline=True the fallback is used but warns loudly."""
    from kicad_tools.optim.placement import PlacementOptimizer

    text = _HEADER
    text += """  (gr_line (start 100 100) (end 140 100)
    (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
"""
    text += _footprint("R1", 110.0, 110.0, 1, 2, uuid_n=10)
    text += ")\n"
    pcb = _write_pcb(tmp_path, text, "board.kicad_pcb")

    with caplog.at_level(logging.WARNING):
        opt = PlacementOptimizer.from_pcb(pcb, allow_estimated_outline=True)

    assert opt.board_outline.vertices  # an estimated outline exists
    assert any("estimated outline" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Boundary-margin guard (margin larger than half the board)
# ---------------------------------------------------------------------------


def test_oversized_margin_does_not_invert_clamp(tmp_path: Path) -> None:
    """A boundary margin larger than half the board collapses to center, not NaN."""
    pcb = _write_pcb(tmp_path, _build_pcb_text(_GR_RECT_OUTLINE, off_board=True), "board.kicad_pcb")

    # Margin 25mm > half of 30mm height -> would invert min/max bounds without
    # the guard. Components should collapse toward the board center, not fly off
    # to NaN / inverted positions.
    workflow = OptimizationWorkflow(
        pcb,
        WorkflowConfig(strategy="force-directed", iterations=200, boundary_margin=25.0),
    )
    workflow.run()

    cx = (BOARD_MIN_X + BOARD_MAX_X) / 2
    cy = (BOARD_MIN_Y + BOARD_MAX_Y) / 2
    for comp in workflow.optimizer.components:
        if comp.fixed:
            continue
        # Center of the (collapsed) clamp box in x; y collapses too since
        # 25 > 15. Positions must be finite and near the board center.
        assert abs(comp.x - cx) < 1.0
        assert abs(comp.y - cy) < 1.0


# ---------------------------------------------------------------------------
# Polygon geometry helper (non-rectangular projection)
# ---------------------------------------------------------------------------


def test_nearest_point_on_boundary_projects_outside_point() -> None:
    """nearest_point_on_boundary projects an external point onto the edge."""
    square = Polygon.rectangle(0.0, 0.0, 10.0, 10.0)  # corners at +/-5
    nearest = square.nearest_point_on_boundary(Vector2D(20.0, 0.0))
    assert nearest.x == pytest.approx(5.0)
    assert nearest.y == pytest.approx(0.0)


def test_out_of_bounds_components_flags_escapee() -> None:
    """out_of_bounds_components reports a component placed outside the outline."""
    from kicad_tools.optim.components import Component
    from kicad_tools.optim.placement import PlacementOptimizer

    board = Polygon.rectangle(0.0, 0.0, 20.0, 20.0)  # +/-10
    opt = PlacementOptimizer(board)
    opt.add_component(Component(ref="R1", x=0.0, y=0.0, width=1.0, height=1.0))
    opt.add_component(Component(ref="R2", x=100.0, y=100.0, width=1.0, height=1.0))
    assert opt.out_of_bounds_components(margin=0.0) == ["R2"]
