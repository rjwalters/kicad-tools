"""Issue #6664: diff-pair skew tuning auto-engaged on lattice output.

On the lattice engine with ``--differential-pairs`` the router runs the
existing skew tuner (#2648) without ``--length-match-diffpairs``, engaged on
the limit ``kct check`` fails the board on
(``resolve_pair_thresholds(...).skew_severity == "error"``) and tuned to that
same ``skew_mm`` -- regardless of the class's ``length_critical`` flag.

(a) a ``length_critical=False`` class with ``skew_tolerance_mm=0.1`` is tuned,
    to the checker's limit;
(b) a limit that resolves to ``skew_severity="warning"`` is not tuned;
(c) ``--length-match-diffpairs`` semantics are unchanged
    (``not_length_critical``);
(d) the grid engine never auto-engages.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.router import diffpair_length_tuning as dlt_module
from kicad_tools.router.core import Autorouter
from kicad_tools.router.diffpair_detection import detect_diff_pairs
from kicad_tools.router.layers import Layer
from kicad_tools.router.length import LengthTracker
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import NetClassRouting
from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

P_NAME, N_NAME = "LVDS1_P", "LVDS1_N"


def _route(net_id: int, name: str, length_mm: float, y: float) -> Route:
    return Route(
        net=net_id,
        net_name=name,
        segments=[
            Segment(0.0, y, length_mm, y, 0.2, Layer.F_CU, net=net_id, net_name=name),
        ],
    )


def _router(net_class: NetClassRouting, skew_mm: float = 0.12) -> Autorouter:
    """A two-net pair whose P half is ``skew_mm`` shorter than N."""
    ar = Autorouter(width=40.0, height=20.0)
    ar.net_names = {1: P_NAME, 2: N_NAME}
    ar.net_class_map = {P_NAME: net_class, N_NAME: net_class}
    ar.routes = [_route(1, P_NAME, 20.0, 0.0), _route(2, N_NAME, 20.0 + skew_mm, 0.5)]
    return ar


def _skew(ar: Autorouter) -> float:
    lengths = {}
    for r in ar.routes:
        lengths[r.net] = lengths.get(r.net, 0.0) + LengthTracker.calculate_route_length(r)
    return abs(lengths[1] - lengths[2])


def _pairs(ar: Autorouter) -> list:
    pairs = detect_diff_pairs(net_names=ar.net_names)
    assert len(pairs) == 1
    return pairs


def _spy():
    calls: list[dict] = []
    real = dlt_module.tune_diff_pair_skew

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    return calls, spy


def test_a_checker_limit_tunes_non_length_critical_pair_to_checker_limit():
    nc = NetClassRouting(name="LVDS", length_critical=False, skew_tolerance_mm=0.1)
    ar = _router(nc)
    limit = resolve_pair_thresholds(ar.net_class_map, P_NAME, N_NAME)
    assert limit.skew_severity == "error"
    assert limit.skew_mm == pytest.approx(0.1)
    assert _skew(ar) > limit.skew_mm

    calls, spy = _spy()
    with patch.object(dlt_module, "tune_diff_pair_skew", spy):
        results = ar.apply_diffpair_length_tuning(
            detected_pairs=_pairs(ar), verbose=False, engage="checker_skew_limit"
        )

    assert len(calls) == 1
    assert calls[0]["tolerance_mm"] == pytest.approx(limit.skew_mm)
    assert calls[0]["length_critical"] is True
    result = results[(P_NAME, N_NAME)]
    assert result.reason == "tuned"
    assert result.tolerance_mm == pytest.approx(limit.skew_mm)
    assert result.skew_after_mm <= limit.skew_mm
    assert _skew(ar) <= limit.skew_mm + 1e-9


def test_b_warning_severity_limit_is_not_tuned():
    # LVDS's protocol default (0.5 mm) is uncited -> kct check only warns.
    nc = NetClassRouting(name="LVDS", length_critical=True, pair_protocol="LVDS")
    ar = _router(nc, skew_mm=4.0)
    limit = resolve_pair_thresholds(ar.net_class_map, P_NAME, N_NAME)
    assert limit.skew_severity == "warning"
    before = [list(r.segments) for r in ar.routes]

    calls, spy = _spy()
    with patch.object(dlt_module, "tune_diff_pair_skew", spy):
        results = ar.apply_diffpair_length_tuning(
            detected_pairs=_pairs(ar), verbose=False, engage="checker_skew_limit"
        )

    assert calls == []
    result = results[(P_NAME, N_NAME)]
    assert result.reason == "skew_limit_not_enforced"
    assert result.skew_before_mm == pytest.approx(4.0)
    assert [list(r.segments) for r in ar.routes] == before


def test_c_length_match_diffpairs_still_gates_on_length_critical():
    nc = NetClassRouting(name="LVDS", length_critical=False, skew_tolerance_mm=0.1)
    ar = _router(nc)
    before = [list(r.segments) for r in ar.routes]

    results = ar.apply_diffpair_length_tuning(detected_pairs=_pairs(ar), verbose=False)

    assert results[(P_NAME, N_NAME)].reason == "not_length_critical"
    assert [list(r.segments) for r in ar.routes] == before


def _args(engine: str, *, length_match: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        route_engine=engine,
        differential_pairs=True,
        length_match_diffpairs=length_match,
    )


def _mock_router() -> MagicMock:
    router = MagicMock()
    router.net_names = {1: P_NAME, 2: N_NAME}
    router.net_class_map = {}
    router.apply_diffpair_length_tuning.return_value = {}
    return router


def test_d_grid_engine_does_not_auto_engage():
    from kicad_tools.cli import route_cmd

    assert route_cmd._diffpair_skew_tuning_mode(_args("grid")) is None
    assert route_cmd._diffpair_skew_tuning_mode(_args("mesh")) is None
    router = _mock_router()
    assert route_cmd._run_diffpair_skew_tuning(router, _args("grid"), quiet=True) is None
    router.apply_diffpair_length_tuning.assert_not_called()


def test_lattice_auto_engages_with_checker_limit_and_flag_keeps_old_mode():
    from kicad_tools.cli import route_cmd

    assert route_cmd._diffpair_skew_tuning_mode(_args("lattice")) == "checker_skew_limit"
    assert (
        route_cmd._diffpair_skew_tuning_mode(_args("lattice", length_match=True))
        == "length_critical"
    )
    assert (
        route_cmd._diffpair_skew_tuning_mode(_args("grid", length_match=True)) == "length_critical"
    )
    no_pairs = argparse.Namespace(route_engine="lattice", differential_pairs=False)
    assert route_cmd._diffpair_skew_tuning_mode(no_pairs) is None

    router = _mock_router()
    with (
        patch.object(route_cmd, "_connectivity_snapshot", return_value=None),
        patch.object(route_cmd, "_enforce_connectivity_invariant_or_exit") as enforce,
    ):
        route_cmd._run_diffpair_skew_tuning(router, _args("lattice"), quiet=True)
    router.apply_diffpair_length_tuning.assert_called_once()
    assert router.apply_diffpair_length_tuning.call_args.kwargs["engage"] == "checker_skew_limit"
    assert enforce.call_args.kwargs["phase"] == "length_match_diffpairs"
