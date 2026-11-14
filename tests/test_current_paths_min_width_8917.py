"""Minimum copper width reported per resolved current path (Issue #8917).

Follow-up to #8741: ``kct pcb current-paths-audit`` reported every resolved
path's segment count, length and layer changes but was silent about the
narrowest copper, so a 0.2 mm neck at a through-hole power-transistor pin on a
30 A-pulsed path read as "resolved" with no width figure at all.

Covers both tiers the feature spans:

* the resolver -- :attr:`PathResolution.min_width`, in **board** coordinates,
  measuring a :class:`ParallelGroup` as one conductor by its summed width;
* the CLI -- ``min_width_mm`` / ``min_width_at`` / ``min_width_layer``
  (plus ``min_width_parallel_group``) in ``--format json`` and the text line,
  in **sheet** coordinates like the audit's layer-change positions (#8798).

Fixtures are built in-memory (``PCB.create`` + ``add_trace`` + hand-built
``Footprint``/``Pad``), reusing ``tests/test_current_paths.py``'s canonical
``_add_pad_footprint`` helper rather than any golden board.
"""

from __future__ import annotations

import argparse
import json

import pytest

from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB

from .test_current_paths import _add_pad_footprint

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _spec(name: str = "TRUNK", *, sink: tuple[str, str] = ("J2", "1")) -> CurrentPathSpec:
    return CurrentPathSpec(
        name=name,
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint(*sink),
        continuous_a=15.0,
    )


def _necked_pcb(*, neck_width: float = 0.2, trunk_width: float = 2.0):
    """J1 (20,50) -> J2 (60,50) on F.Cu, with one necked span at x 30..34.

    Three collinear spans so the neck is an interior segment: the narrowest
    copper is not at either endpoint, which is where a real pin neck sits.
    """
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=60, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), (30, 50), width=trunk_width, layer="F.Cu", net="NET1")
    pcb.add_trace((30, 50), (34, 50), width=neck_width, layer="F.Cu", net="NET1")
    pcb.add_trace((34, 50), ("J2", "1"), width=trunk_width, layer="F.Cu", net="NET1")
    return pcb


def _uniform_pcb(*, width: float = 1.5):
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=60, y=50, net="NET1")
    pcb.add_trace(("J1", "1"), (40, 50), width=width, layer="F.Cu", net="NET1")
    pcb.add_trace((40, 50), ("J2", "1"), width=width, layer="F.Cu", net="NET1")
    return pcb


def _doubled_pcb(*, widths=(1.0, 1.0)):
    """J1 (20,50) -> J2 (32,50): a B.Cu run plus an F.Cu copy of it.

    Mirrors ``tests/test_current_paths.py::_doubled_pcb`` -- the resolution
    carries one :class:`ParallelGroup` spanning the whole run.
    """
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=32, y=50, net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=widths[0], layer="B.Cu", net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=widths[1], layer="F.Cu", net="NET1")
    return pcb


def _doubled_with_neck_pcb(*, doubled: float = 1.0, neck: float = 1.4):
    """A 2 x ``doubled`` mm stretch, then a single ``neck`` mm span to J2.

    Chosen so the doubled stretch sums to *more* than the single span: the
    reported path minimum must be the single span, proving the grouped
    stretch entered the candidate set once at its summed width rather than
    twice at one branch's width.
    """
    pcb = PCB.create(width=100, height=100, center=False)
    _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
    _add_pad_footprint(pcb, ref="J2", x=44, y=50, net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=doubled, layer="B.Cu", net="NET1")
    pcb.add_trace((20, 50), (32, 50), width=doubled, layer="F.Cu", net="NET1")
    pcb.add_via(32, 50, net="NET1")
    pcb.add_trace((32, 50), ("J2", "1"), width=neck, layer="F.Cu", net="NET1")
    return pcb


def _run_audit_cli(board, spec, tmp_path, monkeypatch, *, output_format="json") -> str:
    """Drive the real ``pcb current-paths-audit`` command over ``board``.

    Programmatic footprints do not survive a save/load round trip, so the
    in-memory board is handed to the CLI by patching ``PCB.load`` -- the
    idiom ``tests/test_path_ampacity_check.py`` already uses for this
    command.
    """
    from kicad_tools.cli.commands.pcb import run_pcb_command
    from kicad_tools.router.current_paths import dump_current_path_specs

    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("")
    monkeypatch.setattr(PCB, "load", classmethod(lambda cls, *_a, **_k: board))
    sidecar = tmp_path / "current_paths.json"
    sidecar.write_text(json.dumps(dump_current_path_specs([spec])))
    args = argparse.Namespace(
        pcb_command="current-paths-audit",
        pcb=str(pcb_path),
        current_paths=str(sidecar),
        format=output_format,
    )
    assert run_pcb_command(args) == 0
    return pcb_path


