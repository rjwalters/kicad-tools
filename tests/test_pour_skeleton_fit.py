"""Pour-skeleton fit experiment (issue #9002): prediction-level tests.

``scripts/pour_skeleton_fit.py`` predicts, per zoned net on a routed board,
whether a minimal pad-joining skeleton still fits around the placed
foreign-net copper.  These tests exercise the prediction on hand-authored
min Boards (no kicad-cli needed):

* a foreign trace walling a pour in two is predicted unfit, with the
  blocking segment named;
* the same wall with a gap (and an unrouted board) fit;
* a copper keepout sealing the gap is predicted unfit;
* a single-pad zone needs no skeleton (fits);
* a multi-layer (``F&B.Cu``) zone is analysed per layer;
* a zone-layer with no same-net anchor is predicted unfit (every island of
  such a fill is isolated copper by definition -- the ``orphan_island``
  semantics validated against kicad-cli in ``test_zone_fill.py``);
* ``main()`` writes the JSON summary.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "pour_skeleton_fit.py"

_HEADER = """(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VOUT")
  (gr_rect (start 0 0) (end 40 25) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
"""

_ZONE_FC = """  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2)) (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 2 2) (xy 38 2) (xy 38 23) (xy 2 23))))
"""


def _tht(ref: str, x: float, y: float, pad: str, net_num: int, net_name: str) -> str:
    return (
        f'  (footprint "Test:TH_{ref}" (layer "F.Cu") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "{pad}" thru_hole circle (at 0 0) (size 2 2) (drill 0.9) '
        f'(layers "*.Cu" "*.Mask") (net {net_num} "{net_name}")))\n'
    )


def _smd(ref: str, x: float, y: float, pad: str, net_num: int, net_name: str, layer: str) -> str:
    return (
        f'  (footprint "Test:SM_{ref}" (layer "{layer}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "{pad}" smd rect (at 0 0) (size 2 2) (layers "{layer}") '
        f'(net {net_num} "{net_name}")))\n'
    )


def _segment(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.5) (layer "F.Cu") (net 2 "VOUT"))\n'
    )


def _keepout(pts: list[tuple[float, float]]) -> str:
    poly = " ".join(f"(xy {x} {y})" for x, y in pts)
    return (
        "  (zone (layer " + '"F.Cu"' + ") (keepout (tracks not_allowed) "
        "(vias not_allowed) (pads not_allowed) (copperpour not_allowed))\n"
        f"    (polygon (pts {poly})))\n"
    )


@pytest.fixture(scope="module")
def fit():
    """Import ``scripts/pour_skeleton_fit.py`` as a module."""
    spec = importlib.util.spec_from_file_location("pour_skeleton_fit", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pour_skeleton_fit"] = module
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(_HEADER + body + ")\n")
    return path


def test_trace_wall_cuts_pour_predicted_unfit(fit, tmp_path):
    """A foreign trace walling the pour in two leaves no fitting corridor."""
    path = _write(
        tmp_path,
        "cut.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND")
        + _tht("J2", 35, 12, "1", 1, "GND")
        + _segment(20, 2, 20, 23)
        + _ZONE_FC,
    )
    result = fit.analyse_board(path)
    assert result.predicted_unfit_nets == {"GND"}
    (layer_result,) = result.zone_layers
    assert layer_result.layer == "F.Cu"
    assert layer_result.anchors == 2
    assert not layer_result.fit
    assert layer_result.witnesses, "unfit verdict must name a witness edge"
    assert any("segment net 'VOUT'" in w.blocker for w in layer_result.witnesses)
    witness = layer_result.witnesses[0]
    assert {witness.a_label, witness.b_label} == {"J1.1", "J2.1"}


def test_gap_in_trace_wall_fits(fit, tmp_path):
    """The same wall with a gap leaves a corridor and the skeleton fits."""
    path = _write(
        tmp_path,
        "gap.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND")
        + _tht("J2", 35, 12, "1", 1, "GND")
        + _segment(20, 2, 20, 10)
        + _ZONE_FC,
    )
    result = fit.analyse_board(path)
    assert result.predicted_unfit_nets == set()
    (layer_result,) = result.zone_layers
    assert layer_result.fit


def test_unrouted_board_fits(fit, tmp_path):
    """A board with a zone and pads but no routed traces fits (edge case)."""
    path = _write(
        tmp_path,
        "unrouted.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND") + _tht("J2", 35, 12, "1", 1, "GND") + _ZONE_FC,
    )
    result = fit.analyse_board(path)
    assert result.predicted_unfit_nets == set()
    assert result.zone_layers[0].fit


def test_keepout_sealing_the_gap_is_unfit(fit, tmp_path):
    """A copper keepout covering the wall's gap blocks the detour (edge case)."""
    path = _write(
        tmp_path,
        "keepout.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND")
        + _tht("J2", 35, 12, "1", 1, "GND")
        + _segment(20, 2, 20, 10)
        + _keepout([(12, 8), (28, 8), (28, 23), (12, 23)])
        + _ZONE_FC,
    )
    result = fit.analyse_board(path)
    assert result.predicted_unfit_nets == {"GND"}
    (layer_result,) = result.zone_layers
    assert not layer_result.fit
    assert any(w.blocker == "copper keepout" for w in layer_result.witnesses)


