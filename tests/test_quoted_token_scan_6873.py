"""Issue #6873: S-expression tokens inside quoted strings are not tokens.

Text-level fast paths used to find ``(pad ``, ``(footprint ``, ``(module ``
and ``(zone`` with ``str.find`` / ``re``, so a footprint ``descr`` or
``tags`` string mentioning one of them was taken for the real thing:

* ``load_pcb_for_routing`` split the board into footprints with
  ``re.split`` and paired the pieces with the *parsed* footprints using
  ``zip(strict=True)`` -- an extra piece raised ``ValueError``;
* ``_remove_zones_for_nets`` gave up at a ``(zone`` inside a string whose
  brackets did not balance, leaving every real zone after it in place.

``(pad `` was fixed first (#6740); its cases here guard the shared helper
that now backs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.io import (
    extract_pad_positions,
    load_pads_for_analysis,
    load_pcb_for_routing,
    verify_output_connectivity,
)
from kicad_tools.sexp import parse_string

# The escaped cases put the token *after* an escaped quote: a scanner that
# ignored backslashes would think the string had ended there.
_STRING_TEXT = {
    "pad": "Heatsink pins (pad 4, net HS) are 25.4 mm apart",
    "footprint": "Fits (footprint X) and friends",
    "module": "Legacy (module Y) outline",
    "zone": "pour (zone keepout, see note",
}
_ESCAPED_TEXT = {
    "pad": 'a 1\\" pitch (pad 9 smd HS) note',
    "footprint": 'a 1\\" pitch (footprint X) note',
    "module": 'a 1\\" pitch (module Y) note',
    "zone": 'a 1\\" pitch (zone keepout, see note',
}


def _cases(*tokens: str) -> list:
    return [
        pytest.param(token, field, table[token], id=f"{token}-{field}-{kind}")
        for kind, table in (("plain", _STRING_TEXT), ("escaped", _ESCAPED_TEXT))
        for token in tokens
        for field in ("descr", "tags")
    ]


# ``(pad `` was fixed by #6740; its cases pass on main too and guard the
# shared helper that now backs ``_extract_pad_blocks``.
_LOADER_CASES = _cases("footprint", "module", "pad")
_ZONE_CASES = _cases("zone")


def _board(field: str, text: str, *, zone: bool = False) -> str:
    """Two footprints; J1 carries *text* in *field*, before its pads."""
    zone_block = (
        """
  (zone (net 2) (net_name "GND") (layer "F.Cu") (uuid "z-gnd")
    (hatch edge 0.5)
    (connect_pads (clearance 0.5))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon (pts (xy 100 100) (xy 160 100) (xy 160 150) (xy 100 150)))
  )"""
        if zone
        else ""
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 160 150) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Test:Part"
    (layer "F.Cu")
    (at 130 120)
    ({field} "{text}")
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 1 "VCC"))
    (pad "2" thru_hole circle (at 2.54 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
  )
  (footprint "Test:Other"
    (layer "F.Cu")
    (at 140 130)
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 1 "VCC"))
    (pad "2" thru_hole circle (at 2.54 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
  ){zone_block}
)
"""


_EXPECTED_PADS = {("J1", "1"), ("J1", "2"), ("J2", "1"), ("J2", "2")}
_EXPECTED_XY = [(130.0, 120.0), (132.54, 120.0), (140.0, 130.0), (142.54, 130.0)]


@pytest.mark.parametrize(("token", "field", "text"), _LOADER_CASES)
def test_load_pcb_for_routing_ignores_tokens_in_strings(tmp_path, token, field, text):
    board = tmp_path / "board.kicad_pcb"
    board.write_text(_board(field, text))

    router, _net_map = load_pcb_for_routing(str(board), validate_drc=False)

    assert set(router.pads) == _EXPECTED_PADS


@pytest.mark.parametrize(("token", "field", "text"), _LOADER_CASES)
def test_load_pads_for_analysis_ignores_tokens_in_strings(token, field, text):
    pads = load_pads_for_analysis(_board(field, text))

    assert {(p.ref, p.pin) for p in pads} == _EXPECTED_PADS
    assert sorted((round(p.x, 3), round(p.y, 3)) for p in pads) == _EXPECTED_XY


@pytest.mark.parametrize(("token", "field", "text"), _LOADER_CASES)
def test_extract_pad_positions_ignores_tokens_in_strings(token, field, text):
    positions = extract_pad_positions(_board(field, text))

    assert sorted((round(p.x, 3), round(p.y, 3)) for p in positions) == _EXPECTED_XY


