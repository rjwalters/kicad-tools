"""Layer-aware pad contact in the router's connectivity checks (Issue #6371).

Board 03 (``usb_joystick``) shipped a run that printed "Nets routed 24/24" and
"All nets verified connected in written output" while ``kct check`` and
kicad-cli both found USB_D- pad J1.B7 stranded.  Both router-side checks
linked a pad to its nearest same-net segment endpoint *anywhere within 2 mm,
on any layer*; J1.B7 sits 1.59 mm from its routed sibling J1.A7, so it always
"connected".  These tests pin the contact model that replaced the heuristic:

* a pad is reached only by same-net copper that intersects its outline on a
  layer the pad occupies (PTH pads span every layer; vias bridge their span);
* ``verify_output_connectivity`` (written file) and
  ``validate_net_connectivity`` / ``get_statistics()["nets_routed"]``
  (in-memory routes) give the same verdict for the same geometry;
* ``kct route`` reconciles its summary and exit code with the post-save
  verdict.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import verify_output_connectivity
from kicad_tools.router.layers import Layer
from kicad_tools.router.observability import validate_net_connectivity
from kicad_tools.router.primitives import Pad, Route, Segment, Via

REPO = Path(__file__).resolve().parents[1]
BOARD03_DIR = REPO / "boards" / "03-usb-joystick" / "output"

# Board 03's J1 geometry: A7 and B7 are both USB_D-, 1.59 mm apart.
A7 = (146.075, 63.300)
B7 = (146.925, 61.950)
U2 = (140.000, 63.300)


def _pth(x: float, y: float, ref: str, pin: str, net: int = 1) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=0.7,
        height=0.7,
        net=net,
        net_name="USB_D-",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
        through_hole=True,
        drill=0.4,
        shape="circle",
    )


def _smd(x: float, y: float, ref: str, pin: str, net: int = 1, **kw) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=kw.pop("width", 0.6),
        height=kw.pop("height", 0.6),
        net=net,
        net_name="USB_D-",
        layer=kw.pop("layer", Layer.F_CU),
        ref=ref,
        pin=pin,
        **kw,
    )


def _seg(x1: float, y1: float, x2: float, y2: float, layer: Layer, net: int = 1) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net)


def _sexp(seg: Segment) -> str:
    return (
        f"(segment (start {seg.x1} {seg.y1}) (end {seg.x2} {seg.y2}) (width {seg.width}) "
        f'(layer "{seg.layer.kicad_name}") (net {seg.net}) (uuid "u"))'
    )


def _via_sexp(x: float, y: float, net: int = 1, *, kicad10: bool = False) -> str:
    if kicad10:
        # KiCad 10 nests TWO levels deep: (tenting (front yes) (back yes)).
        return (
            f"(via\n\t\t(at {x} {y})\n\t\t(size 0.6)\n\t\t(drill 0.3)\n"
            f'\t\t(layers "F.Cu" "B.Cu")\n\t\t(tenting\n\t\t\t(front yes)\n\t\t\t(back yes)\n\t\t)\n'
            f"\t\t(capping no)\n\t\t(covering\n\t\t\t(front no)\n\t\t\t(back no)\n\t\t)\n"
            f'\t\t(net {net})\n\t\t(uuid "u")\n\t)'
        )
    return (
        f'(via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) (uuid "u"))'
    )


def _pcb(*fragments: str) -> str:
    body = "\n  ".join(fragments)
    return f'(kicad_pcb (version 20241229)\n  (net 0 "")\n  (net 1 "USB_D-")\n  {body}\n)'


def _both_verdicts(pads: list[Pad], segments: list[Segment], vias: list[Via], via_sexp=()):
    """Run the written-file verifier and the in-memory check on one geometry."""
    written = verify_output_connectivity(_pcb(*(_sexp(s) for s in segments), *via_sexp), {1: pads})
    in_memory = validate_net_connectivity(
        [Route(net=1, net_name="USB_D-", segments=segments, vias=vias)], {1: pads}
    )
    return written[1], in_memory[1]


# ---------------------------------------------------------------------------
# The board 03 failure mode, synthetically: two same-footprint PTH lands
# 1.6 mm apart, one fed, the other reached only by a stub ending 0.5 mm short.
# ---------------------------------------------------------------------------


def _usb_pads() -> list[Pad]:
    return [_smd(*U2, "U2", "1"), _pth(*A7, "J1", "A7"), _pth(*B7, "J1", "B7")]


def _usb_copper(stub_reaches_b7: bool) -> list[Segment]:
    stub_end_y = B7[1] if stub_reaches_b7 else B7[1] + 0.5
    return [
        _seg(*U2, *A7, Layer.F_CU),
        # B.Cu stub out of A7 toward B7 (the shape board 03's router wrote).
        _seg(*A7, B7[0], stub_end_y, Layer.B_CU),
    ]


def test_pad_reached_only_by_a_stub_ending_short_is_stranded():
    w, m = _both_verdicts(_usb_pads(), _usb_copper(stub_reaches_b7=False), [])
    assert w["connected"] is False
    assert w["disconnected_pads"] == ["J1:B7"]
    assert w["connected_pads"] == 2
    # The in-memory check that drives ``nets_routed`` agrees.
    assert m["connected"] is False
    assert m["stranded_pads"] == ["J1.B7"]


def test_stub_that_reaches_the_pad_connects_it():
    """Positive control: same geometry with the stub landing on B7."""
    w, m = _both_verdicts(_usb_pads(), _usb_copper(stub_reaches_b7=True), [])
    assert w["connected"] is True and w["disconnected_pads"] == []
    assert m["connected"] is True and m["stranded_pads"] == []


def test_get_statistics_nets_routed_uses_contact():
    """``get_statistics()["nets_routed"]`` -> "Nets routed"/"Nets connected"."""
    for reaches, expected in ((False, 0), (True, 1)):
        ar = Autorouter(width=60.0, height=60.0)
        pads = _usb_pads()
        ar.pads = {(p.ref, p.pin): p for p in pads}
        ar.nets = {1: [(p.ref, p.pin) for p in pads]}
        ar.net_names = {1: "USB_D-"}
        ar.routes = [Route(net=1, net_name="USB_D-", segments=_usb_copper(reaches))]
        assert ar.get_statistics(nets_to_route_ids={1})["nets_routed"] == expected


# ---------------------------------------------------------------------------
# Wrong layer: an endpoint dead-centre on an SMD pad but on the other side of
# the board, with no via, does not connect it.
# ---------------------------------------------------------------------------


def test_segment_on_the_wrong_layer_does_not_connect_an_smd_pad():
    pads = [_smd(0.0, 0.0, "U1", "1"), _smd(5.0, 0.0, "U2", "1")]
    segs = [_seg(0.0, 0.0, 5.0, 0.0, Layer.B_CU)]
    w, m = _both_verdicts(pads, segs, [])
    assert w["connected"] is False and w["connected_pads"] == 1
    assert m["connected"] is False and m["connected_pads"] == 1


def test_via_bridges_the_layer_change():
    # F.Cu pad -> F.Cu trace -> via -> B.Cu trace -> B.Cu pad.
    pads = [_smd(0.0, 0.0, "U1", "1"), _smd(5.0, 0.0, "U2", "1", layer=Layer.B_CU)]
    segs = [_seg(0.0, 0.0, 2.5, 0.0, Layer.F_CU), _seg(2.5, 0.0, 5.0, 0.0, Layer.B_CU)]
    # ... but not without the via: coincident endpoints on different layers
    # are not copper contact (the old endpoint snapping joined them).
    w, m = _both_verdicts(pads, segs, [])
    assert w["connected"] is False and m["connected"] is False

    via = Via(x=2.5, y=0.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
    for kicad10 in (False, True):
        w, m = _both_verdicts(pads, segs, [via], via_sexp=[_via_sexp(2.5, 0.0, kicad10=kicad10)])
        assert w["connected"] is True, f"via not parsed (kicad10={kicad10})"
        assert m["connected"] is True


def test_off_centre_landing_on_an_elongated_roundrect_connects():
    """No false negatives from the roundrect contact shape.

    Board 03 U1.3 is a 0.55 x 1.475 mm roundrect fed by a trace ending
    0.51 mm from its centre -- inside the copper, outside the inscribed
    circle ``_pad_outline(contact=True)`` uses for roundrects.
    """
    pads = [
        _smd(0.0, 0.0, "U1", "3", width=0.55, height=1.475, shape="roundrect"),
        _smd(0.0, -4.0, "R3", "2"),
    ]
    segs = [_seg(0.0, -0.5125, 0.0, -4.0, Layer.F_CU)]
    w, m = _both_verdicts(pads, segs, [])
    assert w["connected"] is True
    assert m["connected"] is True


# ---------------------------------------------------------------------------
# Real board 03 output.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def board03():
    from kicad_tools.router.io import load_pcb_for_routing

    router, net_map = load_pcb_for_routing(str(BOARD03_DIR / "usb_joystick.kicad_pcb"))
    nid = net_map["USB_D-"]
    pads = {nid: [router.pads[k] for k in router.nets[nid]]}
    names = {v: k for k, v in net_map.items()}
    return (BOARD03_DIR / "usb_joystick_routed.kicad_pcb").read_text(), pads, names, nid


def test_committed_board03_usb_dm_is_connected(board03):
    """Guards against false negatives: the committed board connects B7."""
    text, pads, names, nid = board03
    assert verify_output_connectivity(text, pads, names)[nid]["connected"] is True


def test_board03_with_b7_stub_cut_reports_b7_stranded(board03):
    """The curator's reproduction: delete only B7's first B.Cu stub segment."""
    text, pads, names, nid = board03
    cut = re.sub(
        r"\(segment\s*\(start 146\.925 61\.95\)\s*\(end 146\.925 62\.45\)"
        r"(?:[^()]*\([^()]*\))*[^()]*\)",
        "",
        text,
        count=1,
    )
    assert cut != text, "fixture drifted: B7 stub segment not found"
    report = verify_output_connectivity(cut, pads, names)[nid]
    assert report["connected"] is False
    assert report["disconnected_pads"] == ["J1:B7"]


# ---------------------------------------------------------------------------
# ``kct route`` end to end: a post-save open the router counted as connected
# must not print "All nets verified", must reconcile "Nets connected", and
# must exit non-zero without --strict.
# ---------------------------------------------------------------------------

TWO_RESISTOR_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 100 100) (end 130 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000010") (at 108 110)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (uuid "00000000-0000-0000-0000-000000000020") (at 120 110)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "SIG"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )
)
"""


