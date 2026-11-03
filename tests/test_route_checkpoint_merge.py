"""Checkpoint serialisation collapses per-cell segments (Issue #8270)."""

from kicad_tools.cli.route_cmd import _merge_checkpoint_routes
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def _seg(x1, x2, width=0.2, y=1.0):
    return Segment(x1=x1, y1=y, x2=x2, y2=y, width=width, layer=Layer.F_CU, net=1, net_name="N")


def test_straight_run_becomes_one_segment_without_mutating_input():
    segs = [_seg(i * 0.05, (i + 1) * 0.05) for i in range(100)]
    route = Route(net=1, net_name="N", segments=segs)
    merged = _merge_checkpoint_routes([route])
    assert len(merged[0].segments) == 1
    seg = merged[0].segments[0]
    assert (seg.x1, seg.x2) == (0.0, segs[-1].x2)
    assert len(route.segments) == 100  # live routes untouched


def test_width_change_is_not_merged():
    route = Route(net=1, net_name="N", segments=[_seg(0, 1), _seg(1, 2, width=0.3)])
    assert len(_merge_checkpoint_routes([route])[0].segments) == 2


def test_failure_falls_back_to_raw(monkeypatch):
    import kicad_tools.router.optimizer.merge as m

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(m, "merge_collinear_same_width", boom)
    routes = [Route(net=1, net_name="N", segments=[_seg(0, 1), _seg(1, 2)])]
    assert _merge_checkpoint_routes(routes) is routes


# --- Issue #8270 remaining ACs: pad-centre join, _partial merge, stub listing ---
from types import SimpleNamespace  # noqa: E402

from kicad_tools.cli.route_cmd import _partial_net_stubs  # noqa: E402
from kicad_tools.router.primitives import Pad  # noqa: E402


def _router(routes, pads):
    return SimpleNamespace(
        routes=routes,
        pads={(p.ref, p.pin): p for p in pads},
        grid=SimpleNamespace(resolution=0.1),
        rules=SimpleNamespace(trace_clearance=0.2),
    )


def _pad(x, y, net=1, ref="U1", pin="1"):
    return Pad(x=x, y=y, width=0.5, height=0.5, net=net, net_name="N", ref=ref, pin=pin)


def test_off_grid_pad_end_joins_pad_centre_without_jog():
    pad = _pad(1.03, 1.02)
    jog = Segment(
        x1=1.03, y1=1.02, x2=1.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1, net_name="N"
    )
    run = _seg(1.0, 5.0)
    route = Route(net=1, net_name="N", segments=[jog, run])
    merged = _merge_checkpoint_routes([route], _router([route], [pad]))
    first = merged[0].segments[0]
    assert (first.x1, first.y1) == (1.03, 1.02)
    assert len(merged[0].segments) == 1  # no sub-grid diagonal left
    assert len(route.segments) == 2  # live route untouched


def test_pad_join_skipped_when_it_violates_clearance():
    pad = _pad(1.03, 1.02)
    jog = Segment(
        x1=1.03, y1=1.02, x2=1.0, y2=1.0, width=0.2, layer=Layer.F_CU, net=1, net_name="N"
    )
    route = Route(net=1, net_name="N", segments=[jog, _seg(1.0, 5.0)])
    foreign = Route(
        net=2,
        net_name="O",
        segments=[
            Segment(
                x1=1.5, y1=1.12, x2=2.0, y2=1.12, width=0.2, layer=Layer.F_CU, net=2, net_name="O"
            )
        ],
    )
    merged = _merge_checkpoint_routes([route, foreign], _router([route, foreign], [pad]))
    assert len(merged[0].segments) == 2  # unchanged


def test_partial_net_stubs_lists_dangling_endpoint():
    pad = _pad(0.0, 1.0)
    done = Route(net=1, net_name="N", segments=[_seg(0.0, 2.0)])
    partial = Route(
        net=2,
        net_name="P",
        segments=[
            Segment(
                x1=0.0, y1=3.0, x2=2.0, y2=3.0, width=0.2, layer=Layer.F_CU, net=2, net_name="P"
            )
        ],
    )
    stubs = _partial_net_stubs(_router([done, partial], [pad, _pad(2.0, 1.0, ref="U2")]))
    assert {(s["net"], s["layer"]) for s in stubs} == {("P", "F_CU")}
    assert len(stubs) == 2


# --- Judge round 1 on PR #8283 ---
from kicad_tools.router.primitives import Via  # noqa: E402


def _s(x1, y1, x2, y2, layer=Layer.F_CU, net=1, name="N"):
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net, net_name=name)


def test_stub_list_ignores_sub_tolerance_joint_split():
    a = _s(0.0, 0.0, 164.6625, 96.5)
    b = _s(164.66250610351562, 96.5, 170.0, 96.5)
    pads = [_pad(0.0, 0.0), _pad(170.0, 96.5, ref="U2")]
    route = Route(net=1, net_name="N", segments=[a, b])
    assert _partial_net_stubs(_router([route], pads)) == []


def test_stub_list_reports_lone_via_end():
    pads = [_pad(0.0, 0.0)]
    route = Route(
        net=1,
        net_name="N",
        segments=[_s(0.0, 0.0, 2.0, 0.0)],
        vias=[
            Via(
                x=2.0,
                y=0.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
                net_name="N",
            )
        ],
    )
    stubs = _partial_net_stubs(_router([route], pads))
    assert [(s["layer"], s["x"], s["y"]) for s in stubs] == [("F_CU", 2.0, 0.0)]


