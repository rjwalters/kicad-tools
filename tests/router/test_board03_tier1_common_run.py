"""Board 03 USB_D common run under ``--manufacturer jlcpcb-tier1`` (issue #7084).

Routing board 03 under its own fab tier used to look like it lost the coupled
USB_D common run on In2.Cu: ``routing_plan.usb_geometry()`` measured 1.414 mm
(bound 4.0 mm) against 4.071 mm under plain ``jlcpcb``.

The router is not what lost it.  The only difference between the two profiles
is ``via_in_pad_supported``; with it on, the lattice drops the launch vias into
the ESD lands (y = 11.737 / 17.419) instead of 0.6-0.9 mm outside them
(y = 12.638 / 16.890).  The pair then runs coupled over a longer span of
In2.Cu -- 6.08 mm against 4.59 mm over the whole corridor -- but
``usb_geometry()`` only counts segments with every endpoint inside its fixed
y = 13..16.8 window, which the tier-1 launch clips at both ends.  Forcing
``via_in_pad_supported`` off in the lattice restores the in-window 4.1 mm.

The operator then ruled (2026-10-19, Q10 = 1) to keep that window and move
an edge out only to a USB_D via that sits inside the ESD or termination land
(``routing_plan.usb_common_run_window``), so the tier-1 run counts via to via
while plain ``jlcpcb`` still measures 4.071 mm.  The route below must also
pass ``usb_geometry()`` itself.

This pin measures the corridor, not the plan-tuned window, so it holds the
router to what it can control: the coupled In2.Cu run must stay at least the
4.0 mm common-run bound, with ``usb_geometry()``'s length and skew bounds.
"""

from __future__ import annotations

import importlib.util
import math
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.clearance import CopperElement
from kicad_tools.validate.rules.diffpair_routing_continuity import DiffPairRoutingContinuityRule

REPO = Path(__file__).resolve().parents[2]
BOARD = REPO / "boards" / "03-usb-joystick"
ROUTE_TIMEOUT = 900

# Board-relative corridor of the USB_D ESD-to-termination run, wide enough to
# hold the launch vias whether they sit in the ESD lands (tier-1) or outside.
CORRIDOR_X = (37.5, 41.0)
CORRIDOR_Y = (11.0, 17.6)
COMMON_RUN_BOUND_MM = 4.0


def _routing_plan():
    spec = importlib.util.spec_from_file_location(
        "b03_routing_plan_7084", BOARD / "routing_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _in2_common_run(pcb: PCB) -> float:
    """Coupled length of USB_D+/USB_D- on In2.Cu inside the corridor."""
    tracks = defaultdict(list)
    for segment in pcb.segments:
        tracks[pcb.nets[segment.net_number].name].append(segment)
    run = {
        name: [
            CopperElement.from_segment(s)
            for s in tracks[name]
            if s.layer == "In2.Cu"
            and all(
                CORRIDOR_X[0] <= x <= CORRIDOR_X[1] and CORRIDOR_Y[0] <= y <= CORRIDOR_Y[1]
                for x, y in (s.start, s.end)
            )
        ]
        for name in ("USB_D+", "USB_D-")
    }
    rule = DiffPairRoutingContinuityRule(engaged_pairs=set())
    return min(
        rule._coupled_length(run["USB_D+"], run["USB_D-"]),
        rule._coupled_length(run["USB_D-"], run["USB_D+"]),
    )


@pytest.mark.slow
@pytest.mark.timeout(ROUTE_TIMEOUT + 60)
def test_tier1_route_keeps_the_usb_d_common_run(tmp_path):
    source = BOARD / "output" / "usb_joystick.kicad_pcb"
    pcb_in = tmp_path / "usb_joystick.kicad_pcb"
    pcb_in.write_bytes(source.read_bytes())
    routed = tmp_path / "usb_joystick_routed.kicad_pcb"
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
        "jlcpcb-tier1",
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

    pcb = PCB.load(routed)
    common = _in2_common_run(pcb)
    assert common >= COMMON_RUN_BOUND_MM, (
        f"tier-1 In2.Cu USB_D coupled run {common:.3f} mm < {COMMON_RUN_BOUND_MM} mm"
    )

    plan = _routing_plan()
    lengths = defaultdict(float)
    for segment in pcb.segments:
        lengths[pcb.nets[segment.net_number].name] += math.dist(segment.start, segment.end)
    assert max(lengths["USB_D+"], lengths["USB_D-"]) <= 16.0
    assert abs(lengths["USB_D+"] - lengths["USB_D-"]) <= plan.USB_FS_MAX_SKEW_MM

    # The #7084 via-in-pad exception must hold the tier-1 launches, so the
    # board's own gate passes (it raises on any bound it fails).
    _, _, y0, y1 = plan.usb_common_run_window(pcb)
    assert y0 < plan.USB_COMMON_RUN_WINDOW[2] and y1 > plan.USB_COMMON_RUN_WINDOW[3]
    assert plan.usb_geometry(routed)["common_coupled_mm"] >= COMMON_RUN_BOUND_MM
