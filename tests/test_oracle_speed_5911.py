"""Pour-oracle completion loop: same decisions, less wall time (Issue #5911).

Two changes cut the oracle stage's cost on board 03 without touching what it
decides:

* :func:`append_link_routes` writes every route an attempt commits with one
  load and one save of the board, instead of one per route.  It must produce
  exactly the bytes the per-route writes did.
* ``_make_pour_oracle`` runs the board's own and the source project's
  ``kicad-cli pcb drc`` side by side.  It must call both, on the right
  projects, and combine them exactly as the sequential version did.  Since
  Issue #6095 that two-project run only happens when no project ships next
  to the routed board; a shipped (emitted) project is the sole authority.

The end-to-end check (an oracle-only replay of board 03 leaving a
byte-identical board, round by round) needs kicad-cli and is reported in the
PR; these tests pin each piece without it.
"""

from __future__ import annotations

import shutil
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation, Location, Severity, ViolationType
from kicad_tools.router.link_router import LinkRoute, append_link_route, append_link_routes

FIXTURE = Path(__file__).parent / "fixtures" / "test_zone_fill.kicad_pcb"


def _routes() -> list[LinkRoute]:
    return [
        LinkRoute(
            net_number=1,
            net_name="GND",
            width=0.2,
            via_size=0.6,
            via_drill=0.3,
            segments=[(1.0, 1.0, 3.0, 1.0, "F.Cu"), (3.0, 1.0, 3.0, 1.0, "F.Cu")],
            vias=[(3.0, 1.0)],
        ),
        LinkRoute(
            net_number=2,
            net_name="VCC",
            width=0.25,
            via_size=0.6,
            via_drill=0.3,
            segments=[(5.0, 5.0, 7.5, 7.5, "B.Cu")],
        ),
        LinkRoute(
            net_number=1,
            net_name="GND",
            width=0.2,
            via_size=0.6,
            via_drill=0.3,
            vias=[(9.0, 2.0), (9.5, 2.0)],
        ),
    ]


class TestBatchedLinkRouteWrites:
    def test_batched_write_is_byte_identical_to_one_write_per_route(self, tmp_path):
        one_by_one = tmp_path / "a.kicad_pcb"
        batched = tmp_path / "b.kicad_pcb"
        shutil.copy(FIXTURE, one_by_one)
        shutil.copy(FIXTURE, batched)
        origin = (100.0, 50.0)

        for route in _routes():
            append_link_route(one_by_one, route, origin)
        append_link_routes(batched, _routes(), origin)

        assert batched.read_bytes() == one_by_one.read_bytes()
        assert batched.read_bytes() != FIXTURE.read_bytes()

    def test_no_routes_leaves_the_file_untouched(self, tmp_path):
        board = tmp_path / "b.kicad_pcb"
        shutil.copy(FIXTURE, board)
        before = board.stat().st_mtime_ns, board.read_bytes()
        append_link_routes(board, [], (0.0, 0.0))
        assert (board.stat().st_mtime_ns, board.read_bytes()) == before


def _unconnected(net: str, x: float) -> DRCViolation:
    return DRCViolation(
        type=ViolationType.UNCONNECTED_ITEMS,
        type_str="unconnected_items",
        severity=Severity.ERROR,
        message="Missing connection between items",
        locations=[Location(x, 1.0), Location(x, 2.0)],
        items=[f"Pad 1 [{net}] of R{int(x)} on F.Cu", f"Pad 2 [{net}] of R{int(x)} on F.Cu"],
    )