@pytest.mark.parametrize("drop_written_copper", [False, True])
def test_route_exit_code_follows_post_save_verdict(
    tmp_path: Path, monkeypatch, capsys, drop_written_copper: bool
):
    from kicad_tools.cli.route_cmd import main as route_main
    from kicad_tools.router import io as router_io

    board = tmp_path / "two_res.kicad_pcb"
    board.write_text(TWO_RESISTOR_BOARD)
    out = tmp_path / "two_res_routed.kicad_pcb"

    real_verify = router_io.verify_output_connectivity

    def lossy_verify(pcb_content, *args, **kwargs):
        # Simulate a write step that lost the copper the router counted.
        if drop_written_copper:
            pcb_content = re.sub(r"\(segment\b(?:[^()]*\([^()]*\))*[^()]*\)", "", pcb_content)
        return real_verify(pcb_content, *args, **kwargs)

    monkeypatch.setattr(router_io, "verify_output_connectivity", lossy_verify)
    rc = route_main(
        [
            str(board),
            "-o",
            str(out),
            "--skip-drc",
            "--no-cache",
            "--no-auto-layers",
            "--layers",
            "2",
        ]
    )
    text = capsys.readouterr().out

    if not drop_written_copper:
        assert rc == 0, text
        assert "All nets verified connected in written output" in text
        return
    assert rc == 6, text
    assert "All nets verified connected" not in text
    assert "SUCCESS" not in text
    assert "Nets connected:  0 (verified in written output)" in text
    assert "SIG" in text
