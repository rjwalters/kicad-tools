"""Oracle completion loop + one stranded-pour verdict (Issue #5785)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
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
    STOP_DEADLINE,
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


class TestIslandJoinTargetsPlane:
    """Issue #6399: a stranded pad island joins the plane, not the nearest sliver."""

    def _board(self, tmp_path: Path, walled: bool = False, island: bool = False) -> Path:
        def pad(ref, x, y, w, h):
            return (
                f'  (footprint "T:{ref}" (layer "F.Cu")\n    (at {x:g} {y:g})\n'
                f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
                f'    (pad "1" smd rect (at 0 0) (size {w:g} {h:g}) (layers "F.Cu") '
                f'(net 1 "GND"))\n  )\n'
            )

        text = (
            '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
            '  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n'
            '  (net 0 "")\n  (net 1 "GND")\n  (net 2 "SIG")\n'
            + pad("R1", 10, 10, 0.8, 0.8)  # the stranded pad island
            + pad("R9", 22, 10, 4, 8)  # the plane (largest component)
            # Nearer to R1 than the plane is: a padless sliver or (Issue
            # #6412) a second stranded pad island R2.
            + (
                pad("R2", 14, 10, 0.8, 0.8)
                if island
                else '  (segment (start 13 8) (end 13 12) (width 0.2) (layer "F.Cu") (net 1))\n'
            )
            # Optionally ring the plane with other-net copper so it is unreachable.
            + (
                "".join(
                    f'  (segment (start {a}) (end {b}) (width 0.3) (layer "F.Cu") (net 2))\n'
                    for a, b in (
                        ("18 4", "26 4"),
                        ("26 4", "26 16"),
                        ("26 16", "18 16"),
                        ("18 16", "18 4"),
                    )
                )
                if walled
                else ""
            )
            + '  (gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts") (width 0.1))\n)\n'
        )
        path = tmp_path / "b.kicad_pcb"
        path.write_text(text)
        return path

    def test_committed_route_ends_on_the_plane(self, tmp_path):
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import OracleLink, PourLinkCloser
        from kicad_tools.schema.pcb import PCB

        board = self._board(tmp_path)
        pad_end = OracleEndpoint(
            description="Pad 1 [GND] of R1",
            kind="pad",
            net="GND",
            x=10.0,
            y=10.0,
            layer="F.Cu",
            ref="R1",
            pad="1",
        )
        zone_end = OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=10.0, y=10.0)
        attempt = ClosureAttempt()
        closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
        closer._route_links(board, [OracleLink(net="GND", a=pad_end, b=zone_end)], attempt)
        assert attempt.applied == 1, attempt.notes
        assert not any("nearest island" in n for n in attempt.notes)

        pcb = PCB.load(str(board))
        # Everything written beyond the original sliver segment.
        new = [s for s in pcb.segments if s.start != (13.0, 8.0) and s.end != (13.0, 12.0)]
        assert new
        # The plane pad spans x in [20, 24]; the route must reach it.
        assert max(max(s.start[0], s.end[0]) for s in new) >= 20.0 - 1e-6

    def test_unreachable_plane_falls_back_and_says_so(self, tmp_path):
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import OracleLink, PourLinkCloser

        board = self._board(tmp_path, walled=True)
        pad_end = OracleEndpoint(
            description="Pad 1 [GND] of R1",
            kind="pad",
            net="GND",
            x=10.0,
            y=10.0,
            layer="F.Cu",
            ref="R1",
            pad="1",
        )
        zone_end = OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=10.0, y=10.0)
        attempt = ClosureAttempt()
        closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
        closer._route_links(board, [OracleLink(net="GND", a=pad_end, b=zone_end)], attempt)
        assert attempt.applied == 1, attempt.notes
        assert attempt.notes == [
            "GND R1.1: no clearance-legal route to the plane-owning copper; "
            "joined the nearest island instead"
        ]


def _r1_zone_link():
    from kicad_tools.router.oracle_completion import OracleLink

    pad_end = OracleEndpoint(
        description="Pad 1 [GND] of R1",
        kind="pad",
        net="GND",
        x=10.0,
        y=10.0,
        layer="F.Cu",
        ref="R1",
        pad="1",
    )
    zone_end = OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=10.0, y=10.0)
    return OracleLink(net="GND", a=pad_end, b=zone_end)


