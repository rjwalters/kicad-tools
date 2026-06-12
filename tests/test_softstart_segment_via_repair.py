"""Regression tests for the softstart step-10d segment-vs-via repair
(issue #3516).

The #3487 DRC fix (``clearance_segment_via`` now checks every layer a
via barrel spans) surfaced a real cross-net short on the committed
softstart artifact: a diagonal I_SENSE_OUT In2.Cu segment overlapped a
GND through-via barrel by -0.042 mm.  The step-10d repair sweep
(``_repair_segment_via_clearance``) only handled axis-aligned segments,
so the diagonal slipped through.

These tests pin:

1. The repair function fixes DIAGONAL interior conflicts with a
   three-leg 45-quantized detour that preserves the original endpoints.
2. The repair function still fixes axis-aligned conflicts (the original
   #3481 behavior).
3. Diagonal ENDPOINT conflicts are dragged radially clear along an
   8-direction-snapped vector.
4. The committed artifact has ZERO segment-vs-via-barrel conflicts on
   ANY layer the barrel spans — the #3487-style all-layer check,
   implemented standalone so it guards the artifact even before the
   rule fix (PR #3517) merges.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RECIPE_PATH = REPO_ROOT / "boards" / "external" / "softstart" / "generate_design.py"
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


def _load_recipe():
    spec = importlib.util.spec_from_file_location(
        "softstart_generate_design", RECIPE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("softstart_generate_design", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recipe():
    return _load_recipe()


def _point_seg_dist(px, py, sx, sy, ex, ey):
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-18:
        return math.hypot(px - sx, py - sy)
    t = ((px - sx) * dx + (py - sy) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def _segment_block(sx, sy, ex, ey, width, layer, uuid, net):
    return (
        "\t(segment\n"
        f"\t\t(start {sx} {sy})\n"
        f"\t\t(end {ex} {ey})\n"
        f"\t\t(width {width})\n"
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uuid}")\n'
        f"\t\t(net {net})\n"
        "\t)\n"
    )


def _via_block(x, y, size, uuid, net):
    return (
        "\t(via\n"
        f"\t\t(at {x} {y})\n"
        f"\t\t(size {size})\n"
        "\t\t(drill 0.2)\n"
        '\t\t(layers "F.Cu" "B.Cu")\n'
        f'\t\t(uuid "{uuid}")\n'
        f"\t\t(net {net})\n"
        "\t)\n"
    )


def _write_pcb(tmp_path, body):
    pcb = tmp_path / "mini.kicad_pcb"
    pcb.write_text("(kicad_pcb\n" + body + ")\n")
    return pcb


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


class TestDiagonalInteriorDetour:
    """The #3516 defect geometry: 45-degree diagonal passing a foreign
    via barrel near its interior."""

    def test_detour_clears_via_and_preserves_endpoints(self, recipe, tmp_path):
        # Exact geometry from the issue: I_SENSE_OUT diagonal vs GND via.
        body = (
            _segment_block(196.9, 158.7, 191.8, 163.8, 0.2, "In2.Cu",
                           "9734f61f-0000-0000-0000-000000000001", 14)
            + _via_block(196, 160, 0.45,
                         "c192663e-0000-0000-0000-000000000002", 3)
        )
        pcb = _write_pcb(tmp_path, body)
        fixed = recipe._repair_segment_via_clearance(pcb)
        assert fixed == 1

        segs, vias = _parse(pcb)
        assert len(segs) == 3, "interior conflict must become a 3-leg detour"
        _assert_all_45(segs)
        _assert_no_barrel_conflicts(segs, vias)

        # Original endpoints must survive (connectivity by construction).
        endpoints = {(s[0], s[1]) for s in segs} | {(s[2], s[3]) for s in segs}
        assert (196.9, 158.7) in endpoints
        assert (191.8, 163.8) in endpoints

        # The three legs must chain start-to-end.
        assert (segs[0][2], segs[0][3]) == (segs[1][0], segs[1][1])
        assert (segs[1][2], segs[1][3]) == (segs[2][0], segs[2][1])

    def test_repair_is_idempotent(self, recipe, tmp_path):
        body = (
            _segment_block(196.9, 158.7, 191.8, 163.8, 0.2, "In2.Cu",
                           "9734f61f-0000-0000-0000-000000000001", 14)
            + _via_block(196, 160, 0.45,
                         "c192663e-0000-0000-0000-000000000002", 3)
        )
        pcb = _write_pcb(tmp_path, body)
        assert recipe._repair_segment_via_clearance(pcb) == 1
        assert recipe._repair_segment_via_clearance(pcb) == 0


class TestAxisAlignedShift:
    """Original #3481 behavior must be preserved."""

    def test_horizontal_segment_shifted_clear(self, recipe, tmp_path):
        # The FUSED_LINE dogleg leg vs the AC_NEUTRAL via barrel.
        body = (
            _segment_block(135.8492, 114.5508, 140.0508, 114.5508, 0.4,
                           "In1.Cu", "a616c3ab-0000-0000-0000-000000000001", 21)
            + _via_block(135.9, 114.1, 0.6,
                         "0ddbe986-0000-0000-0000-000000000002", 2)
        )
        pcb = _write_pcb(tmp_path, body)
        fixed = recipe._repair_segment_via_clearance(pcb)
        assert fixed == 1
        segs, vias = _parse(pcb)
        _assert_no_barrel_conflicts(segs, vias)
        # Still a single horizontal segment, shifted away (+y).
        assert len(segs) == 1
        assert segs[0][1] == segs[0][3]
        assert segs[0][1] > 114.5508

    def test_same_net_via_untouched(self, recipe, tmp_path):
        body = (
            _segment_block(135.8492, 114.5508, 140.0508, 114.5508, 0.4,
                           "In1.Cu", "a616c3ab-0000-0000-0000-000000000001", 21)
            + _via_block(135.9, 114.1, 0.6,
                         "0ddbe986-0000-0000-0000-000000000002", 21)
        )
        pcb = _write_pcb(tmp_path, body)
        before = pcb.read_text()
        assert recipe._repair_segment_via_clearance(pcb) == 0
        assert pcb.read_text() == before


