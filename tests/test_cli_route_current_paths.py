"""CLI tests for ``kct route --current-paths <path>`` (Issue #4980).

Third and final consumer surface of the declared branch-specific
current-path model:

* PR #5125 landed the model itself (``CurrentPathSpec`` / ``PathEndpoint``,
  fail-closed ``resolve_current_path``, ``audit_current_paths``) plus the
  reinforcement-generation gate (``kct pcb reinforce --current-paths``).
* PR #5184 wired ``PathAmpacityRule`` into ``DRCChecker`` and added
  ``kct check --current-paths`` with sidecar auto-discovery.
* This module covers the routing surface: ``kct route --current-paths``,
  the post-route DRC running ``path_ampacity`` against each declared
  branch's OWN current, and the re-emitted ``current_paths.json`` sidecar
  next to the routed board that makes route-time intent and the later
  independent final-copper audit read *identical* declarations.

Coverage:

1. **Preload contract** (``_preload_current_paths``) -- explicit-strict /
   auto-discover / degrade-on-malformed-auto-discovery / suppression /
   mutual exclusivity, mirroring ``kct check``'s contract exactly.
2. **Post-route DRC** -- the issue's T-network acceptance test through the
   route gate: an adequate 15 A trunk plus a declared low-current sense
   spur passes, and narrowing the trunk fails.  Plus the fail-closed
   endpoint test (a moved/renamed pad is an explicit unresolved error,
   never a silent fallback to whole-net ampacity).
3. **Sidecar emission** -- written next to the routed board, round-trips
   the declarations, never clobbers the source file, and is *not* written
   when nothing was declared (an empty sidecar would read as "declared and
   clean", the silent pass this feature exists to prevent).
4. **Route-time / audit agreement** -- a bare ``kct check`` on the routed
   board auto-discovers the emitted sidecar and reaches the same
   ``path_ampacity`` verdict the route gate did.
5. **Outer-shim forwarding** -- ``commands/routing.py`` forwards both flags
   to the inner parser (the ``tests/test_cli_parser_drift.py`` bug class).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    dump_current_path_specs,
    load_current_path_specs,
)

# IPC-2221 external 1oz floor for the trunk's declared 15 A is ~12.585 mm,
# and for the sense spec's declared 0.01 A it is ~0.0005 mm -- negligible
# next to any physically routable trace.  Same reference values as
# tests/test_cli_check_current_paths.py.
ADEQUATE_TRUNK_WIDTH_MM = 13.0
NARROW_TRUNK_WIDTH_MM = 2.0
SENSE_WIDTH_MM = 0.2

# 15 A J1->J2 trunk plus a low-current J1->U3 sense spur, all on ONE net --
# the exact topology the issue describes (/AC_NEUTRAL carrying both a 15 A
# force path and a zero-cross / INA181 sense tap).  A single T-junction at
# (60, 50) keeps the net's copper a tree, so both declared paths resolve
# cleanly rather than reporting "ambiguous".
_T_NETWORK_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 0 0) (end 200 120)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 20 50)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 120 50)
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 60 70)
    (fp_text reference "U3" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (segment (start 20 50) (end 60 50) (width {trunk_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 60 50) (end 120 50) (width {trunk_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000002"))
  (segment (start 60 50) (end 60 70) (width {sense_width}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000003"))
)
"""


def _write_t_network_pcb(
    path: Path, *, trunk_width: float, sense_width: float = SENSE_WIDTH_MM
) -> Path:
    path.write_text(
        _T_NETWORK_PCB_TEMPLATE.format(trunk_width=trunk_width, sense_width=sense_width)
    )
    return path