def _new_gnd_segments(path: Path) -> list:
    from kicad_tools.schema.pcb import PCB

    return list(PCB.load(str(path)).segments)


def _touches_r2(seg) -> bool:
    # R2 is a 0.8 mm square pad centred on (14, 10).
    return any(abs(x - 14.0) <= 0.45 and abs(y - 10.0) <= 0.45 for x, y in (seg.start, seg.end))


class TestWithdrawnPlaneJoinFallsBackToIsland:
    """Issue #6412: a withdrawn ``#plane`` join leaves the ``#island`` join eligible."""

    @staticmethod
    def _board(tmp_path: Path, **kwargs) -> Path:
        tmp_path.mkdir(parents=True, exist_ok=True)
        return TestIslandJoinTargetsPlane()._board(tmp_path, **kwargs)

    def test_route_variants_have_their_own_keys(self, tmp_path):
        """T1: banning ``#plane`` routes the nearest island under ``#island``."""
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import PourLinkCloser, _route_key

        lk = _r1_zone_link()
        closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)

        free = self._board(tmp_path / "free", island=True)
        attempt = ClosureAttempt()
        closer._route_links(free, [lk], attempt)
        assert attempt.applied == 1, attempt.notes
        assert list(attempt.footprints) == [_route_key(lk, "plane")]
        assert max(max(s.start[0], s.end[0]) for s in _new_gnd_segments(free)) >= 20.0 - 1e-6

        board = self._board(tmp_path / "banned", island=True)
        attempt = ClosureAttempt()
        closer._route_links(board, [lk], attempt, frozenset({_route_key(lk, "plane")}))
        assert attempt.applied == 1, attempt.notes
        assert list(attempt.footprints) == [_route_key(lk, "island")]
        segs = _new_gnd_segments(board)
        assert segs and all(max(s.start[0], s.end[0]) < 20.0 for s in segs)
        assert any(_touches_r2(s) for s in segs)
        assert attempt.notes == [
            "GND R1.1: plane join withdrawn for KiCad DRC; joined the nearest island instead"
        ]

    def test_every_variant_banned_says_withdrawn(self, tmp_path):
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import PourLinkCloser, _route_key

        lk = _r1_zone_link()
        closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
        board = self._board(tmp_path, island=True)
        attempt = ClosureAttempt()
        banned = frozenset({_route_key(lk, "plane"), _route_key(lk, "island")})
        closer._route_links(board, [lk], attempt, banned)
        assert attempt.applied == 0
        assert len(attempt.notes) == 1
        assert "every route withdrawn for KiCad DRC" in attempt.notes[0]
        assert not any("no clearance-legal route" in n for n in attempt.notes)

    def test_withdrawn_direct_route_still_withdraws_the_link(self, tmp_path):
        """A banned bare key keeps the pre-#6412 whole-link ban: no join is tried."""
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import PourLinkCloser, _route_key

        lk = _r1_zone_link()
        closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
        board = self._board(tmp_path, island=True)
        attempt = ClosureAttempt()
        closer._route_links(board, [lk], attempt, frozenset({_route_key(lk)}))
        assert attempt.applied == 0 and attempt.footprints == {}
        assert attempt.notes == []

    def test_withdrawn_plane_join_retries_nearest_island_same_round(self, tmp_path):
        """T2: the loop's retry routes ``#island`` instead of dropping the link."""
        pytest.importorskip("shapely")
        from kicad_tools.router.oracle_completion import PourLinkCloser, _route_key

        board = self._board(tmp_path, island=True)
        link_violation = DRCViolation(
            type=ViolationType.UNCONNECTED_ITEMS,
            type_str="unconnected_items",
            severity=Severity.ERROR,
            message="Missing connection between items",
            locations=[Location(10.0, 10.0), Location(22.0, 10.0)],
            items=["Pad 1 [GND] of R1 on F.Cu", "Zone [GND] on F.Cu"],
        )
        lk = links_from_violations([link_violation])[0]

        def oracle(path):
            segs = _new_gnd_segments(path)
            errors = [
                DRCViolation(
                    type=ViolationType.CLEARANCE,
                    type_str="clearance",
                    severity=Severity.ERROR,
                    message="plane join",
                    locations=[Location((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)],
                    items=["Track on F.Cu"],
                )
                for s in segs
                if max(s.start[0], s.end[0]) >= 20.0
            ][:1]
            joined = any(_touches_r2(s) for s in segs) and not errors
            return GeometricDRCResult(
                ran=True,
                error_count=len(errors),
                error_violations=errors,
                unconnected_items=[] if joined else [link_violation],
            )

        class RouteOnlyCloser(PourLinkCloser):
            # This fixture has no pour for a weld via to land in; leave the
            # link to the routed joins this issue is about.
            def _weld_pads(self, pcb_path, pads_by_net, attempt):
                return set()

        closer = RouteOnlyCloser(
            via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2, refill=None
        )
        res = run_oracle_completion(board, oracle=oracle, closer=closer, nets={"GND"})
        plane = _route_key(lk, "plane")
        assert res.rounds[0].attempts == 2, (res.rounds, res.closer_notes)
        assert res.rounds[0].banned == (plane,)
        assert res.rounds[0].kept
        assert res.final_links == 0 and res.stop_reason == STOP_CONVERGED
        assert f"{plane}: withdrawn -- its copper raised KiCad DRC by 1" in res.closer_notes


class TestCloserReasonsPrinted:
    def test_reasons_match_link_pad(self):
        from kicad_tools.cli.route_cmd import _closer_reasons_for
        from kicad_tools.router.oracle_completion import OracleLink

        pad = OracleEndpoint(
            description="Pad 36 [+3.3V] of U2",
            kind="pad",
            net="+3.3V",
            x=0,
            y=0,
            ref="U2",
            pad="36",
        )
        zone = OracleEndpoint(description="Zone [+3.3V]", kind="zone", net="+3.3V", x=0, y=0)
        lk = OracleLink(net="+3.3V", a=pad, b=zone)
        notes = ["+3.3V U2.36: no via site", "GND C1.1: other"]
        assert _closer_reasons_for(lk, notes) == ["+3.3V U2.36: no via site"]

    def test_pad_key_matches_whole_token(self):
        from kicad_tools.cli.route_cmd import _closer_reasons_for
        from kicad_tools.router.oracle_completion import OracleLink

        pad = OracleEndpoint(
            description="Pad 3 [+3.3V] of U2", kind="pad", net="+3.3V", x=0, y=0, ref="U2", pad="3"
        )
        zone = OracleEndpoint(description="Zone [+3.3V]", kind="zone", net="+3.3V", x=0, y=0)
        lk = OracleLink(net="+3.3V", a=pad, b=zone)
        notes = ["+3.3V U2.36: no via site", "+3.3V U2.3: boxed in"]
        assert _closer_reasons_for(lk, notes) == ["+3.3V U2.3: boxed in"]

    def test_result_carries_closer_notes(self, tmp_path):
        board = FakeBoard(tmp_path, 2)

        def closer(path, links, banned):
            board.path.write_text("1,0")
            return ClosureAttempt(applied=1, notes=["GND C1.1: boxed in"])

        res = run_oracle_completion(
            board.path, oracle=board.oracle, closer=closer, nets=["GND"], max_rounds=1
        )
        assert res.closer_notes == ["GND C1.1: boxed in"]

    def test_deadline_round_keeps_previous_reasons(self, tmp_path):
        """A round stopped by the deadline before any attempt must not wipe notes."""
        board = FakeBoard(tmp_path, 3)

        def closer(path, links, banned):
            n, e = board.state()
            board.path.write_text(f"{n - 1},{e}")
            return ClosureAttempt(applied=1, notes=["GND C1.1: boxed in"])

        # Initial DRC and round 1's attempt are affordable; round 2's is not.
        budget = iter([None, None])

        res = run_oracle_completion(
            board.path,
            oracle=board.oracle,
            closer=closer,
            nets=["GND"],
            max_rounds=3,
            time_left=lambda: next(budget, 0.0),
            call_cost_estimate=1.0,
        )
        assert res.stop_reason == STOP_DEADLINE
        assert res.rounds[-1].attempts == 0
        assert res.closer_notes == ["GND C1.1: boxed in"]

    def test_withdrawn_culprit_explains_a_no_closer_round(self, tmp_path):
        """Board 04's shape: every weld is withdrawn, the last attempt is empty."""
        from kicad_tools.cli.route_cmd import _closer_reasons_for

        b = FakeBoard(tmp_path, 2)

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
                        locations=[Location(20.0, 5.0)],
                        items=["Track on F.Cu"],
                    )
                ]
            return geo

        def closer(path, links, banned):
            if "pad:GND:C1.1" in banned:
                return ClosureAttempt(applied=0)
            path.write_text("2,1")
            return ClosureAttempt(
                applied=1, footprints={"pad:GND:C1.1": [(20.0, 5.0, 20.0, 5.0, 0.3)]}
            )

        res = run_oracle_completion(b.path, oracle=oracle, closer=closer, nets={"GND"})
        assert res.stop_reason == STOP_NO_CLOSER
        assert res.closer_notes == ["pad:GND:C1.1: withdrawn -- its copper raised KiCad DRC by 1"]
        lk = links_from_violations(_unconnected(1))[0]  # C1.1 <-> zone
        assert _closer_reasons_for(lk, res.closer_notes) == res.closer_notes

    def test_route_key_names_its_link(self):
        from kicad_tools.cli.route_cmd import _closer_reasons_for
        from kicad_tools.router.oracle_completion import OracleLink, _route_key

        a = OracleEndpoint(description="Pad 2 [GND] of C1 on F.Cu", kind="pad", net="GND", x=0, y=0)
        b = OracleEndpoint(description="Pad 2 [GND] of U1 on F.Cu", kind="pad", net="GND", x=1, y=0)
        lk = OracleLink(net="GND", a=a, b=b)
        other = OracleLink(
            net="GND",
            a=a,
            b=OracleEndpoint(
                description="Pad 2 [GND] of C10 on F.Cu", kind="pad", net="GND", x=2, y=0
            ),
        )
        note = f"{_route_key(lk)}: withdrawn -- its copper raised KiCad DRC by 3"
        assert _closer_reasons_for(lk, [note]) == [note]
        assert _closer_reasons_for(other, [note]) == []
        # Issue #6412: each join variant's withdrawn note names its link too.
        for variant in ("plane", "island"):
            note = f"{_route_key(lk, variant)}: withdrawn -- its copper raised KiCad DRC by 3"
            assert _closer_reasons_for(lk, [note]) == [note]
            assert _closer_reasons_for(other, [note]) == []

    def test_route_key_does_not_match_longer_ref(self):
        """``Pad 2 [GND] of C1`` is a substring of ``... of C10``; the key is exact."""
        from kicad_tools.cli.route_cmd import _closer_reasons_for
        from kicad_tools.router.oracle_completion import OracleLink, _route_key

        def link(ref):
            p = OracleEndpoint(
                description=f"Pad 2 [GND] of {ref}",
                kind="pad",
                net="GND",
                x=0,
                y=0,
                ref=ref,
                pad="2",
            )
            z = OracleEndpoint(description="Zone [GND]", kind="zone", net="GND", x=0, y=0)
            return OracleLink(net="GND", a=p, b=z)

        note = f"{_route_key(link('C10'))}: withdrawn -- its copper raised KiCad DRC by 1"
        assert _closer_reasons_for(link("C1"), [note]) == []
        assert _closer_reasons_for(link("C10"), [note]) == [note]
        # Issue #6412: the variant keys stay exact too.
        for variant in ("plane", "island"):
            note = (
                f"{_route_key(link('C10'), variant)}: withdrawn -- its copper raised KiCad DRC by 1"
            )
            assert _closer_reasons_for(link("C1"), [note]) == []
            assert _closer_reasons_for(link("C10"), [note]) == [note]

    def test_padless_link_falls_back_to_its_net(self):
        """Board 04: KiCad names ``Zone <-> Zone``; the closer's notes name U2.36."""
        from kicad_tools.cli.route_cmd import _closer_reasons_for
        from kicad_tools.router.oracle_completion import OracleLink

        z = OracleEndpoint(description="Zone [+3.3V] on F.Cu", kind="zone", net="+3.3V", x=0, y=0)
        lk = OracleLink(net="+3.3V", a=z, b=z)
        notes = [
            "+3.3V U2.36: no valid via location",
            "pad:+3.3V:U2.36: withdrawn -- its copper raised KiCad DRC by 2",
            "GND C1.1: boxed in",
            "+3.3VA U9.1: other net",
        ]
        assert _closer_reasons_for(lk, notes) == notes[:2]