class TestDiagonalEndpointDrag:
    def test_endpoint_dragged_clear(self, recipe, tmp_path):
        # Diagonal whose END lands beside a foreign via barrel (closest
        # point at the endpoint, no room for a detour jog past it).
        body = (
            _segment_block(135, 115.4, 135.8492, 114.5508, 0.4, "In1.Cu",
                           "2171ea18-0000-0000-0000-000000000001", 21)
            + _via_block(135.9, 114.1, 0.6,
                         "0ddbe986-0000-0000-0000-000000000002", 2)
        )
        pcb = _write_pcb(tmp_path, body)
        fixed = recipe._repair_segment_via_clearance(pcb)
        assert fixed >= 1
        segs, vias = _parse(pcb)
        _assert_no_barrel_conflicts(segs, vias)
        # The fixed start endpoint survives.
        assert any((s[0], s[1]) == (135.0, 115.4) for s in segs)


@pytest.mark.skipif(not ARTIFACT.exists(), reason="committed artifact missing")
class TestCommittedArtifact:
    """All-layer barrel scan of the committed softstart artifact — the
    #3487-rule invariant, standalone so it holds before PR #3517 merges."""

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
        from collections import Counter

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


class TestRepairQuantizeFixpoint:
    """The steps-10d/10e interleaving: a quantization dogleg can bulge
    onto a foreign via barrel (how the FUSED_LINE pair shipped on the
    #3516 artifact), and a clearance repair can drag endpoints
    off-angle.  The pipeline iterates both passes to a fixpoint; this
    pins that the loop converges within the pipeline's 5-pass budget
    and that the converged file is conflict-free, 45-aligned, and free
    of duplicate uuids."""

    def test_converges_with_clean_invariants(self, recipe, tmp_path):
        from kicad_tools.router.quantize import quantize_pcb_file

        # Off-angle segment whose default dogleg's axis leg (y=1) runs
        # 0.1 mm from a foreign via barrel -> quantize creates the
        # conflict, repair shifts the leg, dragging the shared dogleg
        # corner -> quantize again. Mirrors the committed-artifact
        # failure shape.
        body = (
            _segment_block(0, 0, 10, 1, 0.4, "In1.Cu",
                           "2171ea18-0000-0000-0000-00000000000a", 21)
            + _via_block(5, 0.9, 0.6,
                         "0ddbe986-0000-0000-0000-00000000000b", 2)
        )
        pcb = _write_pcb(tmp_path, body)
        for _pass in range(5):  # same budget as the pipeline
            shifted = recipe._repair_segment_via_clearance(pcb)
            quantized = quantize_pcb_file(pcb)
            if not shifted and not quantized:
                break
        else:
            pytest.fail("repair/quantize did not converge in 5 passes")

        segs, vias = _parse(pcb)
        _assert_no_barrel_conflicts(segs, vias)
        _assert_all_45(segs)

        # The chord start survives the loop; the far terminal is
        # legitimately dragged by the axis-shift strategy (referencing
        # endpoints follow the shifted leg), so only continuity is
        # asserted: every leg chains to another leg or a chord end.
        endpoints = {(s[0], s[1]) for s in segs} | {(s[2], s[3]) for s in segs}
        assert (0.0, 0.0) in endpoints
        point_degree = {}
        for s in segs:
            for p in ((s[0], s[1]), (s[2], s[3])):
                point_degree[p] = point_degree.get(p, 0) + 1
        dangling = [p for p, d in point_degree.items() if d == 1]
        assert len(dangling) == 2, f"trace broken: open ends {dangling}"

        # No duplicate uuids even though doglegged legs were
        # re-quantized while their siblings survived (PR #3557 fix).
        uuids = re.findall(r'\(uuid "([^"]+)"\)', pcb.read_text())
        assert len(uuids) == len(set(uuids)), f"duplicate uuid in {uuids}"
