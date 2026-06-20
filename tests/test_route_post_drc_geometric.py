"""Tests for native-DRC reconciliation in the ``kct route`` post-route gate.

Issue #3803: ``run_post_route_drc`` used only the internal ``DRCChecker``
and printed ``DRC PASSED`` on a clean internal verdict, even though native
``kicad-cli pcb drc`` could find 400+ violations (including shorts) on the
identical file.  The route gate now reconciles against the shared
:func:`kicad_tools.drc.run_geometric_drc` helper so that:

* PASS requires BOTH the internal engine AND native kicad-cli to be clean.
* A clean-internal / dirty-native divergence emits a loud WARNING and the
  combined error count makes the gate FAIL.
* When kicad-cli is absent the gate still completes but prints an explicit
  "internal-engine-only PASS" note (no silent overstatement).

The reconciliation logic is unit-tested with the geometric helper mocked so
the suite runs in KiCad-less CI.  An optional end-to-end test that actually
shells out to kicad-cli is gated behind a skipif.
"""

from __future__ import annotations

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.drc.geometric import GeometricDRCResult


class _FakeViolation:
    def __init__(self, rule_id="clearance", message="x", location=None):
        self.rule_id = rule_id
        self.message = message
        self.location = location


class _FakeResults:
    """Stand-in for DRCResults exposing the fields run_post_route_drc reads."""

    def __init__(self, errors=None, warnings=None):
        self.errors = errors or []
        self.warnings = warnings or []

    @property
    def error_count(self):
        return len(self.errors)

    @property
    def warning_count(self):
        return len(self.warnings)


def _patch_internal(monkeypatch, results: _FakeResults):
    """Patch PCB.load and DRCChecker so the internal engine returns ``results``."""
    import kicad_tools.validate as validate_mod
    from kicad_tools.schema import pcb as pcb_mod

    monkeypatch.setattr(pcb_mod.PCB, "load", classmethod(lambda cls, p: object()))

    class _FakeChecker:
        def __init__(self, *args, **kwargs):
            pass

        def check_all(self, *args, **kwargs):
            return results

    # run_post_route_drc does ``from kicad_tools.validate import DRCChecker``
    # at call time, so patch the name on the package module.
    monkeypatch.setattr(validate_mod, "DRCChecker", _FakeChecker)


def _patch_geometric(monkeypatch, result: GeometricDRCResult):
    """Patch the shared run_geometric_drc helper at its import site."""
    import kicad_tools.drc as drc_mod

    monkeypatch.setattr(drc_mod, "run_geometric_drc", lambda *a, **k: result)


def test_internal_clean_native_clean_passes(monkeypatch, tmp_path, capsys):
    """Both engines clean -> PASS, error_count 0."""
    _patch_internal(monkeypatch, _FakeResults())
    _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

    errors, warnings = route_cmd.run_post_route_drc(
        output_path=tmp_path / "b.kicad_pcb", manufacturer="jlcpcb", layers=2
    )

    assert errors == 0
    assert warnings == 0
    out = capsys.readouterr().out
    assert "DRC PASSED" in out
    assert "both clean" in out


def test_internal_clean_native_dirty_fails_with_divergence_warning(monkeypatch, tmp_path, capsys):
    """Internal clean but native finds shorts -> FAIL + loud divergence warning."""
    _patch_internal(monkeypatch, _FakeResults())
    _patch_geometric(
        monkeypatch,
        GeometricDRCResult(
            ran=True,
            error_count=52,
            by_type={"shorting_items": 39, "tracks_crossing": 13},
        ),
    )

    errors, warnings = route_cmd.run_post_route_drc(
        output_path=tmp_path / "b.kicad_pcb", manufacturer="jlcpcb", layers=4
    )

    # Native errors folded into the verdict -> gate FAILS.
    assert errors == 52
    out = capsys.readouterr().out
    assert "DRC PASSED" not in out
    assert "WARNING" in out
    assert "kicad-cli found 52 geometric violation" in out
    # Top native violation types are named.
    assert "shorting_items" in out
    assert "tracks_crossing" in out


def test_kicad_cli_absent_falls_back_to_internal_only_pass(monkeypatch, tmp_path, capsys):
    """kicad-cli not found -> internal-only PASS with an explicit note."""
    _patch_internal(monkeypatch, _FakeResults())
    _patch_geometric(
        monkeypatch,
        GeometricDRCResult(ran=False, note="kicad-cli not found; geometric DRC skipped"),
    )

    errors, warnings = route_cmd.run_post_route_drc(
        output_path=tmp_path / "b.kicad_pcb", manufacturer="jlcpcb", layers=2
    )

    assert errors == 0
    out = capsys.readouterr().out
    assert "DRC PASSED" in out
    assert "internal-engine-only PASS" in out
    assert "kicad-cli not found" in out
    assert "not authoritative" in out


def test_kicad_cli_absent_does_not_overstate_when_internal_dirty(monkeypatch, tmp_path, capsys):
    """When kicad-cli is absent and internal is dirty, no PASS is printed."""
    _patch_internal(monkeypatch, _FakeResults(errors=[_FakeViolation()]))
    _patch_geometric(monkeypatch, GeometricDRCResult(ran=False, note="kicad-cli not found"))

    errors, _ = route_cmd.run_post_route_drc(
        output_path=tmp_path / "b.kicad_pcb", manufacturer="jlcpcb", layers=2
    )

    assert errors == 1
    out = capsys.readouterr().out
    assert "DRC PASSED" not in out
    assert "Errors:   1" in out


def test_combined_error_count_sums_both_engines(monkeypatch, tmp_path):
    """Internal errors + native errors are both reflected in the returned count."""
    _patch_internal(monkeypatch, _FakeResults(errors=[_FakeViolation(), _FakeViolation()]))
    _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=3))

    errors, _ = route_cmd.run_post_route_drc(
        output_path=tmp_path / "b.kicad_pcb", manufacturer="jlcpcb", layers=2, quiet=True
    )

    assert errors == 5  # 2 internal + 3 native


# ---------------------------------------------------------------------------
# Optional end-to-end test: actually shell out to kicad-cli.  Skipped when
# kicad-cli is not installed (mirrors tests/test_kicad_cli_roundtrip.py).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_run_geometric_drc_end_to_end_on_committed_board():
    """run_geometric_drc executes against a real committed routed board.

    This does not assert a specific violation count (boards evolve); it
    only confirms the shared helper actually runs kicad-cli and returns a
    populated result (ran=True) without raising in a KiCad environment.
    """
    from pathlib import Path

    from kicad_tools.drc import run_geometric_drc

    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "boards/external/softstart/output/softstart_routed.kicad_pcb",
        repo_root / "boards/external/softstart/output/softstart.kicad_pcb",
    ]
    board = next((c for c in candidates if c.exists()), None)
    if board is None:
        pytest.skip("no committed routed board fixture available")

    result = run_geometric_drc(board)

    # In a KiCad environment the helper must actually run (ran=True) and
    # never raise; error_count is board-dependent and not asserted.
    assert isinstance(result, GeometricDRCResult)
    assert result.ran is True
    assert result.error_count >= 0