def test_stub_list_via_with_copper_on_other_layer_is_connected():
    th = _pad(2.0, 2.0, ref="U2")
    th.through_hole = True
    pads = [_pad(0.0, 0.0), th]
    route = Route(
        net=1,
        net_name="N",
        segments=[_s(0.0, 0.0, 2.0, 0.0), _s(2.0, 0.0, 2.0, 2.0, layer=Layer.B_CU)],
        vias=[
            Via(
                x=2.0,
                y=0.0,
                drill=0.3,
                diameter=0.6,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
                net_name="N",
            )
        ],
    )
    assert _partial_net_stubs(_router([route], pads)) == []


def test_pad_join_does_not_absorb_reversed_neighbour():
    pad = _pad(1.03, 1.02)
    jog = _s(1.03, 1.02, 1.0, 1.0)
    rev = _s(1.0, 3.0, 1.0, 1.0)  # stored tail to head: does not start at the joint
    route = Route(net=1, net_name="N", segments=[jog, rev])
    merged = _merge_checkpoint_routes([route], _router([route], [pad]))
    segs = merged[0].segments
    assert any((s.x1, s.y1, s.x2, s.y2) == (1.0, 3.0, 1.0, 1.0) for s in segs)  # copper kept
    assert all((s.x1, s.y1) != (s.x2, s.y2) for s in segs)


def test_pad_join_keeps_joint_when_t_branch_ends_there():
    pad = _pad(1.03, 1.02)
    jog = _s(1.03, 1.02, 1.0, 1.0)
    run = _s(1.0, 1.0, 5.0, 1.0)
    branch = Route(net=1, net_name="N", segments=[_s(1.0, 1.0, 1.0, 4.0)])
    route = Route(net=1, net_name="N", segments=[jog, run])
    merged = _merge_checkpoint_routes([route, branch], _router([route, branch], [pad]))
    segs = merged[0].segments
    # Joint (1.0, 1.0) is a junction: it survives, so the branch still meets copper.
    assert any((s.x2, s.y2) == (1.0, 1.0) or (s.x1, s.y1) == (1.0, 1.0) for s in segs)


# --- Judge round 2 on PR #8283 ---
def _via(x, y):
    return Via(
        x=x, y=y, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1, net_name="N"
    )


def test_stub_list_reports_fold_back_via():
    # Two F_CU segments meet at a via with nothing on B_CU: DRC ``via_dangling``.
    pads = [_pad(0.0, 0.0), _pad(4.0, 0.0, ref="U2")]
    route = Route(
        net=1,
        net_name="N",
        segments=[_s(0.0, 0.0, 2.0, 0.0), _s(2.0, 0.0, 4.0, 0.0)],
        vias=[_via(2.0, 0.0)],
    )
    stubs = _partial_net_stubs(_router([route], pads))
    assert [(s["net"], s["layer"], s["x"], s["y"]) for s in stubs] == [("N", "F_CU", 2.0, 0.0)]


def test_stub_list_lone_via_end_listed_once():
    pads = [_pad(0.0, 0.0)]
    route = Route(net=1, net_name="N", segments=[_s(0.0, 0.0, 2.0, 0.0)], vias=[_via(2.0, 0.0)])
    assert len(_partial_net_stubs(_router([route], pads))) == 1


class _SaveRouter:
    """Just enough of an Autorouter for ``_save_partial_results``."""

    def __init__(self, routes, pads):
        self.routes = routes
        self.pads = {(p.ref, p.pin): p for p in pads}
        self.grid = SimpleNamespace(resolution=0.1)
        self.rules = SimpleNamespace(trace_clearance=0.2)
        self.routing_plan = None
        self.commit_journal = None
        self.net_names = {1: "N"}

    def cleanup_artifacts(self):
        pass

    def to_sexp(self, name_only=False):
        return "\n\t".join(r.to_sexp(name_only=name_only) for r in self.routes)

    def get_statistics(self):
        return {"nets_routed": 1, "segments": len(self.routes[0].segments), "vias": 0}


def test_save_partial_results_writes_n_cell_run_as_one_segment(tmp_path):
    import re

    from kicad_tools.cli import route_cmd

    src = tmp_path / "b.kicad_pcb"
    src.write_text('(kicad_pcb\n  (version 20240108)\n  (net 0 "")\n  (net 1 "N")\n)\n')
    out = tmp_path / "out.kicad_pcb"
    segs = [_seg(i * 0.05, (i + 1) * 0.05) for i in range(100)]
    router = _SaveRouter([Route(net=1, net_name="N", segments=segs)], [])
    route_cmd._reset_partial_save_state()
    route_cmd._interrupt_state.update(
        router=router,
        output_path=out,
        pcb_path=src,
        quiet=True,
        interrupted=False,
        best_completed_attempt=False,
        dry_run=False,
    )
    try:
        assert route_cmd._save_partial_results() is True
    finally:
        route_cmd._interrupt_state.update(router=None, output_path=None, pcb_path=None)
        route_cmd._reset_partial_save_state()

    text = (tmp_path / "out_partial.kicad_pcb").read_text()
    assert len(re.findall(r"\(segment\b", text)) == 1
    assert len(router.routes[0].segments) == 100  # live routes untouched
    assert router.routes[0].segments is segs
