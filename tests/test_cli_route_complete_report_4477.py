"""``--complete`` bounded termination + blocking-copper report (Issue #4477).

Phase 4 of ``kct route --complete`` (epic #4465).  Scope:

1. A per-link deadline bounds a completion pass instead of grinding (the
   #4434 ">10 minutes" failure mode) -- Phase 2 (#4472) already wires the
   deadline into the lattice negotiation; this phase asserts the CLI-level
   completion attempt actually returns within it on a link that is
   deliberately, topologically unroutable.
2. An unroutable link is reported with the blocking copper enumerated,
   reusing the SAME ``classify_stuck_nets`` machinery ``kct net-status --why``
   prints -- both as a human-readable summary and as a structured
   ``--complete-report PATH`` JSON artifact.
3. ``--complete`` exits nonzero when a link it was asked to close remains
   unroutable, independent of the (irrelevant here) ``--min-completion``
   threshold.

Fixture: NET2's second pad (``U1.1``) sits inside a fully-closed rectangular
copper picture-frame (net ``WALL``, four segments whose corners share exact
endpoints so the loop is closed under BOTH the default 0.01mm-proximity
connectivity model used by ``classify_stuck_nets`` AND real router geometry).
A pad strictly inside a closed loop of another net's copper cannot be routed
out without crossing that copper on a single-layer board -- this is
topologically unroutable, not merely "hard", so the test never depends on
router heuristics or resolution to hold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.route_cmd import main as route_main

# Issue #6134: each CLI test here costs ~2.7 s locally (the --complete
# lattice search on the walled link), but hit CI's 60 s default under
# xdist/native-slot contention (>20x inflation).  Match the 180 s
# convention used by the sibling lattice CLI tests
# (tests/router/lattice/test_post_pass_gating.py).
pytestmark = pytest.mark.timeout(180)

# ---------------------------------------------------------------------------
# Walled-pocket fixture: NET1 fully routed (preserved-copper control), NET2's
# second pad walled in by the closed WALL picture-frame (genuinely
# unroutable), NET4 freely routable (so a multi-link run has a MIX of
# success/failure and exercises the nonzero-exit path distinct from the
# "nothing routed at all" fatal code).
# ---------------------------------------------------------------------------
WALLED_POCKET_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "NET2")
  (net 3 "WALL")
  (net 4 "NET4")
  (gr_rect (start 0 0) (end 60 40)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 10 10)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 10 30)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.3 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET2"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 40 20)
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "target" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at 0 0) (size 0.3 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "NET2"))
  )
  (footprint "wall_pad" (layer "F.Cu") (at 39 19)
    (property "Reference" "W1")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "WALL"))
  )
  (footprint "wall_pad" (layer "F.Cu") (at 41 21)
    (property "Reference" "W2")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "WALL"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000040")
    (at 20 5)
    (property "Reference" "R4" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 4 "NET4"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 4 "NET4"))
  )
  (segment (start 9.5 10) (end 10.5 10) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 39 19) (end 41 19) (width 0.6) (layer "F.Cu") (net 3))
  (segment (start 41 19) (end 41 21) (width 0.6) (layer "F.Cu") (net 3))
  (segment (start 41 21) (end 39 21) (width 0.6) (layer "F.Cu") (net 3))
  (segment (start 39 21) (end 39 19) (width 0.6) (layer "F.Cu") (net 3))
)
"""


