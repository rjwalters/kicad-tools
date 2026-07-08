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
    # Any committed routed board exercises the shared geometric-DRC helper.
    # (softstart used to serve here but is now a local-only external symlink;
    # use a native board that is always committed in this repo.)
    candidates = [
        repo_root / "boards/00-simple-led/output/simple_led_routed.kicad_pcb",
        repo_root / "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb",
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


# ---------------------------------------------------------------------------
# Issue #3919: run_post_route_drc emits .kicad_pro / .kicad_dru constraint
# sidecars next to the routed PCB *before* the kicad-cli geometric cross-check,
# so kicad-cli judges against the manufacturer profile's capability floors
# instead of KiCad's stricter built-in defaults (which produce false
# track_width / clearance / via violations on finer traces).
# ---------------------------------------------------------------------------


class TestSidecarEmittedBeforeGeometricDRC:
    """The constraint sidecars must exist by the time kicad-cli DRC runs."""

    def test_kicad_pro_and_dru_written_next_to_output(self, monkeypatch, tmp_path):
        """Both sidecars land next to the routed PCB after run_post_route_drc."""
        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        out = tmp_path / "board.kicad_pcb"
        route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

        assert (tmp_path / "board.kicad_pro").exists()
        assert (tmp_path / "board.kicad_dru").exists()

    def test_sidecars_present_when_geometric_drc_is_invoked(self, monkeypatch, tmp_path):
        """The .kicad_pro/.kicad_dru exist at the moment run_geometric_drc runs.

        This is the ordering acceptance criterion: kicad-cli must be able to
        auto-load the relaxed rules on the same invocation, so the write has
        to precede run_geometric_drc, not merely happen somewhere in the call.
        """
        _patch_internal(monkeypatch, _FakeResults())

        seen: dict[str, bool] = {}

        import kicad_tools.drc as drc_mod

        def _capture(output_path, *a, **k):
            pro = output_path.parent / "board.kicad_pro"
            dru = output_path.parent / "board.kicad_dru"
            seen["pro"] = pro.exists()
            seen["dru"] = dru.exists()
            return GeometricDRCResult(ran=True, error_count=0)

        monkeypatch.setattr(drc_mod, "run_geometric_drc", _capture)

        out = tmp_path / "board.kicad_pcb"
        route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

        assert seen.get("pro") is True, "kicad_pro must exist before geometric DRC"
        assert seen.get("dru") is True, "kicad_dru must exist before geometric DRC"

    def test_kicad_pro_reflects_profile_min_clearance(self, monkeypatch, tmp_path):
        """Emitted .kicad_pro carries the manufacturer profile's floors."""
        import json

        from kicad_tools.manufacturers import get_profile

        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        profile = get_profile("jlcpcb-tier1")
        rules = profile.get_design_rules(layers=4, copper_oz=1.0)

        out = tmp_path / "board.kicad_pcb"
        route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

        data = json.loads((tmp_path / "board.kicad_pro").read_text())
        design_settings = data["board"]["design_settings"]
        # min_clearance / min track width flow into the applied defaults so
        # kicad-cli's clearance/track_width tests use the profile floors.
        assert design_settings["defaults"]["clearance_min"] == rules.min_clearance_mm
        assert design_settings["defaults"]["track_min_width"] == rules.min_trace_width_mm

    def test_dru_contains_profile_clearance(self, monkeypatch, tmp_path):
        """The .kicad_dru custom-rule text references the profile clearance."""
        from kicad_tools.manufacturers import get_profile

        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        profile = get_profile("jlcpcb-tier1")
        rules = profile.get_design_rules(layers=4, copper_oz=1.0)

        out = tmp_path / "board.kicad_pcb"
        route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

        dru_text = (tmp_path / "board.kicad_dru").read_text()
        # The clearance floor (e.g. 0.1) must appear in the generated rules.
        assert f"{rules.min_clearance_mm}" in dru_text

    def test_unknown_manufacturer_degrades_gracefully(self, monkeypatch, tmp_path, capsys):
        """An unrecognized manufacturer warns and continues (no sidecar, no raise)."""
        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        out = tmp_path / "board.kicad_pcb"
        # Must not raise even though the profile lookup fails.
        errors, _ = route_cmd.run_post_route_drc(
            output_path=out, manufacturer="definitely-not-a-fab", layers=4
        )

        # No sidecar is written for an unknown profile.
        assert not (tmp_path / "board.kicad_pro").exists()
        captured = capsys.readouterr().out
        assert "could not write DRC-constraint sidecars" in captured

    def test_unwritable_output_is_non_fatal(self, monkeypatch, tmp_path, capsys):
        """An OSError while writing sidecars warns and continues (route never fails).

        The failure is injected by monkeypatching ``write_drc_constraints`` to
        raise ``OSError`` rather than relying on filesystem permission bits.
        The latter is not portable to CI, which runs as root and bypasses
        directory mode bits (so ``chmod 0o500`` would not actually block the
        write) — see PR #3950 review.
        """
        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        # _write_drc_constraint_sidecars does
        # ``from kicad_tools.manufacturers import ... write_drc_constraints``
        # at call time, so patch the symbol on that module.
        import kicad_tools.manufacturers as manufacturers_mod

        def _boom(*args, **kwargs):
            raise OSError("Read-only file system")

        monkeypatch.setattr(manufacturers_mod, "write_drc_constraints", _boom)

        out = tmp_path / "board.kicad_pcb"
        # Must not raise despite the write being blocked.
        errors, _ = route_cmd.run_post_route_drc(
            output_path=out, manufacturer="jlcpcb-tier1", layers=4
        )
        assert errors == 0

        # No sidecar is written when the write fails.
        assert not (tmp_path / "board.kicad_pro").exists()
        captured = capsys.readouterr().out
        assert "could not write DRC-constraint sidecars" in captured

    def test_existing_kicad_pro_is_preserved_and_merged(self, monkeypatch, tmp_path):
        """A pre-existing .kicad_pro keeps unrelated keys; only rules overwritten."""
        import json

        _patch_internal(monkeypatch, _FakeResults())
        _patch_geometric(monkeypatch, GeometricDRCResult(ran=True, error_count=0))

        pro = tmp_path / "board.kicad_pro"
        pro.write_text(json.dumps({"sentinel": {"keep": True}, "board": {}}))

        out = tmp_path / "board.kicad_pcb"
        route_cmd.run_post_route_drc(output_path=out, manufacturer="jlcpcb-tier1", layers=4)

        data = json.loads(pro.read_text())
        # Unrelated top-level key survives the merge.
        assert data["sentinel"] == {"keep": True}
        # Constraint block was populated.
        assert "rules" in data["board"]["design_settings"]
