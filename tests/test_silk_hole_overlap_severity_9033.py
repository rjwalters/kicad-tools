"""Fab-graded ``silk_hole_overlap`` severity: error under jlcpcb (Issue #9033).

The fab's silkscreen-to-hole band is danger-only (0 warnings on both
recorded boards), so a profile that sets ``silk_to_hole_danger_mm`` upgrades
the rule's findings to errors at (at least) that clearance; ``None`` keeps
the advisory warning every other manufacturer sees.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.manufacturers import DesignRules, get_profile  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap  # noqa: E402

_HEAD = """(kicad_pcb (version 20240108) (generator "test") (generator_version "9.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (net 0 "")
  (setup (tenting front back))
"""

_VIA = """  (via (at 20 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0))
"""

_NPTH = """  (footprint "X:M" (layer "F.Cu") (at 30 20)
    (property "Reference" "MH1" (at 0 -3) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" np_thru_hole circle (at 0 0) (size 1.6 1.6) (drill 1.0)
      (layers "*.Cu" "*.Mask")))
"""


def _silk(x: float, layer: str = "F.SilkS") -> str:
    return (
        f"  (gr_line (start {x} 18) (end {x} 22) (stroke (width 0.15) (type solid)) "
        f'(layer "{layer}"))\n'
    )


def _load(tmp_path: Path, body: str) -> PCB:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_HEAD + body + ")\n")
    return PCB.load(path)


def _jlc():
    return get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)


def _other():
    return get_profile("pcbway").get_design_rules(layers=2, copper_oz=1)


def test_jlcpcb_profile_wires_the_band():
    """Every stackup of both jlcpcb profiles carries the 0.0 danger band."""
    for mfr in ("jlcpcb", "jlcpcb-tier1"):
        profile = get_profile(mfr)
        rules = profile.get_design_rules(layers=2, copper_oz=1)
        assert rules.silk_to_hole_danger_mm == 0.0, mfr
        deeper = profile.get_design_rules(layers=4, copper_oz=1)
        assert deeper is not None and deeper.silk_to_hole_danger_mm == 0.0, mfr
    assert _other().silk_to_hole_danger_mm is None


def test_silk_over_via_is_error_under_jlcpcb(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), _jlc())
    assert [v.severity for v in results.violations] == ["error"]
    assert results.violations[0].rule_id == "silk_hole_overlap"
    assert results.violations[0].required_value == 0.0
    assert "via" in results.violations[0].items[1]


def test_silk_over_via_is_warning_under_other_manufacturer(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), _other())
    assert [v.severity for v in results.violations] == ["warning"]


def test_none_field_keeps_warning(tmp_path):
    rules = dataclasses.replace(_jlc(), silk_to_hole_danger_mm=None)
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), rules)
    assert [v.severity for v in results.violations] == ["warning"]


def test_bare_design_rules_keep_warning(tmp_path):
    rules = DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.1,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.4,
        min_annular_ring_mm=0.05,
    )
    assert rules.silk_to_hole_danger_mm is None
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), rules)
    assert [v.severity for v in results.violations] == ["warning"]


def test_npth_hole_is_error_under_jlcpcb(tmp_path):
    results = check_silk_hole_overlap(_load(tmp_path, _NPTH + _silk(30.0)), _jlc())
    assert [v.severity for v in results.violations] == ["error"]
    assert "MH1 pad 1" in results.violations[0].items[1]


def test_back_side_silk_over_hole_is_error(tmp_path):
    """Holes pierce every layer, so B.SilkS ink over a drill is flagged too."""
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0, layer="B.SilkS")), _jlc())
    assert [v.severity for v in results.violations] == ["error"]


def test_silk_clear_of_holes_stays_clean(tmp_path):
    assert not check_silk_hole_overlap(
        _load(tmp_path, _VIA + _NPTH + _silk(25.0)), _jlc()
    ).violations


def test_positive_band_buffers_the_hole(tmp_path):
    """A nonzero danger clearance flags near-but-not-touching silk as error."""
    rules = dataclasses.replace(_jlc(), silk_to_hole_danger_mm=0.3)
    # Via drill radius 0.15 + stroke half-width 0.075: silk at x=20.35 sits
    # ~0.125 mm from the drill edge -- clear at 0 mm, inside a 0.3 mm band.
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.35)), rules)
    assert [v.severity for v in results.violations] == ["error"]
    assert results.violations[0].required_value == 0.3
    # The same board stays clean under the profile's real 0.0 band.
    assert not check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.35)), _jlc()).violations


def test_tented_via_still_flagged_as_error(tmp_path):
    """Tenting covers the annulus, not the drill: the finding stands (#8844)."""
    results = check_silk_hole_overlap(_load(tmp_path, _VIA + _silk(20.0)), _jlc())
    assert [v.severity for v in results.violations] == ["error"]