@pytest.fixture(autouse=True)
def _no_post_route_kicad_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the post-route zone fill and pour-oracle kicad-cli stages (#6134).

    Every CLI test here asserts on the route result (rc, spy counts, report
    text, ``Nets routed``, output exists); none reads the zone fill or the
    pour oracle, which run AFTER routing as serialized ``kicad-cli`` launches
    (~2-7 s locally, and subject to native-slot waits under CI load).  Both
    stubs reproduce the documented "kicad-cli unavailable" behaviour: the
    fill is skipped and the oracle leaves the board untouched with 0
    stranded links (same approach as tests/test_route_plan_gate_5521.py).
    """
    from kicad_tools.cli import route_cmd

    def _no_oracle(output_path, *, args, quiet=False, **_kw):
        args._stranded_pour_links = 0
        return 0

    monkeypatch.setattr(route_cmd, "_fill_zones_after_route", lambda *a, **k: None)
    monkeypatch.setattr(route_cmd, "_run_pour_oracle_stage", _no_oracle)


@pytest.fixture
def walled_board(tmp_path: Path) -> Path:
    board = tmp_path / "walled.kicad_pcb"
    board.write_text(WALLED_POCKET_BOARD)
    return board


def _segment_nets(pcb_text: str) -> list[int]:
    import re

    return [int(m.group(1)) for m in re.finditer(r"\(segment.*?\(net (\d+)\)", pcb_text, re.S)]


class TestBoundedTermination:
    """AC1: the completion attempt returns within the configured deadline."""

    def test_walled_link_terminates_bounded(self, walled_board: Path, tmp_path: Path):
        out = tmp_path / "out.kicad_pcb"
        # --skip-drc: isolate the router's own bounded-search guarantee from
        # the (unrelated, pre-existing) kicad-cli DRC subprocess wall-clock.
        rc = route_main(
            [str(walled_board), "-o", str(out), "--complete", "--backend", "cpp", "--skip-drc"]
        )
        # NET2 cannot close (topologically walled); NET4 can -- exit 8, not
        # the "nothing routed" fatal code 1 (see TestExitSemantics below).
        assert rc == 8
        # NET1 (control) and WALL (fixed obstacle) are preserved; NET4 closed;
        # NET2 never received new copper (it was declined, not shipped).
        seg_nets = _segment_nets(out.read_text())
        assert 1 in seg_nets and 3 in seg_nets and 4 in seg_nets

    def test_completes_well_under_the_ten_minute_failure_mode(
        self, walled_board: Path, tmp_path: Path
    ):
        import time

        out = tmp_path / "out.kicad_pcb"
        started = time.monotonic()
        route_main(
            [str(walled_board), "-o", str(out), "--complete", "--backend", "cpp", "--skip-drc"]
        )
        elapsed = time.monotonic() - started
        # The #4434 failure mode was >10 minutes (600s) non-termination on a
        # single walled link.  A generous CI ceiling that is still a small
        # fraction of that -- the per-link deadline default is 60s/link.
        assert elapsed < 90.0


class TestBlockingCopperReport:
    """AC2: an unroutable link names the blocking copper + reason."""

    def test_human_readable_report_printed(self, walled_board: Path, tmp_path: Path, capsys):
        out = tmp_path / "out.kicad_pcb"
        route_main(
            [str(walled_board), "-o", str(out), "--complete", "--backend", "cpp", "--skip-drc"]
        )
        text = capsys.readouterr().out
        assert "unroutable link" in text
        assert "NET2" in text
        assert "WALL" in text  # the blocking copper is named

    def test_json_report_schema(self, walled_board: Path, tmp_path: Path):
        out = tmp_path / "out.kicad_pcb"
        report_path = tmp_path / "report.json"
        route_main(
            [
                str(walled_board),
                "-o",
                str(out),
                "--complete",
                "--backend",
                "cpp",
                "--skip-drc",
                "--complete-report",
                str(report_path),
                # This test pins the report schema, not deadline behaviour
                # (TestBoundedTermination owns that). A generous per-link
                # budget keeps a loaded CI runner from hitting the 30s
                # default backstop (Issue #6468).
                "--per-net-timeout",
                "600",
            ]
        )
        assert report_path.exists()
        report = json.loads(report_path.read_text())
        assert report["deadline_hit"] is False
        assert isinstance(report["elapsed_s"], int | float)
        assert report["budget_s"] is not None

        links = report["unroutable_links"]
        assert len(links) == 1
        entry = links[0]
        assert entry["net"] == "NET2"
        assert entry["link"] == {"start": "R2.1", "end": "U1.1"}
        assert entry["reason"]  # a decline reason is always present
        assert "WALL" in entry["blocking_copper"]
        # Issue #9581: this link was attempted and declined for a geometric
        # reason (generous per-net budget, run deadline never hit) -- run
        # totals stay at report level; per-link timing is null.
        assert entry["attempted"] is True
        assert entry["deadline_hit"] is False
        assert entry["elapsed_s"] is None
        assert entry["budget_s"] is None

    def test_no_report_file_when_nothing_unroutable(self, tmp_path: Path):
        # A fully-solvable board (no walled pocket) must not fabricate a
        # report: --complete-report is only written when links remain
        # unroutable.
        from tests.test_cli_route_complete_4471 import STRANDED_BOARD

        board = tmp_path / "solvable.kicad_pcb"
        board.write_text(STRANDED_BOARD)
        out = tmp_path / "out.kicad_pcb"
        report_path = tmp_path / "report.json"
        rc = route_main(
            [
                str(board),
                "-o",
                str(out),
                "--complete",
                "--backend",
                "cpp",
                "--skip-drc",
                "--complete-report",
                str(report_path),
            ]
        )
        assert rc == 0
        assert not report_path.exists()


class TestExitSemantics:
    """AC3: --complete exits nonzero when links remain unroutable."""

    def test_mixed_success_and_failure_exits_8(self, walled_board: Path, tmp_path: Path):
        # NET4 routes cleanly, NET2 does not: distinct from the "nothing
        # routed at all" fatal code (1) -- exit 8 says "not everything asked
        # for was closed", which --min-completion must never mask.
        out = tmp_path / "out.kicad_pcb"
        rc = route_main(
            [str(walled_board), "-o", str(out), "--complete", "--backend", "cpp", "--skip-drc"]
        )
        assert rc == 8

    def test_lenient_min_completion_does_not_mask_failure(self, walled_board: Path, tmp_path: Path):
        # A lenient --min-completion (1 of 2 links = 50% >= 0.1 threshold)
        # would ordinarily report SUCCESS; --complete must still fail hard.
        out = tmp_path / "out.kicad_pcb"
        rc = route_main(
            [
                str(walled_board),
                "-o",
                str(out),
                "--complete",
                "--backend",
                "cpp",
                "--skip-drc",
                "--min-completion",
                "0.1",
            ]
        )
        assert rc == 8

    def test_fully_solvable_board_exits_0(self, tmp_path: Path):
        from tests.test_cli_route_complete_4471 import STRANDED_BOARD

        board = tmp_path / "solvable.kicad_pcb"
        board.write_text(STRANDED_BOARD)
        out = tmp_path / "out.kicad_pcb"
        rc = route_main(
            [str(board), "-o", str(out), "--complete", "--backend", "cpp", "--skip-drc"]
        )
        assert rc == 0


class TestPerLinkDeadlineFlags:
    """AC2/AC3 (issue #9581): per-link deadline truth, not run totals.

    A run whose negotiation hit its deadline used to flag EVERY link as
    ``deadline_hit`` (the run-level ``stats.deadline_hit`` was OR-ed into
    each entry) and copied the whole-run ``elapsed_s``/``budget_s`` onto
    every link.  The honest per-link signal is the decline reason itself:
    ``route_netset`` stamps a link it never reached with
    ``"deadline-exceeded"``; any other reason means the link was attempted.
    """

    @staticmethod
    def _stub_router():
        from types import SimpleNamespace

        # Run-level totals: the negotiation DID hit its deadline.
        stats = SimpleNamespace(deadline_hit=True, elapsed_s=12.345, budget_s=30.0)
        return SimpleNamespace(
            _lattice_negotiation_stats=stats,
            _lattice_failure_reasons={
                (101, 0): "deadline-exceeded",
                (102, 0): "no-path-found",
            },
            _lattice_connection_endpoints={
                (101, 0): ("A.1", "A.2"),
                (102, 0): ("B.1", "B.2"),
            },
            _complete_creepage_refusals=[
                {
                    "net": "NETC",
                    "reason": "creepage-rolled-back",
                    "link": {"start": "C.1", "end": "C.2"},
                    "blocking_net": "WALL",
                    "measured_mm": 0.2,
                    "required_mm": 0.4,
                }
            ],
            net_names={101: "NETA", 102: "NETB"},
        )

    def test_three_link_classes_get_distinct_flags(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _build_complete_link_report

        report = _build_complete_link_report(self._stub_router(), tmp_path / "absent.kicad_pcb")
        assert report is not None
        # Run level still reports the whole-run truth.
        assert report["deadline_hit"] is True
        assert isinstance(report["elapsed_s"], int | float)
        assert report["budget_s"] is not None

        by_net = {entry["net"]: entry for entry in report["unroutable_links"]}
        deadline_link = by_net["NETA"]
        attempted_link = by_net["NETB"]
        creepage_link = by_net["NETC"]

        # Never reached before the deadline.
        assert deadline_link["deadline_hit"] is True
        assert deadline_link["attempted"] is False
        # Attempted, declined for a geometric reason.
        assert attempted_link["deadline_hit"] is False
        assert attempted_link["attempted"] is True
        # Attempted (committed by the search), rolled back for creepage.
        assert creepage_link["deadline_hit"] is False
        assert creepage_link["attempted"] is True

        # Per-link timing is null -- run totals stay at report level only.
        for entry in (deadline_link, attempted_link, creepage_link):
            assert entry["elapsed_s"] is None
            assert entry["budget_s"] is None

    def test_printer_distinguishes_not_attempted_from_declined(self, tmp_path: Path, capsys):
        from kicad_tools.cli.route_cmd import (
            _build_complete_link_report,
            _print_complete_link_report,
        )

        report = _build_complete_link_report(self._stub_router(), tmp_path / "absent.kicad_pcb")
        assert report is not None
        _print_complete_link_report(report)
        text = capsys.readouterr().out
        # Exactly one not-attempted note, and it belongs to the deadline link.
        assert text.count("not attempted: the run's deadline passed") == 1
        neta_block = text.split("[NETA]")[1].split("[NETB]")[0]
        assert "not attempted" in neta_block

    def test_deadline_stamp_is_cross_pass_honest(self, tmp_path: Path, monkeypatch, capsys):
        """PR #9603 review: ``"deadline-exceeded"`` is a per-PASS stamp.

        The negotiation re-routes every connection on each pass, and a
        deadline-truncated LATER pass can become the best pass (its prefix
        routed more than any full pass).  Such a pass used to stamp every
        not-yet-reached link ``"deadline-exceeded"`` -- relabelling links an
        earlier pass had attempted and declined as "never attempted".
        ``attempted`` must mean the search reached the link in ANY pass, and
        a link declined on its own merits earlier keeps that real reason.

        The per-item search outcomes are stubbed (the negotiation's history
        pressure would produce them on a congested board); the unit under
        test is the cross-pass reason bookkeeping in the real
        ``_route_netset_once`` loop, driven by a fake pathfinder clock.
        """
        import time as _time
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import (
            _build_complete_link_report,
            _print_complete_link_report,
        )
        from kicad_tools.router.lattice import pathfinder as pf_mod
        from kicad_tools.router.lattice.pathfinder import LatticePathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]

        def _pad(x: float, y: float, net: int, pin: str) -> Pad:
            return Pad(
                x=x,
                y=y,
                net=net,
                net_name=f"NET{net}",
                layer=Layer.F_CU,
                ref=f"J{net}",
                pin=pin,
                width=0.6,
                height=0.6,
            )

        # Six singles at strictly increasing lengths (8..13) so every pass
        # visits them shortest-first in the order A..F (x keeps the
        # corridors far apart; only the stubbed outcomes matter).
        connections = []
        for net, length in enumerate((8.0, 9.0, 10.0, 11.0, 12.0, 13.0), start=1):
            x = 1.0 + 2.0 * (net - 1)
            connections.append(((net, 0), _pad(x, 0.0, net, "1"), _pad(x, length, net, "2"), None))

        pf = LatticePathfinder(
            outline,
            [p for c in connections for p in (c[1], c[2])],
            DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb"),
            LayerStack.two_layer(),
        )

        # The deadline is anchored to the REAL clock (+1000 s) so machinery
        # outside the pathfinder module (escape planning) reading its own
        # ``time`` still sees time to spare; the pathfinder module's clock
        # is the fake below, and only it decides when a pass is cut.
        real_now = _time.monotonic()
        deadline = real_now + 1000.0
        clock = {"now": real_now}
        monkeypatch.setattr(pf_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        calls: dict[object, int] = {}

        def fake_single(item, routes, **_kwargs):
            key = item[0]
            calls[key] = calls.get(key, 0) + 1
            if key == (6, 0):
                # F: declined on its own merits in every pass that reaches it.
                return "no-path-found"
            if key in ((2, 0), (3, 0), (4, 0)) and calls[key] == 1:
                # B, C, D: declined in pass 1, ROUTED in pass 2 -- the
                # truncated pass 2 then scores 4 > pass 1's 2 and becomes
                # the best pass.
                return "no-path-found"
            if key == (4, 0):
                # D's pass-2 success jumps the fake clock past the deadline,
                # so the NEXT item (E) is cut before its pass-2 attempt.
                clock["now"] = real_now + 2000.0
            return None

        monkeypatch.setattr(pf, "_route_single_into", fake_single)

        routes, stats = pf.route_netset(connections, deadline=deadline)

        # The truncated pass 2 became the best pass: F (declined pass 1,
        # cut pass 2) keeps its real decline reason, E (routed pass 1, cut
        # pass 2) keeps the deadline stamp, and every key's search started
        # in some pass.
        assert stats.deadline_hit is True
        assert stats.iterations == 2
        assert pf.failure_reasons[(6, 0)] == "no-path-found"
        assert pf.failure_reasons[(5, 0)] == "deadline-exceeded"
        assert stats.attempted_keys == frozenset((n, 0) for n in range(1, 7))

        router = SimpleNamespace(
            _lattice_negotiation_stats=stats,
            _lattice_failure_reasons=dict(pf.failure_reasons),
            _lattice_attempted_keys=stats.attempted_keys,
            _lattice_connection_endpoints={
                (5, 0): ("E.1", "E.2"),
                (6, 0): ("F.1", "F.2"),
            },
            _complete_creepage_refusals=[],
            net_names={5: "NETE", 6: "NETF"},
        )
        report = _build_complete_link_report(router, tmp_path / "absent.kicad_pcb")
        assert report is not None
        assert report["deadline_hit"] is True
        by_net = {entry["net"]: entry for entry in report["unroutable_links"]}

        # Declined on its own merits in pass 1, cut in pass 2: the real
        # decline reason survives and the link reads as attempted.
        f_entry = by_net["NETF"]
        assert f_entry["reason"] == "no-path-found"
        assert f_entry["attempted"] is True
        assert f_entry["deadline_hit"] is False

        # Routed in pass 1, cut in pass 2: attempted (any pass counts) even
        # though its reason IS the deadline stamp of the best pass.
        e_entry = by_net["NETE"]
        assert e_entry["reason"] == "deadline-exceeded"
        assert e_entry["attempted"] is True
        assert e_entry["deadline_hit"] is True

        # Neither final link was "never attempted" -- the human-readable
        # report must not say so.
        _print_complete_link_report(report)
        assert "not attempted: the run's deadline passed" not in capsys.readouterr().out

    def test_stale_decline_cleared_when_later_pass_routes(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        """PR #9603 review (round 2): the cross-pass restore must record
        each key's most recent OUTCOME, success included.

        A link declined in pass 1 and ROUTED in pass 2 has a success as its
        most recent outcome; when pass 3 is then cut by the deadline before
        re-reaching it and pass 3 becomes the best pass, the cut must stamp
        ``"deadline-exceeded"`` -- reviving the pass-1 decline would tell
        the user "make room on the board" when more time had already routed
        the link once.

        Three-pass shape from the review (singles A..F, shortest first):

        ===  ======  ============  =====================  =====
        pass A       B..E          F                       score
        ===  ======  ============  =====================  =====
        1    routed  declined      declined (no-path)     1
        2    routed  declined      ROUTED                 2
        3    routed  routed        cut by the deadline    5
        ===  ======  ============  =====================  =====

        Pass 3 is cut after E (E's success jumps the fake clock) and scores
        5, so the truncated pass becomes the best pass.  F must read the
        deadline stamp with ``attempted: true``, not its stale pass-1
        ``no-path-found``.
        """
        import time as _time
        from types import SimpleNamespace

        from kicad_tools.cli.route_cmd import (
            _build_complete_link_report,
            _print_complete_link_report,
        )
        from kicad_tools.router.lattice import pathfinder as pf_mod
        from kicad_tools.router.lattice.pathfinder import LatticePathfinder
        from kicad_tools.router.layers import Layer, LayerStack
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.rules import DesignRules

        outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]

        def _pad(x: float, y: float, net: int, pin: str) -> Pad:
            return Pad(
                x=x,
                y=y,
                net=net,
                net_name=f"NET{net}",
                layer=Layer.F_CU,
                ref=f"J{net}",
                pin=pin,
                width=0.6,
                height=0.6,
            )

        # Six singles at strictly increasing lengths (8..13) so every pass
        # visits them shortest-first in the order A..F.
        connections = []
        for net, length in enumerate((8.0, 9.0, 10.0, 11.0, 12.0, 13.0), start=1):
            x = 1.0 + 2.0 * (net - 1)
            connections.append(((net, 0), _pad(x, 0.0, net, "1"), _pad(x, length, net, "2"), None))

        pf = LatticePathfinder(
            outline,
            [p for c in connections for p in (c[1], c[2])],
            DesignRules(trace_width=0.2, trace_clearance=0.15, manufacturer="jlcpcb"),
            LayerStack.two_layer(),
        )

        # Deadline anchored to the REAL clock (+1000 s) so machinery outside
        # the pathfinder module keeps working; the pathfinder module's fake
        # clock below is the only thing that decides when pass 3 is cut.
        real_now = _time.monotonic()
        deadline = real_now + 1000.0
        clock = {"now": real_now}
        monkeypatch.setattr(pf_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        calls: dict[object, int] = {}

        def fake_single(item, routes, **_kwargs):
            key = item[0]
            calls[key] = calls.get(key, 0) + 1
            n = calls[key]
            if key == (1, 0):
                # A: routed in every pass.
                return None
            if key == (6, 0):
                # F: declined in pass 1, ROUTED in pass 2, cut in pass 3.
                return "no-path-found" if n == 1 else None
            if n < 3:
                # B..E: declined in passes 1-2, routed in pass 3.
                return "no-path-found"
            if key == (5, 0):
                # E's pass-3 success jumps the fake clock past the deadline,
                # so the NEXT item (F) is cut before its pass-3 attempt.
                clock["now"] = real_now + 2000.0
            return None

        monkeypatch.setattr(pf, "_route_single_into", fake_single)

        routes, stats = pf.route_netset(connections, deadline=deadline)

        # Pass 3 routed A..E (score 5) before the cut and is the best pass.
        assert stats.deadline_hit is True
        assert stats.iterations == 3
        # F's most recent completed outcome was the pass-2 SUCCESS -- the
        # pass-1 decline is stale and must NOT be revived by the cut.
        assert pf.failure_reasons == {(6, 0): "deadline-exceeded"}
        # F's search started in passes 1 and 2: attempted.
        assert (6, 0) in stats.attempted_keys

        router = SimpleNamespace(
            _lattice_negotiation_stats=stats,
            _lattice_failure_reasons=dict(pf.failure_reasons),
            _lattice_attempted_keys=stats.attempted_keys,
            _lattice_connection_endpoints={(6, 0): ("F.1", "F.2")},
            _complete_creepage_refusals=[],
            net_names={6: "NETF"},
        )
        report = _build_complete_link_report(router, tmp_path / "absent.kicad_pcb")
        assert report is not None
        assert report["deadline_hit"] is True
        by_net = {entry["net"]: entry for entry in report["unroutable_links"]}

        # Routed in pass 2, cut in pass 3: the honest label is the deadline
        # stamp -- more time might route it -- and the link was attempted.
        f_entry = by_net["NETF"]
        assert f_entry["reason"] == "deadline-exceeded"
        assert f_entry["attempted"] is True
        assert f_entry["deadline_hit"] is True

        # The human-readable report must not call F "never attempted" (it
        # ran twice) nor hand it a geometric decline (it routed once).
        _print_complete_link_report(report)
        out = capsys.readouterr().out
        assert "not attempted: the run's deadline passed" not in out
        assert "no-path-found" not in out


class TestBudgetDerivationLine:
    """AC1 (issue #9581): a --complete run with --timeout states the
    reserve derivation on stdout, using the values
    ``_set_wall_clock_deadline`` stamps."""

    def test_complete_run_prints_derivation(self, capsys):
        from types import SimpleNamespace

        from kicad_tools.cli import route_cmd

        args = SimpleNamespace(timeout=100.0, auto_fix=False, complete=True)
        route_cmd._set_wall_clock_deadline(args)
        text = capsys.readouterr().out
        # --auto-fix absent -> auto-fix reserve 0; the post-route reserve is
        # whatever the helper computes for this timeout (floor/fraction/cap).
        # No invocation deadline is set here, so nothing has been consumed
        # before the derivation line and the setup term is 0.
        post_route = route_cmd._post_route_reserve(args, 100.0)
        assert (
            f"--timeout 100s - setup 0s - auto-fix reserve 0s - post-route reserve {post_route:.0f}s"
            in text
        )
        assert f"= routing budget {100.0 - post_route:.1f}s" in text

    def test_clamped_timeout_leads_with_typed_value(self, monkeypatch, capsys):
        """PR #9603 review: under a supervisor-set invocation deadline the
        clamp shrinks the budget before the print.  The line must still lead
        with the ``--timeout`` the user typed and give the consumed time its
        own ``setup`` term, so the arithmetic sums to the flag instead of
        relabelling the clamped remainder as the flag's value."""
        import re
        import time
        from types import SimpleNamespace

        from kicad_tools.cli import route_cmd, route_deadline

        # Simulate a supervisor invocation deadline with ~30 s of the typed
        # budget already consumed by setup (env-driven in production; the
        # function imports the helper at call time, so patch the module).
        now = time.monotonic()
        monkeypatch.setattr(route_deadline, "remaining_deadline", lambda: now + 70.0)

        args = SimpleNamespace(timeout=100.0, auto_fix=False, complete=True)
        route_cmd._set_wall_clock_deadline(args)
        text = capsys.readouterr().out

        match = re.search(
            r"--timeout (\d+)s - setup (\d+)s - auto-fix reserve (\d+)s"
            r" - post-route reserve (\d+)s = routing budget ([\d.]+)s",
            text,
        )
        assert match, f"derivation line missing or malformed: {text!r}"
        typed, setup, auto_fix, post_route, budget = (float(g) for g in match.groups())
        # First term is the typed value, not the clamped remainder (~70).
        assert typed == 100.0
        # The consumed ~30 s is visible as its own term (bounded: the clock
        # may tick between the test's ``now`` and the function's own read).
        assert 29 <= setup <= 31
        # The printed equation sums to the displayed budget (each term is
        # rounded to whole seconds, the budget to a tenth, so allow rounding).
        assert typed - setup - auto_fix - post_route == pytest.approx(budget, abs=2.2)
        # The stamp the rest of the run enforces is the clamped budget.
        assert args._wall_clock_deadline is not None
        assert args._wall_clock_deadline - time.monotonic() <= 70.0

    def test_line_absent_without_complete_flag(self, capsys):
        from types import SimpleNamespace

        from kicad_tools.cli import route_cmd

        route_cmd._set_wall_clock_deadline(SimpleNamespace(timeout=100.0, auto_fix=False))
        assert "--timeout" not in capsys.readouterr().out

    def test_line_absent_without_timeout(self, capsys):
        from types import SimpleNamespace

        from kicad_tools.cli import route_cmd

        route_cmd._set_wall_clock_deadline(SimpleNamespace(timeout=None, complete=True))
        assert "routing budget" not in capsys.readouterr().out


class TestStdoutLineBuffering:
    """AC5 (issue #9581): stdout is line-buffered from the route entry
    onward, so progress reaches a redirected log during the run."""

    def test_entry_line_buffers_a_non_tty_stream(self, monkeypatch):
        import io
        import sys

        from kicad_tools.cli.route_cmd import _in_process_main

        wrapper = io.TextIOWrapper(io.BytesIO(), line_buffering=False)
        assert wrapper.line_buffering is False
        monkeypatch.setattr(sys, "stdout", wrapper)
        with pytest.raises(SystemExit):
            # ``--help`` exits at argparse -- i.e. after the entry-level
            # stream setup has run, without loading any board.
            _in_process_main(["--help"])
        assert wrapper.line_buffering is True

    def test_stream_without_reconfigure_is_tolerated(self, monkeypatch):
        import io
        import sys

        from kicad_tools.cli.route_cmd import _in_process_main

        stream = io.StringIO()
        assert not hasattr(stream, "reconfigure")
        monkeypatch.setattr(sys, "stdout", stream)
        with pytest.raises(SystemExit):
            _in_process_main(["--help"])
