"""Tests for ``NetStatusAnalyzer(strict=True)`` real-geometry connectivity.

Issue #4176: the default net-status connectivity model unions copper on a
0.01mm endpoint-proximity radius (``_points_close``) without testing whether
the real copper shapes (segment width, pad size) actually touch, so it can
report a net "complete" that ``kicad-cli pcb drc`` reports as unconnected
(over-connecting relative to KiCad).  ``strict=True`` decides connectivity by
real shapely copper-shape intersection instead, matching KiCad.

These fixtures are minimal synthetic PCBs built from S-expression strings (no
external board files), following the convention in ``test_net_status.py``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatusAnalyzer

shapely = pytest.importorskip("shapely")


def _analyze(pcb_text: str, *, strict: bool):
    """Load a synthetic .kicad_pcb string and return the analyzed result."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "board.kicad_pcb"
        path.write_text(pcb_text)
        return NetStatusAnalyzer(path, strict=strict).analyze()


def _status(pcb_text: str, net: str, *, strict: bool) -> str:
    result = _analyze(pcb_text, strict=strict)
    net_status = result.get_net(net)
    assert net_status is not None, f"net {net!r} not found"
    return net_status.status


def _analyzer(pcb_text: str, tmp_path: Path, *, strict: bool = False) -> NetStatusAnalyzer:
    """Build a ``NetStatusAnalyzer`` from a synthetic PCB string.

    ``NetStatusAnalyzer`` treats a ``str``/``Path`` argument as a file path, so
    the board text is written to a real file first (mirroring ``_analyze``).
    """
    path = tmp_path / "board.kicad_pcb"
    path.write_text(pcb_text)
    return NetStatusAnalyzer(path, strict=strict)


def _pad_seg_board(seg_end_x: float) -> str:
    """Two single-pad footprints on net SIG.

    R1.1 copper is centered at (100, 100); R2.1 copper at (110, 100), each a
    0.6x0.6 SMD rect.  A single trace runs from R1.1's center to ``seg_end_x``.
    When ``seg_end_x`` reaches into R2.1's copper the net is complete; when it
    stops short (near-miss) the copper does not touch R2.1.
    """
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (footprint "R" (layer "F.Cu") (uuid "fp1") (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "r1"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (footprint "R" (layer "F.Cu") (uuid "fp2") (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "r2"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (segment (start 100 100) (end {seg_end_x} 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""


def _two_segment_board(gap: float, width: float) -> str:
    """Two pads joined by two thin segments whose inner endpoints are ``gap`` apart.

    R1.1 is at (100, 100); R2.1 at (105, 105).  Segment A runs (100,100)->(105,100);
    segment B runs (105, 100+gap)->(105, 105).  The two segments' inner endpoints
    are ``gap`` mm apart on perpendicular headings.  With a small ``gap`` (< 0.01)
    the default endpoint-tolerance model chains them; with a thin ``width`` their
    buffered copper does NOT overlap across the gap, so strict mode does not.
    """
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (footprint "R" (layer "F.Cu") (uuid "fp1") (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "r1"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (footprint "R" (layer "F.Cu") (uuid "fp2") (at 105 105)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "r2"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (segment (start 100 100) (end 105 100) (width {width}) (layer "F.Cu") (net 1) (uuid "sa"))
  (segment (start 105 {100 + gap}) (end 105 105) (width {width}) (layer "F.Cu") (net 1) (uuid "sb"))
)
"""


# --------------------------------------------------------------------------
# AC #2 / #3: segment endpoint near vs. touching a pad's copper.
# --------------------------------------------------------------------------


def test_strict_reports_incomplete_when_segment_stops_short_of_pad():
    # R2.1 copper spans x in [109.7, 110.3]; the trace ends at 109.5 (0.2mm
    # short of the pad copper edge, well outside any real overlap).  Strict
    # geometry must leave R2.1 stranded -> incomplete, matching kicad-cli.
    board = _pad_seg_board(seg_end_x=109.5)
    assert _status(board, "SIG", strict=True) == "incomplete"


def test_strict_reports_complete_when_segment_reaches_into_pad():
    # The trace now ends at 109.9, inside R2.1's copper (>= 109.7 edge, and
    # inside the eroded pad polygon) -> real copper contact -> complete.
    board = _pad_seg_board(seg_end_x=109.9)
    assert _status(board, "SIG", strict=True) == "complete"


# --------------------------------------------------------------------------
# AC #4: segment endpoints within the old 0.01mm tolerance but with
# non-overlapping copper.  Proves the mode flag changes behavior AND that the
# default is preserved.
# --------------------------------------------------------------------------


def test_default_over_connects_endpoints_within_tolerance():
    # gap 0.009mm (< 0.01 tolerance), width 0.001mm (buffered copper 0.0005mm
    # each side, so the copper is ~0.008mm short of touching).  The default
    # endpoint-proximity model unions the two chains -> complete.
    board = _two_segment_board(gap=0.009, width=0.001)
    assert _status(board, "SIG", strict=False) == "complete"


def test_strict_rejects_endpoints_within_tolerance_but_copper_apart():
    # Same geometry: strict mode sees the copper does not overlap across the
    # 0.009mm gap -> incomplete (matches KiCad).
    board = _two_segment_board(gap=0.009, width=0.001)
    assert _status(board, "SIG", strict=True) == "incomplete"


def test_strict_and_default_agree_when_copper_actually_overlaps():
    # gap 0.0 (endpoints coincide) with realistic 0.25mm width: copper truly
    # overlaps, so BOTH modes report complete.
    board = _two_segment_board(gap=0.0, width=0.25)
    assert _status(board, "SIG", strict=False) == "complete"
    assert _status(board, "SIG", strict=True) == "complete"


# --------------------------------------------------------------------------
# strict wiring / edge cases
# --------------------------------------------------------------------------


def test_analyzer_defaults_to_non_strict(tmp_path: Path):
    analyzer = _analyzer(_pad_seg_board(seg_end_x=109.9), tmp_path)
    assert analyzer.strict is False


def test_strict_flag_is_recorded(tmp_path: Path):
    analyzer = _analyzer(_pad_seg_board(seg_end_x=109.9), tmp_path, strict=True)
    assert analyzer.strict is True


def test_zero_width_segment_does_not_crash_strict():
    # A degenerate zero-width segment must not crash the strict polygon
    # builder (mirrors the _pad_copper_polygon guard).  The segment reduces to
    # its centerline; with the trace ending short of the pad, the net is
    # incomplete rather than raising.
    board = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (footprint "R" (layer "F.Cu") (uuid "fp1") (at 100 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "r1"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (footprint "R" (layer "F.Cu") (uuid "fp2") (at 110 100)
    (property "Reference" "R2" (at 0 0 0) (layer "F.SilkS") (uuid "r2"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
  (segment (start 100 100) (end 105 100) (width 0) (layer "F.Cu") (net 1) (uuid "s1"))
)
"""
    # Should not raise; R2.1 is stranded.
    assert _status(board, "SIG", strict=True) == "incomplete"


def test_strict_requires_shapely(monkeypatch, tmp_path: Path):
    # If shapely is unavailable, strict mode must fail loud rather than
    # silently degrading to the tolerance model.
    import kicad_tools._shapely as shp

    monkeypatch.setattr(shp, "has_shapely", lambda: False)
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_pad_seg_board(seg_end_x=109.9))
    with pytest.raises(ModuleNotFoundError):
        NetStatusAnalyzer(path, strict=True)
