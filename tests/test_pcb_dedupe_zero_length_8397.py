"""Zero-length copper segments are never emitted and ``pcb dedupe`` removes them (#8397)."""

from __future__ import annotations

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.consolidate import consolidate_net_routes
from kicad_tools.router.optimizer.pcb import dedupe_segments
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.schema.pcb import PCB


def _seg(x1, y1, x2, y2, net=1):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net)


class TestDedupeCopperZeroLength:
    def test_dedupe_copper_removes_zero_length(self, tmp_path):
        pcb = PCB.create(width=100, height=100)
        pcb.add_trace(start=(10.0, 10.0), end=(20.0, 10.0), width=0.25, layer="F.Cu", net="S")
        pcb.add_trace(
            start=(20.0, 10.0), end=(20.0, 10.0), width=0.25, layer="F.Cu", net="S", dedupe=False
        )
        # sub-micron "length" also rounds to zero
        pcb.add_trace(
            start=(30.0, 10.0),
            end=(30.0000001, 10.0),
            width=0.25,
            layer="F.Cu",
            net="S",
            dedupe=False,
        )
        assert len(pcb.segments) == 3
        stats = pcb.dedupe_copper()
        assert stats["segments"] == 2
        assert len(pcb.segments) == 1

        out = tmp_path / "z.kicad_pcb"
        pcb.save(out)
        assert len(PCB.load(out).segments) == 1


class TestRouterWriters:
    def test_dedupe_segments_drops_zero_length(self):
        kept, removed = dedupe_segments([_seg(0, 0, 1, 0), _seg(1, 0, 1, 0)])
        assert len(kept) == 1
        assert removed == 1

    def test_consolidate_drops_zero_length_and_keeps_connectivity(self):
        route = Route(
            net=1,
            net_name="N",
            segments=[_seg(0, 0, 1, 0), _seg(1, 0, 1, 0), _seg(1, 0, 1, 1)],
            vias=[],
        )
        out, stats = consolidate_net_routes([route])
        segs = [s for r in out for s in r.segments]
        assert len(segs) == 2
        assert all((s.x1, s.y1) != (s.x2, s.y2) for s in segs)
        assert stats.segments_before == 3

    def test_lone_zero_length_route_is_kept(self):
        route = Route(net=1, net_name="N", segments=[_seg(1, 1, 1, 1)], vias=[])
        out, _ = consolidate_net_routes([route])
        assert len(out[0].segments) == 1

    def test_fine_grid_run_has_no_zero_length_and_merges(self):
        pitch = 0.04
        segs = [_seg(i * pitch, 0, (i + 1) * pitch, 0) for i in range(100)]
        segs.insert(50, _seg(50 * pitch, 0, 50 * pitch, 0))
        route = Route(net=1, net_name="N", segments=segs, vias=[])
        out, _ = consolidate_net_routes([route])
        res = [s for r in out for s in r.segments]
        assert all((s.x1, s.y1) != (s.x2, s.y2) for s in res)
        routed_mm = 100 * pitch
        short = [s for s in res if ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 < 2 * pitch]
        assert len(short) / routed_mm <= 1.0
