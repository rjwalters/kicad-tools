"""Crossing-tail API, emitted geometry, ranking and deadline controls."""
import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Segment
from tests.test_diffpair_shadow import _crossing_router, _CrossingPathfinder, _tail_pads


def test_crossing_tail_checks_aligned_inner_legs_against_partner():
    from kicad_tools.router.quantize import is_45_aligned

    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 6.0))
    grid = dpr.autorouter.grid
    inner = Layer(grid.index_to_layer(next(li for li in grid.get_routable_indices() if li != 0)))
    partner = Segment(
        x1=6.0,
        y1=5.9,
        x2=6.0,
        y2=6.1,
        width=0.2,
        layer=inner,
        net=2,
        net_name="N",
    )
    tail = dpr._synthesize_crossing_tail(_CrossingPathfinder(), head, goal, 0, [partner])
    assert tail is not None
    # Endpoint barrels clear the partner, but the diagonal-first crossing
    # hits it. The alternate orientation must be checked and selected.
    assert [(v.x, v.y) for v in tail.vias] == [(5.0, 5.0), (8.0, 6.0)]
    assert tail.segments[0].end == (7.0, 5.0)
    for seg in tail.segments:
        assert is_45_aligned(seg.x2 - seg.x1, seg.y2 - seg.y1)
        assert dpr._min_distance_to_partner(
            seg.x1, seg.y1, seg.x2, seg.y2, [partner], seg.layer
        ) >= dpr._pair_seg_clearance(_CrossingPathfinder(), head.net_name)

def test_crossing_barrels_use_via_clearance_for_uncommitted_partner():
    dpr = _crossing_router()
    rules = dpr.autorouter.rules
    rules.trace_clearance = 0.1
    rules.via_clearance = 0.25
    rules.via_diameter = 0.6
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = Segment(
        x1=4.9,
        y1=5.6,
        x2=5.1,
        y2=5.6,
        width=0.2,
        layer=Layer.F_CU,
        net=2,
        net_name="N",
    )
    tail = dpr._synthesize_crossing_tail(_CrossingPathfinder(), head, goal, 0, [partner])
    assert tail is not None
    assert (tail.vias[0].x, tail.vias[0].y) != (5.0, 5.0)
    for via in tail.vias:
        gap = dpr._min_distance_to_partner(via.x, via.y, via.x, via.y, [partner], None)
        assert gap - (via.diameter + partner.width) / 2 >= rules.via_clearance - 1e-9

def test_crossing_ranking_improves_assembled_coupling():
    import time

    from kicad_tools.router.diffpair_routing import _spans_coupled_fraction

    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    partner = Segment(x1=5, y1=5.6, x2=8, y2=5.6, width=0.2, layer=Layer.F_CU, net=2)
    baseline = dpr._synthesize_crossing_tail(_CrossingPathfinder(), head, goal, 0, [partner])
    ranked = dpr._synthesize_crossing_tail(
        _CrossingPathfinder(),
        head,
        goal,
        0,
        [partner],
        body_segments=[],
        deadline=time.monotonic() + 5,
    )
    assert baseline is not None and ranked is not None

    def score(route):
        fractions = []
        for segments, other in ((route.segments, [partner]), ([partner], route.segments)):
            lengths = [math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segments]
            fractions.append(
                sum(
                    length
                    * _spans_coupled_fraction([(s.x1, s.y1, s.x2, s.y2)], s.width, s.layer, other)
                    for s, length in zip(segments, lengths, strict=True)
                )
                / sum(lengths)
            )
        return min(fractions)

    assert score(ranked) > score(baseline)
    assert dpr._route_pad_violation(ranked)[0] <= 1e-9
    assert dpr._route_via_clear(ranked)

def test_crossing_ranking_deadline_returns_validated_best_without_census_credit(monkeypatch):
    import kicad_tools.router.diffpair_routing as module

    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "_CROSSTAIL_CENSUS", True)
    original = module._spans_coupled_fraction

    def score(*args, **kwargs):
        now[0] = 2.0
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_spans_coupled_fraction", score)
    before = dpr._census_elapsed_s
    tail = dpr._synthesize_crossing_tail(
        _CrossingPathfinder(),
        head,
        goal,
        0,
        [],
        body_segments=[],
        deadline=1.0,
    )
    assert tail is not None and len(tail.vias) == 2
    assert dpr._census_elapsed_s == before
    assert (
        dpr._synthesize_crossing_tail(
            _CrossingPathfinder(),
            head,
            goal,
            0,
            [],
            body_segments=[],
            deadline=1.0,
        )
        is None
    )


def test_partial_recovery_calls_real_crossing_tail_with_body_and_deadline(monkeypatch):
    import time
    from types import SimpleNamespace

    from kicad_tools.router import partial_recovery
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.diffpair_routing import CoupledPathfinder
    from kicad_tools.router.primitives import Pad
    from kicad_tools.router.rules import DesignRules, NetClassRouting

    auto = Autorouter(
        width=20, height=10, force_python=True, rules=DesignRules(grid_resolution=0.1)
    )
    klass = NetClassRouting(name="pair", coupled_routing=True, length_critical=True)
    auto.net_class_map = {"P": klass, "N": klass}
    pads = tuple(
        Pad(x=x, y=y, width=0.2, height=0.2, layer=Layer.F_CU, net=net, net_name=name)
        for net, name, y in ((1, "P", 4), (2, "N", 6))
        for x in (2, 8)
    )
    finder = CoupledPathfinder(auto.grid, auto.rules, 20, net_class_map=auto.net_class_map)
    finder._cpp_reconstruct_pads = pads
    finder.last_best_cpp_path = [
        (*auto.grid.world_to_grid(x, 4), 0, *auto.grid.world_to_grid(x, 6), 0, False)
        for x in (2, 3)
    ]
    deadline = time.monotonic() + 5
    observed = []
    original = auto._diffpair._synthesize_crossing_tail

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append((kwargs, result))
        return result

    monkeypatch.setattr(auto._diffpair, "_synthesize_crossing_tail", observe)
    # Stop after the actual caller and crossing implementation; balancing is
    # independently tested and must not manufacture an accepted pair here.
    monkeypatch.setattr(partial_recovery, "_balance_crossing", lambda *args: iter(()))
    assert partial_recovery.recover_partial_pair(
        auto._diffpair, finder, SimpleNamespace(), pads,
        deadline=deadline, board_thickness_mm=1.6, num_copper_layers=2,
    ) is None
    assert observed
    assert any(tail is not None for _, tail in observed)
    for kwargs, tail in observed:
        assert kwargs["body_segments"]
        assert kwargs["deadline"] == deadline
        if tail is not None:
            assert len(tail.vias) == 2
            assert auto._diffpair._route_pad_violation(tail)[0] <= 1e-9
            assert auto._diffpair._route_via_clear(tail)
    assert finder._cpp_reconstruct_pads is pads
    assert not auto.routes and not auto.grid.routes
