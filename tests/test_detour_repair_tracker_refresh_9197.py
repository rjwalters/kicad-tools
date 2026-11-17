"""Skew trackers must reflect the final routes after a kept ``--detour-repair`` (#9197).

``Autorouter._finalize_routing`` populates ``_diffpair_length_tracker`` /
``_match_group_tracker`` from ``self.routes`` and documents that it runs once
the routes are final.  The opt-in detour-repair pass (#9013) dispatches AFTER
that call and mutates ``self.routes`` on a ``kept`` attempt, so the dispatch
sites re-run ``_finalize_routing`` -- only when something was kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.io import load_pcb_for_routing

from .test_detour_repair_9013 import (
    _NEGOTIATED_KWARGS,
    _PLANAR_RULES,
    _offender_board_text,
    _settle_with_probe,
)


def _pair_board_text() -> str:
    """The #9013 offender board with its two nets renamed into a +/- diff pair."""
    return _offender_board_text().replace('"/H"', '"/X+"').replace('"/V"', '"/X-"')


@pytest.fixture
def pair_pcb(tmp_path: Path) -> Path:
    pcb = tmp_path / "pair.kicad_pcb"
    pcb.write_text(_pair_board_text())
    return pcb


def _load(pcb: Path):
    router, _ = load_pcb_for_routing(str(pcb), strategy="grid", rules=_PLANAR_RULES)
    return router


def _net_id(router, name: str) -> int:
    return next(n for n, nm in router.net_names.items() if nm == name)


def _assert_tracker_matches_routes(router) -> None:
    """Tracker lengths for both pair halves equal the measured final copper."""
    model = router._kicad_length_model(router.grid.num_layers)
    tracker = router.diffpair_length_tracker
    for name in ("/X+", "/X-"):
        net = _net_id(router, name)
        measured = sum(float(model.route_length(r)) for r in router.routes if r.net == net)
        assert tracker.lengths[net] == pytest.approx(measured, abs=1e-6), name


class TestTrackersAfterKeptRepair:
    def test_pair_is_detected_and_repair_is_kept(self, pair_pcb: Path) -> None:
        router = _load(pair_pcb)
        router.detour_repair = True
        _settle_with_probe(router)
        verdicts = [(o.net_name, o.verdict) for o in router.detour_repair_outcomes]
        assert verdicts == [("/X+", "kept")]

    def test_negotiated_tail_refreshes_diffpair_tracker(self, pair_pcb: Path) -> None:
        router = _load(pair_pcb)
        router.detour_repair = True
        _settle_with_probe(router)
        assert any(o.verdict == "kept" for o in router.detour_repair_outcomes)
        assert router.diffpair_length_tracker.get_all_skews()
        _assert_tracker_matches_routes(router)

    def test_stale_without_refresh(self, pair_pcb: Path, monkeypatch) -> None:
        """Control: with the refresh disabled the tracker is stale, so the
        assertion above is a real detector and not a tautology."""
        monkeypatch.setattr(
            type(_load(pair_pcb)),
            "_refresh_skew_trackers_after_detour_repair",
            lambda self, outcomes: None,
        )
        router = _load(pair_pcb)
        router.detour_repair = True
        _settle_with_probe(router)
        assert any(o.verdict == "kept" for o in router.detour_repair_outcomes)
        with pytest.raises(AssertionError):
            _assert_tracker_matches_routes(router)

    def test_negotiated_tail_passes_pass_result_to_refresh(
        self, pair_pcb: Path, monkeypatch
    ) -> None:
        router = _load(pair_pcb)
        calls: list[list] = []
        real = type(router)._refresh_skew_trackers_after_detour_repair

        def spy(self, outcomes):
            calls.append(list(outcomes))
            return real(self, outcomes)

        monkeypatch.setattr(type(router), "_refresh_skew_trackers_after_detour_repair", spy)
        router.detour_repair = True
        _settle_with_probe(router)
        assert len(calls) == 1 and calls[0][0].verdict == "kept"

    def test_two_phase_tail_refreshes_on_kept(self, pair_pcb: Path, monkeypatch) -> None:
        from kicad_tools.router import detour_repair
        from kicad_tools.router.detour_repair import RepairOutcome

        router = _load(pair_pcb)
        router.detour_repair = True
        monkeypatch.setattr(
            detour_repair,
            "run_detour_repair_pass_from_autorouter",
            lambda ar, per_net_timeout=None: [RepairOutcome(1, "/X+", 1.5, "kept", "z")],
        )
        finalized: list[int] = []
        real = type(router)._finalize_routing

        def counting(self):
            finalized.append(1)
            return real(self)

        monkeypatch.setattr(type(router), "_finalize_routing", counting)
        router.route_all_two_phase()
        # Once at the tail, once more because the pass reported a kept repair.
        assert len(finalized) == 2


class TestNoExtraFinalizeWithoutKeep:
    def test_flag_off_makes_no_extra_finalize_call(self, pair_pcb: Path, monkeypatch) -> None:
        router = _load(pair_pcb)
        count = {"n": 0}
        real = type(router)._finalize_routing

        def counting(self):
            count["n"] += 1
            return real(self)

        monkeypatch.setattr(type(router), "_finalize_routing", counting)
        router.report_detour_ratio = True
        router.route_all_negotiated(**_NEGOTIATED_KWARGS)
        assert router.detour_repair is False
        assert count["n"] == 1

    def test_helper_skips_when_nothing_kept(self, pair_pcb: Path, monkeypatch) -> None:
        from kicad_tools.router.detour_repair import RepairOutcome

        router = _load(pair_pcb)
        called = []
        monkeypatch.setattr(type(router), "_finalize_routing", lambda self: called.append(1))
        outcomes = [
            RepairOutcome(1, "/X+", 1.5, "rolled back", "x"),
            RepairOutcome(2, "/X-", 1.4, "skipped", "y"),
        ]
        router._refresh_skew_trackers_after_detour_repair(outcomes)
        router._refresh_skew_trackers_after_detour_repair([])
        assert called == []
        router._refresh_skew_trackers_after_detour_repair(
            [RepairOutcome(1, "/X+", 1.5, "kept", "z")]
        )
        assert called == [1]
