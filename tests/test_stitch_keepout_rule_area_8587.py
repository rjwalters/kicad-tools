"""Regression tests for Issue #8587: ``kct stitch`` honours keep-out rule areas.

A via site or pad-to-via stub inside a ``(tracks not_allowed) (vias not_allowed)``
rule area made kicad-cli report ``items_not_allowed``.
"""

from __future__ import annotations

import math
from pathlib import Path

from kicad_tools.cli.stitch_cmd import run_stitch, trace_points

from .test_stitch_mfr import TWO_LAYER_GND_ZONE_PCB

PAD = (109.49, 110.0)  # GND pad of C1


def _circle(cx: float, cy: float, r: float, n: int = 24) -> str:
    pts = " ".join(
        f"(xy {cx + r * math.cos(2 * math.pi * i / n):.4f} {cy + r * math.sin(2 * math.pi * i / n):.4f})"
        for i in range(n)
    )
    return f"(polygon (pts {pts}))"


def _keepout(polygon: str, tracks: str, vias: str, layers: str = '"F.Cu" "B.Cu"') -> str:
    return (
        f'  (zone (net 0) (net_name "") (layers {layers}) (uuid "ko-{tracks}-{vias}") '
        f'(name "MH_keepout") (hatch edge 0.5) (connect_pads (clearance 0)) '
        f"(min_thickness 0.25) (filled_areas_thickness no) "
        f"(keepout (tracks {tracks}) (vias {vias}) (pads allowed) (copperpour allowed) "
        f"(footprints allowed)) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
        f"{polygon})\n"
    )


def _write(tmp_path: Path, extra: str = "") -> Path:
    text = TWO_LAYER_GND_ZONE_PCB.rstrip()
    text = text[:-1] + extra + ")\n"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(text)
    return pcb


def _run(pcb: Path):
    return run_stitch(pcb, ["GND"], via_size=0.45, drill=0.2, dry_run=True)


def _inside(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return math.hypot(x - cx, y - cy) < r


def test_baseline_without_rule_areas_places_via(tmp_path):
    result = _run(_write(tmp_path))
    assert len(result.vias_added) == 1


def test_pad_inside_keepout_is_skipped_with_named_reason(tmp_path):
    ko = _keepout(_circle(*PAD, 3.0), "not_allowed", "not_allowed")
    result = _run(_write(tmp_path, ko))
    assert result.vias_added == []
    assert len(result.pads_skipped) == 1
    assert "MH_keepout" in result.pads_skipped[0][1]


def test_via_moves_to_legal_site_outside_keepout(tmp_path):
    # Small keep-out over the pad's +x side only; the pad itself is outside.
    cx, cy, r = PAD[0] + 0.9, PAD[1], 0.6
    ko = _keepout(_circle(cx, cy, r), "not_allowed", "not_allowed")
    result = _run(_write(tmp_path, ko))
    assert len(result.vias_added) == 1
    via = result.vias_added[0]
    assert not _inside(via.via_x, via.via_y, cx, cy, r + 0.225)
    for trace in result.traces_added:
        pts = trace_points(trace)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
            for i in range(21):
                t = i / 20
                assert not _inside(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, cx, cy, r)


def test_vias_only_area_blocks_via_but_not_track(tmp_path):
    # Vias-only keep-out around the pad: via sites inside are rejected, but
    # a stub may cross it to a site outside (needs a big enough reach).
    ko = _keepout(_circle(*PAD, 0.9), "allowed", "not_allowed")
    result = _run(_write(tmp_path, ko))
    for via in result.vias_added:
        assert not _inside(via.via_x, via.via_y, *PAD, 0.9 + 0.2)


def test_tracks_only_area_does_not_block_via_site(tmp_path):
    # Tracks-only keep-out away from the via sites: vias unaffected.
    ko = _keepout(_circle(PAD[0], PAD[1] + 20, 1.0), "not_allowed", "allowed")
    result = _run(_write(tmp_path, ko))
    assert len(result.vias_added) == 1


def test_area_on_other_layer_does_not_block_track(tmp_path):
    # Tracks-only area on B.Cu only; the F.Cu stub is unaffected.
    cx, cy, r = PAD[0] - 0.9, PAD[1], 0.45
    ko = _keepout(_circle(cx, cy, r), "not_allowed", "allowed", layers='"B.Cu"')
    base = _run(_write(tmp_path))
    result = _run(_write(tmp_path, ko))
    assert len(result.vias_added) == len(base.vias_added) == 1