class TestParallelOracleProjects:
    def _setup(self, tmp_path, src_pro_text: str = "source", *, emitted: bool = True):
        src_dir = tmp_path / "src"
        out_dir = tmp_path / "out"
        src_dir.mkdir()
        out_dir.mkdir()
        (src_dir / "board.kicad_pcb").write_text("(kicad_pcb)")
        (src_dir / "board.kicad_pro").write_text(src_pro_text)
        board = out_dir / "routed.kicad_pcb"
        board.write_text("(kicad_pcb)")
        if emitted:
            board.with_suffix(".kicad_pro").write_text("emitted")
        return Namespace(pcb=str(src_dir / "board.kicad_pcb")), board

    @staticmethod
    def _project_of(path) -> str:
        """The project a fake DRC run sees: ``"none"`` when no sidecar ships."""
        pro = Path(path).with_suffix(".kicad_pro")
        return pro.read_text() if pro.is_file() else "none"

    def test_both_projects_run_concurrently_and_merge_per_net(self, tmp_path, monkeypatch):
        """No project ships with the board: the source project stands in."""
        import kicad_tools.drc as drc_pkg
        from kicad_tools.cli import route_cmd

        args, board = self._setup(tmp_path, emitted=False)
        seen: list[str] = []
        both_running = threading.Barrier(2, timeout=10)

        def fake_drc(path):
            pro = self._project_of(path)
            seen.append(pro)
            # Each call waits for the other: only passes if they overlap.
            both_running.wait()
            if pro == "none":
                return GeometricDRCResult(
                    ran=True, error_count=4, unconnected_items=[_unconnected("GND", 1.0)]
                )
            return GeometricDRCResult(
                ran=True,
                error_count=99,
                unconnected_items=[
                    _unconnected("GND", 5.0),
                    _unconnected("GND", 6.0),
                    _unconnected("VCC", 7.0),
                ],
            )

        monkeypatch.setattr(drc_pkg, "run_geometric_drc", fake_drc)
        geo = route_cmd._make_pour_oracle(args)(board)

        assert sorted(seen) == ["none", "source"]
        # The error count is the board's own run's; links are the per-net union.
        assert geo.error_count == 4
        nets = sorted(v.items[0].split("[")[1].split("]")[0] for v in geo.unconnected_items)
        assert nets == ["GND", "GND", "VCC"]

    def test_identical_projects_run_kicad_once(self, tmp_path, monkeypatch):
        import kicad_tools.drc as drc_pkg
        from kicad_tools.cli import route_cmd

        args, board = self._setup(tmp_path, src_pro_text="emitted")
        calls: list[Path] = []

        def fake_drc(path):
            calls.append(Path(path))
            return GeometricDRCResult(ran=True)

        monkeypatch.setattr(drc_pkg, "run_geometric_drc", fake_drc)
        route_cmd._make_pour_oracle(args)(board)
        assert calls == [board]

    def test_emitted_project_is_authoritative(self, tmp_path, monkeypatch):
        """Issue #6095: links only the source project's fill strands don't count.

        Board 03 shipped fully connected under its emitted project, but the
        source project's wider clearance stranded C8.2 / C10.2 on the F.Cu GND
        pour, so the merged count failed the route with links the closer
        (modelling the emitted fill) could never close.
        """
        import kicad_tools.drc as drc_pkg
        from kicad_tools.cli import route_cmd

        args, board = self._setup(tmp_path)
        seen: list[str] = []

        def fake_drc(path):
            pro = self._project_of(path)
            seen.append(pro)
            if pro == "emitted":
                return GeometricDRCResult(ran=True, error_count=4)
            return GeometricDRCResult(
                ran=True,
                error_count=22,
                unconnected_items=[_unconnected("GND", 5.0), _unconnected("GND", 6.0)],
            )

        monkeypatch.setattr(drc_pkg, "run_geometric_drc", fake_drc)
        geo = route_cmd._make_pour_oracle(args)(board)

        assert seen == ["emitted"]
        assert geo.error_count == 4 and geo.unconnected_items == []

    def test_board_failure_without_shipped_project_is_returned_untouched(
        self, tmp_path, monkeypatch
    ):
        import kicad_tools.drc as drc_pkg
        from kicad_tools.cli import route_cmd

        args, board = self._setup(tmp_path, emitted=False)

        def fake_drc(path):
            if self._project_of(path) == "none":
                time.sleep(0.05)
                return GeometricDRCResult(ran=False, note="timed out")
            return GeometricDRCResult(ran=True, unconnected_items=[_unconnected("GND", 1.0)])

        monkeypatch.setattr(drc_pkg, "run_geometric_drc", fake_drc)
        geo = route_cmd._make_pour_oracle(args)(board)
        assert not geo.ran and geo.note == "timed out" and geo.unconnected_items == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
