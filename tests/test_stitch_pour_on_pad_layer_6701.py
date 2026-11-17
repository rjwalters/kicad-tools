"""Issue #6701: no stitch via for a net whose only pour is on the pad's layer.

On board 07 every ``+1V8`` pad got an F.Cu-B.Cu stitch via although +1V8 has
copper on F.Cu only: the stackup fallback (#2044) targeted In2.Cu (another
net's plane), the same-net fill gate went no-op, and kicad-cli flagged each
via ``via_dangling``.  ``run_stitch`` now skips such pads with an explicit
reason, while nets with a pour on another layer (or no pour at all) keep the
previous behaviour.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import run_stitch

SKIP_REASON = "no same-net pour on any layer other than the pad's layer"


def _zone(net: int, name: str, layer: str) -> str:
    return f"""  (zone (net {net}) (net_name "{name}") (layer "{layer}") (uuid "zone-{net}-{layer}")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
"""


def _pcb(zones: str) -> str:
    """4-layer board: two 0402 caps on F.Cu, each with a PWR pad and a GND pad."""
    footprints = ""
    for i, x in enumerate((110, 125), start=1):
        footprints += f"""  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-00000000010{i}")
    (at {x} 115)
    (property "Reference" "C{i}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c{i}"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "PWR"))
  )
"""
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "PWR")
{footprints}{zones})
"""


def _write(tmp_path: Path, zones: str) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_pcb(zones))
    return pcb


def _vias(result, net: str):
    return [v for v in result.vias_added if v.pad.net_name == net]


def _skips(result, net: str) -> list[str]:
    return [reason for pad, reason in result.pads_skipped if pad.net_name == net]


def test_pour_only_on_pad_layer_places_no_via(tmp_path: Path) -> None:
    pcb = _write(tmp_path, _zone(2, "PWR", "F.Cu"))
    result = run_stitch(pcb_path=pcb, net_names=["PWR"], target_layer=None, dry_run=True)

    assert result.pads_found == 2
    assert _vias(result, "PWR") == []
    skips = _skips(result, "PWR")
    assert len(skips) == 2
    for reason in skips:
        assert SKIP_REASON in reason
        assert "(F.Cu)" in reason


def test_pour_on_other_layer_still_stitches(tmp_path: Path) -> None:
    """Regression: a B.Cu pour with F.Cu pads still gets F.Cu-B.Cu vias."""
    pcb = _write(tmp_path, _zone(2, "PWR", "B.Cu"))
    result = run_stitch(pcb_path=pcb, net_names=["PWR"], target_layer=None, dry_run=True)

    vias = _vias(result, "PWR")
    assert len(vias) == 2
    for via in vias:
        assert via.layers == ("F.Cu", "B.Cu")
    assert not any(SKIP_REASON in r for r in _skips(result, "PWR"))


def test_pour_on_pad_layer_and_other_layer_still_stitches(tmp_path: Path) -> None:
    """A pour on the pad's layer does not suppress a via to another-layer pour."""
    pcb = _write(tmp_path, _zone(2, "PWR", "F.Cu") + _zone(2, "PWR", "In1.Cu"))
    result = run_stitch(pcb_path=pcb, net_names=["PWR"], target_layer=None, dry_run=True)

    assert len(_vias(result, "PWR")) == 2
    assert not any(SKIP_REASON in r for r in _skips(result, "PWR"))


def test_net_without_zones_keeps_stackup_inference(tmp_path: Path) -> None:
    """#2044 behaviour: a 4-layer net with no zones still gets inferred vias."""
    pcb = _write(tmp_path, "")
    result = run_stitch(pcb_path=pcb, net_names=["PWR"], target_layer=None, dry_run=True)

    assert "PWR" in result.stackup_inferred_nets
    assert len(_vias(result, "PWR")) == 2
    assert not any(SKIP_REASON in r for r in _skips(result, "PWR"))


def test_explicit_target_layer_is_not_overridden(tmp_path: Path) -> None:
    """An explicit --target-layer is the user's call; no #6701 skip."""
    pcb = _write(tmp_path, _zone(2, "PWR", "F.Cu"))
    result = run_stitch(pcb_path=pcb, net_names=["PWR"], target_layer="B.Cu", dry_run=True)

    assert len(_vias(result, "PWR")) == 2
    assert not any(SKIP_REASON in r for r in _skips(result, "PWR"))
