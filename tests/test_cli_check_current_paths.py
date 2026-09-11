"""CLI tests for ``kct check --current-paths <path>`` (Issue #4980/#5124).

Verifies the ``--current-paths`` flag is wired into ``check_cmd.py`` and
threaded into ``DRCChecker`` (via the new ``check_path_ampacity`` method) so
``PathAmpacityRule`` is reachable from ``kct check`` -- the second deferred
item from PR #5125 (Issue #5124's Acceptance Criterion 2).

Coverage mirrors ``tests/test_cli_check_net_class_map.py`` /
``tests/test_cli_check_ampacity_net_class_map.py``:

1. **Graceful degradation**: without ``--current-paths`` (and no sidecar on
   disk), ``path_ampacity`` reports zero violations.
2. **Explicit flag wiring**: an adequate trunk + a declared low-current
   sense spur passes; narrowing the trunk below its own declared-current
   IPC-2221 floor fails -- the issue's core T-network acceptance test,
   exercised through the CLI rather than the rule directly.
3. **Sidecar auto-discovery**: a bare ``current_paths.json`` next to the
   board is picked up with no flag, mirroring ``--net-class-map``'s
   auto-discovery contract (Issue #5124 AC2).
4. **Endpoint fail-closed**: a spec whose declared pad no longer resolves
   reports an error, not a silent pass.
5. **Error paths**: missing explicit file, malformed JSON, and the
   ``--current-paths``/``--no-current-paths`` mutual-exclusivity guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint, dump_current_path_specs

# IPC-2221 external 1oz floor for the trunk's declared 15A is ~12.585mm
# (matches tests/test_cli_check_ampacity_net_class_map.py's own 1oz-external
# reference value) and for the sense spec's declared 0.01A is ~0.0005mm --
# negligible next to any physically routable trace.
ADEQUATE_TRUNK_WIDTH_MM = 13.0
NARROW_TRUNK_WIDTH_MM = 2.0
SENSE_WIDTH_MM = 0.2

# 15A J1->J2 trunk plus a low-current J1->U3 sense spur, all on one net.
# Raw-text fixture (mirrors tests/test_router_io.py's footprint/pad style)
# rather than the Python PCB-building API: footprints appended via the
# private ``_footprints`` list are not round-tripped by ``PCB.save()``, and
# the CLI under test always loads the board fresh from disk. Geometry
# mirrors tests/test_path_ampacity_check.py::_t_network_pcb -- a single
# T-junction at (60, 50) keeps the net's copper a tree (no cycle), so both
# declared paths resolve cleanly.
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


def _trunk_spec(current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="TRUNK",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _sense_spec(current_a: float = 0.01) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="SENSE",
        net_name="NET1",
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
    )


def _write_sidecar(path: Path, specs: list[CurrentPathSpec]) -> Path:
    path.write_text(json.dumps(dump_current_path_specs(specs)))
    return path


def _path_ampacity_violations(report_path: Path) -> list[dict]:
    data = json.loads(report_path.read_text())
    return [v for v in data["violations"] if v.get("rule_id") == "path_ampacity"]


def _run_check(pcb: Path, report: Path, *, extra: list[str] | None = None) -> int:
    from kicad_tools.cli.check_cmd import main

    argv = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--only",
        "path_ampacity",
        "--output",
        str(report),
        "--format",
        "json",
        "--allow-incomplete",
    ]
    if extra:
        argv.extend(extra)
    return main(argv)


@pytest.fixture
def adequate_pcb(tmp_path: Path) -> Path:
    return _write_t_network_pcb(tmp_path / "board.kicad_pcb", trunk_width=ADEQUATE_TRUNK_WIDTH_MM)


@pytest.fixture
def narrow_trunk_pcb(tmp_path: Path) -> Path:
    return _write_t_network_pcb(tmp_path / "board.kicad_pcb", trunk_width=NARROW_TRUNK_WIDTH_MM)


class TestGracefulDegradation:
    def test_no_flag_no_sidecar_is_clean_noop(self, adequate_pcb: Path, tmp_path: Path):
        """Without --current-paths (and nothing on disk), path_ampacity is silent."""
        report = tmp_path / "report.json"
        result = _run_check(adequate_pcb, report)
        assert result == 0
        assert _path_ampacity_violations(report) == []


class TestExplicitFlagWiring:
    """The issue's core T-network acceptance test, through the CLI."""

    def test_adequate_trunk_and_narrow_sense_passes(self, adequate_pcb: Path, tmp_path: Path):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0), _sense_spec(0.01)])
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 0
        assert _path_ampacity_violations(report) == []

    def test_narrowing_trunk_fails(self, narrow_trunk_pcb: Path, tmp_path: Path):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0), _sense_spec(0.01)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2  # errors present -> non-zero exit
        violations = _path_ampacity_violations(report)
        trunk_errors = [
            v for v in violations if "TRUNK" in v.get("items", []) and v["severity"] == "error"
        ]
        assert len(trunk_errors) == 2  # both trunk segments under-width
        # The sense spur's own tiny declared current is unaffected.
        assert not any(
            "SENSE" in v.get("items", []) for v in violations if v["severity"] == "error"
        )


