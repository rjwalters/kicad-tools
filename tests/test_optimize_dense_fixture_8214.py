"""Dense-board test: checked rewrites are kept and add no DRC violations (Issue #8214).

The CLI builds a collision checker from the board, so rewrites that are safe
are kept instead of being rolled back net-by-net.  This generates a small
dense two-layer board whose nets are fragmented (collinear slivers and a
corner jog), runs ``optimize-traces`` and compares ``kicad-cli pcb drc`` on
input and output.  Skipped cleanly when kicad-cli is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter

import pytest

from kicad_tools.cli.optimize_cmd import main
from kicad_tools.router.optimizer.pcb import parse_segments

NETS = 12
PITCH = 0.6  # mm between parallel traces: 0.4 mm copper gap, well above fab minimum
WIDTH = 0.2
SLIVER = 0.08  # collinear fragment length


def _seg(x1, y1, x2, y2, layer, net):
    return (
        f"\t(segment (start {x1:.3f} {y1:.3f}) (end {x2:.3f} {y2:.3f}) "
        f'(width {WIDTH}) (layer "{layer}") (net {net}))\n'
    )


def _dense_board() -> str:
    head = (
        '(kicad_pcb\n\t(version 20240108)\n\t(generator "test")\n'
        '\t(general (thickness 1.6))\n\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '\t(setup (pad_to_mask_clearance 0))\n\t(net 0 "")\n'
    )
    for n in range(1, NETS + 1):
        head += f'\t(net {n} "N{n}")\n'
    head += (
        "\t(gr_rect (start 90 80) (end 140 120) (stroke (width 0.1) (type default))"
        ' (fill none) (layer "Edge.Cuts") (uuid "edge"))\n'
    )
    body = ""
    for n in range(1, NETS + 1):
        for layer, y0 in (("F.Cu", 90.0), ("B.Cu", 90.3)):
            y = y0 + n * PITCH
            x = 100.0
            # Slivered run: many collinear fragments, then one long segment.
            for _ in range(6):
                body += _seg(x, y, x + SLIVER, y, layer, n)
                x += SLIVER
            body += _seg(x, y, 120.0, y, layer, n)
    return head + body + ")\n"


def _drc(pcb, out):
    proc = subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--refill-zones",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(out),
            str(pcb),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if not out.exists():
        pytest.skip(f"kicad-cli DRC produced no report: {proc.stderr[-200:]}")
    data = json.loads(out.read_text())
    return Counter(v["type"] for v in data.get("violations", []))


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
def test_dense_board_rewrites_kept_and_no_new_drc_violations(tmp_path):
    src = tmp_path / "dense.kicad_pcb"
    out = tmp_path / "dense_opt.kicad_pcb"
    src.write_text(_dense_board())
    before_segs = sum(len(v) for v in parse_segments(src.read_text()).values())

    assert main([str(src), "-o", str(out), "--quiet"]) == 0

    after_segs = sum(len(v) for v in parse_segments(out.read_text()).values())
    # Every fragmented run is mergeable and nothing is in the way: the
    # rewrites must be kept, not rolled back.
    assert after_segs <= before_segs - NETS * 2 * 5
    assert after_segs <= NETS * 2 * 2

    base = _drc(src, tmp_path / "before.json")
    new = _drc(out, tmp_path / "after.json")
    regress = {k: new[k] - base.get(k, 0) for k in new if new[k] > base.get(k, 0)}
    assert not regress, f"optimizer introduced DRC violations: {regress}"
