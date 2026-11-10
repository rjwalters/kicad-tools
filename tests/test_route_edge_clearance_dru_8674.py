"""``--edge-clearance`` reaches the ``.kicad_dru`` as a track/via rule (Issue #8674).

A board-wide ``edge_clearance`` rule would also bind pads and pours, so a board
with an edge connector inside the route's value would fail its own DRC.  The
rule is therefore scoped to tracks and vias.  Synthetic board, generic names.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru

RULE = "Route Edge Clearance"


def _rules():
    return get_profile("jlcpcb-tier1").get_design_rules(layers=2, copper_oz=1.0)


def test_rule_emitted_scoped_to_tracks_and_vias():
    dru = generate_dru(_rules(), "x", route_edge_clearance_mm=1.6)
    assert RULE in dru
    block = dru[dru.index(RULE) :]
    assert "A.Type == 'track' || A.Type == 'via'" in block.split("(rule", 1)[0]
    assert "(constraint edge_clearance (min 1.6000mm))" in block
    # Emitted after the board-wide rule: KiCad lets the last matching rule win.
    assert dru.index("Copper to Edge") < dru.index(RULE)


def test_no_rule_without_flag_or_below_floor():
    assert RULE not in generate_dru(_rules(), "x")
    assert RULE not in generate_dru(_rules(), "x", route_edge_clearance_mm=0.0)


def test_sidecar_writer_threads_edge_clearance(tmp_path):
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board())
    _write_drc_constraint_sidecars(pcb, "jlcpcb-tier1", 2, quiet=True, edge_clearance=1.6)
    assert RULE in pcb.with_suffix(".kicad_dru").read_text()
    _write_drc_constraint_sidecars(pcb, "jlcpcb-tier1", 2, quiet=True)
    assert RULE not in pcb.with_suffix(".kicad_dru").read_text()


def _board(track_y: float = 100.9) -> str:
    # 20 x 20 mm outline at (100,100); a track 0.9 mm from the top edge and an
    # edge-connector pad 0.9 mm from the bottom edge, on different nets.
    return f"""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "/A") (net 2 "/B")
  (gr_rect (start 100 100) (end 120 120) (layer "Edge.Cuts") (width 0.1) (fill none))
  (segment (start 105 {track_y}) (end 115 {track_y}) (width 0.25) (layer "F.Cu") (net 1))
  (footprint "Test:P" (layer "F.Cu") (at 110 119.1)
    (property "Reference" "J1" (at 0 -3 0) (layer "F.SilkS"))
    (property "Value" "x" (at 0 3 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 1.0 0.6) (layers "F.Cu" "F.Mask") (net 2 "/B")))
)
"""


def _drc_types(pcb: Path) -> list[str]:
    cli = shutil.which("kicad-cli")
    out = pcb.with_suffix(".json")
    subprocess.run(
        [cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    import json

    data = json.loads(out.read_text())
    return [
        v["type"] + ":" + ",".join(i.get("description", "") for i in v.get("items", []))
        for v in data.get("violations", [])
        if v["type"] == "copper_edge_clearance"
    ]


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_kicad_cli_flags_track_not_pad():
    from kicad_tools.cli.route_cmd import _write_drc_constraint_sidecars

    with tempfile.TemporaryDirectory() as d:
        pcb = Path(d) / "b.kicad_pcb"
        pcb.write_text(_board())
        # Wide-open floor first: nothing is reported at 0.9 mm.
        _write_drc_constraint_sidecars(pcb, "jlcpcb-tier1", 2, quiet=True)
        base = _drc_types(pcb)
        _write_drc_constraint_sidecars(pcb, "jlcpcb-tier1", 2, quiet=True, edge_clearance=1.6)
        scoped = _drc_types(pcb)
    assert not any("Pad" in v for v in base + scoped), (base, scoped)
    assert not any("Track" in v for v in base), base
    assert any("Track" in v for v in scoped), scoped
