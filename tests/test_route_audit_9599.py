"""Regression tests for the #9592 router audit, rows 1/2/4 (issue #9599).

Verdicts under test, one test (or group) per row per entry point:

* **Row 1 -- partial copper for nets the run gives up on**:
  ``kct route`` does not apply as reported -- a net reported ``Unrouted``
  carries no copper at all (its orphan escape stubs are removed, its partial
  attempts are discarded), and a net with copper left behind is explicitly
  listed under ``Partially connected nets`` with its k/n pad count.
  ``route-auto`` does not apply either (issue #4165): a partial multi-pad net
  is persisted only behind ``--allow-partial``; a failed net leaves the board
  exactly as it was.

* **Row 2 -- shove/rip-up of existing copper**: ``route-auto`` does not apply
  (append-only persistence; other nets' copper is untouched).  ``kct route``
  DOES apply to the listing half: without ``--preserve-existing`` a plain run
  replaces all top-level routing copper (issue #2976 strip) and printed
  nothing about it.  The fix under test here is the pre-run notice listing
  every net whose copper the run will replace.  The layer-protection half is
  #9591 / #9597; the lock-by-default policy change is a child issue.

* **Row 4 -- stale write**: ``kct route`` does not apply as reported (SIGINT /
  SIGTERM save partial once then exit, #6324; atomic writes, #2808).  The
  residual concurrent-writer gap is a child issue.  ``route-auto`` DOES
  apply: ``route_net_auto`` saved the whole board from a PCB object loaded at
  entry, so any external edit between load and save was silently clobbered.
  The fix under test is the stale-output guard that refuses the save.

The boards are small synthetic ``.kicad_pcb`` files; the sealed/wall keepout
rule areas make specific nets deterministically unreachable.  They are kept
compact (~30x26mm) deliberately: an unreachable pad makes the router search
its whole reachable component to exhaustion, so fixture area is test runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.cli import route_cmd
from kicad_tools.mcp.tools.routing import route_net_auto
from kicad_tools.router import wall_deadline

# Wall-clock bound for the route-auto calls that hit a sealed pad (review of
# PR #9629): the first C++ NO_PATH on an unroutable pad hands off to the
# pure-Python A* fallback, which otherwise searches the grid to exhaustion
# with no deadline on this entry point -- 74s on the original 60x50mm
# fixture, past CI's per-test ``pytest --timeout=60`` cap.  The deadline
# context manager clamps every A* search (both backends) to the remaining
# budget, so the run finishes partial instead of being killed.  25s is well
# above the natural drain time on the compact fixtures below, so unloaded
# behavior is unchanged; a loaded runner cuts the grind instead of the cap.
_ROUTE_AUTO_DEADLINE_S = 25.0


def _bounded_route_net_auto(*args, **kwargs) -> dict:
    """``route_net_auto`` under the wall-clock bound above.

    Only the calls that can hit a sealed pad need it (SIG3/SIGW); routable
    nets finish in seconds either way.
    """
    with wall_deadline.deadline(_ROUTE_AUTO_DEADLINE_S):
        return route_net_auto(*args, **kwargs)


# ---------------------------------------------------------------------------
# Board fixtures
# ---------------------------------------------------------------------------

_BOARD_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "issue-9599-test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 130 126) (layer "Edge.Cuts") (width 0.05))
  (net 0 "")
  (net 1 "SIG3")
  (net 2 "EASY")
  (net 3 "HAND")
  (net 4 "SIGW")
"""

_FOOTPRINTS = """  (footprint "T:U1"
    (layer "F.Cu")
    (at 105 104)
    (fp_text reference "U1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG3"))
    (pad 2 smd rect (at 0 4) (size 1 1) (layers "F.Cu") (net 1 "SIG3"))
    (pad 3 smd rect (at 0 8) (size 1 1) (layers "F.Cu") (net 2 "EASY"))
  )
  (footprint "T:U2"
    (layer "F.Cu")
    (at 117 116)
    (fp_text reference "U2" (at 0 3) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG3"))
    (pad 2 smd rect (at 0 -10) (size 1 1) (layers "F.Cu") (net 2 "EASY"))
  )
  (footprint "T:U3"
    (layer "F.Cu")
    (at 104 121)
    (fp_text reference "U3" (at 0 3) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 3 "HAND"))
    (pad 2 smd rect (at 8 0) (size 1 1) (layers "F.Cu") (net 3 "HAND"))
  )
"""