# --------------------------------------------------------------------------
# Resolver tier: PathResolution.min_width
# --------------------------------------------------------------------------


class TestMinWidthResolver:
    def test_neck_sets_the_minimum_with_its_layer_and_midpoint(self) -> None:
        resolution = resolve_current_path(_necked_pcb(), _spec())
        assert resolution.ok, resolution.reason
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(0.2)
        assert found.layer == "F.Cu"
        # Midpoint of the 30..34 neck, in board coordinates.
        assert found.position == pytest.approx((32.0, 50.0))
        assert found.parallel_group is False

    def test_uniform_path_reports_its_one_width(self) -> None:
        resolution = resolve_current_path(_uniform_pcb(width=1.5), _spec())
        assert resolution.ok, resolution.reason
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(1.5)
        assert found.parallel_group is False

    def test_doubled_stretch_reports_the_summed_width(self) -> None:
        resolution = resolve_current_path(_doubled_pcb(widths=(1.0, 1.0)), _spec())
        assert resolution.ok, resolution.reason
        assert len(resolution.parallel_groups) == 1
        found = resolution.min_width
        assert found is not None
        # 2 x 1.0 mm counts as one 2.0 mm conductor, not twice as 1.0 mm.
        assert found.width_mm == pytest.approx(2.0)
        assert found.parallel_group is True

    def test_summed_group_matches_the_path_ampacity_expression(self) -> None:
        """The audit and ``path_ampacity`` must not print two different figures.

        ``_parallel_group_violation`` prints
        ``sum(min(seg.width for seg in branch) for branch in branches)``;
        this asserts ``min_width`` uses the same expression on an *unequal*
        group, where a mean/max/one-branch reading would differ.
        """
        resolution = resolve_current_path(_doubled_pcb(widths=(2.0, 1.0)), _spec())
        assert resolution.ok, resolution.reason
        (group,) = resolution.parallel_groups
        expected = sum(min(seg.width for seg in branch) for branch in group.branches)
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(expected) == pytest.approx(3.0)
        # The narrowest branch's pinch segment names the layer/position.
        assert found.layer == "F.Cu"

    def test_true_path_minimum_is_the_single_span_not_a_group_branch(self) -> None:
        """AC 3's parenthetical: the minimum over the *whole* path.

        The doubled stretch sums to 2.0 mm and the single span is 1.4 mm, so
        double-counting a 1.0 mm branch would wrongly report 1.0 mm.
        """
        resolution = resolve_current_path(_doubled_with_neck_pcb(doubled=1.0, neck=1.4), _spec())
        assert resolution.ok, resolution.reason
        assert len(resolution.parallel_groups) == 1
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(1.4)
        assert found.parallel_group is False
        assert found.position == pytest.approx((38.0, 50.0))

    def test_group_only_path_reports_the_group(self) -> None:
        """A resolved path whose only copper is a parallel group (edge case)."""
        resolution = resolve_current_path(_doubled_pcb(widths=(0.8, 0.8)), _spec())
        assert resolution.ok, resolution.reason
        grouped = {id(seg) for group in resolution.parallel_groups for seg in group.segments}
        assert grouped == {id(seg) for seg in resolution.segments}
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(1.6)
        assert found.parallel_group is True

    def test_unresolved_path_reports_none_never_zero(self) -> None:
        pcb = _necked_pcb()
        resolution = resolve_current_path(pcb, _spec(sink=("J9", "1")))
        assert resolution.status == "unresolved"
        assert resolution.min_width is None

    def test_ambiguous_path_reports_none_never_zero(self) -> None:
        """A loop the bounded model cannot rule out stays unmeasured."""
        pcb = PCB.create(width=100, height=100, center=False)
        _add_pad_footprint(pcb, ref="J1", x=20, y=50, net="NET1")
        _add_pad_footprint(pcb, ref="J2", x=60, y=50, net="NET1")
        # Two same-layer routes between the endpoints: a cycle on F.Cu.
        pcb.add_trace(("J1", "1"), (40, 44), width=1.0, layer="F.Cu", net="NET1")
        pcb.add_trace((40, 44), ("J2", "1"), width=1.0, layer="F.Cu", net="NET1")
        pcb.add_trace(("J1", "1"), (40, 56), width=1.0, layer="F.Cu", net="NET1")
        pcb.add_trace((40, 56), ("J2", "1"), width=1.0, layer="F.Cu", net="NET1")
        resolution = resolve_current_path(pcb, _spec())
        assert resolution.status == "ambiguous"
        assert resolution.min_width is None

    def test_kelvin_sense_tap_is_not_the_reported_minimum(self) -> None:
        """Sense copper is excluded by construction -- it is not in ``segments``.

        Reuses ``tests/test_current_paths_kelvin_tap.py``'s board: a 2.0 mm
        stub into a 3.0 mm trunk, with a *recognised* 0.2 mm Kelvin tap. The
        reported minimum must be the 2.0 mm stub -- never the thinner tap.
        """
        from .test_current_paths_kelvin_tap import spec as tap_spec
        from .test_current_paths_kelvin_tap import tap_board

        pcb = tap_board(tap_width=0.2)
        resolution = resolve_current_path(pcb, tap_spec())
        assert resolution.ok, resolution.reason
        assert resolution.sense_taps, "fixture must produce a recognised sense tap"
        assert min(seg.width for seg in resolution.sense_taps) == pytest.approx(0.2)
        found = resolution.min_width
        assert found is not None
        assert found.width_mm == pytest.approx(2.0)


