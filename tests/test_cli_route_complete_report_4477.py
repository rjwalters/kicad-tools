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
        # Elapsed/deadline bookkeeping is per-link too (issue #4477 scope).
        assert entry["elapsed_s"] >= 0.0
        assert entry["budget_s"] > 0.0

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
