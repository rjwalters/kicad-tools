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


def test_auto_path_bump_respects_foreign_pad_class_clearance():
    """A bump may not pass a foreign pad closer than that pad's class clearance.

    Board 06rf regression guard: the built-in self-check only sees segments at
    the intra-pair gap, so a bump landed 0.163 mm from a 0.2 mm ``Power`` pad.
    """
    from kicad_tools.router.primitives import Pad

    nc = NetClassRouting(name="LVDS", length_critical=False, skew_tolerance_mm=0.1)
    ar = _router(nc)
    ar.net_names[3] = "+5V"
    ar.net_class_map["+5V"] = NetClassRouting(name="Power", clearance=0.2)
    pad = Pad(x=10.0, y=-0.5, width=0.4, height=0.4, net=3, net_name="+5V", ref="C1", pin="1")
    ar.grid._pads = [pad]
    ok = ar._skew_tune_foreign_clearance_ok(
        {r.net: r for r in ar.routes}, partner_of={1: 2, 2: 1}, intra_pair_clearance_mm=0.15
    )
    width = 0.2
    # Pad edge at y=-0.3; a trace centred at y=-0.3+0.1+0.163 is 0.163 mm clear.
    near = Segment(5.0, -0.037, 15.0, -0.037, width, Layer.F_CU, net=1, net_name=P_NAME)
    far = Segment(5.0, 0.0, 15.0, 0.0, width, Layer.F_CU, net=1, net_name=P_NAME)
    assert ok([near], 1) is False
    assert ok([far], 1) is True


