"""Actual copper, rather than a conservative raster halo, decides the escape."""

import time

import numpy as np
import pytest

from kicad_tools.router import geometric_departure
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from tests.test_coupled_layer_transition import fixture


def setup(monkeypatch, distance=0.635):
    auto, finder, pair, pads = fixture()
    departure = next(
        geometric_departure.geometric_departures(
            auto._diffpair,
            finder,
            pads,
            deadline=time.monotonic() + 5,
        )
    )
    monkeypatch.setattr(
        geometric_departure, "departure_proposals", lambda *args: iter([departure.proposal])
    )
    via = departure.p_route.vias[0]
    foreign = Route(
        net=99,
        net_name="FOREIGN",
        segments=[
            Segment(
                x1=via.x + distance,
                y1=0,
                x2=via.x + distance,
                y2=15,
                width=0.225,
                layer=Layer.B_CU,
                net=99,
            )
        ],
    )
    auto.grid.mark_route(foreign)
    auto.routes.append(foreign)
    return auto, finder, pads, departure


def proposals(auto, finder, pads, **kwargs):
    return list(
        geometric_departure.geometric_departures(
            auto._diffpair,
            finder,
            pads,
            deadline=time.monotonic() + 5,
            **kwargs,
        )
    )


@pytest.mark.skipif(not is_cpp_available(), reason="requires matching native backend")
def test_native_halo_rejection_can_have_a_physically_clear_escape(monkeypatch):
    auto, finder, pads, departure = setup(monkeypatch)
    prefix = departure.proposal.prefix
    finder.route_coupled(
        *pads,
        departure_prefix=list(prefix),
        timeout_seconds=2,
        max_iterations_budget=len(prefix) + 6,
    )
    assert len(finder.last_validated_departure_path) != len(prefix) + 1
    assert finder.last_rejections["via_blocked_p"] > 0
    blocked, nets = auto.grid._blocked.copy(), auto.grid._net.copy()
    routes = tuple(auto.routes), tuple(auto.grid.routes)
    accepted = proposals(auto, finder, pads)
    assert len(accepted) == 1
    assert accepted[0].proposal == departure.proposal
    assert accepted[0].native_validated is False
    assert accepted[0].iterations == 0
    np.testing.assert_array_equal(auto.grid._blocked, blocked)
    np.testing.assert_array_equal(auto.grid._net, nets)
    assert (tuple(auto.routes), tuple(auto.grid.routes)) == routes


def test_real_copper_collision_remains_rejected(monkeypatch):
    auto, finder, pads, _ = setup(monkeypatch, distance=0.5)
    assert proposals(auto, finder, pads) == []


def test_unknown_keepout_over_known_halo_remains_rejected(monkeypatch):
    auto, finder, pads, departure = setup(monkeypatch)
    assert proposals(auto, finder, pads)
    via = departure.p_route.vias[0]
    x, y = auto.grid.world_to_grid(via.x, via.y)
    # CellView's public mutation invalidates route provenance even though
    # the cell already lies inside a known live route's inflated halo.
    auto.grid.cell_at(3, y, x + 2).blocked = True
    assert proposals(auto, finder, pads) == []


def test_expired_geometry_budget_does_not_enumerate_or_mutate(monkeypatch):
    auto, finder, _, pads = fixture()
    monkeypatch.setattr(
        geometric_departure, "departure_proposals", lambda *args: pytest.fail("expired enumeration")
    )
    blocked = auto.grid._blocked.copy()
    assert (
        list(geometric_departure.geometric_departures(auto._diffpair, finder, pads, deadline=0))
        == []
    )
    np.testing.assert_array_equal(auto.grid._blocked, blocked)
    assert not auto.routes and not auto.grid.routes