# Seals U2 pad 1 (117, 116) inside a track-blocking keepout ring.
_SEAL_ZONE = """  (zone
    (net 0)
    (net_name "")
    (layers "F.Cu")
    (name "seal")
    (hatch edge 0.5)
    (priority 1)
    (polygon
      (pts
        (xy 114 113)
        (xy 120 113)
        (xy 120 119)
        (xy 114 119)
      )
    )
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (zones allowed))
  )
"""

# Per-pad track-blocking seal rings: SIGW's two pads (124,104) / (124,112)
# each sit inside a closed keepout rectangle, so no trace can reach either
# pad -- the net is deterministically unroutable while EASY and HAND stay
# routable.
_WALL_BOARD_SIGW = """  (footprint "T:W1"
    (layer "F.Cu")
    (at 124 104)
    (fp_text reference "W1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 4 "SIGW"))
  )
  (footprint "T:W2"
    (layer "F.Cu")
    (at 124 112)
    (fp_text reference "W2" (at 0 3) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 4 "SIGW"))
  )
  (zone
    (net 0)
    (net_name "")
    (layers "F.Cu" "B.Cu")
    (name "seal_w1")
    (hatch edge 0.5)
    (priority 1)
    (polygon
      (pts
        (xy 121 101)
        (xy 127 101)
        (xy 127 107)
        (xy 121 107)
      )
    )
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (zones allowed))
  )
  (zone
    (net 0)
    (net_name "")
    (layers "F.Cu" "B.Cu")
    (name "seal_w2")
    (hatch edge 0.5)
    (priority 1)
    (polygon
      (pts
        (xy 121 109)
        (xy 127 109)
        (xy 127 115)
        (xy 121 115)
      )
    )
    (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (zones allowed))
  )
"""

_HAND_COPPER = """  (segment (start 104 121) (end 112 121) (width 0.25) (layer "F.Cu") (net 3 "HAND"))
  (segment (start 105 104) (end 109 104) (width 0.25) (layer "F.Cu") (net 1 "SIG3"))
"""


def _partial_seal_board(tmp_path: Path, *, hand_copper: bool = False) -> Path:
    """3-pad SIG3 (one pad sealed -> deterministic partial), plus EASY/HAND."""
    body = _BOARD_HEADER + _FOOTPRINTS + _SEAL_ZONE + "\n)\n"
    if hand_copper:
        body = _BOARD_HEADER + _FOOTPRINTS + _HAND_COPPER + _SEAL_ZONE + "\n)\n"
    board = tmp_path / "board_partial_seal.kicad_pcb"
    board.write_text(body)
    return board


def _wall_board(tmp_path: Path) -> Path:
    """SIGW's two pads separated by a full-height keepout wall -> unrouted."""
    body = _BOARD_HEADER + _FOOTPRINTS + _WALL_BOARD_SIGW + _SEAL_ZONE + "\n)\n"
    board = tmp_path / "board_wall.kicad_pcb"
    board.write_text(body)
    return board


def _route_argv(board: Path, out: Path) -> list[str]:
    return [
        str(board),
        "-o",
        str(out),
        # 15s, not 25s: the sealed nets grind until this budget expires (the
        # run is budget-bound, not work-bound), and every second of it lands
        # on the test's wall clock.  The compact fixtures keep setup ~2s, so
        # this still leaves ~7x headroom before the "deadline reached before
        # attempt 1" failure mode a loaded host can produce.
        "--timeout",
        "15",
        "--skip-drc",
        "--no-auto-pour",
        # The sealed nets are unroutable by construction; the chained
        # completion pass would otherwise re-run on them and exit 2.
        "--no-chain-complete",
        # The post-negotiation rescue sweep re-attempts stranded nets with
        # its own 10s-per-net cap AFTER the main budget expires (measured
        # +10.3s here); it cannot rescue a pad sealed by a keepout, so the
        # listing/persistence behavior under test is identical without it.
        "--no-rescue-pass",
    ]


