"""Issue #6823: every pad-to-via stitch stub is written 45-degree-legal.

The router's pour-link closer (``PourLinkCloser._weld_pads`` -> ``run_stitch``)
hands ``run_stitch`` the via sites ``kct route`` reserved for each pad.  A
reserved site a few hundredths of a mm off the pad's axis used to be joined
by a single straight stub, ~1-2 degrees off the 0/45/90/135 set -- board 03's
``(143.30, 91.16) -> (143.32, 92.20)`` VCC stub -- which
``routing_plan.save_plan()`` rejects (#3907).  The stub is now a dogleg, and
the dogleg's legs are what the clearance checks see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    TraceSegment,
    add_trace_to_pcb,
    run_stitch,
    trace_to_track_segments,
)
from kicad_tools.router.quantize import verify_segment_45
from kicad_tools.sexp import SExp

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

# C1.1 (GND) centre in ``TWO_LAYER_GND_ZONE_PCB``.
PAD = (109.49, 110.0)

_SEGMENT_RE = re.compile(r"\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)")


def _write(tmp_path: Path, extra: str = "", pad_dy: float = 0.0) -> Path:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    if pad_dy:
        text = text.replace("(at 110 110)", f"(at 110 {110 + pad_dy})", 1)
    assert text.endswith(")")
    if extra:
        text = text.replace('(net 1 "GND")\n', '(net 1 "GND")\n  (net 2 "SIG")\n', 1)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text[:-1] + extra + ")\n")
    return pcb


def _segments(pcb: Path) -> list[tuple[float, float, float, float]]:
    return [
        (float(a), float(b), float(c), float(d))
        for a, b, c, d in _SEGMENT_RE.findall(pcb.read_text())
    ]


def _stitch(pcb: Path, site: tuple[float, float]):
    return run_stitch(
        pcb,
        ["GND"],
        via_size=0.6,
        drill=0.3,
        clearance=0.2,
        trace_width=0.2,
        preferred_sites={"C1.1": [site]},
    )


def _assert_45_path(segments, site, pad=PAD) -> None:
    assert segments, "no stub was written"
    for x1, y1, x2, y2 in segments:
        verify_segment_45(x1, y1, x2, y2, strict=True, context="#6823 test")
    # The legs form one connected path from the pad to the reserved site.
    assert (segments[0][0], segments[0][1]) == pytest.approx(pad, abs=0.005)
    assert (segments[-1][2], segments[-1][3]) == pytest.approx(site)
    for prev, nxt in zip(segments, segments[1:], strict=False):
        assert (prev[2], prev[3]) == pytest.approx((nxt[0], nxt[1]))


@pytest.mark.parametrize("pad_dy", [0.0, 0.0025], ids=["on-grid-pad", "off-grid-pad"])
def test_near_axis_reserved_site_is_doglegged(tmp_path: Path, pad_dy: float) -> None:
    """The issue's shape: site offset (0.02, 1.04) mm from the pad.

    ``off-grid-pad`` puts the pad centre between the writer's 0.01 mm
    coordinates (board 03's U1.24 sits at y = 91.1625), so the stub start is
    written 0.0025 mm from the float centre.
    """
    pad = (PAD[0], PAD[1] + pad_dy)
    site = (pad[0] + 0.02, round(pad[1] + 1.04, 2))
    pcb = _write(tmp_path, pad_dy=pad_dy)

    result = _stitch(pcb, site)

    assert [(v.via_x, v.via_y) for v in result.vias_added] == [pytest.approx(site)]
    segments = _segments(pcb)
    assert len(segments) == 2
    _assert_45_path(segments, site, pad)
    # What later placements clearance-check is what was written.
    (trace,) = result.traces_added
    written = [(s.start_x, s.start_y, s.end_x, s.end_y) for s in trace_to_track_segments(trace)]
    assert written == [pytest.approx(s) for s in segments]


def test_blocked_default_dogleg_falls_back_to_other_variant(tmp_path: Path) -> None:
    """The dogleg is clearance-checked; a blocked variant is not written.

    Site (0.3, 1.04) off the pad.  The default variant (diagonal first) runs
    its vertical leg at x = 109.79, 0.36 mm from a foreign track at
    x = 110.15 (needs 0.1 + 0.1 + 0.2 = 0.4); the axis-first variant's
    vertical leg runs at x = 109.49, well clear.
    """
    site = (PAD[0] + 0.3, PAD[1] + 1.04)
    track = (
        "  (segment (start 110.15 110.0) (end 110.15 110.4) (width 0.2) "
        '(layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000006823"))\n'
    )
    pcb = _write(tmp_path, track)

    result = _stitch(pcb, site)

    assert [(v.via_x, v.via_y) for v in result.vias_added] == [pytest.approx(site)]
    stub = [s for s in _segments(pcb) if s[0] != 110.15]
    _assert_45_path(stub, site)
    # Axis-first: vertical leg straight down from the pad, then the diagonal.
    assert stub[0][0] == pytest.approx(PAD[0]) and stub[0][2] == pytest.approx(PAD[0])


def test_add_trace_to_pcb_legalizes_hand_built_straight_stub() -> None:
    """``kct stitch``'s shared writer cannot emit an off-angle straight stub.

    Board 03's U1.24 (VCC) shape: the pad centre is at y = 91.1625, which the
    writer serializes at 91.16 (2 decimals), so legality has to be decided on
    the written coordinates: a dogleg built from the float centre wrote its
    first leg as (143.3, 91.16) -> (143.32, 91.19), 11 degrees off.
    """
    pad = PadInfo(
        reference="C1",
        pad_number="1",
        net_number=1,
        net_name="GND",
        x=143.30,
        y=91.1625,
        width=0.54,
        height=0.64,
        layer="F.Cu",
    )
    trace = TraceSegment(pad=pad, via_x=143.32, via_y=92.20, width=0.2, layer="F.Cu")
    sexp = SExp.list("kicad_pcb")

    add_trace_to_pcb(sexp, trace)

    segments = [
        (
            seg.find("start").get_float(0),
            seg.find("start").get_float(1),
            seg.find("end").get_float(0),
            seg.find("end").get_float(1),
        )
        for seg in sexp.find_all("segment")
    ]
    assert len(segments) == 2
    for x1, y1, x2, y2 in segments:
        verify_segment_45(x1, y1, x2, y2, strict=True)
    written = [(s.start_x, s.start_y, s.end_x, s.end_y) for s in trace_to_track_segments(trace)]
    assert written == [pytest.approx(s) for s in segments]
