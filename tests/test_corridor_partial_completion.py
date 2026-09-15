"""Saved corridor bodies retain attempt identity and the shared construction caps."""

import time
from dataclasses import replace

import pytest

from kicad_tools.router import pair_construction as pc
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from tests.test_coupled_layer_transition import fixture


def setup(monkeypatch):
    auto, finder, pair, pads = fixture()
    grid = finder.grid
    root = (
        *grid.world_to_grid(pads[0].x, pads[0].y),
        0,
        *grid.world_to_grid(pads[2].x, pads[2].y),
        0,
        False,
    )
    end = (
        *grid.world_to_grid(pads[1].x, pads[1].y),
        3,
        *grid.world_to_grid(pads[3].x, pads[3].y),
        3,
        False,
    )
    finder.last_best_cpp_path = [root, end]
    finder._cpp_reconstruct_pads = pads
    routes = tuple(
        Route(
            net=pad.net,
            net_name=pad.net_name,
            segments=[
                Segment(
                    x1=pad.x - 1,
                    y1=pad.y,
                    x2=pad.x,
                    y2=pad.y,
                    width=0.15,
                    layer=Layer.B_CU,
                    net=pad.net,
                )
            ],
        )
        for pad in (pads[1], pads[3])
    )
    monkeypatch.setattr(
        finder, "_reconstruct_coupled_routes_from_cpp_path", lambda *a, **kw: routes
    )
    budget = pc.ConstructionBudget(time.monotonic() + 5, 64, 2)
    return auto, finder, pair, pads, budget, routes


def run(values):
    auto, finder, pair, pads, budget, _ = values
    return pc._complete_corridor_partial(
        auto._diffpair,
        finder,
        pair,
        pads,
        budget,
        deadline=budget.deadline,
        board_thickness_mm=1.6,
        num_copper_layers=4,
    )


@pytest.mark.parametrize("failure", ["rejection", "exception"])
def test_failed_completion_spends_body_and_uses_original_widen_ledger(monkeypatch, failure):
    values = setup(monkeypatch)
    budget = values[4]

    def complete(*args, **kwargs):
        assert kwargs["widen_budget"] is budget.widen_budget
        assert kwargs["deadline"] <= budget.deadline
        kwargs["widen_budget"].remaining -= 1
        kwargs["widen_budget"].spent += 1
        if failure == "exception":
            raise RuntimeError("completion failed")
        return None

    monkeypatch.setattr(pc, "complete_pair_body", complete)
    if failure == "exception":
        with pytest.raises(RuntimeError, match="completion failed"):
            run(values)
    else:
        assert run(values) is None
    assert budget.bodies_remaining == 1 and budget.bodies_used == 1
    assert budget.completions_tried == 1 and budget.widen_budget.spent == 1


@pytest.mark.parametrize(
    "guard", ["expired", "empty", "exhausted", "other_pads", "other_root", "terminal_via"]
)
def test_stale_or_unusable_native_body_cannot_enter_completion(monkeypatch, guard):
    values = setup(monkeypatch)
    _, finder, _, pads, budget, routes = values
    if guard == "expired":
        budget.deadline = 0
    elif guard == "empty":
        finder.last_best_cpp_path = []
    elif guard == "exhausted":
        budget.bodies_remaining = 0
    elif guard == "other_pads":
        finder._cpp_reconstruct_pads = (replace(pads[0], net=99), *pads[1:])
    elif guard == "other_root":
        finder.last_best_cpp_path[0] = (0, 0, 0, 0, 0, 0, False)
    else:
        routes[0].segments[0] = replace(routes[0].segments[0], layer=Layer.F_CU)
    monkeypatch.setattr(
        pc, "complete_pair_body", lambda *a, **kw: pytest.fail("unsafe body entered completion")
    )
    assert run(values) is None
    assert budget.completions_tried == 0
    assert budget.bodies_used == (1 if guard == "terminal_via" else 0)


def test_later_child_deadline_cannot_renew_expired_parent(monkeypatch):
    auto, finder, pair, pads, budget, _ = setup(monkeypatch)
    budget.deadline = 0
    monkeypatch.setattr(pc, "complete_pair_body", lambda *a, **kw: pytest.fail("expired parent"))
    assert (
        pc._complete_corridor_partial(
            auto._diffpair,
            finder,
            pair,
            pads,
            budget,
            deadline=time.monotonic() + 5,
            board_thickness_mm=1.6,
            num_copper_layers=4,
        )
        is None
    )
    assert budget.bodies_used == 0