class TestSidecarAutoDiscovery:
    def test_bare_sidecar_next_to_board_is_auto_loaded(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        """A bare current_paths.json next to the board engages the rule
        with no flag -- mirrors --net-class-map's auto-discovery (#5124 AC2)."""
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec(15.0)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report)

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 2

    def test_no_current_paths_suppresses_auto_discovery(
        self, narrow_trunk_pcb: Path, tmp_path: Path
    ):
        _write_sidecar(tmp_path / "current_paths.json", [_trunk_spec(15.0)])
        report = tmp_path / "report.json"

        result = _run_check(narrow_trunk_pcb, report, extra=["--no-current-paths"])

        assert result == 0
        assert _path_ampacity_violations(report) == []


class TestEndpointFailClosed:
    def test_moved_pad_reports_error_not_silent_pass(self, adequate_pcb: Path, tmp_path: Path):
        """A declared endpoint that no longer resolves is a visible error --
        the issue's fail-closed acceptance criterion, through the CLI."""
        bogus_spec = CurrentPathSpec(
            name="TRUNK",
            net_name="NET1",
            source=PathEndpoint("J1", "99"),  # pad "99" does not exist
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
        )
        sidecar = _write_sidecar(tmp_path / "cp.json", [bogus_spec])
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report, extra=["--current-paths", str(sidecar)])

        assert result == 2
        errors = [v for v in _path_ampacity_violations(report) if v["severity"] == "error"]
        assert len(errors) == 1
        assert "unresolved" in errors[0]["message"]


class TestErrorPaths:
    def test_missing_explicit_file_returns_1(self, adequate_pcb: Path, capsys):
        from kicad_tools.cli.check_cmd import main

        result = main([str(adequate_pcb), "--current-paths", "/does/not/exist/current_paths.json"])
        assert result == 1
        captured = capsys.readouterr()
        assert "current-paths" in captured.err
        assert "not found" in captured.err

    def test_malformed_explicit_json_returns_1(self, adequate_pcb: Path, tmp_path: Path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("not { valid json")
        from kicad_tools.cli.check_cmd import main

        result = main([str(adequate_pcb), "--current-paths", str(bad)])
        assert result == 1
        captured = capsys.readouterr()
        assert "current-paths" in captured.err.lower() or "parsing" in captured.err.lower()

    def test_malformed_autodiscovered_json_degrades_gracefully(
        self, adequate_pcb: Path, tmp_path: Path, capsys
    ):
        (tmp_path / "current_paths.json").write_text("not { valid json")
        report = tmp_path / "report.json"

        result = _run_check(adequate_pcb, report)

        assert result == 0
        captured = capsys.readouterr()
        assert "malformed" in captured.err.lower()
        assert _path_ampacity_violations(report) == []

    def test_flag_and_no_flag_mutually_exclusive(self, adequate_pcb: Path, tmp_path: Path, capsys):
        sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(15.0)])
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(adequate_pcb),
                "--current-paths",
                str(sidecar),
                "--no-current-paths",
            ]
        )
        assert result == 1
        captured = capsys.readouterr()
        assert "--no-current-paths" in captured.err
        assert "--current-paths" in captured.err


@pytest.mark.parametrize("bridge", [False, True])
def test_cli_cross_layer_path_requires_via(tmp_path, bridge):
    board = tmp_path / "layers.kicad_pcb"
    copper = _T_NETWORK_PCB_TEMPLATE.format(
        trunk_width=ADEQUATE_TRUNK_WIDTH_MM, sense_width=SENSE_WIDTH_MM
    )
    copper = copper.replace(
        f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "F.Cu")',
        f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "B.Cu")',
    )
    assert f'(end 120 50) (width {ADEQUATE_TRUNK_WIDTH_MM}) (layer "B.Cu")' in copper
    if bridge:
        copper = (
            copper.rstrip()[:-1]
            + '(via (at 60 50) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))\n)\n'
        )
    board.write_text(copper)
    before = board.read_bytes()
    sidecar = _write_sidecar(tmp_path / "cp.json", [_trunk_spec(), _sense_spec()])
    report = tmp_path / "report.json"
    status = _run_check(board, report, extra=["--current-paths", str(sidecar)])
    violations = _path_ampacity_violations(report)
    if bridge:
        assert status == 0
        assert violations == []
    else:
        assert status == 2
        assert any("unresolved" in v["message"].lower() for v in violations)
    assert board.read_bytes() == before
