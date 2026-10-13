"""Route-time DRC counts a violation both engines report once (Issue #6425).

``run_post_route_drc`` adds kicad-cli's error count to the internal
``DRCChecker``'s.  An undersized via is reported by BOTH: the internal
``dimension_via_drill`` / ``dimension_via_diameter`` / ``dimension_annular_ring``
rules and kicad-cli's ``drill_out_of_range`` / ``via_diameter`` /
``annular_width``.  Board 04 printed ``Errors: 20`` for 10 such vias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli import route_cmd
from kicad_tools.cli.route_cmd import count_cross_engine_duplicates
from kicad_tools.drc.geometric import GeometricDRCResult
from kicad_tools.drc.violation import DRCViolation as NativeViolation
from kicad_tools.drc.violation import Location, Severity, ViolationType
from kicad_tools.validate.violations import DRCResults
from kicad_tools.validate.violations import DRCViolation as InternalViolation

VIA = (151.75, 93.30)


def _internal(rule_id: str, at=VIA) -> InternalViolation:
    return InternalViolation(rule_id=rule_id, severity="error", message=rule_id, location=at)


def _native(type_str: str, at=VIA) -> NativeViolation:
    return NativeViolation(
        type=ViolationType.UNKNOWN,
        type_str=type_str,
        severity=Severity.ERROR,
        message=type_str,
        locations=[Location(*at)],
        items=["Via [GND] on F.Cu - B.Cu"],
    )


class TestCountCrossEngineDuplicates:
    def test_same_via_drill_counts_once(self):
        assert (
            count_cross_engine_duplicates(
                [_internal("dimension_via_drill")], [_native("drill_out_of_range")]
            )
            == 1
        )

    def test_all_three_dimension_pairs(self):
        internal = [
            _internal("dimension_via_drill"),
            _internal("dimension_via_diameter"),
            _internal("dimension_annular_ring"),
        ]
        native = [_native("annular_width"), _native("via_diameter"), _native("drill_out_of_range")]
        assert count_cross_engine_duplicates(internal, native) == 3

    def test_clearance_seen_by_one_engine_still_counts(self):
        assert (
            count_cross_engine_duplicates(
                [_internal("clearance_trace_trace")], [_native("clearance")]
            )
            == 0
        )
        assert count_cross_engine_duplicates([], [_native("clearance")]) == 0

    def test_different_location_is_not_a_duplicate(self):
        far = (VIA[0] + 0.05, VIA[1])
        assert (
            count_cross_engine_duplicates(
                [_internal("dimension_via_drill", far)], [_native("drill_out_of_range")]
            )
            == 0
        )

    def test_matching_is_one_to_one(self):
        # Two internal reports of one via cannot cancel one native report twice.
        internal = [_internal("dimension_via_drill"), _internal("dimension_via_drill")]
        assert count_cross_engine_duplicates(internal, [_native("drill_out_of_range")]) == 1


@pytest.fixture
def stubbed_drc(monkeypatch, tmp_path):
    """``run_post_route_drc`` with both engines and the sidecar writers stubbed."""

    for name in (
        "_write_net_class_map_sidecar",
        "_write_current_paths_sidecar",
        "_write_routing_plan_sidecar",
        "_write_access_witness_sidecar",
        "_write_fab_profile_sidecar",
        "_write_drc_constraint_sidecars",
    ):
        monkeypatch.setattr(route_cmd, name, lambda *a, **k: None)
    monkeypatch.setattr("kicad_tools.schema.pcb.PCB.load", staticmethod(lambda *_a, **_k: object()))
    state: dict = {}

    class FakeChecker:
        def __init__(self, *_a, **_k):
            pass

        def check_all(self):
            return DRCResults(violations=list(state["internal"]))

    monkeypatch.setattr("kicad_tools.validate.DRCChecker", FakeChecker)

    def fake_geo(*_a, **_k):
        native = list(state["native"])
        return GeometricDRCResult(ran=True, error_count=len(native), error_violations=native)

    monkeypatch.setattr("kicad_tools.drc.run_geometric_drc", fake_geo)

    def run(internal, native):
        state["internal"], state["native"] = internal, native
        errors, _warnings = route_cmd.run_post_route_drc(
            Path(tmp_path / "b.kicad_pcb"), "jlcpcb", 2, quiet=False
        )
        return errors

    return run


def test_route_time_errors_equal_kicad_count_when_internal_adds_nothing(stubbed_drc, capsys):
    # Board 04 shape: ten undersized signal vias, each flagged by both engines.
    vias = [(150.0 + i, 90.0) for i in range(10)]
    internal = [_internal("dimension_via_drill", v) for v in vias]
    native = [_native("drill_out_of_range", v) for v in vias]
    assert stubbed_drc(internal, native) == 10
    out = capsys.readouterr().out
    assert "Errors:   10" in out and "10 reported by both" in out


def test_unmatched_violations_of_either_engine_still_count(stubbed_drc):
    internal = [_internal("dimension_via_drill"), _internal("clearance_trace_trace", (1.0, 1.0))]
    native = [_native("drill_out_of_range"), _native("clearance", (2.0, 2.0))]
    assert stubbed_drc(internal, native) == 3
