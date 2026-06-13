"""Tests for FootprintOutsideBoardRule placement validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from kicad_tools.validate.rules.placement import (
    FootprintOutsideBoardRule,
    point_in_polygon,
)

# ---------------------------------------------------------------------------
# Helpers -- lightweight stand-ins for schema objects
# ---------------------------------------------------------------------------


@dataclass
class _FakeFootprint:
    reference: str
    position: tuple[float, float]
    layer: str = "F.Cu"
    rotation: float = 0.0
    pads: list = field(default_factory=list)


class _FakePCB:
    """Minimal PCB stub that provides the methods used by the rule."""

    def __init__(
        self,
        footprints: list[_FakeFootprint],
        outline_polygon: list[tuple[float, float]],
        outline_segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    ) -> None:
        self.footprints = footprints
        self._outline_polygon = outline_polygon
        # Derive segments from consecutive polygon vertices if not given.
        if outline_segments is not None:
            self._outline_segments = outline_segments
        elif outline_polygon and len(outline_polygon) >= 3:
            segs = []
            for i in range(len(outline_polygon)):
                segs.append(
                    (
                        outline_polygon[i],
                        outline_polygon[(i + 1) % len(outline_polygon)],
                    )
                )
            self._outline_segments = segs
        else:
            self._outline_segments = []

    def get_board_outline(self) -> list[tuple[float, float]]:
        return self._outline_polygon

    def get_board_outline_segments(
        self,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        return self._outline_segments


# A simple 10x10 rectangle from (0,0) to (10,10)
_RECT_OUTLINE = [(0, 0), (10, 0), (10, 10), (0, 10)]

# Dummy design rules (unused by this rule but required by the interface)
_DUMMY_RULES = MagicMock()


# ---------------------------------------------------------------------------
# point_in_polygon unit tests
# ---------------------------------------------------------------------------


class TestPointInPolygon:
    """Direct tests for the ray-casting helper."""

    def test_inside_rectangle(self):
        assert point_in_polygon(5.0, 5.0, _RECT_OUTLINE) is True

    def test_outside_rectangle(self):
        assert point_in_polygon(15.0, 5.0, _RECT_OUTLINE) is False

    def test_outside_negative(self):
        assert point_in_polygon(-1.0, 5.0, _RECT_OUTLINE) is False

    def test_concave_polygon_inside(self):
        # L-shaped polygon (concave)
        polygon = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        # Inside the bottom leg
        assert point_in_polygon(7.0, 2.0, polygon) is True
        # Inside the left leg
        assert point_in_polygon(2.0, 7.0, polygon) is True

    def test_concave_polygon_outside_notch(self):
        # Point in the concave "notch" of the L
        polygon = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        assert point_in_polygon(7.0, 7.0, polygon) is False


# ---------------------------------------------------------------------------
# FootprintOutsideBoardRule tests
# ---------------------------------------------------------------------------


class TestFootprintOutsideBoardRule:
    """Tests for the DRC rule itself."""

    def test_all_footprints_inside(self):
        """No violations when all footprints are inside the board."""
        pcb = _FakePCB(
            footprints=[
                _FakeFootprint("U1", (5.0, 5.0)),
                _FakeFootprint("C1", (2.0, 3.0)),
            ],
            outline_polygon=_RECT_OUTLINE,
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_footprint_outside(self):
        """Violations reported for footprints outside the board."""
        pcb = _FakePCB(
            footprints=[
                _FakeFootprint("U1", (5.0, 5.0)),  # inside
                _FakeFootprint("R1", (15.0, 5.0)),  # outside
                _FakeFootprint("C1", (-5.0, 5.0)),  # outside
            ],
            outline_polygon=_RECT_OUTLINE,
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 2
        refs = {v.items[0] for v in results.violations}
        assert refs == {"R1", "C1"}
        for v in results.violations:
            assert v.rule_id == "footprint_outside_board"
            assert v.severity == "error"
            assert v.actual_value > 0

    def test_no_board_outline(self):
        """No violations and no crash when the board has no outline."""
        pcb = _FakePCB(
            footprints=[_FakeFootprint("U1", (5.0, 5.0))],
            outline_polygon=[],
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_degenerate_outline(self):
        """Fewer than 3 vertices -- treated as no outline."""
        pcb = _FakePCB(
            footprints=[_FakeFootprint("U1", (5.0, 5.0))],
            outline_polygon=[(0, 0), (10, 0)],
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_concave_outline(self):
        """Non-rectangular (concave) outline classifies correctly."""
        polygon = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        pcb = _FakePCB(
            footprints=[
                _FakeFootprint("U1", (2.0, 2.0)),  # inside bottom leg
                _FakeFootprint("R1", (7.0, 7.0)),  # in the notch (outside)
            ],
            outline_polygon=polygon,
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 1
        assert results.violations[0].items == ("R1",)

    def test_violation_has_distance(self):
        """Violation message includes distance from board edge."""
        pcb = _FakePCB(
            footprints=[_FakeFootprint("R1", (15.0, 5.0))],
            outline_polygon=_RECT_OUTLINE,
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 1
        v = results.violations[0]
        # Distance from (15, 5) to nearest edge at x=10 should be 5.0
        assert v.actual_value == pytest.approx(5.0)
        assert "5.00mm" in v.message

    def test_nonzero_origin_footprints_inside(self):
        """With board-relative coordinates, footprints inside should pass.

        Simulates the scenario where get_board_outline() returns
        board-relative coordinates (0,0)-(10,10) and footprint positions
        are also board-relative. This is the post-fix behaviour.
        """
        pcb = _FakePCB(
            footprints=[
                _FakeFootprint("U1", (5.0, 5.0)),
                _FakeFootprint("J1", (1.5, 3.25)),
            ],
            outline_polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 0

    def test_nonzero_origin_footprint_outside_detected(self):
        """A footprint genuinely outside the board-relative outline is flagged."""
        pcb = _FakePCB(
            footprints=[
                _FakeFootprint("U1", (5.0, 5.0)),  # inside
                _FakeFootprint("R1", (-5.0, 5.0)),  # outside (negative x)
            ],
            outline_polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 1
        assert results.violations[0].items == ("R1",)

    def test_no_footprints(self):
        """Empty footprint list produces no violations."""
        pcb = _FakePCB(
            footprints=[],
            outline_polygon=_RECT_OUTLINE,
        )
        rule = FootprintOutsideBoardRule()
        results = rule.check(pcb, _DUMMY_RULES)

        assert len(results.violations) == 0
        assert results.rules_checked == 1
