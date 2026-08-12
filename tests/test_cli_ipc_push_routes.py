"""Tests for ``kct ipc push-routes`` board loading and conversion (#4788).

The handler shipped (since #2363) importing ``kicad_tools.pcb.parser`` — a
module that has never existed — so every invocation on a real board exited 1
with "PCB parser not available."  Because the conversion loop also used
``getattr(..., default)`` fallbacks against attribute names the real model
does not have, fixing only the import would have silently pushed zero-length
tracks at the origin on net 0.

These tests pin the wiring to the real board loader
(``kicad_tools.schema.pcb.PCB.load``) and the real model attribute names
(``segments``, ``start``/``end``/``position`` tuples, ``net_number``).

Fixture notes: ``stale_nets.kicad_pcb`` contains real routed copper
(2 segments + 1 via, all on net 4 ``Net-(C11-2)``).
``routing-diagnostic.kicad_pcb`` is deliberately UNROUTED — it is the input
board the router suites route — so it exercises the zero-copper path.
"""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli.commands.ipc import _to_ipc_routes, run_ipc_command
from kicad_tools.cli.parser import create_parser

FIXTURES = Path(__file__).parent / "fixtures"
ROUTED_BOARD = FIXTURES / "stale_nets.kicad_pcb"
UNROUTED_BOARD = FIXTURES / "routing-diagnostic.kicad_pcb"


def _run(argv: list[str], capsys) -> tuple[int, str]:
    rc = run_ipc_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr().out


class TestImportResolution:
    """Regression tests that would have caught the dead import."""

    def test_dry_run_does_not_hit_parser_unavailable(self, capsys):
        """The dead-import era turned EVERY real board into exit 1 + this prose."""
        rc, out = _run(["ipc", "push-routes", str(ROUTED_BOARD), "--dry-run"], capsys)
        assert "PCB parser not available" not in out
        assert rc == 0

    def test_board_loader_is_first_party_schema_pcb(self):
        """The handler must load boards via ``kicad_tools.schema.pcb.PCB``.

        Import-resolution check: the module the handler imports at push time
        must exist and expose ``load``.  (``kicad_tools.pcb.parser`` never
        did; the swallowed ImportError hid that for three releases.)
        """
        from kicad_tools.schema.pcb import PCB

        assert callable(PCB.load)


class TestDryRun:
    def test_routed_board_reports_nonzero_counts(self, capsys):
        rc, out = _run(["ipc", "push-routes", str(ROUTED_BOARD), "--dry-run"], capsys)
        assert rc == 0
        assert "Found 2 tracks and 1 vias" in out
        assert "Dry run" in out

    def test_routed_board_json_document(self, capsys):
        rc, out = _run(
            ["ipc", "push-routes", str(ROUTED_BOARD), "--dry-run", "--format", "json"],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["command"] == "push-routes"
        assert payload["success"] is True
        assert payload["tracks"] == 2
        assert payload["vias"] == 1
        assert payload["pushed"] == 0
        assert payload["dry_run"] is True

    def test_unrouted_board_reports_zero_and_exits_zero(self, capsys):
        """A copper-free board is a valid (empty) push, not an error."""
        rc, out = _run(["ipc", "push-routes", str(UNROUTED_BOARD), "--dry-run"], capsys)
        assert rc == 0
        assert "Found 0 tracks and 0 vias" in out


class TestNetFilter:
    """``--net`` must match by NAME against ``PCB.nets`` (a dict[int, Net])."""

    def test_existing_net_name_selects_its_copper(self, capsys):
        rc, out = _run(
            [
                "ipc",
                "push-routes",
                str(ROUTED_BOARD),
                "--dry-run",
                "--net",
                "Net-(C11-2)",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["net_filter"] == "Net-(C11-2)"
        assert payload["tracks"] == 2
        assert payload["vias"] == 1

    def test_existing_net_without_copper_selects_nothing(self, capsys):
        """GND exists in the netlist but owns no segments/vias."""
        rc, out = _run(
            [
                "ipc",
                "push-routes",
                str(ROUTED_BOARD),
                "--dry-run",
                "--net",
                "GND",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 0
        payload = json.loads(out)
        assert payload["tracks"] == 0
        assert payload["vias"] == 0

    def test_unknown_net_name_is_a_clear_error(self, capsys):
        rc, out = _run(
            ["ipc", "push-routes", str(ROUTED_BOARD), "--dry-run", "--net", "NO_SUCH_NET"],
            capsys,
        )
        assert rc == 1
        assert "Net not found" in out
        assert "NO_SUCH_NET" in out

    def test_unknown_net_name_json_error_document(self, capsys):
        rc, out = _run(
            [
                "ipc",
                "push-routes",
                str(ROUTED_BOARD),
                "--dry-run",
                "--net",
                "NO_SUCH_NET",
                "--format",
                "json",
            ],
            capsys,
        )
        assert rc == 1
        payload = json.loads(out)
        assert payload["command"] == "push-routes"
        assert payload["success"] is False
        assert payload["pushed"] == 0
        assert "NO_SUCH_NET" in payload["error"]


class TestConversion:
    """The IPC conversion must read real model attributes, not getattr defaults."""

    def test_tracks_and_vias_convert_with_real_geometry(self):
        from kicad_tools.schema.pcb import PCB

        board = PCB.load(ROUTED_BOARD)
        ipc_tracks, ipc_vias = _to_ipc_routes(board.segments, board.vias)

        assert len(ipc_tracks) == 2
        assert len(ipc_vias) == 1

        for track in ipc_tracks:
            # The getattr-fallback bug emitted zero-length tracks at the origin.
            assert (track.start.x_nm, track.start.y_nm) != (track.end.x_nm, track.end.y_nm)
            assert track.net == 4  # not the net-0 fallback
            assert track.width_nm == 250_000  # 0.25 mm
            assert track.layer == "F.Cu"

        # seg-1 in the fixture: (11, 10) -> (19, 10) mm.
        assert ipc_tracks[0].start.to_dict() == {"x": 11_000_000, "y": 10_000_000}
        assert ipc_tracks[0].end.to_dict() == {"x": 19_000_000, "y": 10_000_000}

        # via-1 in the fixture: at (19, 11) mm, size 0.6, drill 0.3, net 4.
        via = ipc_vias[0]
        assert via.position.to_dict() == {"x": 19_000_000, "y": 11_000_000}
        assert via.diameter_nm == 600_000
        assert via.drill_nm == 300_000
        assert via.net == 4


class TestUnparseableBoard:
    def test_parse_failure_is_a_clear_error(self, tmp_path, capsys):
        bad = tmp_path / "garbage.kicad_pcb"
        bad.write_text("this is not a board")
        rc, out = _run(["ipc", "push-routes", str(bad), "--dry-run"], capsys)
        assert rc == 1
        assert "Failed to parse PCB" in out
