"""Board 03: the committed plan and a fresh route agree on USB width (Issue #7256).

The fresh arm C route lays each pair at its net-class ``trace_width``: the
router's impedance sizing is dormant on the CLI path once the targets are
declared (#7294, #2967). So the net-class map is the router's width input.
These tests pin three things. The committed copper uses exactly that width.
The width is inside the declared 90 ohm band on the layer the pair runs on.
And the 0.20 mm default would not be in band on the F.Cu MCU launch.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from kicad_tools.physics import Stackup
from kicad_tools.physics.coupled_lines import CoupledLines
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parent.parent / "boards" / "03-usb-joystick"
ROUTED = BOARD / "output" / "usb_joystick_routed.kicad_pcb"
NET_CLASS_MAP = BOARD / "output" / "net_class_map.json"
PAIRS = {"USB_MCU_D": "F.Cu", "USB_D": "In2.Cu"}


@pytest.fixture(scope="module")
def pcb() -> PCB:
    return PCB.load(str(ROUTED))


@pytest.fixture(scope="module")
def classes() -> dict:
    return json.loads(NET_CLASS_MAP.read_text())


def _widths(pcb: PCB) -> dict[tuple[str, str], set[float]]:
    out: dict[tuple[str, str], set[float]] = defaultdict(set)
    for seg in pcb.segments:
        out[(pcb.get_net(seg.net_number).name, seg.layer)].add(round(seg.width, 4))
    return out


@pytest.mark.parametrize("prefix", sorted(PAIRS))
def test_committed_pair_width_equals_net_class_width(pcb, classes, prefix):
    widths = _widths(pcb)
    layer = PAIRS[prefix]
    for net in (prefix + "+", prefix + "-"):
        assert widths[(net, layer)] == {classes[net]["trace_width"]}, (net, layer)


def _zdiff(pcb: PCB, layer: str, width: float, gap: float) -> float:
    stackup = Stackup.from_pcb(pcb)
    cl = CoupledLines(stackup)
    calc = (
        cl.edge_coupled_microstrip if stackup.is_outer_layer(layer) else cl.edge_coupled_stripline
    )
    return calc(width, gap, layer).zdiff


@pytest.mark.parametrize("prefix", sorted(PAIRS))
def test_net_class_width_is_in_the_declared_band(pcb, classes, prefix):
    nc = classes[prefix + "+"]
    target = nc["target_diff_impedance"]
    tol = nc["impedance_tolerance_percent"] / 100.0
    z = _zdiff(pcb, PAIRS[prefix], nc["trace_width"], nc["intra_pair_clearance"])
    assert target * (1 - tol) <= z <= target * (1 + tol), (prefix, z)


def test_default_width_would_be_out_of_band_on_the_mcu_launch(pcb, classes):
    # Witness: the 0.20 mm the router fell back to before #7256 is out of band.
    nc = classes["USB_MCU_D+"]
    z = _zdiff(pcb, "F.Cu", 0.20, nc["intra_pair_clearance"])
    assert z > nc["target_diff_impedance"] * (1 + nc["impedance_tolerance_percent"] / 100.0)
