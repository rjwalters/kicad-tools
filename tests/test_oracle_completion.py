"""Oracle completion loop + one stranded-pour verdict (Issue #5785)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router import oracle_completion as oc
from kicad_tools.router.completion_verdict import (
    ALLOW_STRANDED_ENV,
    ORACLE_DISABLE_ENV,
    allow_stranded_pour_pads,
    oracle_rounds,
    stranded_pour_blocks,
    stranded_pour_exit,
)
from kicad_tools.router.oracle_completion import (
    STOP_CONVERGED,
    STOP_DRC_REGRESSED,
    STOP_MAX_ROUNDS,
    STOP_NO_CLOSER,
    STOP_NO_PROGRESS,
    STOP_NOT_RUN,
    STOP_ORACLE_FAILED,
    ClosureAttempt,
    OracleEndpoint,
    links_from_violations,
    run_oracle_completion,
)


def _unconnected(n: int, net: str = "GND") -> list[DRCViolation]:
    out = []
    for i in range(n):
        out.append(
            DRCViolation(
                type=ViolationType.UNCONNECTED_ITEMS,
                type_str="unconnected_items",
                severity=Severity.ERROR,
                message="Missing connection between items",
                locations=[Location(10.0 + i, 5.0), Location(20.0 + i, 5.0)],
                items=[f"Pad {i + 1} [{net}] of C{i + 1} on F.Cu", f"Zone [{net}] on In1.Cu"],
            )
        )
    return out


def _geo(links: int, errors: int = 0, ran: bool = True, net: str = "GND") -> GeometricDRCResult:
    return GeometricDRCResult(
        ran=ran,
        error_count=errors,
        unconnected_items=_unconnected(links, net),
    )


class FakeBoard:
    """A board whose link count is the state the fake oracle reports."""

    def __init__(self, tmp_path: Path, links: int, errors: int = 0):
        self.path = tmp_path / "b.kicad_pcb"
        self.path.write_text(f"{links},{errors}")

    def state(self) -> tuple[int, int]:
        a, b = self.path.read_text().split(",")
        return int(a), int(b)

    def oracle(self, _path: Path) -> GeometricDRCResult:
        links, errors = self.state()
        return _geo(links, errors)


def _closer(board: FakeBoard, deltas: list[int], err_delta: int = 0, applied: int = 1):
    calls = {"n": 0}

    def closer(path, links, banned):
        i = min(calls["n"], len(deltas) - 1)
        calls["n"] += 1
        n, e = board.state()
        board.path.write_text(f"{max(n + deltas[i], 0)},{e + err_delta}")
        return ClosureAttempt(applied=applied)

    closer.calls = calls  # type: ignore[attr-defined]
    return closer


class TestParsing:
    def test_pad_and_zone_endpoints(self):
        links = links_from_violations(_unconnected(1))
        assert len(links) == 1
        lk = links[0]
        assert lk.net == "GND"
        assert lk.a.kind == "pad" and lk.a.pad_key == "C1.1" and lk.a.layer == "F.Cu"
        assert lk.b.kind == "zone" and lk.b.layer == "In1.Cu"

    def test_cross_net_and_single_item_dropped(self):
        v = _unconnected(1)[0]
        v.items[1] = "Zone [VCC] on In1.Cu"
        solo = _unconnected(1)[0]
        solo.items = solo.items[:1]
        assert links_from_violations([v, solo]) == []

    def test_no_net_marker(self):
        ep = OracleEndpoint.parse("Pad 1 [<no net>] of U1", 0, 0)
        assert ep.net is None


class TestLoopTermination:
    def test_reaches_zero(self, tmp_path):
        b = FakeBoard(tmp_path, 3)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [-3]), nets={"GND"}, max_rounds=3
        )
        assert res.stop_reason == STOP_CONVERGED
        assert (res.initial_links, res.final_links, res.converged) == (3, 0, True)
        assert len(res.rounds) == 1 and res.rounds[0].kept

    def test_already_clean_runs_no_rounds(self, tmp_path):
        b = FakeBoard(tmp_path, 0)
        closer = _closer(b, [-1])
        res = run_oracle_completion(b.path, oracle=b.oracle, closer=closer, nets={"GND"})
        assert res.stop_reason == STOP_CONVERGED and closer.calls["n"] == 0

    def test_multiple_rounds_then_zero(self, tmp_path):
        b = FakeBoard(tmp_path, 4)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [-2, -2]), nets={"GND"}, max_rounds=5
        )
        assert res.stop_reason == STOP_CONVERGED
        assert [r.links_after for r in res.rounds] == [2, 0]

    def test_stops_when_count_stops_falling_and_restores(self, tmp_path):
        b = FakeBoard(tmp_path, 4)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [-1, 0]), nets={"GND"}, max_rounds=5
        )
        assert res.stop_reason == STOP_NO_PROGRESS
        assert res.final_links == 3
        assert b.state() == (3, 0)  # the non-improving round was rolled back
        assert [r.kept for r in res.rounds] == [True, False]

    def test_worse_round_is_restored(self, tmp_path):
        b = FakeBoard(tmp_path, 2)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [+1]), nets={"GND"}, max_rounds=5
        )
        assert res.stop_reason == STOP_NO_PROGRESS and b.state() == (2, 0)

    def test_max_rounds_budget(self, tmp_path):
        b = FakeBoard(tmp_path, 10)
        closer = _closer(b, [-1])
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=closer, nets={"GND"}, max_rounds=3
        )
        assert res.stop_reason == STOP_MAX_ROUNDS
        assert closer.calls["n"] == 3 and res.final_links == 7

    def test_zero_rounds_is_a_noop(self, tmp_path):
        b = FakeBoard(tmp_path, 2)
        closer = _closer(b, [-2])
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=closer, nets={"GND"}, max_rounds=0
        )
        assert closer.calls["n"] == 0 and res.final_links == 2

    def test_closer_places_nothing(self, tmp_path):
        b = FakeBoard(tmp_path, 2)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [0], applied=0), nets={"GND"}
        )
        assert res.stop_reason == STOP_NO_CLOSER and res.final_links == 2

    def test_drc_regression_rejected_and_restored(self, tmp_path):
        b = FakeBoard(tmp_path, 3)
        res = run_oracle_completion(
            b.path, oracle=b.oracle, closer=_closer(b, [-3], err_delta=1), nets={"GND"}
        )
        assert res.stop_reason == STOP_DRC_REGRESSED
        assert b.state() == (3, 0) and res.final_links == 3

    def test_oracle_not_run(self, tmp_path):
        p = tmp_path / "x"
        p.write_text("")
        res = run_oracle_completion(
            p,
            oracle=lambda _p: GeometricDRCResult(ran=False, note="no kicad-cli"),
            closer=lambda *_a: ClosureAttempt(applied=1),
            nets={"GND"},
        )
        assert not res.ran and res.stop_reason == STOP_NOT_RUN and not res.converged

    def test_oracle_fails_midloop(self, tmp_path):
        b = FakeBoard(tmp_path, 2)
        seen = {"n": 0}

        def flaky(path):
            seen["n"] += 1
            return b.oracle(path) if seen["n"] == 1 else GeometricDRCResult(ran=False)

        res = run_oracle_completion(b.path, oracle=flaky, closer=_closer(b, [-2]), nets={"GND"})
        assert res.stop_reason == STOP_ORACLE_FAILED and b.state() == (2, 0)

    def test_out_of_scope_links_ignored(self, tmp_path):
        b = FakeBoard(tmp_path, 3)
        closer = _closer(b, [-3])
        res = run_oracle_completion(b.path, oracle=b.oracle, closer=closer, nets={"VCC"})
        assert res.initial_links == 0 and closer.calls["n"] == 0

    def test_drc_regression_culprit_banned_on_retry(self, tmp_path):
        b = FakeBoard(tmp_path, 2)
        state = {"n": 0}
        err_at = (20.0, 5.0)

        def oracle(path):
            links, errors = b.state()
            geo = _geo(links, errors)
            if errors:
                geo.error_violations = [
                    DRCViolation(
                        type=ViolationType.CLEARANCE,
                        type_str="clearance",
                        severity=Severity.ERROR,
                        message="x",
                        locations=[Location(*err_at)],
                        items=["Track on F.Cu"],
                    )
                ]
            return geo

        def closer(path, links, banned):
            state["n"] += 1
            if "pad:GND:C1.1" in banned:
                path.write_text("0,0")
                return ClosureAttempt(applied=1)
            path.write_text("0,1")
            return ClosureAttempt(
                applied=1, footprints={"pad:GND:C1.1": [(20.0, 5.0, 20.0, 5.0, 0.3)]}
            )

        res = run_oracle_completion(b.path, oracle=oracle, closer=closer, nets={"GND"})
        assert res.stop_reason == STOP_CONVERGED
        assert state["n"] == 2 and res.rounds[0].banned == ("pad:GND:C1.1",)


class TestVerdictMapping:
    def test_blocks_only_without_opt_in(self):
        assert stranded_pour_blocks(2, False)
        assert not stranded_pour_blocks(2, True)
        assert not stranded_pour_blocks(0, False)

    @pytest.mark.parametrize(
        ("rc", "stranded", "allow", "expected"),
        [
            (0, 0, False, 0),
            (0, 2, False, 3),  # board 02: was exit 0, now fails like kicad-cli
            (2, 2, False, 4),
            (0, 2, True, 0),  # explicit advisory opt-in keeps the old behaviour
            (2, 2, True, 2),
            (1, 2, False, 1),  # more specific codes pass through
            (8, 2, False, 8),
        ],
    )
    def test_exit_mapping(self, rc, stranded, allow, expected):
        assert stranded_pour_exit(rc, stranded, allow) == expected

    def test_env_opt_in(self, monkeypatch):
        monkeypatch.delenv(ALLOW_STRANDED_ENV, raising=False)
        assert not allow_stranded_pour_pads()
        assert allow_stranded_pour_pads(True)
        monkeypatch.setenv(ALLOW_STRANDED_ENV, "1")
        assert allow_stranded_pour_pads()

    def test_oracle_rounds(self, monkeypatch):
        monkeypatch.delenv(ORACLE_DISABLE_ENV, raising=False)
        assert oracle_rounds(None, 3) == 3
        assert oracle_rounds(0, 3) == 0
        assert oracle_rounds(-4, 3) == 0
        monkeypatch.setenv(ORACLE_DISABLE_ENV, "0")
        assert oracle_rounds(5, 3) == 0


class TestStitchOverlapFilter:
    def test_overlap_catches_off_centre_drill_that_inside_test_misses(self):
        from kicad_tools.cli.stitch_cmd import (
            _via_drill_inside_pad_bbox,
            _via_drill_overlaps_pad_bbox,
        )

        bbox = (154.95, 116.775, 155.95, 118.225)  # board 02 C1.2 land
        # The #5785 regression: hole centre 0.055 mm outside the land edge.
        assert not _via_drill_inside_pad_bbox(155.17, 118.28, 0.3, bbox)
        assert _via_drill_overlaps_pad_bbox(155.17, 118.28, 0.3, bbox)
        # Tangent / clear holes are not overlaps.
        assert not _via_drill_overlaps_pad_bbox(155.17, 118.375, 0.3, bbox)
        assert not _via_drill_overlaps_pad_bbox(155.17, 119.0, 0.3, bbox)


class TestCliSurfaces:
    def test_route_parser_has_flags(self):
        from kicad_tools.cli import route_cmd

        src = Path(route_cmd.__file__).read_text()
        assert '"--oracle-rounds"' in src and '"--allow-stranded-pour-pads"' in src

    def test_net_status_opt_in_changes_exit(self, monkeypatch):
        from types import SimpleNamespace

        from kicad_tools.cli.net_status_cmd import exit_code_for

        monkeypatch.delenv(ALLOW_STRANDED_ENV, raising=False)
        # One incomplete pour net: raw count 1, blocking count 0.
        pour_only = SimpleNamespace(
            incomplete_count=1, blocking_incomplete_count=0, unrouted_count=0
        )
        assert exit_code_for(pour_only) == 2  # default: same verdict as kicad-cli
        assert exit_code_for(pour_only, True) == 0  # explicit advisory opt-in
        monkeypatch.setenv(ALLOW_STRANDED_ENV, "1")
        assert exit_code_for(pour_only) == 0
        signal_gap = SimpleNamespace(
            incomplete_count=1, blocking_incomplete_count=1, unrouted_count=0
        )
        assert exit_code_for(signal_gap, True) == 2  # opt-in never hides a signal gap


class TestDeclaredClearanceRetry:
    """Issue #6288: links the working clearance cannot close are retried at the
    board's declared clearance (board 05 U3: 0.5 mm-pitch pads boxed in by
    copper the router kept 0.15 mm apart, while the board declares 0.1016 mm).
    """

    @staticmethod
    def _link() -> oc.OracleLink:
        a = oc.OracleEndpoint.parse("Pad 7 [GND] of U3 on F.Cu", 136.25, 83.75)
        b = oc.OracleEndpoint.parse("Zone [GND] on F.Cu", 150.0, 92.0)
        return oc.OracleLink(net="GND", a=a, b=b)

    def test_relaxed_clearance_is_the_declared_rule(self) -> None:
        from kicad_tools.router.clearance_resolver import DeclaredClearanceRules

        declared = DeclaredClearanceRules(dru_mm=0.1016, project_min_clearance_mm=0.1016)
        assert oc.relaxed_closure_clearance(declared, None, 0.15) == pytest.approx(0.1016)

    def test_no_relaxation_when_not_tighter_or_undeclared(self) -> None:
        from kicad_tools.router.clearance_resolver import DeclaredClearanceRules

        declared = DeclaredClearanceRules(dru_mm=0.2)
        assert oc.relaxed_closure_clearance(declared, None, 0.15) is None
        assert oc.relaxed_closure_clearance(DeclaredClearanceRules(), None, 0.15) is None
        assert oc.relaxed_closure_clearance(None, None, 0.15) is None

    def _patch_close(self, monkeypatch, closes_at: float | None):
        calls: list[tuple[float, int]] = []

        def fake_close(self, pcb_path, links, banned):
            calls.append((self.clearance, len(links)))
            attempt = oc.ClosureAttempt()
            if closes_at is not None and abs(self.clearance - closes_at) < 1e-9:
                for lk in links:
                    attempt.footprints[oc._route_key(lk)] = [(0.0, 0.0, 1.0, 0.0, 0.1)]
                    attempt.applied += 1
            else:
                attempt.notes.append("GND U3.7: no valid via location")
            return attempt, list(links)

        monkeypatch.setattr(oc.PourLinkCloser, "_close", fake_close)
        return calls

    def _closer(self, fallback: float | None) -> oc.PourLinkCloser:
        return oc.PourLinkCloser(
            via_size=0.6,
            via_drill=0.3,
            clearance=0.15,
            trace_width=0.2,
            fallback_clearance=fallback,
        )

    def test_open_link_is_retried_at_declared_clearance(self, monkeypatch, tmp_path) -> None:
        calls = self._patch_close(monkeypatch, closes_at=0.1016)
        attempt = self._closer(0.1016)(tmp_path / "b.kicad_pcb", [self._link()], frozenset())
        assert calls == [(0.15, 1), (0.1016, 1)]
        assert attempt.applied == 1
        assert oc._route_key(self._link()) in attempt.footprints

    def test_closed_link_is_not_retried(self, monkeypatch, tmp_path) -> None:
        calls = self._patch_close(monkeypatch, closes_at=0.15)
        attempt = self._closer(0.1016)(tmp_path / "b.kicad_pcb", [self._link()], frozenset())
        assert calls == [(0.15, 1)]
        assert attempt.applied == 1

    def test_retry_disabled_without_fallback(self, monkeypatch, tmp_path) -> None:
        calls = self._patch_close(monkeypatch, closes_at=0.1016)
        attempt = self._closer(None)(tmp_path / "b.kicad_pcb", [self._link()], frozenset())
        assert calls == [(0.15, 1)]
        assert attempt.applied == 0

    def test_banned_link_is_not_retried(self, monkeypatch, tmp_path) -> None:
        calls = self._patch_close(monkeypatch, closes_at=0.1016)
        banned = frozenset({oc._route_key(self._link())})
        self._closer(0.1016)(tmp_path / "b.kicad_pcb", [self._link()], banned)
        assert calls == [(0.15, 1)]

    def test_route_stage_resolves_fallback_from_emitted_project(self, tmp_path) -> None:
        import json
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import _pour_closure_fallback_clearance

        pcb = tmp_path / "board_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240108))\n")
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps({"board": {"design_settings": {"rules": {"min_clearance": 0.1016}}}})
        )
        args = SimpleNamespace(manufacturer="jlcpcb-tier1")
        assert _pour_closure_fallback_clearance(pcb, args, 0.15) == pytest.approx(0.1016)
        assert _pour_closure_fallback_clearance(pcb, args, 0.1016) is None
