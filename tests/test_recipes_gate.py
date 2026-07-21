"""Tests for the shared recipe pipeline success gate (issue #3912).

Covers:
* route completion as a first-class leg (allowance-aware);
* the authoritative geometric DRC leg (``kicad-cli pcb drc --refill-zones``)
  including advisory exclusion and per-rule allowances;
* the supplemental DRC verdict for rule families kicad-cli cannot express
  (board-06 differential-pair skew);
* the LVS leg's ``None`` (not-run) vs ``False`` (failed) semantics;
* the divergence-guard invariant: the SUMMARY status and the exit code are
  BOTH derived from one ``PipelineGateResult`` and cannot disagree;
* a guard that the DRC leg really invokes ``--refill-zones`` (regression
  guard against a slide back to ``kct check --drc-only``).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.recipes.gate import (
    DEFAULT_ADVISORY_DRC_TYPES,
    PipelineGateResult,
    evaluate_pipeline_gate,
)


def _clean_drc() -> GeometricDRCResult:
    return GeometricDRCResult(ran=True, error_count=0, by_type={})


def _drc(by_type: dict[str, int]) -> GeometricDRCResult:
    return GeometricDRCResult(ran=True, error_count=sum(by_type.values()), by_type=dict(by_type))


PCB = Path("dummy_routed.kicad_pcb")


# --------------------------------------------------------------------------
# Route leg
# --------------------------------------------------------------------------
class TestRouteLeg:
    def test_fully_routed_clean_passes(self):
        res = evaluate_pipeline_gate(PCB, nets_routed=35, nets_total=35, _drc_result=_clean_drc())
        assert res.route_ok is True
        assert res.drc_ok is True
        assert res.passed is True
        assert res.exit_code() == 0

    def test_partial_beyond_allowance_fails_route(self):
        res = evaluate_pipeline_gate(
            PCB, nets_routed=28, nets_total=35, route_allowance=0, _drc_result=_clean_drc()
        )
        assert res.route_ok is False
        assert res.passed is False
        assert res.exit_code() == 1
        assert any("route incomplete" in r for r in res.reasons)

    def test_declared_partial_within_allowance_passes(self):
        # board-07 #3438 style: 5 seed-invariant nets tolerated explicitly.
        res = evaluate_pipeline_gate(
            PCB, nets_routed=46, nets_total=51, route_allowance=5, _drc_result=_clean_drc()
        )
        assert res.route_ok is True
        assert res.passed is True

    def test_route_ok_bool_path(self):
        # Recipes whose route_pcb returns only a bool.
        assert evaluate_pipeline_gate(PCB, route_ok=True, _drc_result=_clean_drc()).route_ok
        assert not evaluate_pipeline_gate(PCB, route_ok=False, _drc_result=_clean_drc()).route_ok

    def test_no_route_info_does_not_block_but_is_noted(self):
        res = evaluate_pipeline_gate(PCB, _drc_result=_clean_drc())
        assert res.route_ok is True
        assert any("route completion not evaluated" in r for r in res.reasons)


# --------------------------------------------------------------------------
# DRC leg (authoritative geometric engine)
# --------------------------------------------------------------------------
class TestDrcLeg:
    def test_copper_short_fails(self):
        # board-05 defect: shorting_items only visible via --refill-zones.
        res = evaluate_pipeline_gate(PCB, route_ok=True, _drc_result=_drc({"shorting_items": 2}))
        assert res.drc_ok is False
        assert res.passed is False
        assert res.drc_blocking == {"shorting_items": 2}
        assert any("shorting_items" in r for r in res.reasons)

    def test_unconnected_items_is_advisory(self):
        # Route completeness is a separate leg; unrouted nets must not
        # double-count as a blocking DRC error.
        assert "unconnected_items" in DEFAULT_ADVISORY_DRC_TYPES
        res = evaluate_pipeline_gate(
            PCB, route_ok=True, _drc_result=_drc({"unconnected_items": 12})
        )
        assert res.drc_ok is True
        assert res.passed is True

    def test_rule_allowance_tolerates_grandfathered_drills(self):
        # board-04 #4017: up to 2 legacy hole_clearance drills tolerated.
        res = evaluate_pipeline_gate(
            PCB,
            route_ok=True,
            rule_allowances={"hole_clearance": 2},
            _drc_result=_drc({"hole_clearance": 2}),
        )
        assert res.drc_ok is True
        assert res.passed is True

    def test_rule_allowance_exceeded_fails(self):
        res = evaluate_pipeline_gate(
            PCB,
            route_ok=True,
            rule_allowances={"hole_clearance": 2},
            _drc_result=_drc({"hole_clearance": 3}),
        )
        assert res.drc_ok is False
        assert res.drc_blocking == {"hole_clearance": 3}

    def test_non_allowlisted_clearance_fails(self):
        res = evaluate_pipeline_gate(PCB, route_ok=True, _drc_result=_drc({"clearance": 1}))
        assert res.drc_ok is False

    def test_drc_not_run_fails_closed_by_default(self):
        res = evaluate_pipeline_gate(
            PCB,
            route_ok=True,
            _drc_result=GeometricDRCResult(ran=False, note="kicad-cli not found"),
        )
        assert res.drc_ran is False
        assert res.drc_ok is False
        assert any("did not run" in r for r in res.reasons)

    def test_drc_not_run_can_be_waived(self):
        res = evaluate_pipeline_gate(
            PCB,
            route_ok=True,
            require_drc=False,
            _drc_result=GeometricDRCResult(ran=False, note="kicad-cli not found"),
        )
        assert res.drc_ok is True
        assert any("unverified" in r for r in res.reasons)


# --------------------------------------------------------------------------
# Supplemental DRC verdict (kct-check-only rule families)
# --------------------------------------------------------------------------
class TestSupplementalDrc:
    def test_supplemental_false_fails_even_when_geometric_clean(self):
        # board-06: 18 diffpair-skew errors invisible to kicad-cli but
        # caught by kct check --net-class-map.
        res = evaluate_pipeline_gate(
            PCB,
            route_ok=True,
            supplemental_drc_ok=False,
            supplemental_reason="18 diffpair errors",
            _drc_result=_clean_drc(),
        )
        assert res.drc_ok is False
        assert res.passed is False
        assert any("18 diffpair errors" in r for r in res.reasons)

    def test_supplemental_none_does_not_fail(self):
        res = evaluate_pipeline_gate(
            PCB, route_ok=True, supplemental_drc_ok=None, _drc_result=_clean_drc()
        )
        assert res.drc_ok is True

    def test_supplemental_true_passes(self):
        res = evaluate_pipeline_gate(
            PCB, route_ok=True, supplemental_drc_ok=True, _drc_result=_clean_drc()
        )
        assert res.drc_ok is True


# --------------------------------------------------------------------------
# LVS leg
# --------------------------------------------------------------------------
class TestLvsLeg:
    def test_lvs_none_is_not_run_and_passes(self):
        res = evaluate_pipeline_gate(PCB, route_ok=True, lvs_ok=None, _drc_result=_clean_drc())
        assert res.lvs_status() == "n/a"
        assert res.passed is True

    def test_lvs_false_fails(self):
        res = evaluate_pipeline_gate(PCB, route_ok=True, lvs_ok=False, _drc_result=_clean_drc())
        assert res.passed is False
        assert res.lvs_status() == "FAIL"
        assert any("copper-LVS failed" in r for r in res.reasons)

    def test_lvs_true_passes(self):
        res = evaluate_pipeline_gate(PCB, route_ok=True, lvs_ok=True, _drc_result=_clean_drc())
        assert res.lvs_status() == "PASS"
        assert res.passed is True


# --------------------------------------------------------------------------
# Divergence guard: SUMMARY and exit code cannot disagree
# --------------------------------------------------------------------------
class TestDivergenceGuard:
    @pytest.mark.parametrize(
        "route_ok,drc,lvs_ok",
        list(
            itertools.product(
                [True, False],
                [_clean_drc(), _drc({"shorting_items": 1})],
                [None, True, False],
            )
        ),
    )
    def test_summary_and_exit_code_agree(self, route_ok, drc, lvs_ok):
        res = evaluate_pipeline_gate(PCB, route_ok=route_ok, lvs_ok=lvs_ok, _drc_result=drc)
        summary = "\n".join(res.summary_lines())
        # The SUMMARY overall line and the exit code both read res.passed,
        # so a board can never print "Overall: PASS" while exiting 1.
        printed_pass = "Overall: PASS" in summary
        assert printed_pass == (res.exit_code() == 0)
        assert printed_pass == res.passed
        assert res.overall_status() == ("PASS" if res.passed else "FAIL")

    def test_summary_lines_derive_from_same_object(self):
        res = PipelineGateResult(route_ok=True, drc_ok=False, lvs_ok=None)
        summary = "\n".join(res.summary_lines())
        assert "DRC:     FAIL" in summary
        assert "Overall: FAIL" in summary
        assert res.exit_code() == 1


# --------------------------------------------------------------------------
# Authoritative-engine guard: the DRC leg must use --refill-zones
# --------------------------------------------------------------------------
class TestAuthoritativeEngine:
    def test_drc_leg_invokes_refill_zones(self, monkeypatch, tmp_path):
        """The gate's DRC leg must shell ``kicad-cli pcb drc --refill-zones``.

        Regression guard against a slide back to ``kct check --drc-only``
        (which trusts stale zone fills and misses copper shorts -- the
        board-05 defect).  We drive a real ``run_geometric_drc`` call
        through the gate with ``subprocess.run`` stubbed and assert the
        argv carried ``--refill-zones``.
        """
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text(
                '{"source": "", "date": "", "coordinate_units": "mm", "violations": []}'
            )

            class _Proc:
                returncode = 0
                stdout = b""
                stderr = b""

            return _Proc()

        monkeypatch.setattr(
            "kicad_tools.cli.runner.find_kicad_cli", lambda: Path("/usr/bin/kicad-cli")
        )
        monkeypatch.setattr("kicad_tools.drc.geometric.subprocess.run", fake_run)

        pcb = tmp_path / "board_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        res = evaluate_pipeline_gate(pcb, route_ok=True)

        assert "cmd" in captured, "run_geometric_drc did not shell kicad-cli"
        assert "--refill-zones" in captured["cmd"]
        assert "drc" in captured["cmd"]
        assert res.drc_ran is True
        assert res.drc_ok is True