# --------------------------------------------------------------------------
# CLI tier: current-paths-audit JSON and text output
# --------------------------------------------------------------------------


class TestMinWidthAuditJson:
    def test_neck_is_reported_with_layer_and_sheet_position(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        _run_audit_cli(_necked_pcb(), _spec(), tmp_path, monkeypatch)
        entry = json.loads(capsys.readouterr().out)["resolutions"][0]
        assert entry["status"] == "resolved"
        assert entry["min_width_mm"] == pytest.approx(0.2)
        assert entry["min_width_layer"] == "F.Cu"
        assert entry["min_width_at"] == pytest.approx([32.0, 50.0])
        assert entry["min_width_parallel_group"] is False

    def test_position_is_sheet_not_board_coordinates(self, tmp_path, capsys, monkeypatch) -> None:
        """AC 5: the audit adds ``board_origin``, like its layer-change positions.

        A non-zero origin is set deliberately so a board-vs-sheet mix-up
        fails here instead of passing by accident at the sheet origin.
        """
        board = _necked_pcb()
        board._board_origin = (100.0, 25.0)
        assert board.board_origin == (100.0, 25.0)
        _run_audit_cli(board, _spec(), tmp_path, monkeypatch)
        entry = json.loads(capsys.readouterr().out)["resolutions"][0]
        # Board midpoint (32, 50) + origin (100, 25).
        assert entry["min_width_at"] == pytest.approx([132.0, 75.0])

    def test_uniform_path_reports_its_one_width(self, tmp_path, capsys, monkeypatch) -> None:
        _run_audit_cli(_uniform_pcb(width=1.5), _spec(), tmp_path, monkeypatch)
        entry = json.loads(capsys.readouterr().out)["resolutions"][0]
        assert entry["min_width_mm"] == pytest.approx(1.5)

    def test_doubled_stretch_reports_summed_width_and_flags_it(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        _run_audit_cli(_doubled_pcb(widths=(1.0, 1.0)), _spec(), tmp_path, monkeypatch)
        entry = json.loads(capsys.readouterr().out)["resolutions"][0]
        assert entry["min_width_mm"] == pytest.approx(2.0)
        assert entry["min_width_parallel_group"] is True

    def test_unresolved_path_carries_null_not_zero(self, tmp_path, capsys, monkeypatch) -> None:
        _run_audit_cli(_necked_pcb(), _spec(sink=("J9", "1")), tmp_path, monkeypatch)
        entry = json.loads(capsys.readouterr().out)["resolutions"][0]
        assert entry["status"] == "unresolved"
        assert entry["min_width_mm"] is None
        assert entry["min_width_at"] is None
        assert entry["min_width_layer"] is None
        assert entry["min_width_parallel_group"] is None


class TestMinWidthAuditText:
    def test_text_output_prints_the_neck(self, tmp_path, capsys, monkeypatch) -> None:
        _run_audit_cli(_necked_pcb(), _spec(), tmp_path, monkeypatch, output_format="text")
        out = capsys.readouterr().out
        assert "min width 0.200 mm on F.Cu at (32.000, 50.000)" in out
        assert "summed parallel group" not in out

    def test_text_output_marks_a_summed_group(self, tmp_path, capsys, monkeypatch) -> None:
        _run_audit_cli(
            _doubled_pcb(widths=(1.0, 1.0)),
            _spec(),
            tmp_path,
            monkeypatch,
            output_format="text",
        )
        out = capsys.readouterr().out
        assert "min width 2.000 mm on" in out
        assert "(summed parallel group)" in out

    def test_text_output_omits_the_line_when_unresolved(
        self, tmp_path, capsys, monkeypatch
    ) -> None:
        _run_audit_cli(
            _necked_pcb(),
            _spec(sink=("J9", "1")),
            tmp_path,
            monkeypatch,
            output_format="text",
        )
        out = capsys.readouterr().out
        assert "[unresolved]" in out
        assert "min width" not in out
