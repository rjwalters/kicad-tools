"""Tests for the 45-degree quantization helpers (Issue #3532).

Covers:

* the pure-geometry helpers (``off_angle_degrees``, ``is_45_aligned``,
  ``snap_direction_8``, ``dogleg_points``);
* the file-level pass ``quantize_pcb_file`` (exactness, endpoint
  preservation, idempotency, uuid determinism, both s-expression field
  orders);
* the optimizer emitters that used to skew copper off the 45-degree
  set (``convert_corners_45`` terminal restoration, ``pull_tight_pass``
  neighbour re-aiming);
* the Python pathfinder pad-tail emitter (``_path_to_segments``).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from kicad_tools.router.optimizer.algorithms import (
    _restore_terminal_endpoints,
    pull_tight_pass,
)
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.primitives import Segment
from kicad_tools.router.quantize import (
    ANGLE_TOL_DEG,
    dogleg_points,
    is_45_aligned,
    off_angle_degrees,
    quantize_pcb_file,
    segment_angle_census,
    snap_direction_8,
)


def _seg(x1, y1, x2, y2, **kw):
    kw.setdefault("width", 0.2)
    kw.setdefault("layer", "F.Cu")
    kw.setdefault("net", 1)
    kw.setdefault("net_name", "N1")
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, **kw)


def _all_aligned(segments, tol=ANGLE_TOL_DEG):
    return all(is_45_aligned(s.x2 - s.x1, s.y2 - s.y1, tol) for s in segments)


class TestGeometryHelpers:
    @pytest.mark.parametrize(
        "dx,dy",
        [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1), (0, 0)],
    )
    def test_aligned_directions(self, dx, dy):
        assert is_45_aligned(dx, dy)
        assert off_angle_degrees(dx, dy) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.parametrize(
        "dx,dy,expected_off",
        [
            (1.0, 0.05, math.degrees(math.atan2(0.05, 1.0))),
            (0.79, -0.04, math.degrees(math.atan2(0.04, 0.79))),
            (1.0, 0.5, 45 - math.degrees(math.atan2(0.5, 1.0))),
        ],
    )
    def test_off_angle(self, dx, dy, expected_off):
        assert not is_45_aligned(dx, dy)
        assert off_angle_degrees(dx, dy) == pytest.approx(expected_off, abs=1e-9)

    def test_snap_direction_8(self):
        d = math.sqrt(0.5)
        assert snap_direction_8(1.0, 0.01) == (1.0, 0.0)
        assert snap_direction_8(1.0, 0.9) == (d, d)
        assert snap_direction_8(-0.01, -2.0) == (0.0, -1.0)
        assert snap_direction_8(-1.0, 1.1) == (-d, d)

    def test_dogleg_aligned_passthrough(self):
        assert dogleg_points(0, 0, 3, 3) == [(0, 0), (3, 3)]
        assert dogleg_points(1, 2, 1, 9) == [(1, 2), (1, 9)]

    @pytest.mark.parametrize("axis_first", [False, True])
    @pytest.mark.parametrize(
        "x1,y1,x2,y2",
        [
            (170.4, 125.5, 171.19, 125.46),  # near-horizontal pad tail
            (105.46, 142.0, 105.8157, 144.0843),  # near-vertical
            (181.0, 196.0, 181.76, 196.32),  # ~23 deg
            (240.3252, 110.5748, 192.1, 109.9),  # long skewed run
        ],
    )
    def test_dogleg_exact_legs(self, axis_first, x1, y1, x2, y2):
        points = dogleg_points(x1, y1, x2, y2, axis_first=axis_first)
        assert len(points) == 3
        assert points[0] == (x1, y1)
        assert points[-1] == (x2, y2)
        for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
            dx, dy = bx - ax, by - ay
            # The mid vertex shares one coordinate with an endpoint so
            # the axis leg is exact; the diagonal leg is aligned to
            # float64 rounding (residual < 1e-6 degrees, far below
            # ANGLE_TOL_DEG = 0.01).
            assert off_angle_degrees(dx, dy) < 1e-6

    def test_dogleg_variants_bulge_opposite_sides(self):
        diag = dogleg_points(0, 0, 4, 1, axis_first=False)
        axis = dogleg_points(0, 0, 4, 1, axis_first=True)
        assert diag[1] != axis[1]
        # diag-first: diagonal leaves the start -> mid above the chord;
        # axis-first: horizontal leaves the start -> mid below the chord.
        assert diag[1] == (1.0, 1.0)
        assert axis[1] == (3.0, 0.0)


SEG_BLOCK_UUID_BEFORE_NET = """\t(segment
\t\t(start 170.4 125.5)
\t\t(end 171.19 125.46)
\t\t(width 0.4)
\t\t(layer "F.Cu")
\t\t(uuid "00000000-0000-0000-0000-000000000001")
\t\t(net 1)
\t)
"""

SEG_BLOCK_NET_BEFORE_UUID = """\t(segment
\t\t(start 223.9 158.95)
\t\t(end 230.54 158.54)
\t\t(width 0.2)
\t\t(layer "B.Cu")
\t\t(net 15)
\t\t(uuid "00000000-0000-0000-0000-000000000002")
\t)
"""

SEG_BLOCK_ALIGNED = """\t(segment
\t\t(start 10 10)
\t\t(end 20 20)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(uuid "00000000-0000-0000-0000-000000000003")
\t\t(net 2)
\t)
"""


class TestQuantizePcbFile:
    def _board(self, tmp_path: Path, *blocks: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb\n" + "".join(blocks) + ")\n")
        return pcb

    def test_quantizes_both_field_orders(self, tmp_path):
        pcb = self._board(
            tmp_path,
            SEG_BLOCK_UUID_BEFORE_NET,
            SEG_BLOCK_NET_BEFORE_UUID,
            SEG_BLOCK_ALIGNED,
        )
        replaced = quantize_pcb_file(pcb)
        assert len(replaced) == 2
        total, bad = segment_angle_census(pcb)
        assert total == 5  # two doglegged (2 legs each) + 1 untouched
        assert bad == []

    def test_endpoints_preserved(self, tmp_path):
        pcb = self._board(tmp_path, SEG_BLOCK_UUID_BEFORE_NET)
        quantize_pcb_file(pcb)
        text = pcb.read_text()
        assert "(start 170.4 125.5)" in text
        assert "(end 171.19 125.46)" in text
        # The two legs share the intermediate vertex.
        assert text.count("(segment") == 2

    def test_idempotent_and_deterministic(self, tmp_path):
        pcb = self._board(tmp_path, SEG_BLOCK_UUID_BEFORE_NET, SEG_BLOCK_NET_BEFORE_UUID)
        quantize_pcb_file(pcb)
        first = pcb.read_text()
        assert quantize_pcb_file(pcb) == []
        assert pcb.read_text() == first

        # Determinism: a fresh copy quantizes to byte-identical output
        # (second-leg uuids are uuid5 of the original, not random).
        other = tmp_path / "copy.kicad_pcb"
        other.write_text(
            "(kicad_pcb\n" + SEG_BLOCK_UUID_BEFORE_NET + SEG_BLOCK_NET_BEFORE_UUID + ")\n"
        )
        quantize_pcb_file(other)
        assert other.read_text() == first

    def test_dry_run_reports_without_writing(self, tmp_path):
        pcb = self._board(tmp_path, SEG_BLOCK_UUID_BEFORE_NET)
        before = pcb.read_text()
        replaced = quantize_pcb_file(pcb, dry_run=True)
        assert replaced == ["00000000-0000-0000-0000-000000000001"]
        assert pcb.read_text() == before

    def test_dogleg_uuid_collision_proof(self, tmp_path):
        """Re-quantizing a previously-quantized parent must not duplicate
        its surviving sibling leg's uuid (PR #3557 judge finding).

        The repair->quantize fixpoint loop can drag a previously-emitted
        first leg off-angle while the old second leg (carrying
        ``uuid5(parent + ":dogleg")``) is still in the file.  The
        derivation must then deterministically extend the suffix
        (``:dogleg:2``) instead of emitting the same uuid twice.
        """
        import uuid as _uuid

        parent = "00000000-0000-0000-0000-000000000001"
        base = str(_uuid.uuid5(_uuid.NAMESPACE_OID, parent + ":dogleg"))
        bumped = str(_uuid.uuid5(_uuid.NAMESPACE_OID, parent + ":dogleg:2"))
        stale_sibling = (
            "\t(segment\n"
            "\t\t(start 171.19 125.46)\n"
            "\t\t(end 175 125.46)\n"
            "\t\t(width 0.4)\n"
            '\t\t(layer "F.Cu")\n'
            f'\t\t(uuid "{base}")\n'
            "\t\t(net 1)\n"
            "\t)\n"
        )
        pcb = self._board(tmp_path, SEG_BLOCK_UUID_BEFORE_NET, stale_sibling)
        assert quantize_pcb_file(pcb) == [parent]
        text = pcb.read_text()
        uuids = re.findall(r'\(uuid "([^"]+)"\)', text)
        assert len(uuids) == len(set(uuids)), "duplicate uuid emitted"
        assert uuids.count(base) == 1  # the stale sibling, untouched
        assert bumped in uuids  # new second leg got the :dogleg:2 uuid
        _, bad = segment_angle_census(pcb)
        assert bad == []

        # Determinism survives the bump: a fresh copy quantizes to
        # byte-identical output.
        other = tmp_path / "copy.kicad_pcb"
        other.write_text("(kicad_pcb\n" + SEG_BLOCK_UUID_BEFORE_NET + stale_sibling + ")\n")
        quantize_pcb_file(other)
        assert other.read_text() == text

    def test_axis_first_flip(self, tmp_path):
        pcb_a = self._board(tmp_path, SEG_BLOCK_UUID_BEFORE_NET)
        quantize_pcb_file(pcb_a)
        pcb_b = tmp_path / "flipped.kicad_pcb"
        pcb_b.write_text("(kicad_pcb\n" + SEG_BLOCK_UUID_BEFORE_NET + ")\n")
        quantize_pcb_file(
            pcb_b,
            axis_first_uuids={"00000000-0000-0000-0000-000000000001"},
        )
        assert pcb_a.read_text() != pcb_b.read_text()
        for p in (pcb_a, pcb_b):
            _, bad = segment_angle_census(p)
            assert bad == []


class TestOptimizerEmitters:
    def test_restore_terminal_endpoints_emits_dogleg(self):
        # Chamfered chain whose original start was an off-grid pad
        # centre 0.79 right / 0.04 up of the current chain head.
        chain = [_seg(10.0, 10.0, 15.0, 10.0), _seg(15.0, 10.0, 15.0, 20.0)]
        restored = _restore_terminal_endpoints(
            chain,
            orig_start=(9.21, 9.96),
            orig_end=(15.0, 20.0),
            pad_positions=None,
            tolerance=1e-4,
        )
        assert restored[0].x1 == 9.21 and restored[0].y1 == 9.96
        assert _all_aligned(restored)
        # Chain continuity preserved.
        for a, b in zip(restored, restored[1:], strict=False):
            assert (a.x2, a.y2) == (b.x1, b.y1)

    def test_restore_aligned_endpoint_stays_single_segment(self):
        chain = [_seg(10.0, 10.0, 15.0, 10.0)]
        restored = _restore_terminal_endpoints(
            chain,
            orig_start=(8.0, 10.0),
            orig_end=(15.0, 10.0),
            pad_positions=None,
            tolerance=1e-4,
        )
        assert len(restored) == 1
        assert restored[0].x1 == 8.0

    def test_pull_tight_rectilinear_jog_still_compresses(self):
        # Classic H-V-H jog: prev/nxt run parallel to the translation
        # direction, so pull-tight must still shorten the chain.
        chain = [
            _seg(0.0, 0.0, 5.0, 0.0),
            _seg(5.0, 0.0, 5.0, 1.0),
            _seg(5.0, 1.0, 10.0, 1.0),
        ]
        result = pull_tight_pass(chain, OptimizationConfig())
        assert _all_aligned(result)
        total = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in result)
        original = 5.0 + 1.0 + 5.0
        assert total <= original

    def test_pull_tight_never_emits_off_angle(self):
        # Diagonal-vertical-diagonal window: translating the middle
        # segment would skew the diagonal neighbours off the 45 set.
        # The #3532 guard must skip the move (geometry unchanged or
        # still aligned -- never skewed).
        chain = [
            _seg(0.0, 0.0, 2.0, 2.0),
            _seg(2.0, 2.0, 2.0, 6.0),
            _seg(2.0, 6.0, 5.0, 9.0),
        ]
        result = pull_tight_pass(chain, OptimizationConfig())
        assert _all_aligned(result)


class TestPathfinderPadTails:
    def test_convert_path_to_route_quantizes_pad_tails(self):
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.pathfinder import Router
        from kicad_tools.router.primitives import Pad, Route
        from kicad_tools.router.rules import DesignRules

        rules = DesignRules()
        grid = RoutingGrid(width=20.0, height=20.0, rules=rules)
        router = Router(grid=grid, rules=rules)
        # Off-grid pads: centres do not land on the grid.
        start_pad = Pad(
            x=2.13,
            y=2.07,
            width=1.0,
            height=1.0,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U1",
        )
        end_pad = Pad(
            x=10.86,
            y=6.44,
            width=1.0,
            height=1.0,
            net=1,
            net_name="N1",
            layer=Layer.F_CU,
            ref="U2",
        )
        # Grid-aligned A* path (all 8-direction steps).
        path = [
            (2.0, 2.0, 0, False),
            (6.0, 2.0, 0, False),
            (10.0, 6.0, 0, False),
            (11.0, 6.0, 0, False),
        ]
        route = Route(net=1, net_name="N1")
        router._convert_path_to_route(path, route, start_pad, end_pad)

        assert route.segments, "expected segments to be emitted"
        for seg in route.segments:
            assert is_45_aligned(seg.x2 - seg.x1, seg.y2 - seg.y1), (
                f"off-angle segment ({seg.x1}, {seg.y1}) -> ({seg.x2}, {seg.y2})"
            )
        # Terminal connectivity to the exact pad centres is preserved.
        assert (route.segments[0].x1, route.segments[0].y1) == (2.13, 2.07)
        assert (route.segments[-1].x2, route.segments[-1].y2) == (10.86, 6.44)
        # Chain continuity.
        for a, b in zip(route.segments, route.segments[1:], strict=False):
            assert (a.x2, a.y2) == (b.x1, b.y1)