# Same J1/J2 endpoints as the T-network, but the copper between them splits
# into TWO disjoint routes that rejoin -- a parallel return path, or the same
# net energised by a different operating mode.  The issue's third acceptance
# test: current is free to divide between the branches, so NO single resolved
# path represents the real loading, and the gate must say so rather than
# silently adopting whichever branch a traversal happened to reach first.
_PARALLEL_RETURN_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 0 0) (end 200 120)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 20 50)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (footprint "TH:Pad"
    (layer "F.Cu")
    (at 120 50)
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu") (net 1 "NET1"))
  )
  (segment (start 20 50) (end 60 50) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a1"))
  (segment (start 60 50) (end 80 30) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a2"))
  (segment (start 80 30) (end 100 50) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a3"))
  (segment (start 60 50) (end 80 70) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a4"))
  (segment (start 80 70) (end 100 50) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a5"))
  (segment (start 100 50) (end 120 50) (width {w}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-0000000000a6"))
)
"""


def _write_parallel_return_pcb(path: Path, *, width: float = ADEQUATE_TRUNK_WIDTH_MM) -> Path:
    path.write_text(_PARALLEL_RETURN_PCB_TEMPLATE.format(w=width))
    return path


def _trunk_spec(current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
        notes="15A force path",
    )


def _sense_spec(current_a: float = 0.01) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="SENSE",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
        notes="sense tap -- never reinforced",
    )


def _write_sidecar(path: Path, specs: list[CurrentPathSpec]) -> Path:
    path.write_text(json.dumps(dump_current_path_specs(specs), indent=2))
    return path


def _route_args(**overrides) -> argparse.Namespace:
    """A minimal parsed-route namespace for the preload helper."""
    base = {"current_paths": None, "no_current_paths": False, "quiet": True}
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# 1. Preload contract
# ---------------------------------------------------------------------------


class TestPreloadCurrentPaths:
    def test_no_flag_and_no_sidecar_leaves_specs_empty(self, tmp_path):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        args = _route_args()
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert args._loaded_current_paths == []
        assert args._current_paths_input_path is None

    def test_no_sidecar_says_the_check_is_inactive(self, tmp_path, capsys):
        """An inactive check must never be mistaken for a passing one.

        One line only -- this fires on every route of every board that does
        not use the feature, so it points at the flag rather than dumping
        ``kct check``'s full probed-candidate list.
        """
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        args = _route_args(quiet=False)
        assert route_cmd._preload_current_paths(args, pcb) == 0
        out = capsys.readouterr().out
        assert "path_ampacity) are INACTIVE" in out
        assert len([line for line in out.splitlines() if line.strip()]) == 1

    def test_suppression_flag_silences_the_inactive_note(self, tmp_path, capsys):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        args = _route_args(quiet=False, no_current_paths=True)
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert capsys.readouterr().out == ""

    def test_explicit_sidecar_loads(self, tmp_path):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        sidecar = _write_sidecar(tmp_path / "declared.json", [_trunk_spec(), _sense_spec()])
        args = _route_args(current_paths=str(sidecar))
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert [s.name for s in args._loaded_current_paths] == ["TRUNK", "SENSE"]
        assert args._current_paths_input_path == sidecar.resolve()

    def test_explicit_missing_file_is_error(self, tmp_path, capsys):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        args = _route_args(current_paths=str(tmp_path / "nope.json"))
        assert route_cmd._preload_current_paths(args, pcb) == 1
        assert "current-paths file not found" in capsys.readouterr().err

    def test_explicit_malformed_json_is_error(self, tmp_path, capsys):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        args = _route_args(current_paths=str(bad))
        assert route_cmd._preload_current_paths(args, pcb) == 1
        assert "parsing current-paths JSON" in capsys.readouterr().err

    def test_explicit_invalid_structure_is_error(self, tmp_path, capsys):
        """A structurally-invalid spec fails loudly, not as an empty list."""
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"paths": [{"name": "X", "net": "NET1"}]}))
        args = _route_args(current_paths=str(bad))
        assert route_cmd._preload_current_paths(args, pcb) == 1
        assert "parsing current-paths JSON" in capsys.readouterr().err

    def test_auto_discovered_sidecar_loads(self, tmp_path, capsys):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec()])
        args = _route_args()
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert [s.name for s in args._loaded_current_paths] == ["TRUNK"]
        assert "auto-loaded current-paths sidecar" in capsys.readouterr().err

    def test_stem_keyed_sidecar_wins_over_bare(self, tmp_path):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        _write_sidecar(tmp_path / "current_paths.json", [_sense_spec()])
        _write_sidecar(tmp_path / "b.current_paths.json", [_trunk_spec()])
        args = _route_args()
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert [s.name for s in args._loaded_current_paths] == ["TRUNK"]

    def test_auto_discovered_malformed_degrades_to_warning(self, tmp_path, capsys):
        """A broken file the user did not name must not fail the route."""
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        (tmp_path / "current_paths.json").write_text("{not json")
        args = _route_args()
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert args._loaded_current_paths == []
        assert "ignoring malformed current-paths sidecar" in capsys.readouterr().err

    def test_no_current_paths_suppresses_discovery(self, tmp_path):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec()])
        args = _route_args(no_current_paths=True)
        assert route_cmd._preload_current_paths(args, pcb) == 0
        assert args._loaded_current_paths == []
        assert args._current_paths_input_path is None

    def test_mutually_exclusive_flags_error(self, tmp_path, capsys):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        sidecar = _write_sidecar(tmp_path / "declared.json", [_trunk_spec()])
        args = _route_args(current_paths=str(sidecar), no_current_paths=True)
        assert route_cmd._preload_current_paths(args, pcb) == 1
        assert "--no-current-paths cannot be combined with" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 2./3./4. Post-route DRC, sidecar emission, audit agreement
# ---------------------------------------------------------------------------


def _patch_geometric_absent(monkeypatch) -> None:
    """Stand in for a KiCad-less environment so the suite never shells out."""
    import kicad_tools.drc as drc_mod

    monkeypatch.setattr(
        drc_mod,
        "run_geometric_drc",
        lambda *a, **k: GeometricDRCResult(ran=False, note="kicad-cli not found"),
    )


def _capture_drc_results(monkeypatch) -> dict:
    """Record the DRCChecker kwargs + check_all() results of the route gate."""
    import kicad_tools.validate as validate_mod

    real_checker = validate_mod.DRCChecker
    captured: dict = {}

    class _RecordingChecker(real_checker):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            super().__init__(*args, **kwargs)

        def check_all(self, *args, **kwargs):
            results = super().check_all(*args, **kwargs)
            captured["results"] = results
            return results

    monkeypatch.setattr(validate_mod, "DRCChecker", _RecordingChecker)
    return captured


def _path_ampacity_violations(captured: dict) -> list:
    results = captured["results"]
    return [v for v in results.errors if getattr(v, "rule_id", "") == "path_ampacity"]


def _path_ampacity_warnings(captured: dict) -> list:
    results = captured["results"]
    return [v for v in results.warnings if getattr(v, "rule_id", "") == "path_ampacity"]


def _run_gate(board: Path, specs, *, input_path: Path | None = None) -> tuple[int, int]:
    return route_cmd.run_post_route_drc(
        output_path=board,
        manufacturer="jlcpcb",
        layers=2,
        quiet=True,
        current_path_specs=specs,
        current_paths_input_path=input_path,
    )


class TestRoutePostDrcPathAmpacity:
    def test_adequate_trunk_and_narrow_sense_passes(self, tmp_path, monkeypatch):
        """T-network acceptance test, pass side (Issue #4980)."""
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec(), _sense_spec()])

        assert captured["kwargs"]["current_path_specs"] is not None
        assert _path_ampacity_violations(captured) == []

    def test_narrow_trunk_fails(self, tmp_path, monkeypatch):
        """T-network acceptance test, fail side: the 15 A trunk is too thin.

        The sense spur is UNCHANGED between this test and the pass case --
        only the trunk narrows -- so the failure is attributable to the
        declared branch current, not to a net-wide width decision.
        """
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=NARROW_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec(), _sense_spec()])

        violations = _path_ampacity_violations(captured)
        assert violations, "narrow 15A trunk must fail its own declared-current check"
        assert any("TRUNK" in v.message for v in violations)
        # The 0.2mm sense spur is fine for its OWN declared 0.01A: a net-wide
        # model would have condemned it alongside the trunk.
        assert not any("SENSE" in v.message for v in violations)

    def test_moved_endpoint_fails_closed(self, tmp_path, monkeypatch):
        """A declared pad that no longer resolves is an explicit error."""
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )
        moved = CurrentPathSpec(
            name="TRUNK",
            net_name="NET1",
            source=PathEndpoint("J1", "1"),
            sink=PathEndpoint("J9", "1"),  # component no longer on the board
            continuous_a=15.0,
            reinforcement_eligible=True,
        )

        _run_gate(board, [moved])

        violations = _path_ampacity_violations(captured)
        assert violations, "an unresolved endpoint must never silently pass"
        assert any("J9" in v.message for v in violations)

    def test_without_specs_path_ampacity_stays_inactive(self, tmp_path, monkeypatch):
        """Flag-off parity: the pre-#4980 verdict is unchanged."""
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=NARROW_TRUNK_WIDTH_MM
        )

        _run_gate(board, None)

        assert _path_ampacity_violations(captured) == []


class TestAmbiguityAndUncoveredCopperAtTheRouteGate:
    """Issue #4980's remaining audit-visibility acceptance tests, at the gate.

    The rule itself is covered by ``tests/test_path_ampacity_check.py``; what
    these add is that the ROUTE gate propagates both outcomes -- an ambiguous
    path as a blocking error, uncovered copper as a non-blocking but visible
    warning -- rather than dropping either on the way through
    ``run_post_route_drc``.
    """

    def test_parallel_return_path_is_not_silently_inherited(self, tmp_path, monkeypatch):
        """A loop between the endpoints is reported, never resolved to one branch.

        Both branches are the SAME generous width, so any width-based check
        would pass: the only thing that can fail here is the gate correctly
        refusing to claim it knows how 15 A divides between two routes.
        """
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_parallel_return_pcb(tmp_path / "routed.kicad_pcb")

        errors, _ = _run_gate(board, [_trunk_spec()])

        violations = _path_ampacity_violations(captured)
        assert violations, "a parallel return path must never silently pass"
        assert any("ambiguous" in v.message for v in violations)
        # ...and it blocks: the route's own error count carries it, so the
        # command exits non-zero rather than reporting a clean route.
        assert errors > 0

    def test_uncovered_copper_is_visible_but_does_not_block(self, tmp_path, monkeypatch):
        """Declaring only the trunk leaves the sense spur explicitly unmodeled.

        "Unknown/uncovered current paths must be visible rather than silently
        waived" -- as a warning, since undeclared copper on a partially
        modeled net is usually work-in-progress rather than a defect.
        """
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec()])  # SENSE deliberately NOT declared

        warnings = _path_ampacity_warnings(captured)
        assert warnings, "undeclared copper on a declared net must be surfaced"
        assert any("not covered by any resolved path" in w.message for w in warnings)
        # Visible, not blocking: the trunk itself is adequately wide.
        assert _path_ampacity_violations(captured) == []

    def test_declaring_the_spur_clears_the_uncovered_warning(self, tmp_path, monkeypatch):
        """The warning is actionable: declaring the missing branch retires it."""
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec(), _sense_spec()])

        assert _path_ampacity_warnings(captured) == []


