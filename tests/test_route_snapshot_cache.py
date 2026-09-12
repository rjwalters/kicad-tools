"""Physical occupancy and congestion must survive cache/best-state replay."""

import copy

import numpy as np
import pytest

from kicad_tools.router import Autorouter, Route, RoutingCache, Segment
from kicad_tools.router.layers import Layer


def _route(y, net=1):
    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[
            Segment(2, y, 18, y, 0.2, Layer.F_CU, net=net),
        ],
    )


def _router():
    ar = Autorouter(width=20, height=20, force_python=True)
    fixed = _route(2, 99)
    ar.existing_routes.append(fixed)
    ar.grid.mark_route(fixed)
    return ar, fixed


@pytest.mark.parametrize("negotiated", [False, True])
def test_snapshot_replay_preserves_fixed_copper_and_exact_usage(tmp_path, negotiated):
    expected, expected_fixed = _router()
    winner = [_route(8), _route(8)]
    for route in winner:
        expected._mark_route(route)
        if negotiated:
            expected.grid.mark_route_usage(route)
    expected.routes[:] = winner
    expected.restore_route_snapshot(winner)
    usage = expected.grid.export_route_usage()
    cache = RoutingCache(cache_dir=tmp_path)
    payload = cache.serialize_routes(winner, route_usage=usage)

    cold, fixed = _router()
    loser = _route(12)
    cold._mark_route(loser)
    cold.grid.mark_route_usage(loser)
    cold.routes.append(loser)
    before_fixed = copy.deepcopy(fixed)
    cold.grid.import_route_usage(cache.deserialize_route_usage(payload))
    cold.restore_route_snapshot(cache.deserialize_routes(payload))

    assert fixed == before_fixed
    assert any(route is fixed for route in cold.grid.routes)
    assert not any(route is loser for route in cold.grid.routes)
    assert cold.routes == winner
    for name in ("_blocked", "_net", "_usage_count"):
        np.testing.assert_array_equal(getattr(cold.grid, name), getattr(expected.grid, name))
    assert cold.router._routed_segments == expected.router._routed_segments
    assert (cold.grid.get_total_overflow() > 0) is negotiated
    assert expected_fixed.segments == fixed.segments


@pytest.mark.parametrize("damage", ["shape", "counts", "encoding"])
def test_bad_usage_snapshot_does_not_mutate_grid(damage):
    router, _ = _router()
    router.grid.mark_route_usage(_route(8))
    before = router.grid._usage_count.copy()
    state = router.grid.export_route_usage()
    if damage == "shape":
        state["shape"][0] += 1
    elif damage == "counts":
        state["counts"] = ""
    else:
        state["counts"] = "not base64!"
    with pytest.raises(ValueError):
        router.grid.import_route_usage(state)
    np.testing.assert_array_equal(router.grid._usage_count, before)


def test_legacy_cache_entry_has_no_replayable_usage(tmp_path):
    cache = RoutingCache(cache_dir=tmp_path)
    payload = cache.serialize_routes([_route(8)])
    assert cache.deserialize_routes(payload)
    with pytest.raises(ValueError, match="no congestion snapshot"):
        cache.deserialize_route_usage(payload)


@pytest.mark.parametrize("failure_stage", ["usage", "geometry"])
def test_cli_cache_apply_failure_never_routes_or_publishes(
    tmp_path, monkeypatch, capsys, failure_stage
):
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock

    from kicad_tools.cli import route_cmd
    from kicad_tools.router.grid import RoutingGrid

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    payload = RoutingCache(cache_dir=tmp_path / "cache").serialize_routes([], route_usage={})
    entry = SimpleNamespace(
        routes_data=payload,
        success_count=0,
        total_segments=0,
        total_vias=0,
        compute_time_ms=1,
    )
    monkeypatch.setattr(RoutingCache, "get", lambda *_: entry)
    usage = Mock(
        side_effect=RuntimeError("injected usage failure") if failure_stage == "usage" else None
    )
    geometry = Mock(side_effect=RuntimeError("injected geometry failure"))
    search = Mock(side_effect=AssertionError("fresh routing after partial cache apply"))
    monkeypatch.setattr(RoutingGrid, "import_route_usage", usage)
    monkeypatch.setattr(Autorouter, "restore_route_snapshot", geometry)
    monkeypatch.setattr(Autorouter, "route_all_negotiated", search)
    source = (
        Path(__file__).resolve().parents[1]
        / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"
    )
    output = tmp_path / "must-not-exist.kicad_pcb"
    result = route_cmd._in_process_main(
        [
            str(source),
            "--output",
            str(output),
            "--strategy",
            "negotiated",
            "--no-auto-pour",
            "--no-auto-layers",
            "--grid",
            "0.1",
        ]
    )
    assert result == 1
    assert "cannot restore cached routing state" in capsys.readouterr().err
    usage.assert_called_once()
    assert geometry.call_count == (failure_stage == "geometry")
    search.assert_not_called()
    assert not output.exists()