def test_single_pad_zone_needs_no_skeleton(fit, tmp_path):
    """A zone with one pad connects trivially (edge case in the issue)."""
    path = _write(tmp_path, "single.kicad_pcb", _tht("J1", 5, 12, "1", 1, "GND") + _ZONE_FC)
    result = fit.analyse_board(path)
    (layer_result,) = result.zone_layers
    assert layer_result.anchors == 1
    assert layer_result.fit


def test_no_anchor_zone_layer_is_unfit(fit, tmp_path):
    """A zone-layer with no same-net anchor would refill as isolated copper."""
    path = _write(
        tmp_path,
        "orphan.kicad_pcb",
        _tht("J9", 30, 20, "1", 2, "VOUT") + _ZONE_FC,
    )
    result = fit.analyse_board(path)
    (layer_result,) = result.zone_layers
    assert layer_result.anchors == 0
    assert not layer_result.fit
    assert "no same-net anchor" in layer_result.reason


def test_multilayer_zone_analysed_per_layer(fit, tmp_path):
    """An ``F&B.Cu`` zone is analysed on both copper layers (edge case)."""
    zone_fb = _ZONE_FC.replace('(layer "F.Cu")', '(layers "F&B.Cu")')
    path = _write(
        tmp_path,
        "multi.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND") + _smd("U1", 35, 12, "1", 1, "GND", "B.Cu") + zone_fb,
    )
    result = fit.analyse_board(path)
    by_layer = {r.layer: r for r in result.zone_layers}
    assert set(by_layer) == {"F.Cu", "B.Cu"}
    assert by_layer["F.Cu"].anchors == 1  # the THT pad only
    assert by_layer["B.Cu"].anchors == 2  # THT pad + B.Cu SMD pad
    assert all(r.fit for r in by_layer.values())


def test_zone_connect_zero_pad_is_not_an_anchor(fit, tmp_path):
    """A pad with ``(zone_connect 0)`` is deliberately unconnected."""
    pad = (
        '  (footprint "Test:TH_J3" (layer "F.Cu") (at 35 12)\n'
        '    (property "Reference" "J3" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 0.9) '
        '(layers "*.Cu" "*.Mask") (net 1 "GND") (zone_connect 0)))\n'
    )
    path = _write(tmp_path, "zc0.kicad_pcb", _tht("J1", 5, 12, "1", 1, "GND") + pad + _ZONE_FC)
    result = fit.analyse_board(path)
    (layer_result,) = result.zone_layers
    assert layer_result.anchors == 1  # J3.1 excluded
    assert layer_result.fit


def test_main_writes_json_summary(fit, tmp_path, monkeypatch, capsys):
    """``main()`` runs the prediction over boards and writes the JSON file."""
    cut = _write(
        tmp_path,
        "cut.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND")
        + _tht("J2", 35, 12, "1", 1, "GND")
        + _segment(20, 2, 20, 23)
        + _ZONE_FC,
    )
    fits = _write(
        tmp_path,
        "fits.kicad_pcb",
        _tht("J1", 5, 12, "1", 1, "GND") + _tht("J2", 35, 12, "1", 1, "GND") + _ZONE_FC,
    )
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        sys, "argv", ["pour_skeleton_fit.py", str(cut), str(fits), "--json", str(out)]
    )
    assert fit.main() == 0
    payload = json.loads(out.read_text())
    by_board = {b["path"]: b for b in payload["boards"]}
    assert {r["net"] for r in by_board[str(cut)]["zone_layers"] if not r["fit"]} == {"GND"}
    assert all(r["fit"] for r in by_board[str(fits)]["zone_layers"])
    assert "predicted-unfit nets" in capsys.readouterr().out
