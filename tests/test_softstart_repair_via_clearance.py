"""Unit tests for the softstart repair-via clearance gate (issue #3495).

The softstart recipe's stranded-pad / island-bridge repairs (steps 10 and
10b of ``boards/external/softstart/generate_design.py``) append F.Cu↔B.Cu
through-vias at SMD pad centers.  Those vias span every copper layer, so a
via dropped on top of a foreign-net trace is a real cross-net short that
the same-net connectivity audit cannot see.  This is how a full-pipeline
regen grew 6 B.Cu cross-net overlaps the committed clean artifact never
had (GATE_POS_A via over both PRECHARGE traces, a VGATE bridge via on
SRC_NEG, 3 PRECHARGE_POS/NEG overlaps).

These tests exercise the recipe's clearance gate in isolation (no routing
pipeline) so the regression is pinned cheaply: a via that would short a
foreign-net trace must be detected, relocated within the pad if possible,
and skipped when the pad is boxed in.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
RECIPE_PATH = REPO_ROOT / "boards" / "external" / "softstart" / "generate_design.py"


@pytest.fixture(scope="module")
def recipe():
    """Import the softstart recipe module by path (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("softstart_generate_design", RECIPE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pcb_with_segment(net_id: int, x1: float, y1: float, x2: float, y2: float, width: float) -> str:
    """Minimal PCB text holding one foreign-net segment."""
    return (
        "(kicad_pcb\n"
        f"  (segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) "
        f'(layer "B.Cu") (net {net_id}))\n'
        ")\n"
    )


def test_parse_foreign_segments_excludes_same_net(recipe):
    text = (
        "(kicad_pcb\n"
        '  (segment (start 0 0) (end 10 0) (width 0.3) (layer "B.Cu") (net 5))\n'
        '  (segment (start 0 5) (end 10 5) (width 0.3) (layer "F.Cu") (net 9))\n'
        ")\n"
    )
    # Via on net 5 should only see net 9's segment as foreign.
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    assert len(foreign) == 1
    assert foreign[0][:4] == (0.0, 5.0, 10.0, 5.0)


def test_via_on_top_of_foreign_segment_is_a_short(recipe):
    text = _pcb_with_segment(net_id=9, x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.3)
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    # Via center sits directly on the foreign trace centerline → short.
    assert recipe._via_clears_foreign_copper(5.0, 0.0, foreign) is False


def test_via_far_from_foreign_segment_clears(recipe):
    text = _pcb_with_segment(net_id=9, x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.3)
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    # 2 mm away is well beyond via_radius + width/2 + clearance (~0.575 mm).
    assert recipe._via_clears_foreign_copper(5.0, 2.0, foreign) is True


def test_clearance_threshold_matches_via_geometry(recipe):
    text = _pcb_with_segment(net_id=9, x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.3)
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    required = recipe._REPAIR_VIA_DIAMETER_MM / 2.0 + 0.3 / 2.0 + recipe._REPAIR_VIA_CLEARANCE_MM
    # Just inside the clearance band → short; just outside → clear.
    assert recipe._via_clears_foreign_copper(5.0, required - 0.01, foreign) is False
    assert recipe._via_clears_foreign_copper(5.0, required + 0.01, foreign) is True


def test_clear_location_returns_pad_center_when_clear(recipe):
    foreign: list = []  # empty board, nothing to short
    loc = recipe._find_clear_via_location(3.0, 4.0, pad_min_dim=1.2, foreign_segments=foreign)
    assert loc == (3.0, 4.0)


def test_clear_location_relocates_within_pad(recipe):
    # Foreign trace through the pad CENTER but a wide pad lets the via move
    # off-center while still landing on its own copper.
    text = _pcb_with_segment(net_id=9, x1=-5.0, y1=0.0, x2=5.0, y2=0.0, width=0.3)
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    loc = recipe._find_clear_via_location(0.0, 0.0, pad_min_dim=2.5, foreign_segments=foreign)
    assert loc is not None
    vx, vy = loc
    assert (vx, vy) != (0.0, 0.0)
    # Relocated via must actually clear the foreign trace...
    assert recipe._via_clears_foreign_copper(vx, vy, foreign) is True
    # ...and stay within the pad copper (center offset bounded by the barrel).
    max_offset = 2.5 / 2.0 - recipe._REPAIR_VIA_DIAMETER_MM / 2.0
    assert (vx**2 + vy**2) ** 0.5 <= max_offset + 1e-9


def test_clear_location_returns_none_for_boxed_in_small_pad(recipe):
    # A foreign trace crosses a SMALL pad's center; the via barrel cannot
    # move far enough to clear without leaving the pad → must skip (None).
    text = _pcb_with_segment(net_id=9, x1=-5.0, y1=0.0, x2=5.0, y2=0.0, width=0.3)
    foreign = recipe._parse_foreign_segments(text, via_net_id=5)
    loc = recipe._find_clear_via_location(0.0, 0.0, pad_min_dim=0.6, foreign_segments=foreign)
    assert loc is None