def test_auto_path_measures_skew_with_via_barrels_like_the_checker():
    """07rf regression guard: the checker counts drilled via length.

    P is 20.0 mm of track with no vias; N is 18.5 mm of track plus two
    through vias (2 x 1.58 mm: KiCad's stackup span on the default 1.6 mm
    board, Issue #6725), i.e. 21.66 mm as
    ``kct check`` measures it.  A via-blind tuner would lengthen N and push
    the checker's skew from 1.66 mm up; the auto path must lengthen P.
    """
    from kicad_tools.router.primitives import Via

    nc = NetClassRouting(name="LVDS", length_critical=False, skew_tolerance_mm=0.1)
    ar = _router(nc)
    vias = [
        Via(x=x, y=1.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
        for x in (1.0, 2.0)
    ]
    ar.routes = [
        _route(1, P_NAME, 20.0, 0.0),
        Route(net=2, net_name=N_NAME, segments=_route(2, N_NAME, 18.5, 1.0).segments, vias=vias),
    ]
    n_segments = ar.routes[1].segments
    measure = ar._checker_route_length(2)
    before = abs(measure(ar.routes[0]) - measure(ar.routes[1]))
    assert before == pytest.approx(1.66)

    results = ar.apply_diffpair_length_tuning(
        detected_pairs=_pairs(ar), verbose=False, engage="checker_skew_limit"
    )
    result = results[(P_NAME, N_NAME)]
    assert result.skew_before_mm == pytest.approx(1.66)
    by_net = {r.net: r for r in ar.routes}
    assert by_net[2].segments == n_segments  # the checker's longer half is never touched
    after = abs(measure(by_net[1]) - measure(by_net[2]))
    assert after <= before
    assert after == pytest.approx(result.skew_after_mm)
    assert result.reason == "tuned", result.message
    assert after <= 0.1


def test_pair_skew_tuning_runs_before_match_group_tuning():
    """The group tuner (#6674) adds identical meanders to both legs of a pair,
    so it preserves whatever pair skew it receives: pair skew must be fixed
    first.  Pin the call order in the route pipeline."""
    import inspect

    from kicad_tools.cli import route_cmd

    source = inspect.getsource(route_cmd)
    pair_call = source.index("_run_diffpair_skew_tuning(router, args, quiet=quiet)")
    group_call = source.index("_mg_engage = _match_group_tuning_engage(args, router)")
    assert pair_call < group_call


def test_summary_lines_count_overshoot(capsys):
    """Issue #6795: ``overshoot`` has its own bucket in both Summary lines."""
    from kicad_tools.cli import route_cmd
    from kicad_tools.router.diffpair_length_tuning import DiffPairTuneResult

    results = {
        ("A_P", "A_N"): DiffPairTuneResult(reason="overshoot", tolerance_mm=0.1),
        ("B_P", "B_N"): DiffPairTuneResult(reason="overshoot", tolerance_mm=0.1),
    }
    for args, label in ((_args("lattice"), "lattice"), (_args("grid", length_match=True), "grid")):
        router = _mock_router()
        router.apply_diffpair_length_tuning.return_value = results
        with (
            patch.object(route_cmd, "_connectivity_snapshot", return_value=None),
            patch.object(route_cmd, "_enforce_connectivity_invariant_or_exit"),
        ):
            route_cmd._run_diffpair_skew_tuning(router, args, quiet=False)
        out = capsys.readouterr().out
        assert "2 overshoot" in out, label


def test_skew_tune_line_prints_message_for_non_success():
    from kicad_tools.router.diffpair_length_tuning import DiffPairTuneResult

    line = Autorouter._format_skew_tune_line(
        "A_P",
        "A_N",
        DiffPairTuneResult(success=False, reason="overshoot", message="overshot the partner"),
    )
    assert "overshot the partner" in line


# -- Issue #6795: the uncoupled-length limit reaches the tuner ----------------


def test_checker_path_passes_class_uncoupled_total_to_tuner():
    nc = NetClassRouting(
        name="LVDS", length_critical=False, skew_tolerance_mm=0.1, max_uncoupled_total_mm=7.5
    )
    ar = _router(nc)
    calls, spy = _spy()
    with patch.object(dlt_module, "tune_diff_pair_skew", spy):
        ar.apply_diffpair_length_tuning(
            detected_pairs=_pairs(ar), verbose=False, engage="checker_skew_limit"
        )
    assert len(calls) == 1
    assert calls[0]["max_uncoupled_mm"] == pytest.approx(7.5)


def test_checker_path_derives_uncoupled_limit_from_component_count():
    """Per-terminal allowance x components, counted like ``net_component_counts``.

    P lands on three components and N on two; the checker's limit uses the
    larger count (``PairMeasure.components``), so 2.0 mm x 3.
    """
    from kicad_tools.router.primitives import Pad
    from kicad_tools.validate.rules.diffpair_uncoupled_length import uncoupled_limit

    nc = NetClassRouting(
        name="LVDS",
        length_critical=False,
        skew_tolerance_mm=0.1,
        max_uncoupled_per_terminal_mm=2.0,
    )
    ar = _router(nc)
    for i, (ref, pin, net, name) in enumerate(
        [
            ("U1", "1", 1, P_NAME),
            ("U1", "3", 1, P_NAME),  # a second pad on U1 is still one component
            ("J1", "1", 1, P_NAME),
            ("R1", "1", 1, P_NAME),
            ("U1", "2", 2, N_NAME),
            ("J1", "2", 2, N_NAME),
        ]
    ):
        ar.pads[(ref, pin)] = Pad(
            x=30.0 + i, y=15.0, width=0.5, height=0.5, net=net, net_name=name, ref=ref, pin=pin
        )
    thresholds = resolve_pair_thresholds(ar.net_class_map, P_NAME, N_NAME)
    expected, _ = uncoupled_limit(thresholds, 3)
    assert expected == pytest.approx(6.0)

    calls, spy = _spy()
    with patch.object(dlt_module, "tune_diff_pair_skew", spy):
        ar.apply_diffpair_length_tuning(
            detected_pairs=_pairs(ar), verbose=False, engage="checker_skew_limit"
        )
    assert len(calls) == 1
    assert calls[0]["max_uncoupled_mm"] == pytest.approx(expected)


def test_length_critical_path_applies_no_uncoupled_guard():
    """``--length-match-diffpairs`` keeps its pre-#6795 behaviour: no guard."""
    nc = NetClassRouting(
        name="LVDS", length_critical=True, skew_tolerance_mm=0.1, max_uncoupled_total_mm=7.5
    )
    ar = _router(nc)
    calls, spy = _spy()
    with patch.object(dlt_module, "tune_diff_pair_skew", spy):
        ar.apply_diffpair_length_tuning(detected_pairs=_pairs(ar), verbose=False)
    assert len(calls) == 1
    assert calls[0]["max_uncoupled_mm"] is None
