"""Board 03 MCU USB launch bound is the sourced USB2_FS value (Issue #7256)."""

import runpy
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

BOARD = Path(__file__).resolve().parents[1] / "boards/03-usb-joystick"
ROUTED = BOARD / "output/usb_joystick_routed.kicad_pcb"


@pytest.fixture
def routing():
    return runpy.run_path(str(BOARD / "routing_plan.py"))


def _with_launch_lengths(routing, monkeypatch, lengths):
    """Run usb_geometry on the committed board with MCU launch lengths overridden."""
    import math

    real_dist = math.dist
    scope = routing["usb_geometry"].__globals__
    pcb = PCB.load(ROUTED)
    mcu = {s.net_number: pcb.nets[s.net_number].name for s in pcb.segments}
    seen = set()
    for s in pcb.segments:
        name = mcu[s.net_number]
        if name in lengths and name not in seen:
            seen.add(name)
            # Stretch one segment so the net totals the requested length.
            total = sum(
                real_dist(t.start, t.end) for t in pcb.segments if mcu[t.net_number] == name
            )
            extra = lengths[name] - total
            dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
            norm = real_dist(s.start, s.end)
            s.end = (s.end[0] + dx / norm * extra, s.end[1] + dy / norm * extra)
    monkeypatch.setattr(scope["PCB"], "load", staticmethod(lambda path: pcb))
    return routing["usb_geometry"](ROUTED)


def test_limit_is_usb2_fs_sourced_value(routing):
    assert routing["USB_FS_MAX_LAUNCH_MM"] == 6.35


def test_committed_board_passes(routing):
    lengths = routing["usb_geometry"](ROUTED)["lengths_mm"]
    assert lengths["USB_MCU_D+"] == pytest.approx(3.804, abs=1e-3)
    assert lengths["USB_MCU_D-"] == pytest.approx(3.649, abs=1e-3)


def test_fresh_route_launch_lengths_pass(routing, monkeypatch):
    out = _with_launch_lengths(routing, monkeypatch, {"USB_MCU_D+": 4.138, "USB_MCU_D-": 4.284})
    assert out["lengths_mm"]["USB_MCU_D-"] == pytest.approx(4.284, abs=1e-3)


def test_launch_beyond_limit_raises_with_measurements(routing, monkeypatch):
    with pytest.raises(ValueError, match=r"6\.35.*USB_MCU_D\+"):
        _with_launch_lengths(routing, monkeypatch, {"USB_MCU_D+": 6.5, "USB_MCU_D-": 3.649})
