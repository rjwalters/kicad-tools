"""Tests for floating-point epsilon tolerance in pad-pad clearance DRC.

Issue #2465: Board 05 reported a "0.149mm vs 0.150mm" pad-pad clearance
violation that was attributable to IEEE-754 rounding -- a sub-micron
shortfall well below any manufacturing tolerance.  The
``IncrementalDRC._check_pair_clearance_*`` methods compared raw clearance
values against ``rules.min_clearance_mm`` without an epsilon, so floating
point noise in the radius-and-trig math produced spurious violations.

This mirrors the fix from #2428 for edge clearance: a 0.1 micron
(``1e-4`` mm) tolerance on the comparison, which is well below
manufacturing precision but eliminates IEEE-754 false positives.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_tools.drc.incremental import IncrementalDRC, _CLEARANCE_EPSILON_MM


# ---------------------------------------------------------------------------
# Sanity checks on the epsilon constant
# ---------------------------------------------------------------------------


class TestEpsilonConstant:
    """Sanity-checks on the pad-pad clearance epsilon."""

    def test_epsilon_is_positive(self):
        assert _CLEARANCE_EPSILON_MM > 0

    def test_epsilon_is_sub_micron(self):
        """Epsilon must be much smaller than any real clearance."""
        assert _CLEARANCE_EPSILON_MM < 0.001  # less than 1 micron

    def test_epsilon_matches_edge_clearance_epsilon(self):
        """For consistency the pad-pad epsilon should match the edge
        clearance epsilon introduced in #2428."""
        from kicad_tools.validate.rules.edge import (
            _CLEARANCE_EPSILON_MM as edge_epsilon,
        )

        assert _CLEARANCE_EPSILON_MM == edge_epsilon


# ---------------------------------------------------------------------------
# Helpers for building lightweight Footprint / Pad mocks
# ---------------------------------------------------------------------------


def _make_pad(number: str, position: tuple[float, float], size: tuple[float, float],
              net_number: int = 1, net_name: str = "NET1"):
    """Create a Pad-like object with the attributes the DRC reads."""
    pad = SimpleNamespace()
    pad.number = number
    pad.position = position
    pad.size = size
    pad.net_number = net_number
    pad.net_name = net_name
    pad.layers = ["F.Cu"]
    return pad


def _make_footprint(reference: str, position: tuple[float, float], pads: list,
                    rotation: float = 0.0):
    """Create a Footprint-like object with the attributes the DRC reads."""
    fp = SimpleNamespace()
    fp.reference = reference
    fp.position = position
    fp.rotation = rotation
    fp.pads = pads
    return fp


def _make_drc(min_clearance: float = 0.150) -> IncrementalDRC:
    """Create an IncrementalDRC with mocked PCB and rules.

    We only need the clearance-checking methods, so we construct a minimal
    instance via ``object.__new__`` to skip the full PCB initialization.
    """
    drc = object.__new__(IncrementalDRC)
    drc.rules = SimpleNamespace(min_clearance_mm=min_clearance)
    drc.pcb = MagicMock()
    return drc


# ---------------------------------------------------------------------------
# Tests: pad-pad clearance with epsilon (Python path)
# ---------------------------------------------------------------------------


