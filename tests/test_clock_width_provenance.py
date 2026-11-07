"""Width provenance for impedance-sized nets (Issue #8564).

A 0.2 mm ``Clock`` class net matching the validator default ``.*CLK$``
is silently widened to the 50 ohm solved width on a 4-layer board.  The
router must say so (log line + decision rationale) without changing the
routed widths.
"""

from __future__ import annotations

import logging

from kicad_tools.physics import Stackup
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Route
from kicad_tools.router.rules import DesignRules, NetClassRouting

CLOCK = NetClassRouting(name="Clock", trace_width=0.2, clearance=0.25)


def _make(stackup: Stackup, layer_stack: LayerStack, net_class_map) -> Autorouter:
    ar = Autorouter(
        width=20.0,
        height=20.0,
        rules=DesignRules(manufacturer="jlcpcb"),
        stackup=stackup,
        layer_stack=layer_stack,
        net_class_map=net_class_map,
        record_decisions=True,
    )
    for nid, name in ((1, "DAC_CLK"), (2, "Net-(Y2-OUT)")):
        ar.nets[nid] = [("U1", str(nid))]
        ar.net_names[nid] = name
    return ar


def _make_4l(net_class_map) -> Autorouter:
    return _make(Stackup.jlcpcb_4layer(), LayerStack.four_layer_sig_gnd_pwr_sig(), net_class_map)


def _rationale(ar: Autorouter, nid: int) -> str:
    ar._record_routing_decision(nid, [Route(net=nid, net_name=ar.net_names[nid])])
    return ar.get_decision_store()._decisions[-1].rationale


def test_synthesized_target_is_logged_and_in_rationale(caplog):
    ar = _make_4l({"DAC_CLK": CLOCK, "Net-(Y2-OUT)": CLOCK})
    with caplog.at_level(logging.WARNING, logger="kicad_tools.router.core"):
        ar._prepare_routing()

    width = ar.net_class_map["DAC_CLK"].trace_width
    assert width != 0.2  # the widening itself is unchanged behaviour

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    clk = [m for m in msgs if "DAC_CLK" in m]
    assert len(clk) == 1
    assert "50" in clk[0] and "synthesized" in clk[0] and ".*CLK$" in clk[0]
    assert f"{width:g}mm" in clk[0]
    assert not any("Net-(Y2-OUT)" in m for m in msgs)

    assert "synthesized from validator default pattern" in _rationale(ar, 1)
    other = _rationale(ar, 2)
    assert "impedance" not in other
    assert ar.net_class_map["Net-(Y2-OUT)"].trace_width == 0.2


def test_declared_target_is_reported_as_declared(caplog):
    declared = NetClassRouting(
        name="Clock", trace_width=0.2, clearance=0.25, target_single_impedance=50.0
    )
    ar = _make_4l({"DAC_CLK": declared, "Net-(Y2-OUT)": CLOCK})
    ar.net_class_map["DAC_CLK"] = declared
    with caplog.at_level(logging.WARNING, logger="kicad_tools.router.core"):
        ar._prepare_routing()
    rationale = _rationale(ar, 1)
    assert "declared" in rationale and "synthesized" not in rationale


def test_two_layer_board_says_nothing(caplog):
    ar = _make(
        Stackup.default_2layer(),
        LayerStack.two_layer(),
        {"DAC_CLK": CLOCK, "Net-(Y2-OUT)": CLOCK},
    )
    with caplog.at_level(logging.WARNING, logger="kicad_tools.router.core"):
        ar._prepare_routing()
    assert ar.net_class_map["DAC_CLK"].trace_width == 0.2
    assert "impedance" not in _rationale(ar, 1)
    assert not [r for r in caplog.records if "impedance sizing" in r.getMessage()]


_CLI_PCB = """(kicad_pcb (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
    (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "DAC_CLK")
  (footprint "R" (layer "F.Cu") (at 105 105)
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at -1 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DAC_CLK")))
  (footprint "R" (layer "F.Cu") (at 115 105)
    (property "Reference" "R2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 1 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DAC_CLK")))
  (gr_rect (start 100 100) (end 120 110) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
"""


def test_kct_route_persists_width_reason_for_explain_route(tmp_path, capsys):
    """AC2 through the CLI: route, then ``explain-route`` states the reason."""
    from kicad_tools.cli.route_cmd import main as route_main
    from kicad_tools.explain.rationale import explain_route
    from kicad_tools.schema.pcb import PCB

    pcb = tmp_path / "in.kicad_pcb"
    pcb.write_text(_CLI_PCB)
    out = tmp_path / "out.kicad_pcb"
    rc = route_main(
        [str(pcb), "-o", str(out), "--layers", "4", "--manufacturer", "jlcpcb", "--skip-drc"]
    )
    assert rc == 0, capsys.readouterr()
    assert out.with_suffix(".decisions.json").exists()
    text = explain_route("DAC_CLK", PCB.load(str(out))).rationale
    assert "set by impedance sizing" in text, text