@pytest.mark.parametrize(("token", "field", "text"), _ZONE_CASES)
def test_remove_zones_for_nets_ignores_tokens_in_strings(tmp_path, token, field, text):
    from kicad_tools.router.auto_pour import _remove_zones_for_nets

    board = tmp_path / "board.kicad_pcb"
    board.write_text(_board(field, text, zone=True))

    _remove_zones_for_nets(board, {"GND"})

    result = board.read_text()
    assert '(uuid "z-gnd")' not in result
    # Only the zone went: both footprints (and the string) are intact.
    assert result.count('(footprint "Test:') == 2
    assert f'({field} "{text}")' in result
    parse_string(result)


@pytest.mark.parametrize(("token", "field", "text"), _ZONE_CASES)
def test_zone_presence_check_ignores_tokens_in_strings(tmp_path, token, field, text):
    from kicad_tools.export.gerber import _pcb_has_unfilled_zones

    board = tmp_path / "board.kicad_pcb"
    board.write_text(_board(field, text))
    assert _pcb_has_unfilled_zones(board) is False

    board.write_text(_board(field, text, zone=True))
    assert _pcb_has_unfilled_zones(board) is True


def test_verify_output_connectivity_ignores_copper_spelled_in_a_string():
    """A ``(segment ...)`` written out inside a descr carries no current."""
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad

    phantom = "(segment (start 130 120) (end 140 130) (width 0.25) (layer F.Cu) (net 1))"
    content = _board("descr", phantom)
    pads = [
        Pad(x=130, y=120, width=1.7, height=1.7, net=1, net_name="VCC", layer=Layer.F_CU,
            ref="J1", pin="1", through_hole=True),
        Pad(x=140, y=130, width=1.7, height=1.7, net=1, net_name="VCC", layer=Layer.F_CU,
            ref="J2", pin="1", through_hole=True),
    ]  # fmt: skip

    report = verify_output_connectivity(content, {1: pads}, {1: "VCC"})

    assert report[1]["connected"] is False


def test_placement_fixer_moves_the_footprint_not_its_descr_text():
    """A descr ending in ``(footprint `` used to start a bogus block.

    The old ``\\(footprint\\s+"[^"]+"`` scan read the descr's closing quote
    and the tags' opening quote as a library id, so J1's reference landed in
    the bogus block and the fixer moved J1's *reference text* instead.
    """
    from kicad_tools.placement.conflict import PlacementFix, Point
    from kicad_tools.placement.fixer import PlacementFixer

    content = _board("descr", "placeholder").replace(
        '(descr "placeholder")', '(descr "fits (footprint ") (tags "Test:Other")'
    )
    fix = PlacementFix(conflict=None, component="J1", move_vector=Point(5.0, 0.0))  # type: ignore[arg-type]

    patched = PlacementFixer()._apply_fix_to_content(content, fix)

    assert "(at 135.0000 120.0000)" in patched
    assert '(fp_text reference "J1" (at 0 -3)' in patched


def test_footprint_library_ignores_pads_in_descr(tmp_path):
    """Same shape for ``.kicad_mod`` pads: ``(pad `` ends a descr string."""
    from kicad_tools.pcb.footprints import FootprintLibrary

    mod = tmp_path / "Part.kicad_mod"
    mod.write_text(
        '(footprint "Part"\n'
        '  (descr "see (pad ") (tags " smd rect (at 5 5)")\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n'
        ")\n"
    )

    pads = FootprintLibrary(library_paths=[])._parse_footprint_file(mod)

    assert pads == {"1": (0.0, 0.0)}


def test_legacy_module_board_loads(tmp_path):
    """The span-based split covers the pre-KiCad-6 ``(module`` spelling."""
    text = _board("descr", "plain").replace("(footprint ", "(module ")
    board = tmp_path / "legacy.kicad_pcb"
    board.write_text(text)

    router, _ = load_pcb_for_routing(str(board), validate_drc=False)

    assert set(router.pads) == _EXPECTED_PADS


def test_board_text_round_trip_is_unchanged(tmp_path):
    board = Path(tmp_path / "b.kicad_pcb")
    board.write_text(_board("descr", "plain", zone=True))
    before = board.read_text()

    load_pcb_for_routing(str(board), validate_drc=False)

    assert board.read_text() == before