def _segments_for_net(board_text: str, net_name: str, net_id: int | None = None) -> list[str]:
    """Top-level ``(segment ...)`` blocks referencing the given net.

    Matches both the ``(net 1 "SIG3")`` inline-name form and the numeric-only
    ``(net 1)`` form route-auto's persistence path writes.  Pass *net_id*
    (the board's ``(net N "NAME")`` declaration number) to disambiguate.
    """
    if net_id is None:
        m = re.search(rf'\(net\s+(\d+)\s+"{re.escape(net_name)}"', board_text)
        if m is None:
            return []
        net_id = int(m.group(1))
    pattern = re.compile(rf"\(net\s+{net_id}(?=[\s)\"])")
    found: list[str] = []
    for m in re.finditer(r"\(segment\b", board_text):
        start = m.start()
        depth = 0
        i = start
        in_string = False
        prev = ""
        while i < len(board_text):
            ch = board_text[i]
            if ch == '"' and prev != "\\":
                in_string = not in_string
            elif not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            prev = ch
            i += 1
        block = board_text[start : i + 1]
        if pattern.search(block):
            found.append(" ".join(block.split()).replace(" )", ")"))
    return found


# ---------------------------------------------------------------------------
# Row 1 -- kct route: unrouted nets carry no copper; partial nets are listed
# ---------------------------------------------------------------------------


class TestRow1KctRoute:
    def test_fully_sealed_net_reports_unrouted_and_leaves_no_copper(self, tmp_path, capsys):
        board = _wall_board(tmp_path)
        out = tmp_path / "wall_routed.kicad_pcb"

        rc = route_cmd._in_process_main_impl(_route_argv(board, out))

        # 2 = partial routing below threshold: SIGW is unroutable by
        # construction, so the run is partial on purpose.
        assert rc == 2
        captured = capsys.readouterr()
        # The run reports SIGW as unrouted with no segments at all (not as
        # "partial" and not silently).
        assert re.search(r"Unrouted:.*no segments at all", captured.out)
        assert re.search(r"Unrouted nets:.*SIGW", captured.out, re.S)
        # And the saved board carries NO copper for it.
        assert _segments_for_net(out.read_text(), "SIGW") == []

    def test_partial_net_is_listed_with_copper_kept(self, tmp_path, capsys):
        board = _partial_seal_board(tmp_path)
        out = tmp_path / "partial_routed.kicad_pcb"

        rc = route_cmd._in_process_main_impl(_route_argv(board, out))

        # 2 = partial routing below threshold: the sealed pad keeps SIG3
        # partial by construction.
        assert rc == 2
        captured = capsys.readouterr()
        # The partial copper is written AND explicitly marked partial, per net.
        assert re.search(r"Partial routes:.*have segments, not all pads connected", captured.out)
        assert re.search(
            r"Partially connected nets \(1\):\s*SIG3: 2/3 pads connected", captured.out
        )
        assert _segments_for_net(out.read_text(), "SIG3")


# ---------------------------------------------------------------------------
# Row 1 -- route-auto: partial copper only behind --allow-partial (#4165)
# ---------------------------------------------------------------------------


class TestRow1RouteAuto:
    def test_partial_net_not_persisted_without_allow_partial(self, tmp_path):
        board = _partial_seal_board(tmp_path)
        out = tmp_path / "ra_partial.kicad_pcb"
        baseline = board.read_text()

        result = _bounded_route_net_auto(str(board), "SIG3", output_path=str(out))

        assert result["success"] is False
        assert result.get("partial") is True
        assert result.get("pads_connected") == 2
        assert result.get("pads_total") == 3
        # Nothing was persisted: no output file, and the input is unchanged.
        assert not out.exists()
        assert board.read_text() == baseline

    def test_partial_net_persisted_only_behind_allow_partial(self, tmp_path):
        board = _partial_seal_board(tmp_path)
        out = tmp_path / "ra_partial_optin.kicad_pcb"

        result = _bounded_route_net_auto(
            str(board), "SIG3", output_path=str(out), allow_partial=True
        )

        assert result["success"] is False
        assert result.get("partial") is True
        assert out.exists()
        assert _segments_for_net(out.read_text(), "SIG3")

    def test_failed_net_leaves_board_unchanged(self, tmp_path):
        board = _wall_board(tmp_path)
        out = tmp_path / "ra_failed.kicad_pcb"
        baseline = board.read_text()

        result = _bounded_route_net_auto(str(board), "SIGW", output_path=str(out))

        assert result["success"] is False
        assert not result.get("partial", False)
        assert not out.exists()
        assert board.read_text() == baseline


# ---------------------------------------------------------------------------
# Row 2 -- kct route: the replacement notice lists the affected nets
# ---------------------------------------------------------------------------


