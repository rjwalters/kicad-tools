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
CI_DIR = REPO_ROOT / "scripts" / "ci"
HELPER_SCRIPT_PATH = CI_DIR / "check_routed_drc.py"
COVERAGE_SCRIPT_PATH = CI_DIR / "check_matchgroup_coverage.py"


def _load_script_module(name: str, path: Path):
    """Import a ``scripts/ci`` helper script as a module.

    ``scripts/ci`` is added to ``sys.path`` first so the scripts'
    ``from net_class_map_resolver import ...`` statements resolve when the
    module is loaded outside its own ``sys.path.insert`` (the insert runs at
    import time, but pytest may import a sibling first)."""
    if str(CI_DIR) not in sys.path:
        sys.path.insert(0, str(CI_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_helper_module():
    """Import ``scripts/ci/check_routed_drc.py`` as a module."""
    return _load_script_module("check_routed_drc", HELPER_SCRIPT_PATH)


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
    """Pin that the shared counter uses the classifier from the canonical
    source.

    A future refactor that hardcodes a local set instead of using
    ``DRCChecker.is_advisory_rule`` would silently drift -- the audit
    pipeline and the gates must stay in lockstep.  Issue #4008 hoisted the
    counter into ``net_class_map_resolver.count_blocking_errors``, so the
    ``DRCChecker`` import now lives there (imported lazily) rather than at
    ``check_routed_drc.py`` module scope.
    """

    def test_connectivity_is_currently_advisory(self) -> None:
        """Document the current ADVISORY_RULE_IDS membership so a future
        refactor that drops ``connectivity`` (or adds a new rule whose
        severity should NOT block the gate) surfaces a test failure that
        triggers an explicit re-think rather than a silent CI drift."""
        from kicad_tools.validate.checker import DRCChecker

        assert DRCChecker.is_advisory_rule("connectivity")
        # Non-advisory blocking rules must NOT be misclassified.
        assert not DRCChecker.is_advisory_rule("clearance_segment_via")
        assert not DRCChecker.is_advisory_rule("clearance_pad_via")


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


class TestCrossScriptCounterParity:
    """Issue #4008: the two routed-PCB CI gates must count the SAME blocking
    errors for the same ``kct check`` payload.

    Before this issue, ``check_routed_drc.py`` filtered advisory rules while
    ``check_matchgroup_coverage.py`` read the raw ``summary.errors`` integer,
    so on board 07 they reported 9 vs 14 against the SAME
    ``.github/routed-drc-tolerance.yml`` floor -- forcing +5 dead slack onto
    the blocking gate.  These tests pin that both gates now delegate to the
    single shared ``net_class_map_resolver.count_blocking_errors``.
    """

    def setup_method(self) -> None:
        # Drop any stale cached copies so the two scripts import a fresh,
        # shared ``net_class_map_resolver`` in a deterministic order.
        for name in ("check_routed_drc", "check_matchgroup_coverage", "net_class_map_resolver"):
            sys.modules.pop(name, None)
        self.drc = _load_helper_module()
        self.coverage = _load_script_module("check_matchgroup_coverage", COVERAGE_SCRIPT_PATH)
        # Reuse the resolver the scripts already imported (same object) so the
        # identity assertion below is meaningful; both scripts do
        # ``from net_class_map_resolver import count_blocking_errors``.
        self.resolver = sys.modules["net_class_map_resolver"]

    def _stub_kct(self, json_payload: str, returncode: int = 2):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.stdout = json_payload
        mock_proc.stderr = ""
        return patch.object(subprocess, "run", return_value=mock_proc)

    def test_board07_shaped_payload_agrees_across_scripts(self) -> None:
        """A payload matching board 07 (9 blocking + 5 advisory
        connectivity = 14 raw) must yield 9 from BOTH gates, not 9 vs 14."""
        violations = (
            [_make_violation("diffpair_length_skew") for _ in range(4)]
            + [_make_violation("diffpair_routing_continuity") for _ in range(4)]
            + [_make_violation("match_group_length_skew")]
            + [_make_violation("connectivity") for _ in range(5)]
        )
        payload = _make_kct_json(violations)
        # Raw summary.errors is 14 (the OLD count) -- assert we do NOT use it.
        assert json.loads(payload)["summary"]["errors"] == 14

        # check_routed_drc.py path
        with self._stub_kct(payload):
            drc_blocking, drc_advisory = self.drc.count_errors(
                Path("boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb")
            )
        # check_matchgroup_coverage.py path
        with self._stub_kct(payload):
            coverage_blocking = self.coverage.count_errors_via_kct_check(
                Path("boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"),
                sidecar=None,
            )
        # Shared helper path (the source of truth both delegate to)
        shared_blocking, shared_advisory = self.resolver.count_blocking_errors(json.loads(payload))

        assert drc_blocking == coverage_blocking == shared_blocking == 9, (
            f"Gates disagree: check_routed_drc={drc_blocking}, "
            f"check_matchgroup_coverage={coverage_blocking}, "
            f"shared={shared_blocking} (expected 9 blocking, advisory excluded)."
        )
        assert drc_advisory == shared_advisory == {"connectivity": 5}

    def test_both_scripts_share_one_counter_function(self) -> None:
        """Both scripts must reference the SAME ``count_blocking_errors``
        object from ``net_class_map_resolver`` -- not private copies that
        could drift.  This is the structural guard against the seam ever
        re-opening."""
        assert self.drc.count_blocking_errors is self.resolver.count_blocking_errors
        assert self.coverage.count_blocking_errors is self.resolver.count_blocking_errors

    def test_pure_advisory_payload_agrees_at_zero(self) -> None:
        """A payload with only advisory connectivity must yield 0 blocking
        from both gates (not the raw connectivity count)."""
        violations = [_make_violation("connectivity") for _ in range(3)]
        payload = _make_kct_json(violations)
        assert json.loads(payload)["summary"]["errors"] == 3

        with self._stub_kct(payload):
            drc_blocking, _ = self.drc.count_errors(Path("synthetic.kicad_pcb"))
        with self._stub_kct(payload):
            coverage_blocking = self.coverage.count_errors_via_kct_check(
                Path("synthetic.kicad_pcb"), sidecar=None
            )
        assert drc_blocking == coverage_blocking == 0


class TestMfrCliOverride:
    """``--mfr TIER`` is a per-invocation CLI override for the manufacturer
    profile, mirroring ``--allow``.

    Consumer repos that vendor this gate (Epic #4054, issue #4058 pilot into
    ../chorus) have no ``.github/routed-drc-tolerance.yml``, so the only way
    to gate a board routed to a non-default tier (e.g. ``jlcpcb-tier1`` in-pad
    via rescue) used to be authoring a repo-internal allowlist YAML just to
    name a profile.  ``--mfr`` is the escape hatch: it overrides both the
    ``jlcpcb`` default AND the YAML ``manufacturers:`` map.
    """

    def setup_method(self) -> None:
        self.helper = _load_helper_module()

    def _run_main_capturing_mfr(self, tmp_path: Path, argv: list[str]) -> tuple[int, list[str]]:
        """Run ``main(argv)`` with a stubbed ``kct check`` subprocess.

        Returns ``(exit_code, captured_mfr_values)`` where each captured value
        is the ``--mfr`` argument the gate passed to ``kct check`` (one per
        file). The stub returns a clean (zero-error) payload so the gate's
        verdict depends only on argument wiring, not on DRC content.
        """
        captured: list[str] = []

        def fake_run(cmd, *args, **kwargs):
            # cmd is the full ["uv","run","kct","check",<pcb>,"--mfr",<tier>,...]
            if "--mfr" in cmd:
                captured.append(cmd[cmd.index("--mfr") + 1])
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = _make_kct_json([])  # no violations -> clean
            proc.stderr = ""
            return proc

        # main() resolves lookup_key relative to cwd; run from tmp_path so the
        # board path is repo-relative and the (absent) default allowlist is a
        # clean no-op.
        board = tmp_path / "board_routed.kicad_pcb"
        board.write_text("(kicad_pcb)")
        old_cwd = Path.cwd()
        import os

        os.chdir(tmp_path)
        try:
            with patch.object(subprocess, "run", side_effect=fake_run):
                code = self.helper.main([*argv, "board_routed.kicad_pcb"])
        finally:
            os.chdir(old_cwd)
        return code, captured

    def test_default_mfr_is_jlcpcb(self, tmp_path: Path) -> None:
        """No ``--mfr`` and no allowlist YAML -> the gate checks at the
        ``jlcpcb`` default."""
        code, captured = self._run_main_capturing_mfr(tmp_path, ["--allow", "0"])
        assert code == 0
        assert captured == ["jlcpcb"]

    def test_mfr_flag_overrides_default(self, tmp_path: Path) -> None:
        """``--mfr jlcpcb-tier1`` threads through to ``kct check --mfr``.

        This is the exact chorus-pilot scenario: a tier1 board in a consumer
        repo with no allowlist YAML."""
        code, captured = self._run_main_capturing_mfr(
            tmp_path, ["--mfr", "jlcpcb-tier1", "--allow", "0"]
        )
        assert code == 0
        assert captured == ["jlcpcb-tier1"]

    def test_mfr_help_documents_flag(self) -> None:
        """The ``--help`` output advertises ``--mfr`` so a consumer can
        discover the escape hatch."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            self.helper.main(["--help"])
        out = buf.getvalue()
        assert "--mfr" in out
        assert "jlcpcb-tier1" in out
