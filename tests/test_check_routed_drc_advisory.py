"""Tests for advisory-rule exclusion in the routed-PCB DRC CI gate (issue #3074).

PR #3060 added the ``connectivity`` rule to ``DRCChecker``; the rule
reports partial-route GND/VCC nets that the audit pipeline classifies as
*advisory* (``DRCChecker.ADVISORY_RULE_IDS = frozenset({"connectivity"})``)
because they reflect routing completeness rather than manufacturability.
PR #3064 introduced the ``DRCChecker.is_advisory_rule()`` classifier so
every entry point can filter consistently.

Issue #3074 found that ``scripts/ci/check_routed_drc.py`` was still
trusting the unfiltered ``summary.errors`` from ``kct check --format
json``, so board 04's main-routed PCB produced 5 errors (4 blocking + 1
``connectivity``) and tripped its allowlist of 4.  Bumping the allowlist
to 5 would have to be repeated every time a new advisory rule shipped;
the right fix is for the gate to honour the same advisory classification
the audit pipeline already uses (``ManufacturingAudit._check_drc`` at
``src/kicad_tools/audit/auditor.py:768``).

These tests stub the subprocess boundary (``kct check`` is expensive and
needs KiCad data) so they exercise the gate's filtering logic in
isolation.  The synthetic JSON payloads mirror the shape produced by
``DRCViolation.to_dict()`` (per ``src/kicad_tools/validate/models.py``).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_routed_drc.py"


def _load_helper_module():
    """Import ``scripts/ci/check_routed_drc.py`` as a module."""
    spec = importlib.util.spec_from_file_location("check_routed_drc", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_routed_drc"] = module
    spec.loader.exec_module(module)
    return module


def _make_violation(rule_id: str, severity: str = "error", message: str = "synthetic") -> dict:
    """Synthesize a single violation dict matching ``DRCViolation.to_dict()``.

    Only the fields the gate reads (``rule_id`` and ``severity``) are
    load-bearing; the rest are filled in so the payload looks like real
    ``kct check`` JSON output for any future caller that inspects more
    fields.
    """
    return {
        "rule_id": rule_id,
        "type": rule_id,
        "severity": severity,
        "message": message,
        "location": [10.0, 20.0],
        "layer": "F.Cu",
        "actual_value": 0.1,
        "required_value": 0.127,
        "items": [],
    }


def _make_kct_json(violations: list[dict]) -> str:
    """Build a ``kct check --format json`` payload around a violation list.

    Mirrors ``src/kicad_tools/cli/drc_cmd.py::output_json``.
    """
    payload = {
        "source": "synthetic.kicad_pcb",
        "pcb_name": "synthetic",
        "summary": {
            "errors": sum(1 for v in violations if v.get("severity") == "error"),
            "warnings": sum(1 for v in violations if v.get("severity") != "error"),
        },
        "violations": violations,
    }
    return json.dumps(payload)


class TestAdvisoryRuleIdsImport:
    """Pin that the helper imports the classifier from the canonical source.

    A future refactor that hardcodes a local set instead of using
    ``DRCChecker.is_advisory_rule`` would silently drift -- the audit
    pipeline and the gate must stay in lockstep.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_helper_imports_drcchecker(self) -> None:
        """The helper module must expose the imported ``DRCChecker`` so
        the gate's filtering uses the same classifier as the audit
        pipeline (``src/kicad_tools/audit/auditor.py:768``)."""
        assert hasattr(self.helper, "DRCChecker"), (
            "scripts/ci/check_routed_drc.py must import DRCChecker so the "
            "advisory-rule classifier stays in lockstep with the audit "
            "pipeline (issue #3074)."
        )

    def test_connectivity_is_currently_advisory(self) -> None:
        """Document the current ADVISORY_RULE_IDS membership so a future
        refactor that drops ``connectivity`` (or adds a new rule whose
        severity should NOT block the gate) surfaces a test failure that
        triggers an explicit re-think rather than a silent CI drift."""
        assert self.helper.DRCChecker.is_advisory_rule("connectivity")
        # Non-advisory blocking rules must NOT be misclassified.
        assert not self.helper.DRCChecker.is_advisory_rule("clearance_segment_via")
        assert not self.helper.DRCChecker.is_advisory_rule("clearance_pad_via")


