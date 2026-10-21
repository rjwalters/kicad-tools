"""Board 03 arm C route: USB_D equal via counts, end to end (Issue #7580).

#7093 (continued in #7580) taught the lattice fallback to rank and search for
via parity.  Board 03's own route stayed unproven end to end while VBUS U2.5
was stranded (#7463): a route that leaves a net open cannot pass copper-LVS,
so its USB_D via counts did not settle anything.  PR #7475 routed VBUS U2.5
under operator ruling Q36 = 1, which accepted a bounded USB margin loss
(common run >= 4.0 mm, MCU launch <= 4.0 mm; recovery tracked in #7650).

This runs the arm C route -- the tier-1 common-run test's argv
(``test_board03_tier1_common_run.py``) with plain ``--manufacturer jlcpcb``,
the profile board 03's copper is derived under -- and holds every gate of
#7580 item 3 that does not need kicad-cli:

* USB_D+ / USB_D- commit with equal via counts, and the
  ``diffpair_uncoupled_length`` rule raises no via-count finding on the pair;
* copper-LVS is clean (0 shorts / 0 opens), so VBUS U2.5 is routed;
* ``routing_plan.usb_geometry()`` passes (it raises on any bound) and the
  Q36 bounds hold explicitly;
* every segment is on a 45-degree multiple.

Pinned kicad-cli DRC and tier-1 ``kct check`` are recorded on #7580 for the
same route; they need Docker / a full check and stay out of this test.

The committed board (``output/usb_joystick_routed.kicad_pcb``) still carries
the pre-#7463 copper with USB_D 2 / 3 under the #7108 exception
(``tests/board03_via_count_exception_7093.py``); retiring it needs a board
re-derive from this route, which is separate work.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.lvs import compare_copper_netlist
from kicad_tools.router.rules import net_class_map_from_dict
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.diffpair_uncoupled_length import is_via_count_finding

REPO = Path(__file__).resolve().parents[2]
BOARD = REPO / "boards" / "03-usb-joystick"
ROUTE_TIMEOUT = 900

PAIR = ("USB_D+", "USB_D-")
# Operator ruling Q36 = 1 on #7463: the bounds that hold while VBUS U2.5 routes.
COMMON_RUN_MIN_MM = 4.0
MCU_LAUNCH_MAX_MM = 4.0


def _routing_plan():
    spec = importlib.util.spec_from_file_location(
        "b03_routing_plan_7580", BOARD / "routing_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.fixture(scope="module")
def armc_route(tmp_path_factory) -> Path:
    """Route board 03 once under arm C (lattice, plain jlcpcb, seed 42)."""
    work = tmp_path_factory.mktemp("board03-armc-7580")
    pcb_in = work / "usb_joystick.kicad_pcb"
    pcb_in.write_bytes((BOARD / "output" / "usb_joystick.kicad_pcb").read_bytes())
    routed = work / "usb_joystick_routed.kicad_pcb"
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(pcb_in),
        "-o",
        str(routed),
        "--timeout",
        "600",
        "--no-cache",
        "--manufacturer",
        "jlcpcb",
        "--native-zone-clearance",
        "--net-class-map",
        str(BOARD / "output" / "net_class_map.json"),
        "--seed",
        "42",
        "--route-engine",
        "lattice",
        "--strategy",
        "basic",
        "--differential-pairs",
        "--layers",
        "4",
        "--no-auto-layers",
        "--deterministic-budget",
        "--per-net-iterations",
        "1000000",
        "--no-placement-delta-feedback",
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        env=dict(os.environ, PYTHONHASHSEED="0"),
        capture_output=True,
        text=True,
        timeout=ROUTE_TIMEOUT,
    )
    assert routed.is_file(), proc.stdout[-2000:] + proc.stderr[-2000:]
    return routed


@pytest.mark.slow
@pytest.mark.timeout(ROUTE_TIMEOUT + 120)
def test_armc_route_gives_usb_d_equal_vias_with_every_gate(armc_route: Path) -> None:
    pcb = PCB.load(armc_route)

    vias = Counter(pcb.nets[v.net_number].name for v in pcb.vias)
    assert vias["USB_D+"] == vias["USB_D-"] > 0, (
        f"USB_D via counts differ: USB_D+ {vias['USB_D+']}, USB_D- {vias['USB_D-']}"
    )

    ncm = net_class_map_from_dict(json.loads((BOARD / "output" / "net_class_map.json").read_text()))
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, net_class_map=ncm)
    via_findings = [
        v
        for v in checker.check_diffpair_uncoupled_length().violations
        if tuple(sorted(v.nets)) == PAIR and is_via_count_finding(v)
    ]
    assert via_findings == []

    lvs = compare_copper_netlist(BOARD / "output" / "usb_joystick.kicad_sch", armc_route)
    assert lvs.bound_pad_count > 0
    assert lvs.shorts == () and lvs.opens == (), [str(m) for m in lvs.mismatches]

    plan = _routing_plan()
    plan._require_45_degree_copper(armc_route)
    geometry = plan.usb_geometry(armc_route)  # raises on any bound it fails
    lengths = geometry["lengths_mm"]
    assert geometry["common_coupled_mm"] >= COMMON_RUN_MIN_MM
    assert max(lengths["USB_MCU_D+"], lengths["USB_MCU_D-"]) <= MCU_LAUNCH_MAX_MM
