"""Committed-artifact invariants for the softstart board (issue #3486).

Issue #3486 moved the segment-vs-via-barrel repair OUT of the softstart
recipe (the ``_repair_segment_via_clearance`` / ``_point_segment_distance``
workaround was deleted) and INTO the router finalization backstop
(``Autorouter._demote_via_segment_violation_nets``, exercised by
``tests/test_via_segment_short_3486.py``).

The repair-function unit tests (``tests/test_softstart_segment_via_repair.py``)
went away with the workaround.  The ARTIFACT-LEVEL invariants those tests
also asserted are NOT workaround-specific, so they are preserved here as
standalone scans of the committed board files:

1. The committed softstart artifact has ZERO segment-vs-via-barrel
   conflicts on ANY copper layer the barrel spans (the #3487 all-layer
   rule — a through-via barrel is copper on every layer it spans).
2. Every segment in the artifact is 45-quantized.
3. Object uuids are unique within every committed board artifact
   (PR #3557 regression: a repair->quantize fixpoint once regenerated a
   dogleg sibling uuid that already shipped).

These scans depend only on the committed ``.kicad_pcb`` files and the
``is_45_aligned`` geometry helper — not on any deleted recipe function.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = (
    REPO_ROOT / "boards" / "external" / "softstart" / "output"
    / "softstart_routed.kicad_pcb"
)

MIN_CLEARANCE = 0.1016  # jlcpcb-tier1 inner-layer clearance (mm)

SEG_RE = re.compile(
    r'\(segment\s*\n\s*\(start ([\d.-]+) ([\d.-]+)\)\s*\n\s*'
    r'\(end ([\d.-]+) ([\d.-]+)\)\s*\n\s*\(width ([\d.]+)\)\s*\n\s*'
    r'\(layer "([^"]+)"\)\s*\n\s*'
    r'(?:\(uuid "[^"]+"\)\s*\n\s*\(net (\d+)\)'
    r'|\(net (\d+)\)\s*\n\s*\(uuid "[^"]+"\))'
)
VIA_RE = re.compile(
    r"\(via\s*\n\s*\(at ([\d.-]+) ([\d.-]+)\)\s*\n\s*\(size ([\d.]+)\)"
    r"[\s\S]*?\(net (\d+)\)"
)


def _point_seg_dist(px, py, sx, sy, ex, ey):
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        return math.hypot(px - sx, py - sy)
    t = ((px - sx) * dx + (py - sy) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def _parse(pcb_path):
    text = pcb_path.read_text()
    segs = [
        (
            float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)),
            float(m.group(5)), m.group(6), m.group(7) or m.group(8),
        )
        for m in SEG_RE.finditer(text)
    ]
    vias = [
        (float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4))
        for m in VIA_RE.finditer(text)
    ]
    return segs, vias


def _assert_no_barrel_conflicts(segs, vias, min_clearance=MIN_CLEARANCE):
    """#3487-style check: every via barrel is copper on EVERY copper
    layer (all vias here are through-vias), so test every segment
    against every foreign via regardless of declared layer pair."""
    violations = []
    for sx, sy, ex, ey, width, layer, net in segs:
        for vx, vy, v_size, v_net in vias:
            if v_net == net:
                continue
            required = width / 2.0 + v_size / 2.0 + min_clearance
            dist = _point_seg_dist(vx, vy, sx, sy, ex, ey)
            if dist < required - 1e-4:
                violations.append(
                    f"seg ({sx},{sy})-({ex},{ey}) [{layer} net {net}] vs "
                    f"via ({vx},{vy}) [net {v_net}]: "
                    f"{dist:.4f} < {required:.4f}"
                )
    assert not violations, "\n".join(violations)


def _assert_all_45(segs):
    from kicad_tools.router.quantize import is_45_aligned

    for sx, sy, ex, ey, *_ in segs:
        dx, dy = ex - sx, ey - sy
        if dx == 0 and dy == 0:
            continue
        assert is_45_aligned(dx, dy), (
            f"off-angle segment ({sx},{sy})-({ex},{ey})"
        )


@pytest.mark.skipif(not ARTIFACT.exists(), reason="committed artifact missing")
class TestCommittedArtifact:
    """All-layer barrel scan of the committed softstart artifact — the
    #3487-rule invariant, now guarded by the router backstop (#3486)."""

    def test_no_segment_via_barrel_conflicts_any_layer(self):
        segs, vias = _parse(ARTIFACT)
        assert len(segs) > 1000, "artifact parse failure (regex drift?)"
        assert len(vias) > 50, "artifact parse failure (regex drift?)"
        _assert_no_barrel_conflicts(segs, vias)

    def test_all_segments_45_aligned(self):
        segs, _ = _parse(ARTIFACT)
        _assert_all_45(segs)

    def test_uuids_unique_fleet_wide(self):
        """KiCad object uuids must be unique per file (PR #3557 judge
        finding: the repair->quantize fixpoint regenerated a dogleg
        sibling uuid that already shipped, duplicating it).  Scan EVERY
        committed board artifact — the failure mode is not
        softstart-specific."""
        uuid_re = re.compile(r'\(uuid "([^"]+)"\)')
        boards_dir = REPO_ROOT / "boards"
        scanned = 0
        problems = []
        for pcb in sorted(boards_dir.rglob("*.kicad_pcb")):
            scanned += 1
            counts = Counter(uuid_re.findall(pcb.read_text()))
            dupes = sorted(u for u, c in counts.items() if c > 1)
            if dupes:
                problems.append(f"{pcb.relative_to(REPO_ROOT)}: {dupes}")
        assert scanned > 0, "no board artifacts found (path drift?)"
        assert not problems, "duplicate uuids:\n" + "\n".join(problems)
