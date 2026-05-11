"""Regression tests for clearance_segment_via false-positives at in-pad via centers.

When the router places an in-pad escape via (see
``_try_in_pad_escape`` in ``src/kicad_tools/router/escape.py``), the
inner-layer escape segment's endpoint is placed at *exactly* the via
center.  If a neighboring net's escape segment terminates at the same
coordinates, the segment/via clearance check sees endpoint == via center
on a different net and reports a spurious "negative clearance" violation
at the via center.

The Via PCB schema has no ``in_pad`` flag (it is dropped at
serialization), so the detection is geometric: when any segment endpoint
falls within ``_COLOCATION_EPSILON_MM`` (1e-4 mm) of a via center, the
``(segment, via)`` pair is skipped.

These tests verify:

1.  **Coincident endpoint, cross-net**: 0 violations expected (the
    primary bug case from Issue #2706).
2.  **Endpoint shifted 0.1 mm, cross-net**: 1 violation expected
    (over-suppression guard -- the rule must still fire on real
    near-misses).
3.  **Coincident endpoint, same-net**: 0 violations expected (the
    same-net skip at ``clearance.py`` line 461 already handled this,
    but the new co-location skip is a no-op for same-net pairs).
4.  **Near-miss away from via center**: 1 violation expected (real
    clearance violations on cross-net traces still fire).

See Issue #2706 and PR #2704 (in-pad via escape for fine-pitch LQFP/QFP).
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Synthetic PCB fixtures
# ---------------------------------------------------------------------------


# Cross-net case 1: segment endpoint coincides exactly with via center.
# Without the co-location skip, this produces a false-positive
# clearance_segment_via violation.
PCB_CROSS_NET_COINCIDENT = """\
(kicad_pcb
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
  (net 1 "SIG1")
  (net 2 "SIG2")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))
  (segment (start 100 100) (end 101 100) (width 0.2) (layer "B.Cu") (net 2 "SIG2") (uuid "seg-1"))
)
"""

# Cross-net case 2: segment endpoint shifted 0.1mm from via center.
# This is far closer than the 0.127mm minimum clearance for jlcpcb +
# the via radius 0.3mm; the rule must still fire.
PCB_CROSS_NET_SHIFTED = """\
(kicad_pcb
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
  (net 1 "SIG1")
  (net 2 "SIG2")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))
  (segment (start 100.1 100) (end 101.1 100) (width 0.2) (layer "B.Cu") (net 2 "SIG2") (uuid "seg-1"))
)
"""

# Same-net case: segment endpoint coincides with via center on the SAME
# net.  The same-net skip at clearance.py:461 already handles this; the
# new co-location skip is a no-op here.  Included to confirm no
# regression.
PCB_SAME_NET_COINCIDENT = """\
(kicad_pcb
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
  (net 1 "SIG1")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))
  (segment (start 100 100) (end 101 100) (width 0.2) (layer "B.Cu") (net 1 "SIG1") (uuid "seg-1"))
)
"""

# Real near-miss away from via center: segment endpoint is 0.3 mm from
# the via center on a different net.  With trace width 0.2mm and via
# radius 0.3mm, edge-to-edge clearance = 0.3 - 0.1 - 0.3 = -0.1 mm,
# which is well below the 0.127mm jlcpcb minimum.  Must fire 1 violation.
PCB_REAL_NEAR_MISS = """\
(kicad_pcb
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
  (net 1 "SIG1")
  (net 2 "SIG2")
  (via (at 100 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1 "SIG1") (uuid "via-1"))
  (segment (start 100.3 100) (end 101 100) (width 0.2) (layer "B.Cu") (net 2 "SIG2") (uuid "seg-1"))
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_clearances(pcb_content: str, tmp_path: Path):
    """Write a PCB fixture and return the clearance check results."""
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate import DRCChecker

    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(pcb_content)
    pcb = PCB.load(pcb_path)

    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    return checker.check_clearances()


def _count_segment_via(results) -> int:
    """Count clearance_segment_via violations."""
    return len(
        [v for v in results.violations if v.rule_id == "clearance_segment_via"]
    )


# ---------------------------------------------------------------------------
# Tests: epsilon constant sanity
# ---------------------------------------------------------------------------


class TestColocationEpsilonConstant:
    """Sanity-checks on the _COLOCATION_EPSILON_MM constant."""

    def test_epsilon_is_positive(self):
        from kicad_tools.validate.rules.clearance import _COLOCATION_EPSILON_MM

        assert _COLOCATION_EPSILON_MM > 0

    def test_epsilon_is_sub_micron(self):
        """Epsilon must be much smaller than any real clearance threshold.

        A 0.1-micron tolerance is far below any manufacturing precision
        (typical fine-pitch fab precision is ~10-25 microns), so the
        co-location skip cannot mask real near-misses.
        """
        from kicad_tools.validate.rules.clearance import _COLOCATION_EPSILON_MM

        assert _COLOCATION_EPSILON_MM < 0.001  # < 1 micron

    def test_epsilon_matches_edge_rule_precedent(self):
        """Epsilon should match the edge-rule precedent for consistency."""
        from kicad_tools.validate.rules.clearance import _COLOCATION_EPSILON_MM
        from kicad_tools.validate.rules.edge import _CLEARANCE_EPSILON_MM

        assert _COLOCATION_EPSILON_MM == _CLEARANCE_EPSILON_MM


# ---------------------------------------------------------------------------
# Tests: cross-net co-location (the primary bug case)
# ---------------------------------------------------------------------------


class TestCrossNetColocation:
    """Cross-net segment endpoint at via center must NOT produce a violation."""

    def test_coincident_endpoint_no_violation(self, tmp_path: Path):
        """Segment endpoint exactly at cross-net via center -> 0 violations.

        This is the primary bug case from Issue #2706.  Without the
        co-location skip, the rule reports a negative clearance at the
        via center because the geometric distance from the endpoint to
        the via center is 0.
        """
        results = _check_clearances(PCB_CROSS_NET_COINCIDENT, tmp_path)
        assert _count_segment_via(results) == 0, (
            "False positive: cross-net segment endpoint coincident with "
            "via center should be skipped by the co-location guard"
        )

    def test_endpoint_shifted_still_fires(self, tmp_path: Path):
        """Endpoint shifted 0.1mm from via center -> 1 violation expected.

        Over-suppression guard: the co-location skip must not mask real
        near-misses.  At 0.1mm separation with a 0.6mm via and 0.2mm
        trace, edge-to-edge clearance = 0.1 - 0.1 - 0.3 = -0.3 mm, well
        below jlcpcb's 0.127mm minimum.
        """
        results = _check_clearances(PCB_CROSS_NET_SHIFTED, tmp_path)
        assert _count_segment_via(results) == 1, (
            "Real near-miss at 0.1mm separation should still fire -- the "
            "co-location skip must not over-suppress"
        )

    def test_real_near_miss_still_fires(self, tmp_path: Path):
        """Endpoint 0.3mm from via center -> 1 violation (bonus check).

        Confirms the rule still detects clearance violations on traces
        that are merely close (not coincident) with cross-net vias.
        """
        results = _check_clearances(PCB_REAL_NEAR_MISS, tmp_path)
        assert _count_segment_via(results) == 1, (
            "Real clearance violation 0.3mm from via center must still "
            "fire after the co-location skip"
        )


# ---------------------------------------------------------------------------
# Tests: same-net co-location (already handled by same-net skip)
# ---------------------------------------------------------------------------


class TestSameNetColocation:
    """Same-net coincidence: already handled by same-net skip, no regression."""

    def test_same_net_coincident_no_violation(self, tmp_path: Path):
        """Segment endpoint at same-net via center -> 0 violations.

        This is the normal in-pad escape case (both the segment and
        the via share the pad's net).  Already handled by the same-net
        skip at clearance.py:461; the new co-location skip is a no-op
        here.  Included to confirm no regression.
        """
        results = _check_clearances(PCB_SAME_NET_COINCIDENT, tmp_path)
        assert _count_segment_via(results) == 0


# ---------------------------------------------------------------------------
# Tests: direct unit-level check on _check_layer
# ---------------------------------------------------------------------------


class TestCheckLayerColocationUnit:
    """Unit-level check that ClearanceRule._check_layer skips the pair."""

    def test_check_layer_skips_coincident_pair(self):
        """Build CopperElements directly and feed them into _check_layer.

        Mirrors ``tests/test_edge_clearance_epsilon.py`` style of
        directly invoking the rule helper with synthetic elements.
        """
        from unittest.mock import MagicMock

        from kicad_tools.schema.pcb import Layer
        from kicad_tools.validate.rules.clearance import (
            ClearanceRule,
            CopperElement,
        )

        # Manually construct a segment and a via on the same layer,
        # different nets, with the segment endpoint at the via center.
        seg = CopperElement(
            element_type="segment",
            layer="B.Cu",
            net_number=2,
            geometry=(100.0, 100.0, 101.0, 100.0, 0.2),
            reference="Trace-test",
            net_name="SIG2",
        )
        via = CopperElement(
            element_type="via",
            layer="*",
            net_number=1,
            geometry=(100.0, 100.0, 0.6, 0.6),
            reference="Via-test",
            net_name="SIG1",
        )

        # Mock PCB whose collection returns our two elements.
        rule = ClearanceRule()
        pcb = MagicMock()
        pcb.copper_layers = [Layer(number=31, name="B.Cu", type="signal")]

        # Bypass _collect_elements by monkey-patching:
        rule._collect_elements = MagicMock(return_value=[seg, via])

        violations = rule._check_layer(
            pcb, "B.Cu", min_clearance=0.127, diff_pair_set=set()
        )
        assert len(violations) == 0, (
            "_check_layer should skip the coincident (segment, via) pair"
        )

    def test_check_layer_fires_on_close_but_not_coincident(self):
        """Endpoint just outside epsilon -> violation fires."""
        from unittest.mock import MagicMock

        from kicad_tools.schema.pcb import Layer
        from kicad_tools.validate.rules.clearance import (
            ClearanceRule,
            CopperElement,
            _COLOCATION_EPSILON_MM,
        )

        # Place segment endpoint just outside the epsilon (2x epsilon
        # away).  This is far closer than min_clearance + radius, so a
        # violation MUST fire.
        offset = 2 * _COLOCATION_EPSILON_MM
        seg = CopperElement(
            element_type="segment",
            layer="B.Cu",
            net_number=2,
            geometry=(100.0 + offset, 100.0, 101.0, 100.0, 0.2),
            reference="Trace-test",
            net_name="SIG2",
        )
        via = CopperElement(
            element_type="via",
            layer="*",
            net_number=1,
            geometry=(100.0, 100.0, 0.6, 0.6),
            reference="Via-test",
            net_name="SIG1",
        )

        rule = ClearanceRule()
        pcb = MagicMock()
        pcb.copper_layers = [Layer(number=31, name="B.Cu", type="signal")]
        rule._collect_elements = MagicMock(return_value=[seg, via])

        violations = rule._check_layer(
            pcb, "B.Cu", min_clearance=0.127, diff_pair_set=set()
        )
        assert len(violations) == 1, (
            "Endpoint just outside epsilon should still produce a "
            "violation -- the skip must be strictly less-than"
        )