class TestCountBlockingErrorsDirect:
    """Unit tests for the pure ``_count_blocking_errors`` filter (no
    subprocess required).  These exercise the load-bearing JSON-shape
    handling in isolation."""

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def test_empty_violations_is_zero(self) -> None:
        """A clean PCB returns 0 blocking with no advisory entries."""
        data = json.loads(_make_kct_json([]))
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 0
        assert advisory == {}

    def test_pure_blocking_passes_through(self) -> None:
        """No advisory rules -> the count matches summary.errors verbatim
        (this is the historical behaviour the original gate produced)."""
        violations = [
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("clearance_pad_segment"),
        ]
        data = json.loads(_make_kct_json(violations))
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 3
        assert advisory == {}

    def test_pure_advisory_does_not_block(self) -> None:
        """A board whose only errors are advisory must report 0 blocking
        so the gate's verdict passes regardless of allowlist value.

        This is the headline AC for issue #3074: the connectivity rule
        is a routing-completeness signal, not a manufacturability
        blocker."""
        violations = [
            _make_violation("connectivity"),
            _make_violation("connectivity"),
        ]
        data = json.loads(_make_kct_json(violations))
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 0
        assert advisory == {"connectivity": 2}

    def test_mixed_keeps_blocking_drops_advisory(self) -> None:
        """The board 04 scenario from issue #3074: 4 blocking +
        1 connectivity -> gate counts 4."""
        violations = [
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("connectivity"),
        ]
        data = json.loads(_make_kct_json(violations))
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 4
        assert advisory == {"connectivity": 1}

    def test_warning_severity_excluded_from_blocking(self) -> None:
        """``--errors-only`` filters warnings upstream, but the helper
        defends against a future flag change by re-checking severity."""
        violations = [
            _make_violation("clearance_segment_via", severity="error"),
            _make_violation("clearance_segment_via", severity="warning"),
        ]
        data = json.loads(_make_kct_json(violations))
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 1
        assert advisory == {}

    def test_legacy_payload_no_violations_array_falls_back(self) -> None:
        """If a future ``kct check`` mode omits per-violation entries,
        the helper degrades to trusting ``summary.errors`` so the gate
        keeps working with no advisory awareness (the original
        pre-#3074 behaviour)."""
        data = {"summary": {"errors": 7, "warnings": 0}}  # no "violations" key
        blocking, advisory = self.helper._count_blocking_errors(data)
        assert blocking == 7
        assert advisory == {}

    def test_malformed_payload_raises_runtimeerror(self) -> None:
        """Neither a ``violations`` list nor a ``summary.errors`` int
        means the JSON is corrupt -- the helper must surface that
        rather than silently report 0."""
        data: dict = {}  # empty payload
        with pytest.raises(RuntimeError, match="missing both violations and summary.errors"):
            self.helper._count_blocking_errors(data)