class TestKelvinShuntChain:
    """The route gate's emitted sidecar drives the reinforcement gate (#4980).

    ``tests/test_pcb_reinforce.py`` already proves ``kct pcb reinforce
    --current-paths <authored file>`` never bridges a Kelvin sense tap to its
    force path.  What is untested until here is the *chain*: that the sidecar
    ``kct route`` re-emits is itself a sufficient, lossless input to that
    gate.  If ``reinforcement_eligible`` did not survive the round trip, the
    sense branch would silently become anchorable one command later.
    """

    def _build_kelvin_board(self, tmp_path: Path) -> Path:
        from kicad_tools.schema.pcb import PCB
        from tests.test_pcb_reinforce import _write_single_pad_footprint_lib

        mod_path = _write_single_pad_footprint_lib(tmp_path)
        pcb = PCB.create(width=200, height=120, center=False)
        pcb.add_footprint_from_file(mod_path, "RSH1", 20, 50)
        pcb.add_footprint_from_file(mod_path, "J2", 120, 50)
        pcb.add_footprint_from_file(mod_path, "U3", 60, 80)
        for ref in ("RSH1", "J2", "U3"):
            pcb.assign_net_to_footprint_pad(ref, "1", "PGND")
        pcb.add_trace(("RSH1", "1"), (60, 50), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((60, 50), ("J2", "1"), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((60, 50), ("U3", "1"), width=0.2, layer="F.Cu", net="PGND")
        pcb_path = tmp_path / "routed.kicad_pcb"
        pcb.save(pcb_path)
        return pcb_path

    @staticmethod
    def _force_spec() -> CurrentPathSpec:
        return CurrentPathSpec(
            name="FORCE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
            reinforcement_eligible=True,
        )

    @staticmethod
    def _kelvin_sense_spec() -> CurrentPathSpec:
        return CurrentPathSpec(
            name="KELVIN_SENSE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("U3", "1"),
            continuous_a=0.001,
            reinforcement_eligible=False,
        )

    def test_emitted_sidecar_still_gates_reinforcement(self, tmp_path, monkeypatch):
        import math

        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB
        from tests.test_pcb_reinforce import _reinforce_args

        _patch_geometric_absent(monkeypatch)
        _capture_drc_results(monkeypatch)
        board = self._build_kelvin_board(tmp_path)

        _run_gate(board, [self._force_spec(), self._kelvin_sense_spec()])

        emitted = tmp_path / "current_paths.json"
        assert emitted.is_file(), "the route gate must emit what it validated against"
        # Eligibility survived the round trip -- the property the next command reads.
        assert [s.reinforcement_eligible for s in load_current_path_specs(emitted)] == [
            True,
            False,
        ]

        rc = run_pcb_command(
            _reinforce_args(
                board,
                net="PGND",
                spacing=10.0,
                all_runs=True,
                current_paths=str(emitted),
                format="json",
            )
        )
        assert rc == 0

        after = PCB.load(board)
        assert after.vias, "the force path is reinforcement-eligible and must be anchored"
        u3_pos = after.get_pad_position("U3", "1")
        assert u3_pos is not None
        assert all(math.dist(v.position, u3_pos) > 1.0 for v in after.vias), (
            "no anchor may land on the Kelvin sense tap"
        )

    def test_route_gate_checks_force_and_sense_against_their_own_currents(
        self, tmp_path, monkeypatch
    ):
        """The 0.2 mm sense tap is fine at 1 mA; the 3 mm force path is not at 15 A.

        Both branches sit on ONE net, so a whole-net ``target_ampacity`` would
        have had to condemn both or neither.
        """
        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        board = self._build_kelvin_board(tmp_path)

        _run_gate(board, [self._force_spec(), self._kelvin_sense_spec()])

        violations = _path_ampacity_violations(captured)
        assert any("FORCE" in v.message for v in violations)
        assert not any("KELVIN_SENSE" in v.message for v in violations)


class TestCurrentPathsSidecarEmission:
    def test_sidecar_written_next_to_routed_board(self, tmp_path, monkeypatch):
        _patch_geometric_absent(monkeypatch)
        _capture_drc_results(monkeypatch)
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        board = _write_t_network_pcb(
            out_dir / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec(), _sense_spec()])

        sidecar = out_dir / "current_paths.json"
        assert sidecar.is_file()
        reloaded = load_current_path_specs(sidecar)
        assert [s.name for s in reloaded] == ["TRUNK", "SENSE"]
        # Full round-trip fidelity -- currents and reinforcement eligibility
        # survive, so the later audit checks the same intent, not a lossy copy.
        assert [s.continuous_a for s in reloaded] == [15.0, 0.01]
        assert [s.reinforcement_eligible for s in reloaded] == [True, False]

    def test_no_sidecar_written_when_nothing_declared(self, tmp_path, monkeypatch):
        """An empty sidecar would read as 'declared and clean' -- never write one."""
        _patch_geometric_absent(monkeypatch)
        _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        _run_gate(board, [])

        assert not (tmp_path / "current_paths.json").exists()

    def test_authored_input_is_never_clobbered(self, tmp_path, monkeypatch):
        """Routing in place must not rewrite the user's own sidecar (#4428)."""
        _patch_geometric_absent(monkeypatch)
        _capture_drc_results(monkeypatch)
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )
        authored = _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec()])
        authored_text = authored.read_text()

        _run_gate(board, [_trunk_spec(), _sense_spec()], input_path=authored)

        assert authored.read_text() == authored_text
        diverted = tmp_path / "current_paths.effective.json"
        assert diverted.is_file()
        assert [s.name for s in load_current_path_specs(diverted)] == ["TRUNK", "SENSE"]

    def test_write_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """A blocked output directory warns; it never fails the route."""
        board = _write_t_network_pcb(
            tmp_path / "routed.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM
        )

        def _boom(self, *a, **k):
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "write_text", _boom)
        # Calls the writer directly: run_post_route_drc's later stages would
        # hit the same patched write_text for unrelated sidecars.
        route_cmd._write_current_paths_sidecar(board, [_trunk_spec()], quiet=True)