class TestRow2KctRoute:
    def test_plain_run_notice_lists_existing_copper_nets(self, tmp_path, capsys):
        board = _partial_seal_board(tmp_path, hand_copper=True)
        out = tmp_path / "notice_routed.kicad_pcb"

        rc = route_cmd._in_process_main_impl(_route_argv(board, out))

        # 2 = partial routing below threshold (SIG3 is partial by
        # construction); the notice is the assertion target here.
        assert rc == 2
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Every net carrying input copper is named, with the escape hatch.
        assert "existing routing copper" in combined
        assert "HAND" in combined
        assert "SIG3" in combined
        assert "--preserve-existing" in combined

    def test_notice_helper_skips_preserve_dry_run_and_quiet(self, tmp_path, capsys):
        """The notice must not fire under --preserve-existing/--dry-run/--quiet."""
        from types import SimpleNamespace

        board = _partial_seal_board(tmp_path, hand_copper=True)

        for attrs in (
            {"preserve_existing": True},
            {"dry_run": True},
            {"quiet": True},
        ):
            args = SimpleNamespace(**attrs)
            route_cmd._warn_existing_copper_replacement(board, args)
        assert capsys.readouterr().out == ""

    def test_baseline_copper_nets_parses_names(self, tmp_path):
        board = _partial_seal_board(tmp_path, hand_copper=True)
        segs, vias, nets = route_cmd._baseline_copper_nets(board.read_text())

        assert segs == 2
        assert vias == 0
        assert nets == ["HAND", "SIG3"]


# ---------------------------------------------------------------------------
# Row 2 -- route-auto: other nets' copper is untouched (append-only)
# ---------------------------------------------------------------------------


class TestRow2RouteAuto:
    def test_hand_placed_copper_survives_a_route_auto_run(self, tmp_path):
        board = _partial_seal_board(tmp_path, hand_copper=True)
        out = tmp_path / "ra_hand_kept.kicad_pcb"

        result = route_net_auto(str(board), "EASY", output_path=str(out))

        assert result["success"] is True
        out_text = out.read_text()
        # The hand-placed HAND segment survives verbatim; nothing of net 3
        # was removed or moved by routing a different net.
        assert _segments_for_net(out_text, "HAND") == _segments_for_net(board.read_text(), "HAND")


# ---------------------------------------------------------------------------
# Row 4 -- route-auto: a stale output file refuses the save
# ---------------------------------------------------------------------------


class TestRow4RouteAuto:
    def test_external_edit_between_load_and_save_is_refused(self, tmp_path, monkeypatch):
        board = _partial_seal_board(tmp_path)
        out = tmp_path / "ra_stale.kicad_pcb"

        import kicad_tools.router.orchestrator as orch_mod

        real_route = orch_mod.RoutingOrchestrator.route_net

        def route_then_external_write(self, *, net, pads):
            # Another writer edits the output file while this run holds its
            # (now stale) in-memory copy -- after the run's load, before its
            # save.
            out.write_text(
                _BOARD_HEADER + "  (segment (start 155 105) (end 159 105) (width 0.25) "
                '(layer "F.Cu") (net 2 "EASY"))\n)\n'
            )
            return real_route(self, net=net, pads=pads)

        monkeypatch.setattr(orch_mod.RoutingOrchestrator, "route_net", route_then_external_write)

        result = route_net_auto(str(board), "EASY", output_path=str(out))

        assert result["success"] is False
        assert "changed since" in (result.get("error_message") or "")
        # The external editor's content is intact -- the stale run did not
        # clobber it.
        assert "start 155 105" in out.read_text()

    def test_uncontended_run_still_saves(self, tmp_path):
        board = _partial_seal_board(tmp_path)
        out = tmp_path / "ra_normal.kicad_pcb"

        result = route_net_auto(str(board), "EASY", output_path=str(out))

        assert result["success"] is True
        assert out.exists()
        assert result.get("output_path") == str(out)


# ---------------------------------------------------------------------------
# Fixtures guard: the boards must parse and carry the nets the tests assume.
# ---------------------------------------------------------------------------


def test_seal_and_wall_boards_are_valid_boards(tmp_path):
    """The synthetic boards parse and carry the expected nets."""
    from kicad_tools.router.io import load_pcb_for_routing

    router, net_map = load_pcb_for_routing(str(_partial_seal_board(tmp_path)))
    assert {"SIG3", "EASY", "HAND"} <= set(net_map)

    router2, net_map2 = load_pcb_for_routing(str(_wall_board(tmp_path)))
    assert "SIGW" in net_map2
