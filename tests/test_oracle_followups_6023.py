"""Pour-oracle follow-ups from the #6017 / #6053 reviews (Issue #6023).

* ``_run_fill_zones_via_drc`` strips the stale zone fills before KiCad's
  refill (#5934).  Every path that does not end in a verified save must put
  them back -- including exceptions it does not name (``PermissionError``,
  ``KeyboardInterrupt``) and a run where KiCad wrote the DRC report but did
  not save the board.
* ``PourLinkCloser._route_links`` writes the routes an attempt committed in
  one batch (#5911).  When a later link raises, the routes committed so far
  are still written and the ORIGINAL error propagates, even if that write
  fails too.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router.link_router import LinkRoute, LinkTerminal, append_link_routes
from kicad_tools.router.oracle_completion import (
    ClosureAttempt,
    OracleEndpoint,
    OracleLink,
    PourLinkCloser,
)

FIXTURE = Path(__file__).parent / "fixtures" / "test_zone_fill.kicad_pcb"

_BOARD = """(kicad_pcb
\t(version 20240108)
\t(generator "pcbnew")
\t(net 0 "")
\t(net 1 "GND")
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "In1.Cu")
\t\t(uuid "99b8f0a0-0000-0000-0000-000000000002")
\t\t(polygon
\t\t\t(pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
\t\t)
\t\t(filled_polygon
\t\t\t(layer "In1.Cu")
\t\t\t(pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
\t\t)
\t)
)
"""

# ---------------------------------------------------------------------------
# Stale-fill restore (items 2 and 3)
# ---------------------------------------------------------------------------


def _fill(tmp_path, monkeypatch, behaviour):
    """Run ``_run_fill_zones_via_drc`` against a fake kicad-cli.

    ``behaviour(cmd)`` plays kicad-cli: it may write the report / the board,
    or raise.  Returns ``(board_path, call)`` where ``call()`` runs the fill.
    """
    from kicad_tools.cli import runner

    board = tmp_path / "b.kicad_pcb"
    board.write_text(_BOARD)
    seen: dict = {}

    def fake_run(cmd, *a, **k):
        seen["board"] = Path(cmd[-1]).read_text()
        behaviour(cmd)
        return subprocess.CompletedProcess(cmd, 5, "", "")

    monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: True)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    def call():
        return runner._run_fill_zones_via_drc(board, None, Path("/fake/kicad-cli"))

    return board, seen, call


def _write_report(cmd) -> None:
    Path(cmd[cmd.index("--output") + 1]).write_text('{"violations": []}')


class TestStaleFillRestore:
    @pytest.mark.parametrize("exc", [PermissionError("denied"), KeyboardInterrupt()])
    def test_unnamed_exceptions_restore_the_fills(self, tmp_path, monkeypatch, exc):
        def behaviour(cmd):
            raise exc

        board, seen, call = _fill(tmp_path, monkeypatch, behaviour)
        with pytest.raises(type(exc)):
            call()
        assert "filled_polygon" not in seen["board"]  # it was stripped ...
        assert board.read_text() == _BOARD  # ... and put back

    def test_report_without_save_is_a_failure_and_restores(self, tmp_path, monkeypatch):
        """KiCad wrote the DRC report but never saved the refilled board."""
        board, seen, call = _fill(tmp_path, monkeypatch, _write_report)
        result = call()
        assert not result.success
        assert "did not save" in result.stderr
        assert "filled_polygon" not in seen["board"]
        assert board.read_text() == _BOARD

    def test_report_and_save_is_a_success(self, tmp_path, monkeypatch):
        refilled = _BOARD.replace("(xy 10 10)", "(xy 9 9)")

        def behaviour(cmd):
            _write_report(cmd)
            Path(cmd[-1]).write_text(refilled)

        board, _seen, call = _fill(tmp_path, monkeypatch, behaviour)
        result = call()
        assert result.success
        assert "(xy 9 9)" in board.read_text()  # KiCad's fill, not the stale one


# ---------------------------------------------------------------------------
# Batched route write on a mid-batch failure (PR #6053 review)
# ---------------------------------------------------------------------------


def _route(x: float) -> LinkRoute:
    return LinkRoute(
        net_number=1,
        net_name="GND",
        width=0.2,
        via_size=0.6,
        via_drill=0.3,
        segments=[(x, 1.0, x + 2.0, 1.0, "F.Cu")],
        vias=[(x + 2.0, 1.0)],
    )


def _pad_link(ref: str) -> OracleLink:
    def end(pad: str) -> OracleEndpoint:
        return OracleEndpoint(
            description=f"Pad {pad} [GND] of {ref} on F.Cu",
            kind="pad",
            net="GND",
            x=0.0,
            y=0.0,
            layer="F.Cu",
            ref=ref,
            pad=pad,
        )

    return OracleLink(net="GND", a=end("1"), b=end("2"))


class _Boom(RuntimeError):
    pass


@pytest.fixture
def mid_batch_failure(tmp_path, monkeypatch):
    """A closer whose first link routes and whose second link raises."""
    from shapely.geometry import Point  # type: ignore[import-untyped]

    from kicad_tools.router import link_router

    board = tmp_path / "b.kicad_pcb"
    shutil.copy(FIXTURE, board)

    def terminal(_pcb, ref, pad):
        return LinkTerminal(f"{ref}.{pad}", (0.0, 0.0), {"F.Cu": Point(0.0, 0.0).buffer(0.1)})

    calls = iter([_route(1.0), _Boom("second link exploded")])

    def route_link(*_a, **_k):
        nxt = next(calls)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(link_router, "terminal_for_pad", terminal)
    monkeypatch.setattr(link_router, "route_link", route_link)
    closer = PourLinkCloser(via_size=0.6, via_drill=0.3, clearance=0.2, trace_width=0.2)
    return board, closer, [_pad_link("R1"), _pad_link("R2")]


class TestMidBatchFailure:
    def test_committed_routes_are_written_and_the_error_reraises(self, tmp_path, mid_batch_failure):
        board, closer, links = mid_batch_failure
        attempt = ClosureAttempt()
        with pytest.raises(_Boom, match="second link exploded"):
            closer._route_links(board, links, attempt)

        # Exactly the first route landed: the same bytes a direct write gives.
        from kicad_tools.schema.pcb import PCB

        expected = tmp_path / "expected.kicad_pcb"
        shutil.copy(FIXTURE, expected)
        append_link_routes(expected, [_route(1.0)], PCB.load(str(FIXTURE)).board_origin)
        assert board.read_bytes() == expected.read_bytes()
        assert attempt.applied == 1

    def test_a_failing_write_does_not_mask_the_original_error(
        self, mid_batch_failure, monkeypatch, caplog
    ):
        from kicad_tools.router import link_router

        board, closer, links = mid_batch_failure
        before = board.read_bytes()

        def broken_write(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(link_router, "append_link_routes", broken_write)
        with caplog.at_level(logging.WARNING, logger="kicad_tools.router.oracle_completion"):
            with pytest.raises(_Boom, match="second link exploded"):
                closer._route_links(board, links, ClosureAttempt())
        assert board.read_bytes() == before
        assert any("disk full" in (r.exc_text or "") for r in caplog.records)
