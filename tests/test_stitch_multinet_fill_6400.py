"""Issue #6400: multi-net ``run_stitch`` must gate each via on its OWN net's fill.

Board 03 (#6388 part B): with ``nets=["VBUS", "VCC"]`` and
``only_pads={"C1.1"}``, stitch placed C1.1's VCC via inside the VBUS
auto-pour rectangle on In2.Cu.  The same-net fill gate accepted the site
because VBUS's fill was in the "same-net" pool (every target net's fill),
and VBUS's fill was not an obstacle because the other-net pool excluded
every target net.  Stitching ``VCC`` alone correctly skipped the pad.

The fixture mirrors that split plane: In2.Cu carries a VCC fill on the right
(x >= 125) and a VBUS fill rectangle on the left that swallows the VCC pad
C1.1.  A via for C1.1 within the stitch offset can only land on VBUS fill.

The work list must hold a pad of EACH net (board 03's repro was
``only_pads={"C1.1", "U1.7"}``): ``run_stitch`` derives its target-net set
from the pads it will process, so ``only_pads={"C1.1"}`` alone leaves VBUS a
foreign net and never hit the bug.  C2.1 plays U1.7 (a VBUS pad).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.stitch_cmd import point_in_polygon, run_stitch

VBUS_FILL = [(104.0, 104.0), (124.0, 104.0), (124.0, 136.0), (104.0, 136.0)]

SPLIT_PLANE_PCB = """(kicad_pcb
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
  (net 1 "VBUS")
  (net 2 "VCC")
  (zone (net 2) (net_name "VCC") (layer "In2.Cu") (uuid "zone-vcc")
    (name "VCC_plane")
    (priority 0)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes)
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140)))
    (filled_polygon (layer "In2.Cu") (pts (xy 125 100) (xy 140 100) (xy 140 140) (xy 125 140)))
  )
  (zone (net 1) (net_name "VBUS") (layer "In2.Cu") (uuid "zone-vbus")
    (name "VBUS_pour")
    (priority 1)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes)
    (polygon (pts (xy 104 104) (xy 124 104) (xy 124 136) (xy 104 136)))
    (filled_polygon (layer "In2.Cu") (pts (xy 104 104) (xy 124 104) (xy 124 136) (xy 104 136)))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c1")
    (at 115 120)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c1"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c2")
    (at 110 110)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c2"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VBUS"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000c3")
    (at 132 120)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-c3"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
  )
)
"""


def _stitch(tmp_path: Path, nets: list[str], only: set[str] | None):
    pcb = tmp_path / "split_plane.kicad_pcb"
    pcb.write_text(SPLIT_PLANE_PCB)
    return run_stitch(
        pcb_path=pcb,
        net_names=nets,
        via_size=0.45,
        drill=0.2,
        clearance=0.2,
        offset=0.5,
        trace_width=0.2,
        target_layer="In2.Cu",
        dry_run=True,
        only_pads=frozenset(only) if only is not None else None,
    )


def _via_on_vbus_fill(v) -> bool:
    return point_in_polygon(v.via_x, v.via_y, VBUS_FILL)


def test_multi_net_stitch_never_lands_vcc_via_on_vbus_fill(tmp_path: Path) -> None:
    """``nets=[VBUS, VCC], only_pads={C1.1, C2.1}``: C1.1's via must not land on VBUS."""
    result = _stitch(tmp_path, ["VBUS", "VCC"], {"C1.1", "C2.1"})

    bad = [v for v in result.vias_added if v.pad.net_name == "VCC" and _via_on_vbus_fill(v)]
    assert bad == [], [(v.pad.reference, v.via_x, v.via_y) for v in bad]
    assert "C1" not in {v.pad.reference for v in result.vias_added}
    assert "C1" in {p.reference for p, _ in result.pads_skipped}


def test_multi_net_matches_single_net_for_each_pad(tmp_path: Path) -> None:
    """A multi-net call gates each pad exactly like a single-net call would."""
    single = _stitch(tmp_path, ["VCC"], {"C1.1"})
    multi = _stitch(tmp_path, ["VBUS", "VCC"], {"C1.1", "C2.1"})

    assert [v.pad.reference for v in single.vias_added] == []
    assert "C1" not in {v.pad.reference for v in multi.vias_added}
    single_reasons = {p.reference: r for p, r in single.pads_skipped}
    multi_reasons = {p.reference: r for p, r in multi.pads_skipped}
    assert set(single_reasons) == {"C1"}
    assert multi_reasons.get("C1") == single_reasons["C1"]


def test_multi_net_still_stitches_pads_on_their_own_fill(tmp_path: Path) -> None:
    """No over-rejection: each net's pad over its own fill still gets a via there."""
    result = _stitch(tmp_path, ["VBUS", "VCC"], None)

    by_ref = {v.pad.reference: v for v in result.vias_added}
    # VBUS pad C2 sits on VBUS fill; its via lands on VBUS fill.
    assert "C2" in by_ref
    assert _via_on_vbus_fill(by_ref["C2"])
    # VCC pad C3 sits on VCC fill (x >= 125); its via lands there.
    assert "C3" in by_ref
    assert by_ref["C3"].via_x >= 125.0
    # VCC pad C1 (inside VBUS) gets no via.
    assert "C1" not in by_ref
