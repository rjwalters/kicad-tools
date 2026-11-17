"""Targeted post-settle detour repair pass (issue #9013).

The pass is opt-in (``--detour-repair``, off by default) and transactional
per offender: lift the nets blocking the offender's alone-on-empty
corridor, re-route the offender the short way, re-land the lifted nets,
and keep the rearrangement only if the whole-board ``RouteScore`` is
strictly better.  Pinned here:

- the kept case on a fixture with a known >1.25x offender (copper
  shrinks, board score strictly better, nothing unrouted);
- the rollback cases (a repair that would strand a net, a
  not-strictly-better score, and involved copper that grows even behind
  an improving score) restore the exact pre-attempt board;
- the skip case (no committed copper on the corridor);
- one bounded attempt per offender with per-offender outcome logging;
- the flag reaching the pass through the real ``kct route`` dispatch
  (the #5908 silent-no-op guard: absent flag prints nothing, an
  unsupported strategy says so instead of ignoring silently).

The fixture is the asymmetric-crossing board from the issue's worked
example: the second-routed net's dodge around the first net's far ends
is expensive (it becomes the >1.25x offender), but once the offender is
straight the first net re-lands cheaply around the offender's near end,
so the repaired arrangement is strictly shorter.  Vias are priced out
(``cost_via``) so the crossing must resolve planarly.
"""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.acceleration import to_numpy
from kicad_tools.analysis.detour_ratio import (
    DETOUR_RATIO_THRESHOLD,
    DetourConnectionRecord,
)
from kicad_tools.cli import route_cmd
from kicad_tools.router import detour_repair
from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.detour_repair import (
    _board_route_score,
    _discard_partial,
    _find_offenders,
    run_detour_repair_pass,
    run_detour_repair_pass_from_autorouter,
)
from kicad_tools.router.io import load_pcb_for_routing
from kicad_tools.router.rules import DesignRules

