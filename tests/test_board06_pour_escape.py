"""Physical constraints of the bounded Board06 pour escape fallback."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

spec = importlib.util.spec_from_file_location(
    "board06_pour_escape",
    Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/pour_escape.py",
)
escape = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = escape
spec.loader.exec_module(escape)


@pytest.mark.parametrize("offset", [0, 100])
def test_long_corridor_uses_legal_via_and_preserves_geometry(offset):
    start = (offset, 0)
    pads = [(Point(start).buffer(0.15), "GND", {"F.Cu"}, 0, start)]
    segments = [
        (box(offset - 1, 0.41, offset + 18, 1), "other", "F.Cu"),
        (box(offset - 1, -1, offset + 18, -0.41), "other", "F.Cu"),
    ]
    primary = [(box(offset + 19, -2, offset + 21, 2), {"In1.Cu"}, "fill")]
    before = [g.wkb for g, _, _ in segments]
    result = escape.find_escape(
        start,
        "GND",
        "F.Cu",
        pads,
        segments,
        [],
        primary,
        (offset - 0.5, -0.5, offset + 21, 0.5),
        escape.EscapeRules(),
    )
    assert result is not None and result.via
    assert result.points[-1][0] - offset > 18
    copper = LineString(result.points).buffer(result.rules.width / 2)
    assert all(copper.distance(g) >= result.rules.clearance for g, _, _ in segments)
    assert [g.wkb for g, _, _ in segments] == before


def test_front_copper_needs_no_via():
    result = escape.find_escape(
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [],
        [(box(0.5, -0.5, 1, 0.5), {"F.Cu"}, "seg")],
        (-1, -1, 2, 2),
        escape.EscapeRules(),
    )
    assert result is not None and not result.via


def test_budget_exhaustion_and_blocked_start_return_no_partial_path():
    args = (
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [],
        [(box(5, 5, 6, 6), {"F.Cu"}, "fill")],
        (-1, -1, 7, 7),
        escape.EscapeRules(),
    )
    assert escape.find_escape(*args, node_budget=2) is None
    args = list(args)
    args[4] = [(box(-0.1, -0.1, 0.1, 0.1), "other", "F.Cu")]
    assert escape.find_escape(*args) is None


def test_actual_existing_drill_blocks_otherwise_legal_via():
    args = (
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [(Point(1, 0), "GND", 0.5, 0.8)],
        [(box(-0.1, -0.1, 0.1, 0.1), {"In1.Cu"}, "fill")],
        (-0.3, -0.3, 0.3, 0.3),
        escape.EscapeRules(),
    )
    assert escape.find_escape(*args) is None
    args = list(args)
    args[5] = [(Point(1, 0), "GND", 0.5, 0.2)]
    assert escape.find_escape(*args).via


def test_project_constraints_strengthen_defaults(tmp_path):
    p = tmp_path / "board.kicad_pro"
    p.write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_track_width": 0.3,
                            "min_through_hole_diameter": 0.4,
                            "min_via_annular_width": 0.15,
                        }
                    }
                },
                "net_settings": {"classes": [{"clearance": 0.4}]},
            }
        )
    )
    rules = escape.EscapeRules.from_project(p)
    assert rules.width == 0.3 and rules.clearance == 0.4
    assert rules.diameter == pytest.approx(0.7)
    p.with_suffix(".kicad_dru").write_text("(version 1)")
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


def _kct_managed_dru(*rule_lines):
    from kicad_tools.manufacturers.dru_generator import (
        DRU_FLOORS_BLOCK_BEGIN,
        DRU_FLOORS_BLOCK_END,
    )

    body = "\n".join(rule_lines)
    return f"(version 1)\n\n{DRU_FLOORS_BLOCK_BEGIN}\n{body}\n{DRU_FLOORS_BLOCK_END}\n"


def test_kct_managed_dru_sidecar_folds_in_floors(tmp_path):
    """A pure kct fab-floors sidecar (Issue #4600) is known-safe, not rejected.

    Every rule's floor here is set stronger than the ``EscapeRules``
    dataclass default so folding it in is observable (a weaker fab floor,
    like board 06's real 0.1016mm jlcpcb-tier1 trace width against the
    0.2mm default, would be masked by the "strengthen, never weaken"
    ``max()`` and prove nothing).
    """
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    p.with_suffix(".kicad_dru").write_text(
        _kct_managed_dru(
            '(rule "Trace Width - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'track'\")\n"
            "  (constraint track_width (min 0.3mm)))",
            '(rule "Clearance - jlcpcb-tier1"\n  (constraint clearance (min 0.25mm)))',
            '(rule "Via Drill - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
            "  (constraint hole_size (min 0.35mm)))",
            '(rule "Via Diameter - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
            "  (constraint via_diameter (min 0.6mm)))",
            '(rule "Annular Ring - jlcpcb-tier1"\n'
            "  (condition \"A.Via_Type != 'Micro'\")\n"
            "  (constraint annular_width (min 0.2mm)))",
            '(rule "PTH Annular Ring - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'pad'\")\n"
            "  (constraint annular_width (min 0.5mm)))",
            '(rule "Copper to Edge - jlcpcb-tier1"\n  (constraint edge_clearance (min 0.3mm)))',
        )
    )
    rules = escape.EscapeRules.from_project(p)
    assert rules.width == pytest.approx(0.3)
    assert rules.clearance == pytest.approx(0.25)
    assert rules.drill == pytest.approx(0.35)
    # The pad-only "PTH Annular Ring" floor (0.5mm) must not leak into the
    # via annulus -- only the via "Annular Ring" floor (0.2mm) applies.
    assert rules.annulus == pytest.approx(0.2)
    # diameter is re-derived from drill + 2*annulus (0.35 + 0.4 = 0.75),
    # which exceeds the raw "Via Diameter" floor (0.6mm).
    assert rules.diameter == pytest.approx(0.75)


def test_kct_managed_dru_sidecar_with_extra_content_still_rejected(tmp_path):
    """Anything beyond the exact managed block is unmodeled -- fail closed."""
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    managed = _kct_managed_dru(
        '(rule "Clearance - jlcpcb-tier1"\n  (constraint clearance (min 0.1016mm)))'
    )
    p.with_suffix(".kicad_dru").write_text(
        managed + '\n(rule "Custom hand rule"\n  (constraint clearance (min 1mm)))\n'
    )
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


def test_kct_managed_dru_sidecar_with_unknown_rule_family_rejected(tmp_path):
    """A rule family this recipe has never enumerated is not silently ignored."""
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    p.with_suffix(".kicad_dru").write_text(
        _kct_managed_dru(
            '(rule "Something New - jlcpcb-tier1"\n  (constraint clearance (min 1mm)))'
        )
    )
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


@pytest.mark.parametrize("value", [-1, True, "0.2", float("nan")])
def test_invalid_project_dimensions_fail_closed(tmp_path, value):
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": value}}}}))
    with pytest.raises(ValueError, match="finite nonnegative"):
        escape.EscapeRules.from_project(p)