class TestCheckFileAdvisoryFiltering:
    """Integration of ``count_errors`` + ``check_file`` -- the gate's
    public surface -- with the advisory filter enabled.

    These stub the subprocess so we exercise the full message-formatting
    flow without needing a real ``kct check`` run.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def _stub_kct(self, json_payload: str, returncode: int = 2):
        """Build a ``subprocess.run`` patch that returns the given JSON.

        ``returncode=2`` matches ``kct check`` exiting with errors found
        (the natural state for any payload with blocking violations).
        """
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = json_payload
        mock_proc.stderr = ""
        return patch.object(subprocess, "run", return_value=mock_proc)

    def test_board04_scenario_passes_with_advisory_exclusion(self) -> None:
        """4 blocking + 1 connectivity -> reports 4 errors, exits 0
        against allowlist 4.

        This is the exact scenario from issue #3074 (board 04 on main).
        Without advisory filtering the count would be 5 and the gate
        would fail; with filtering it passes and the connectivity
        finding shows up in the message suffix."""
        violations = [
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("connectivity"),
        ]
        with self._stub_kct(_make_kct_json(violations)):
            passed, msg, errors = self.helper.check_file(
                Path("boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"),
                allowed=4,
                mfr="jlcpcb-tier1",
            )
        assert passed
        assert errors == 4
        # The OK message must mention the advisory finding so reviewers
        # can still see the connectivity violation for debugging.
        assert "connectivity=1" in msg
        assert "advisory" in msg.lower()

    def test_blocking_only_regression_still_fails(self) -> None:
        """5 blocking + 1 connectivity vs allowlist 4 -> still fails on
        the 5 blocking.  Advisory exclusion must NOT mask a real
        regression."""
        violations = [
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_segment_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("clearance_pad_via"),
            _make_violation("clearance_segment_segment"),
            _make_violation("connectivity"),
        ]
        with self._stub_kct(_make_kct_json(violations)):
            passed, msg, errors = self.helper.check_file(
                Path("boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"),
                allowed=4,
                mfr="jlcpcb-tier1",
            )
        assert not passed
        assert errors == 5
        assert "regression" in msg.lower()
        # Advisory is still surfaced even on FAIL so the reviewer sees
        # the complete picture in the diagnostic.
        assert "connectivity=1" in msg

    def test_pure_advisory_on_strict_board_passes(self) -> None:
        """A board not in the allowlist (allowed=0) with only advisory
        violations passes the strict gate.  Issue #3074 scenario for
        clean boards like 01: 0 blocking + 1 connectivity = pass."""
        violations = [_make_violation("connectivity")]
        # kct check exits 2 when ANY error is reported (advisory or not);
        # the helper must still produce a clean-pass verdict.
        with self._stub_kct(_make_kct_json(violations)):
            passed, msg, errors = self.helper.check_file(
                Path("boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb"),
                allowed=0,
                mfr="jlcpcb",
            )
        assert passed
        assert errors == 0
        assert "0 errors" in msg
        assert "connectivity=1" in msg

    def test_no_advisory_no_suffix(self) -> None:
        """Common case: no advisory violations -> message has no
        advisory suffix, preserving the historical log shape so the
        existing CI-output parsers / human reviewers don't see noise."""
        violations = [_make_violation("clearance_segment_via")]
        with self._stub_kct(_make_kct_json(violations)):
            passed, msg, errors = self.helper.check_file(
                Path("boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"),
                allowed=4,
                mfr="jlcpcb",
            )
        assert passed
        assert errors == 1
        assert "advisory" not in msg.lower()


class TestStdoutPrefixTolerance:
    """Defensive: ``count_errors`` must tolerate stdout that has any
    non-JSON prefix before the ``kct check --format json`` payload.

    The advisory drift banner from ``_emit_drift_banner`` is now routed
    to stderr (so the gate no longer hits this path in practice), but a
    stale ``kct`` binary in CI, or a future regression that re-introduces
    a stdout warning, would otherwise crash the gate with
    ``json.JSONDecodeError`` -- a brittle failure mode the original PR
    #3217 cycle hit on board 05.  These tests pin the strip-to-first-brace
    behaviour so we never re-regress.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def _stub_kct(self, stdout: str, returncode: int = 2):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = stdout
        mock_proc.stderr = ""
        return patch.object(subprocess, "run", return_value=mock_proc)

    def test_leading_warning_line_is_stripped(self) -> None:
        """The exact stdout shape observed on board 05 PR #3217 CI run
        27026666522: one warning line, then the JSON body.  The gate must
        parse the JSON and return the correct error count."""
        payload = _make_kct_json([_make_violation("clearance_pad_segment")])
        polluted = (
            "  WARNING: PCB out of sync with schematic -- 4 PCB-only. "
            "Run 'kct sync --analyze foo.kicad_pcb' to inspect.\n" + payload
        )
        with self._stub_kct(polluted):
            blocking, advisory = self.helper.count_errors(Path("synthetic.kicad_pcb"))
        assert blocking == 1
        assert advisory == {}

    def test_clean_stdout_still_parses(self) -> None:
        """A pristine JSON-only stdout (the post-fix steady state) must
        still parse identically -- the strip is a no-op when no prefix
        is present."""
        payload = _make_kct_json([_make_violation("clearance_segment_via")])
        with self._stub_kct(payload):
            blocking, advisory = self.helper.count_errors(Path("synthetic.kicad_pcb"))
        assert blocking == 1
        assert advisory == {}

    def test_unparseable_payload_still_raises_with_raw_preview(self) -> None:
        """If the stripped output still is not valid JSON, the gate must
        surface a clear RuntimeError with the *raw* stdout preview so the
        reviewer sees the actual offending bytes (not a post-strip slice
        that hides the prefix)."""
        polluted = "totally not json at all"
        with self._stub_kct(polluted):
            with pytest.raises(RuntimeError, match="invalid JSON"):
                self.helper.count_errors(Path("synthetic.kicad_pcb"))