class TestPadPadEpsilonPython:
    """Verify the Python pad-pad clearance check applies epsilon tolerance."""

    def test_no_violation_at_exact_minimum(self):
        """Two pads spaced at exactly min_clearance_mm must NOT flag.

        Pads of size 0.5mm radius each, separated so edge-to-edge distance
        is exactly 0.150mm (the min clearance).
        """
        # Pad radius = max(size) / 2 = 0.5 / 2 = 0.25
        # We want clearance = dist - r1 - r2 = 0.150
        # => dist = 0.150 + 0.25 + 0.25 = 0.650
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5), net_number=1, net_name="A")
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5), net_number=2, net_name="B")
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (0.650, 0.0), [pad2])

        drc = _make_drc(min_clearance=0.150)
        violation = drc._check_pair_clearance_python(fp1, fp2, "R1", "R2")
        assert violation is None, (
            "Clearance exactly at minimum must not flag a violation"
        )

    def test_no_violation_at_minimum_minus_half_epsilon(self):
        """Floating-point edge case: clearance = min - 0.5*epsilon (within
        tolerance) must NOT flag.  This is the 0.149999mm vs 0.150mm
        case that motivated #2465."""
        # Half-epsilon = 5e-5 mm; aim for clearance = 0.15 - 5e-5
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5), net_number=1, net_name="A")
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5), net_number=2, net_name="B")
        # dist = 0.150 + 0.25 + 0.25 - 5e-5 = 0.65 - 5e-5
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (0.650 - 5e-5, 0.0), [pad2])

        drc = _make_drc(min_clearance=0.150)
        violation = drc._check_pair_clearance_python(fp1, fp2, "R1", "R2")
        assert violation is None, (
            "Sub-epsilon shortfall (0.149999mm vs 0.150mm) must not flag"
        )

    def test_violation_at_real_shortfall(self):
        """Real violations (well above epsilon) must still flag.
        Clearance = 0.145mm (5 microns shy of 0.150mm) must flag."""
        # dist = 0.145 + 0.25 + 0.25 = 0.645
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5), net_number=1, net_name="A")
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5), net_number=2, net_name="B")
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (0.645, 0.0), [pad2])

        drc = _make_drc(min_clearance=0.150)
        violation = drc._check_pair_clearance_python(fp1, fp2, "R1", "R2")
        assert violation is not None, (
            "5-micron clearance shortfall must flag a violation"
        )
        assert violation.actual_value < 0.150
        assert violation.required_value == 0.150

    def test_violation_at_clearance_minus_2x_epsilon(self):
        """Clearance just past the epsilon tolerance must flag."""
        # Make distance < min - 2*epsilon
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5), net_number=1, net_name="A")
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5), net_number=2, net_name="B")
        # clearance shortfall = 3*epsilon = 3e-4 mm
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (0.650 - 3e-4, 0.0), [pad2])

        drc = _make_drc(min_clearance=0.150)
        violation = drc._check_pair_clearance_python(fp1, fp2, "R1", "R2")
        assert violation is not None, (
            "Shortfall of 3*epsilon must still flag (just past the tolerance)"
        )

    def test_same_net_does_not_flag(self):
        """Same-net pads can touch and must not flag regardless of distance."""
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5), net_number=5, net_name="A")
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5), net_number=5, net_name="A")
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (0.1, 0.0), [pad2])  # very close

        drc = _make_drc(min_clearance=0.150)
        violation = drc._check_pair_clearance_python(fp1, fp2, "R1", "R2")
        assert violation is None, "Same-net pads must not flag clearance violations"


# ---------------------------------------------------------------------------
# Tests: pad-pad clearance with epsilon (C++ path -- mocked)
# ---------------------------------------------------------------------------


class TestPadPadEpsilonCpp:
    """Verify the C++ pad-pad clearance path also applies epsilon tolerance.

    The C++ extension just returns the raw clearance value; the threshold
    comparison happens in Python.  We mock the C++ result and assert that
    the same epsilon logic gates whether a violation is reported.
    """

    def test_cpp_no_violation_at_exact_minimum(self):
        """Mocked C++ result of exactly min_clearance must NOT flag."""
        drc = _make_drc(min_clearance=0.150)
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5))
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5))
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (1.0, 0.0), [pad2])

        with patch(
            "kicad_tools.drc.incremental.check_pair_clearance_cpp",
            return_value=(0.150, (0.5, 0.0), ("R1-1", "R2-2"), ("A", "B")),
        ):
            violation = drc._check_pair_clearance_cpp(fp1, fp2, "R1", "R2")
        assert violation is None

    def test_cpp_no_violation_at_sub_epsilon_shortfall(self):
        """The 0.149999mm-vs-0.150mm case via the C++ path must NOT flag."""
        drc = _make_drc(min_clearance=0.150)
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5))
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5))
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (1.0, 0.0), [pad2])

        # Just below minimum but within epsilon
        clearance_within_eps = 0.150 - (_CLEARANCE_EPSILON_MM * 0.5)
        with patch(
            "kicad_tools.drc.incremental.check_pair_clearance_cpp",
            return_value=(
                clearance_within_eps,
                (0.5, 0.0),
                ("R1-1", "R2-2"),
                ("A", "B"),
            ),
        ):
            violation = drc._check_pair_clearance_cpp(fp1, fp2, "R1", "R2")
        assert violation is None, (
            f"Clearance {clearance_within_eps}mm is within "
            f"{_CLEARANCE_EPSILON_MM}mm of the minimum and must not flag"
        )

    def test_cpp_violation_at_real_shortfall(self):
        """A real violation (5 microns shy) via C++ path must flag."""
        drc = _make_drc(min_clearance=0.150)
        pad1 = _make_pad("1", (0.0, 0.0), (0.5, 0.5))
        pad2 = _make_pad("2", (0.0, 0.0), (0.5, 0.5))
        fp1 = _make_footprint("R1", (0.0, 0.0), [pad1])
        fp2 = _make_footprint("R2", (1.0, 0.0), [pad2])

        with patch(
            "kicad_tools.drc.incremental.check_pair_clearance_cpp",
            return_value=(0.145, (0.5, 0.0), ("R1-1", "R2-2"), ("A", "B")),
        ):
            violation = drc._check_pair_clearance_cpp(fp1, fp2, "R1", "R2")
        assert violation is not None
        assert violation.actual_value == 0.145
        assert violation.required_value == 0.150