class TestRouteIntentAndAuditAgree:
    def test_emitted_sidecar_is_what_a_bare_check_audits(self, tmp_path, monkeypatch):
        """Route-time intent and the independent final-copper audit agree.

        The route gate emits the declarations it validated against; a later
        bare ``kct check`` (no ``--current-paths`` flag) auto-discovers that
        emission and reaches the same ``path_ampacity`` verdict.  Both
        surfaces therefore judge identical intent -- any disagreement would
        be a real copper difference, not a difference in what was declared.
        """
        from kicad_tools.cli.check_cmd import main as check_main
        from kicad_tools.router.current_paths import discover_current_paths_sidecar

        _patch_geometric_absent(monkeypatch)
        captured = _capture_drc_results(monkeypatch)
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        board = _write_t_network_pcb(
            out_dir / "routed.kicad_pcb", trunk_width=NARROW_TRUNK_WIDTH_MM
        )

        _run_gate(board, [_trunk_spec(), _sense_spec()])
        route_time = _path_ampacity_violations(captured)
        assert route_time, "route gate must flag the narrow trunk"

        # The audit side reads exactly what the route side declared.
        discovered = discover_current_paths_sidecar(board)
        assert discovered == out_dir / "current_paths.json"

        report = tmp_path / "report.json"
        check_main(
            [
                str(board),
                "--only",
                "path_ampacity",
                "--format",
                "json",
                "--output",
                str(report),
            ]
        )
        audited = [
            v
            for v in json.loads(report.read_text())["violations"]
            if v.get("rule_id") == "path_ampacity"
        ]
        assert audited, "bare kct check must auto-discover the emitted sidecar"
        # Same branch assignments on both sides: the trunk is condemned, the
        # sense spur is not.
        assert {v.message for v in route_time} == {v["message"] for v in audited}


