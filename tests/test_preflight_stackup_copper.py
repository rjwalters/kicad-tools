"""Stackup-vs-order copper weight preflight check (issue #7209)."""

from pathlib import Path

import pytest

from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.manufacturers import get_profile

_OUTLINE = """
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
"""


def _pcb(tmp_path: Path, outer: float | None, inner: float | None, four_layer=True) -> Path:
    layers = '(0 "F.Cu" signal)\n'
    if four_layer:
        layers += '(4 "In1.Cu" signal)\n(6 "In2.Cu" signal)\n'
    layers += '(31 "B.Cu" signal)\n(44 "Edge.Cuts" user)'
    stackup = ""
    if outer is not None:

        def cu(name: str, t: float) -> str:
            return f'(layer "{name}" (type "copper") (thickness {t}))'

        parts = [cu("F.Cu", outer)]
        if four_layer:
            parts += [cu("In1.Cu", inner), cu("In2.Cu", inner)]
        parts.append(cu("B.Cu", outer))
        stackup = "(setup (stackup " + " ".join(parts) + "))"
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        f'(kicad_pcb (version 20231014) (generator "pcbnew")\n'
        f'  (general (thickness 1.6))\n  (paper "A4")\n  (layers {layers})\n'
        f'  {stackup}\n  (net 0 "")\n{_OUTLINE})\n'
    )
    return path


def _run(pcb: Path):
    checker = PreflightChecker(
        pcb_path=pcb, manufacturer="jlcpcb", config=PreflightConfig(skip_drc=True, skip_erc=True)
    )
    return next(r for r in checker.run_all() if r.name == "stackup_copper")


def test_profile_offers_one_oz_inner_on_four_layers():
    options = get_profile("jlcpcb").copper_options(4)
    assert (1.0, 0.5) in options
    assert (1.0, 1.0) in options


def test_one_oz_inner_matches_the_one_oz_inner_option(tmp_path):
    r = _run(_pcb(tmp_path, 0.035, 0.035))
    assert r.status == "OK"
    assert "inner 1 oz" in r.message


def test_half_oz_inner_stackup_matches_default_profile(tmp_path):
    assert _run(_pcb(tmp_path, 0.035, 0.0175)).status == "OK"


def test_unorderable_inner_copper_fails_naming_both_values(tmp_path):
    r = _run(_pcb(tmp_path, 0.035, 0.105))  # 3 oz inner: not a JLCPCB option
    assert r.status == "FAIL"
    assert "inner 3 oz" in r.message
    assert "outer 1 oz / inner 0.5 oz" in r.details


def test_jlcpcb_finished_half_oz_inner_0152mm_matches(tmp_path):
    # JLCPCB's published finished 0.5 oz inner copper (jlcpcb.com/impedance).
    assert _run(_pcb(tmp_path, 0.035, 0.0152)).status == "OK"


def test_six_layer_one_oz_inner_is_offered():
    assert (1.0, 1.0) in get_profile("jlcpcb").copper_options(6)
    assert (1.0, 1.0) in get_profile("jlcpcb-tier1").copper_options(4)


def test_two_oz_outer_half_oz_inner_ok(tmp_path):
    assert _run(_pcb(tmp_path, 0.07, 0.0175)).status == "OK"


def test_two_layer_board_has_no_inner_to_compare(tmp_path):
    assert _run(_pcb(tmp_path, 0.035, None, four_layer=False)).status == "OK"


def test_no_declared_stackup_uses_profile_default(tmp_path):
    r = _run(_pcb(tmp_path, None, None))
    assert r.status == "OK"
    assert "profile default" in r.message


def test_board05_manifest_copper_equals_stackup():
    from kicad_tools.export.preflight import resolve_ordered_copper
    from kicad_tools.schema.pcb import PCB

    root = Path(__file__).resolve().parents[1]
    pcb_path = root / "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb"
    if not pcb_path.exists():
        pytest.skip("board 05 not present")
    info = resolve_ordered_copper(PCB.load(str(pcb_path)), "jlcpcb")
    assert info["status"] == "match"
    assert (info["ordered_outer_oz"], info["ordered_inner_oz"]) == (1.0, 1.0)