# --- synthetic boards --------------------------------------------------------


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.4 0.4) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _net_fp(ref: str, uid: str, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid}")
    (at 0 0)
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _offender_board_text() -> str:
    """Two crossing nets whose settled state has a known >1.25x offender.

    /V (net 2, shorter) routes first: straight.  /H (net 1) must then
    dodge around /V's far-reaching ends -> ratio ~1.5x.  Repair: lift
    both, /H straight, /V re-lands around /H's near end -> strictly
    shorter.  Geometry per the inequality in the module docstring:
    d(V, H-end) = 7 < d(H-path, V-ends) = 11 and 7 > 0.25 * 22.
    """
    pads_h = _pad("1", 2.0, 21.0, 1, "/H") + " " + _pad("2", 26.0, 21.0, 1, "/H")
    pads_v = _pad("1", 19.0, 10.0, 2, "/V") + " " + _pad("2", 19.0, 32.0, 2, "/V")
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "/H")
  (net 2 "/V")
  (gr_rect (start 0 0) (end 30 42)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{_net_fp("RH", "a1", pads_h)}
{_net_fp("RV", "b1", pads_v)})
"""


#: Planar-only fixture: with the default via price the crossing resolves as
#: a 2-via hop and no detour survives, so no offender exists to repair.
_PLANAR_RULES = DesignRules(cost_via=100000.0)

_NEGOTIATED_KWARGS = {"max_iterations": 2, "per_net_timeout": 10.0, "timeout": 60.0}


def _load(pcb: Path) -> object:
    router, _ = load_pcb_for_routing(str(pcb), strategy="grid", rules=_PLANAR_RULES)
    return router


@pytest.fixture
def offender_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "offender.kicad_pcb"
    pcb.write_text(_offender_board_text())
    return pcb


def _settle_with_probe(router) -> None:
    """Settle the board through the negotiated loop with the probe on."""
    router.report_detour_ratio = True
    router.route_all_negotiated(**_NEGOTIATED_KWARGS)
    router.finalize_detour_probe_records()


def _net_id(router, name: str) -> int:
    for net, net_name in router.net_names.items():
        if net_name == name:
            return net
    raise AssertionError(f"net {name} not found")


def _loop_state(router):
    """The (net_routes, pads_by_net, neg_router) triple the hooks pass."""
    neg = NegotiatedRouter(
        router.grid,
        router.router,
        router.rules,
        router.net_class_map,
        congestion_estimator=router._ensure_congestion_estimator(),
    )
    net_routes: dict[int, list] = {}
    for route in router.routes:
        if getattr(route, "is_escape", False) or not route.segments:
            continue
        net_routes.setdefault(route.net, []).append(route)
    pads_by_net: dict[int, list] = {}
    for net, pads in router.nets.items():
        if pads and len(pads) >= 2:
            pads_by_net[net] = [router._escape_pad_overrides.get(p, router.pads[p]) for p in pads]
    return net_routes, pads_by_net, neg


def _route_signature(router) -> list:
    """Engine-level route geometry, for exact-restore equality."""

    def layer_of(layer):
        return getattr(layer, "value", str(layer))

    return sorted(
        (
            route.net,
            tuple(
                (s.x1, s.y1, s.x2, s.y2, layer_of(s.layer), round(s.width, 6))
                for s in route.segments
            ),
            tuple((v.x, v.y, v.net) for v in route.vias),
        )
        for route in router.routes
    )


def _total_copper_mm(router) -> float:
    model = router._kicad_length_model(router.grid.num_layers)
    return sum(float(model.route_length(r)) for r in router.routes)


# --- kept case: known offender shortened, score strictly better --------------


class TestKeptCase:
    def test_settled_board_has_known_offender(self, offender_pcb: Path) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        records = router.detour_probe_records
        assert records
        offenders = [r for r in records if r.ratio is not None and r.ratio > DETOUR_RATIO_THRESHOLD]
        assert [r.net_name for r in offenders] == ["/H"]
        assert offenders[0].ratio == pytest.approx(1.52, abs=0.05)

    def test_repair_shortens_offender_and_score_strictly_better(self, offender_pcb: Path) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        net_routes, pads_by_net, neg = _loop_state(router)
        before = _board_route_score(router, net_routes, pads_by_net, neg)
        copper_before = _total_copper_mm(router)

        router.detour_repair = True
        outcomes = run_detour_repair_pass(
            router, net_routes=net_routes, pads_by_net=pads_by_net, neg_router=neg
        )

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.verdict == "kept"
        assert outcome.net_name == "/H"
        # The kept reason carries both measures: the offender's own copper
        # and the involved-nets total the ground rule requires to shrink.
        assert "copper" in outcome.reason and "involved" in outcome.reason
        after = _board_route_score(router, net_routes, pads_by_net, neg)
        assert after.is_better_than(before)
        # Nothing unrouted by the rearrangement; strictly less copper.
        assert after.nets_complete == before.nets_complete
        assert _total_copper_mm(router) < copper_before
        assert router.detour_repair_outcomes is outcomes

    def test_flag_off_pass_is_a_no_op(self, offender_pcb: Path) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        signature = _route_signature(router)
        net_routes, pads_by_net, neg = _loop_state(router)

        # Attribute stays off (the default); calling the pass is a no-op.
        assert router.detour_repair is False
        outcomes = run_detour_repair_pass(
            router, net_routes=net_routes, pads_by_net=pads_by_net, neg_router=neg
        )
        assert outcomes == []
        assert _route_signature(router) == signature

    def test_flag_reaches_pass_inside_route_all_negotiated(self, offender_pcb: Path) -> None:
        """The real-driver path: the attribute the CLI sets makes the
        negotiated hook run the pass before returning."""
        plain = _load(offender_pcb)
        _settle_with_probe(plain)
        assert plain.detour_repair_outcomes == []

        flagged = _load(offender_pcb)
        flagged.detour_repair = True
        _settle_with_probe(flagged)
        outcomes = flagged.detour_repair_outcomes
        assert len(outcomes) == 1
        assert outcomes[0].verdict == "kept"
        assert outcomes[0].net_name == "/H"
        assert _total_copper_mm(flagged) < _total_copper_mm(plain)


# --- rollback: the accept gate restores the exact pre-attempt board ----------


class TestRollback:
    def test_stranded_lifted_net_rolls_back_and_board_is_unchanged(
        self, offender_pcb: Path, monkeypatch
    ) -> None:
        """Constructed blocked case: the lifted net cannot re-land, so the
        attempt must strand it -- the transaction rolls the whole board
        back to the exact pre-attempt copper instead."""
        router = _load(offender_pcb)
        _settle_with_probe(router)
        signature = _route_signature(router)
        routes_before = list(router.routes)
        failures_before = len(router.routing_failures)
        usage_before = to_numpy(router.grid._usage_count).copy()
        lifted = _net_id(router, "/V")

        from kicad_tools.router.core import Autorouter

        original_route = Autorouter._route_net_negotiated

        def fail_lifted(self, net, present_cost_factor, **kwargs):
            if net == lifted:
                return []
            return original_route(self, net, present_cost_factor, **kwargs)

        monkeypatch.setattr(Autorouter, "_route_net_negotiated", fail_lifted)

        router.detour_repair = True
        outcomes = run_detour_repair_pass_from_autorouter(router)

        assert len(outcomes) == 1
        assert outcomes[0].verdict == "rolled back"
        assert "did not re-land" in outcomes[0].reason
        # Exact restore: same Route geometry, same master list, no new
        # search failures left behind by the aborted attempt.
        assert _route_signature(router) == signature
        assert len(router.routes) == len(routes_before)
        assert len(router.routing_failures) == failures_before
        # #3501 discipline: the usage ledger must be restored per cell
        # too.  A rolled-back attempt's partial copper never incremented
        # it, so any drift here means counts were stolen from committed
        # nets sharing those cells (the pre-fix _discard_partial did
        # exactly that).
        assert np.array_equal(to_numpy(router.grid._usage_count), usage_before)

    def test_discard_partial_never_touches_the_usage_ledger(self, offender_pcb: Path) -> None:
        """#3501: a partial search result is blocked-grid-marked ONLY
        (the search's commit closure never writes usage), so discarding
        it must leave the usage ledger untouched -- per-cell counts on
        shared cells belong to committed nets and are stolen, not
        underflowed, by a stray ``unmark_route_usage``."""
        router = _load(offender_pcb)
        _settle_with_probe(router)
        # A committed route carries real usage on its cells ...
        committed = router.routes[0]
        router.grid.mark_route_usage(committed)
        usage_before = to_numpy(router.grid._usage_count).copy()

        # ... and a partial the search produced over the same cells is
        # grid-marked (never usage-marked) before its transaction dies.
        partial = copy.deepcopy(committed)
        router._mark_route(partial)
        _discard_partial(router, [partial])

        assert np.array_equal(to_numpy(router.grid._usage_count), usage_before)

    def test_score_not_strictly_better_rolls_back_equal_outcome(
        self, offender_pcb: Path, monkeypatch
    ) -> None:
        """Edge case from the issue: an equal-length result is not an
        improvement -- the gate must refuse and restore."""
        router = _load(offender_pcb)
        _settle_with_probe(router)
        signature = _route_signature(router)

        class FlatScore:
            """A gate that never sees an improvement (equal or worse)."""

            def __init__(self, *a, **k):
                self.describe = lambda: "flat"

            @classmethod
            def from_routes(cls, routes, metrics=None, **k):
                return cls()

            def is_better_than(self, other):
                return False

        monkeypatch.setattr(detour_repair, "RouteScore", FlatScore)

        router.detour_repair = True
        outcomes = run_detour_repair_pass_from_autorouter(router)

        assert len(outcomes) == 1
        assert outcomes[0].verdict == "rolled back"
        assert "not strictly better" in outcomes[0].reason
        assert _route_signature(router) == signature

    def test_involved_copper_growth_rolls_back_even_when_score_improves(
        self, offender_pcb: Path, monkeypatch
    ) -> None:
        """Fleet evidence (board 05 CPL, 2026-11-17): the lexicographic
        score can accept on a higher-priority term (less overflow) while
        the rearrangement ADDS copper -- the issue's ground rule forbids
        keeping that.  With a gate stubbed always-better and the involved
        nets' copper stubbed to grow, the attempt must still roll back
        exactly."""
        router = _load(offender_pcb)
        _settle_with_probe(router)
        signature = _route_signature(router)
        original_ids = {id(r) for r in router.routes}

        class AlwaysBetterScore:
            def __init__(self, *a, **k):
                self.describe = lambda: "always-better"

            @classmethod
            def from_routes(cls, routes, metrics=None, **k):
                return cls()

            def is_better_than(self, other):
                return True

        real_mm = detour_repair._route_mm

        def growing_mm(ar, route):
            # Settled (pre-attempt) copper measures its real length; anything
            # the attempt places measures real + 50mm: involved copper can
            # only grow, while offender detection still sees real ratios.
            return real_mm(ar, route) + (0.0 if id(route) in original_ids else 50.0)

        monkeypatch.setattr(detour_repair, "RouteScore", AlwaysBetterScore)
        monkeypatch.setattr(detour_repair, "_route_mm", growing_mm)

        router.detour_repair = True
        outcomes = run_detour_repair_pass_from_autorouter(router)

        assert len(outcomes) == 1
        assert outcomes[0].verdict == "rolled back"
        assert "involved copper grew" in outcomes[0].reason
        assert _route_signature(router) == signature


# --- skip case and offender detection ----------------------------------------


class TestSkipAndDetection:
    def test_offender_without_blockers_is_skipped(self, offender_pcb: Path, monkeypatch) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        signature = _route_signature(router)

        monkeypatch.setattr(
            NegotiatedRouter, "find_blocking_nets_relaxed", lambda self, *a, **k: {}
        )

        router.detour_repair = True
        outcomes = run_detour_repair_pass_from_autorouter(router)

        assert len(outcomes) == 1
        assert outcomes[0].verdict == "skipped"
        assert "no committed copper" in outcomes[0].reason
        assert _route_signature(router) == signature

    def test_find_offenders_orders_worst_first_one_per_net(self, offender_pcb: Path) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        records = router.detour_probe_records
        assert any(r.ratio is not None and r.ratio > DETOUR_RATIO_THRESHOLD for r in records)

        net_routes, _, _ = _loop_state(router)
        offenders, copper = _find_offenders(router, net_routes)
        assert offenders, "the fixture's settled board must contain an offender"
        ratios = [o.ratio for o in offenders]
        assert ratios == sorted(ratios, reverse=True)
        assert len({o.net for o in offenders}) == len(offenders)
        assert all(net_routes.get(o.net) for o in offenders)
        assert copper > 0.0

    def test_probe_only_rows_never_become_offenders(self, offender_pcb: Path) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        net_routes, _, _ = _loop_state(router)
        router.detour_probe_records = [
            DetourConnectionRecord(net_name="/H", connection_index=0, probe_no_path=True),
            DetourConnectionRecord(net_name="/V", connection_index=0, unmatched=True),
            DetourConnectionRecord(net_name="/H", connection_index=1, alone_mm=None),
        ]
        offenders, copper = _find_offenders(router, net_routes)
        assert offenders == []
        assert copper == 0.0


# --- CLI plumbing through the real kct route dispatch ------------------------


def _cli_base(pcb: Path, out: Path) -> list[str]:
    return [str(pcb), "-o", str(out), "--skip-drc", "--seed", "7"]


class TestCliPlumbing:
    def test_flag_reaches_pass_through_cli_dispatch(
        self, offender_pcb: Path, tmp_path: Path, capsys
    ) -> None:
        """The #5908 guard: ``--detour-repair`` must make the pass run on
        the default ``kct route`` dispatch (negotiated driver), and the
        absent flag must print nothing for it."""
        out_on = tmp_path / "on.kicad_pcb"
        out_off = tmp_path / "off.kicad_pcb"

        rc_on = route_cmd.main([*_cli_base(offender_pcb, out_on), "--detour-repair"])
        assert rc_on == 0
        captured_on = capsys.readouterr().out
        assert "Detour repair" in captured_on

        rc_off = route_cmd.main(_cli_base(offender_pcb, out_off))
        assert rc_off == 0
        captured_off = capsys.readouterr().out
        assert "Detour repair" not in captured_off

    def test_repair_only_run_writes_no_report_sidecar(
        self, offender_pcb: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "repair.kicad_pcb"
        rc = route_cmd.main([*_cli_base(offender_pcb, out), "--detour-repair"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Detour ratio" not in captured
        assert not (tmp_path / "repair.detour_ratio.json").exists()

    def test_report_flag_composes_with_repair(
        self, offender_pcb: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "both.kicad_pcb"
        rc = route_cmd.main(
            [*_cli_base(offender_pcb, out), "--detour-repair", "--report-detour-ratio"]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Detour repair" in captured
        assert "Detour ratio" in captured
        assert (tmp_path / "both.detour_ratio.json").exists()

    def test_unsupported_strategy_warns_instead_of_silently_ignoring(
        self, offender_pcb: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "basic.kicad_pcb"
        rc = route_cmd.main(
            [*_cli_base(offender_pcb, out), "--detour-repair", "--strategy", "basic"]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "not available with --strategy basic" in captured
        assert "Detour repair" not in captured.replace("not available with --strategy basic", "")

    def test_flag_off_output_byte_identical_across_runs(
        self, offender_pcb: Path, tmp_path: Path
    ) -> None:
        """Off by default: two flag-less runs produce identical bytes (the
        with/without ``--report-detour-ratio`` identity is pinned by the
        #9003 suite)."""
        out_a = tmp_path / "a.kicad_pcb"
        out_b = tmp_path / "b.kicad_pcb"
        assert route_cmd.main(_cli_base(offender_pcb, out_a)) == 0
        assert route_cmd.main(_cli_base(offender_pcb, out_b)) == 0
        assert out_a.read_bytes() == out_b.read_bytes()


# --- outcome logging and termination -----------------------------------------


class TestLoggingAndTermination:
    def test_per_offender_outcomes_logged(self, offender_pcb: Path, capsys) -> None:
        router = _load(offender_pcb)
        _settle_with_probe(router)
        capsys.readouterr()
        router.detour_repair = True
        outcomes = run_detour_repair_pass_from_autorouter(router)

        assert outcomes
        assert all(o.verdict in {"kept", "rolled back", "skipped"} for o in outcomes)
        # One bounded attempt per offender net: no net appears twice.
        nets = [o.net for o in outcomes]
        assert len(nets) == len(set(nets))
        log = capsys.readouterr().out
        for outcome in outcomes:
            assert outcome.net_name in log
            assert outcome.verdict in log
        assert "Detour repair:" in log