# ---------------------------------------------------------------------------
# 5. Outer-shim forwarding (the tests/test_cli_parser_drift.py bug class)
# ---------------------------------------------------------------------------


def _forwarded_argv(monkeypatch, argv: list[str]) -> list[str]:
    """Parse ``kct <argv>`` and capture the sub-argv the shim builds."""
    from kicad_tools.cli import route_cmd as route_cmd_mod
    from kicad_tools.cli.commands import routing as routing_mod
    from kicad_tools.cli.parser import create_parser

    captured: dict[str, list[str]] = {}

    def _fake_main(sub_argv):
        captured["argv"] = list(sub_argv)
        return 0

    monkeypatch.setattr(route_cmd_mod, "main", _fake_main)
    monkeypatch.setattr(routing_mod, "route_main", _fake_main, raising=False)

    args = create_parser().parse_args(argv)
    routing_mod.run_route_command(args)
    return captured["argv"]


class TestOuterShimForwarding:
    def test_current_paths_is_forwarded(self, tmp_path, monkeypatch):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        sidecar = _write_sidecar(tmp_path / "declared.json", [_trunk_spec()])
        argv = _forwarded_argv(monkeypatch, ["route", str(pcb), "--current-paths", str(sidecar)])
        assert "--current-paths" in argv
        assert argv[argv.index("--current-paths") + 1] == str(sidecar)

    def test_no_current_paths_is_forwarded(self, tmp_path, monkeypatch):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        argv = _forwarded_argv(monkeypatch, ["route", str(pcb), "--no-current-paths"])
        assert "--no-current-paths" in argv

    def test_flag_off_argv_carries_neither_flag(self, tmp_path, monkeypatch):
        pcb = _write_t_network_pcb(tmp_path / "b.kicad_pcb", trunk_width=13.0)
        argv = _forwarded_argv(monkeypatch, ["route", str(pcb)])
        assert "--current-paths" not in argv
        assert "--no-current-paths" not in argv


@pytest.mark.parametrize("flag", ["--current-paths", "--no-current-paths"])
def test_flag_on_both_route_parsers(flag):
    """Guards the #2817/#2819 argparse-drift bug class for the new flags."""
    from tests.test_cli_parser_drift import _inner_route_parser_flags, _outer_route_parser_flags

    assert flag in _inner_route_parser_flags()
    assert flag in _outer_route_parser_flags()
