"""Board 03 ``usb_geometry()`` via-in-pad window exception (issue #7084).

Under the board's own ``jlcpcb-tier1`` profile via-in-pad drops the USB_D
launch vias into the ESD lands (board-relative y = 11.737 / 17.419), against
y = 12.638 / 16.890 under plain ``jlcpcb``.  The fixed y = 13..16.8 window
clipped both tier-1 ends, so the gate measured a coupled In2.Cu run far
shorter than the one the router laid.  The operator ruled (2026-10-19,
Q10 = 1) to widen the window *only* for vias inside the ESD / termination
lands: plain ``jlcpcb`` measures exactly as before (4.071 mm).  The 4.0 mm
coupled bound, the length and skew bounds and the MCU-launch limit are
unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

from kicad_tools.geometry.copper import pad_size_box
from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards" / "03-usb-joystick"
ROUTED = BOARD / "output" / "usb_joystick_routed.kicad_pcb"

# Measured launch-via positions (board-relative y, mm), seed 42, lattice.
TIER1_LAUNCH_Y = (11.737, 17.419)
JLCPCB_LAUNCH_Y = (12.638, 16.890)

# The tier-1 route's USB_D launch vias at y >= 11 mm (same route).
TIER1_LAUNCH_VIAS = {
    "USB_D+": [(37.05, 11.737), (39.379, 17.419)],
    "USB_D-": [(38.95, 11.737), (40.479, 17.419)],
}

# The USB_D copper at y >= 11 mm (ESD launch to termination) of a
# ``--manufacturer jlcpcb-tier1`` lattice route of board 03 (seed 42,
# committed net-class map, the ``tests/router/test_board03_tier1_common_run.py``
# recipe), as board-relative (layer, x0, y0, x1, y1).  The connector-side
# copper (y < 11 mm) is kept from the committed board.
TIER1_CORRIDOR = {
    "USB_D+": [
        ("F.Cu", 37.2, 11.2, 37.0, 11.4),
        ("F.Cu", 37.0, 11.4, 37.0, 11.588),
        ("F.Cu", 37.0, 11.588, 37.05, 11.638),
        ("F.Cu", 37.05, 11.638, 37.05, 11.737),
        ("F.Cu", 39.45, 17.49, 39.379, 17.419),
        ("In2.Cu", 37.05, 11.737, 37.113, 11.8),
        ("In2.Cu", 37.113, 11.8, 38.025, 11.8),
        ("In2.Cu", 38.025, 11.8, 38.025, 13.073),
        ("In2.Cu", 38.025, 13.073, 38.225, 13.272),
        ("In2.Cu", 38.225, 13.272, 38.225, 14.472),
        ("In2.Cu", 38.225, 14.472, 38.825, 15.073),
        ("In2.Cu", 38.825, 15.073, 38.825, 16.865),
        ("In2.Cu", 38.825, 16.865, 39.379, 17.419),
    ],
    "USB_D-": [
        ("F.Cu", 38.8, 11.2, 39.0, 11.4),
        ("F.Cu", 39.0, 11.4, 39.0, 11.588),
        ("F.Cu", 39.0, 11.588, 38.95, 11.638),
        ("F.Cu", 38.95, 11.638, 38.95, 11.737),
        ("F.Cu", 40.55, 17.49, 40.479, 17.419),
        ("In2.Cu", 38.95, 11.737, 38.887, 11.8),
        ("In2.Cu", 38.887, 11.8, 38.375, 11.8),
        ("In2.Cu", 38.375, 11.8, 38.375, 12.927),
        ("In2.Cu", 38.375, 12.927, 38.575, 13.127),
        ("In2.Cu", 38.575, 13.127, 38.575, 14.328),
        ("In2.Cu", 38.575, 14.328, 39.175, 14.927),
        ("In2.Cu", 39.175, 14.927, 39.175, 16.6),
        ("In2.Cu", 39.175, 16.6, 39.66, 16.6),
        ("In2.Cu", 39.66, 16.6, 40.479, 17.419),
    ],
}


@pytest.fixture
def routing_plan():
    spec = importlib.util.spec_from_file_location(
        "b03_routing_plan_7084w", BOARD / "routing_plan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


def _with_tier1_corridor(pcb: PCB) -> PCB:
    """Swap the committed board's USB_D launch-to-termination copper for tier-1's."""
    nets = {pcb.nets[n].name: n for n in pcb.nets}
    corridor = [
        s
        for s in pcb.segments
        if pcb.nets[s.net_number].name in TIER1_CORRIDOR and min(s.start[1], s.end[1]) >= 11.0
    ]
    assert corridor, "committed board lost its USB_D corridor copper"
    template = corridor[0]
    kept = [s for s in pcb.segments if s not in corridor]
    added = [
        dataclasses.replace(
            template,
            layer=layer,
            start=(x0, y0),
            end=(x1, y1),
            net_number=nets[name],
            net_name=name,
            uuid=f"tier1-{name}-{i}",
        )
        for name, segments in TIER1_CORRIDOR.items()
        for i, (layer, x0, y0, x1, y1) in enumerate(segments)
    ]
    pcb._segments[:] = kept + added
    _move_launch_vias(pcb, TIER1_LAUNCH_VIAS)
    return pcb


def _move_launch_vias(pcb: PCB, vias: dict[str, list[tuple[float, float]]]) -> PCB:
    """Replace the USB_D vias at y >= 11 mm with vias at ``vias``' positions."""
    nets = {pcb.nets[n].name: n for n in pcb.nets}
    launch = [v for v in pcb.vias if v.net_name in vias and v.position[1] >= 11.0]
    assert launch, "committed board lost its USB_D launch vias"
    template = launch[0]
    pcb._vias[:] = [v for v in pcb.vias if v not in launch] + [
        dataclasses.replace(
            template,
            position=pos,
            net_number=nets[name],
            net_name=name,
            uuid=f"launch-{name}-{i}",
        )
        for name, positions in vias.items()
        for i, pos in enumerate(positions)
    ]
    return pcb


def _load(monkeypatch, routing_plan, pcb: PCB) -> None:
    monkeypatch.setattr(routing_plan.PCB, "load", classmethod(lambda cls, path: pcb))


def test_lands_are_the_boards_pads(routing_plan):
    """The exception's land boxes are the board's own U2 / R3 / R4 pad boxes."""
    pcb = PCB.load(ROUTED)
    fps = {fp.reference: fp for fp in pcb.footprints}
    expected = {
        ("esd", "USB_D+"): ("U2", "4"),
        ("esd", "USB_D-"): ("U2", "6"),
        ("termination", "USB_D+"): ("R4", "1"),
        ("termination", "USB_D-"): ("R3", "1"),
    }
    tables = {
        "esd": routing_plan.USB_ESD_LANDS_7084,
        "termination": routing_plan.USB_TERMINATION_LANDS_7084,
    }
    for (kind, net), (ref, number) in expected.items():
        fp = fps[ref]
        pad = next(p for p in fp.pads if p.number == number)
        assert pad.net_name == net
        minx, miny, maxx, maxy = pad_size_box(pad, fp.position, fp.rotation).bounds
        assert tables[kind][net] == pytest.approx((minx, maxx, miny, maxy), abs=1e-3)


def test_exception_is_tied_to_7487_and_not_expired(routing_plan):
    assert routing_plan.USB_VIA_IN_PAD_EXCEPTION_ISSUE == 7487
    expires = datetime.date.fromisoformat(routing_plan.USB_VIA_IN_PAD_EXCEPTION_EXPIRES)
    assert datetime.date.today() <= expires, (
        "board 03's #7487 via-in-pad usb_geometry() window exception expired on "
        f"{expires}; re-rule it or remove it (see routing_plan.py)"
    )


def test_base_window_and_bounds_unchanged(routing_plan):
    assert routing_plan.USB_COMMON_RUN_WINDOW == (37.5, 40.3, 13.0, 16.8)


def test_via_in_land_widens_the_window(routing_plan):
    pcb = _move_launch_vias(PCB.load(ROUTED), TIER1_LAUNCH_VIAS)
    x0, x1, y0, y1 = routing_plan.usb_common_run_window(pcb)
    assert (x0, x1) == (37.5, 40.3)
    assert y0 == pytest.approx(TIER1_LAUNCH_Y[0], abs=1e-5)
    assert y1 == pytest.approx(TIER1_LAUNCH_Y[1], abs=1e-5)


def test_only_the_edge_with_an_in_land_via_moves(routing_plan):
    vias = {
        "USB_D+": [(37.05, 11.737), (39.45, 16.89)],
        "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
    }
    pcb = _move_launch_vias(PCB.load(ROUTED), vias)
    _, _, y0, y1 = routing_plan.usb_common_run_window(pcb)
    assert y0 == pytest.approx(11.737, abs=1e-5)
    assert y1 == 16.8


@pytest.mark.parametrize(
    "vias",
    [
        # Plain jlcpcb: just outside both lands.
        {
            "USB_D+": [(37.05, 12.6375), (39.45, 16.89)],
            "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
        },
        # Right y, wrong x: beside the land, not in it.
        {"USB_D+": [(37.6, 11.737), (39.0, 17.419)], "USB_D-": [(38.5, 11.737), (41.0, 17.419)]},
        # Swapped nets: a via in the *other* net's land does not count.
        {
            "USB_D+": [(38.95, 11.737), (40.479, 17.419)],
            "USB_D-": [(37.05, 11.737), (39.379, 17.419)],
        },
    ],
    ids=["jlcpcb-outside", "beside-land", "wrong-net-land"],
)
def test_via_outside_land_does_not_widen(routing_plan, vias):
    pcb = _move_launch_vias(PCB.load(ROUTED), vias)
    assert routing_plan.usb_common_run_window(pcb) == routing_plan.USB_COMMON_RUN_WINDOW


def test_committed_jlcpcb_board_measures_as_before(routing_plan):
    pcb = PCB.load(ROUTED)
    assert routing_plan.usb_common_run_window(pcb) == routing_plan.USB_COMMON_RUN_WINDOW
    geometry = routing_plan.usb_geometry(ROUTED)
    assert geometry["common_coupled_mm"] == pytest.approx(4.071, abs=0.001)
    assert geometry["branch_skew_mm"] == pytest.approx(0.0, abs=0.001)
    assert max(geometry["lengths_mm"].values()) <= 16.0


def test_tier1_launch_corridor_meets_the_common_run_bound(routing_plan, monkeypatch):
    _load(monkeypatch, routing_plan, _with_tier1_corridor(PCB.load(ROUTED)))
    geometry = routing_plan.usb_geometry(ROUTED)
    assert geometry["common_coupled_mm"] >= 4.0
    assert geometry["common_coupled_mm"] == pytest.approx(5.131, abs=0.001)
    assert max(geometry["lengths_mm"].values()) <= 16.0
    assert geometry["branch_skew_mm"] <= routing_plan.USB_FS_MAX_SKEW_MM


def test_tier1_corridor_without_in_land_vias_still_fails(routing_plan, monkeypatch):
    """Without the via-in-pad launches the fixed window clips the run: < 4.0 mm."""
    pcb = _with_tier1_corridor(PCB.load(ROUTED))
    _move_launch_vias(
        pcb,
        {
            "USB_D+": [(37.05, 12.6375), (39.45, 16.89)],
            "USB_D-": [(38.95, 12.6375), (40.55, 16.89)],
        },
    )
    _load(monkeypatch, routing_plan, pcb)
    with pytest.raises(ValueError, match="USB branch bounds failed"):
        routing_plan.usb_geometry(ROUTED)
