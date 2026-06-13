"""Unit tests for net restore logic and net format validation in runner.py.

Covers the two failure modes from issue #1812:
1. ``_run_fill_zones_native()`` missing snapshot/restore protection.
2. ``_has_nonzero_net()`` treating name-only format as valid, causing
   ``_restore_net_declarations()`` to skip restoration of corrupted nets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.sexp import SExp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUNNER = "kicad_tools.cli.runner"


def _net_node(
    *args: int | str,
) -> SExp:
    """Build a ``(net ...)`` SExp node from positional arguments.

    Examples:
        _net_node(18, "SYNC_R")  -> (net 18 "SYNC_R")
        _net_node(0)             -> (net 0)
        _net_node("SYNC_R")     -> (net "SYNC_R")   # name-only
        _net_node("")            -> (net "")          # empty-string
    """
    return SExp.list("net", *args)


# ---------------------------------------------------------------------------
# _has_nonzero_net
# ---------------------------------------------------------------------------


class TestHasNonzeroNet:
    """Verify ``_has_nonzero_net`` behaviour for different net formats."""

    def test_canonical_nonzero(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(_net_node(18, "SYNC_R")) is True

    def test_numeric_only_nonzero(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(_net_node(18)) is True

    def test_zero_net(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(_net_node(0)) is False

    def test_zero_with_empty_name(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(_net_node(0, "")) is False

    def test_name_only(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        # Name-only format is still "has a net" for snapshot purposes.
        assert _has_nonzero_net(_net_node("SYNC_R")) is True

    def test_empty_string(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(_net_node("")) is False

    def test_none(self):
        from kicad_tools.cli.runner import _has_nonzero_net

        assert _has_nonzero_net(None) is False


# ---------------------------------------------------------------------------
# _has_canonical_net
# ---------------------------------------------------------------------------


class TestHasCanonicalNet:
    """Verify ``_has_canonical_net`` returns False for name-only corruption."""

    def test_canonical_nonzero(self):
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(_net_node(18, "SYNC_R")) is True

    def test_numeric_only_nonzero(self):
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(_net_node(18)) is True

    def test_zero_net(self):
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(_net_node(0)) is False

    def test_name_only_returns_false(self):
        """Name-only ``(net "SYNC_R")`` must be flagged as needing restore."""
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(_net_node("SYNC_R")) is False

    def test_empty_string_returns_false(self):
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(_net_node("")) is False

    def test_none(self):
        from kicad_tools.cli.runner import _has_canonical_net

        assert _has_canonical_net(None) is False


# ---------------------------------------------------------------------------
# _canonicalize_net_node
# ---------------------------------------------------------------------------


class TestCanonicalizeNetNode:
    """Verify ``_canonicalize_net_node`` format selection (issue #1820)."""

    def test_name_only_default_produces_dual_atom(self):
        """Default (pads): name-only -> (net N "name")."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        node = _canonicalize_net_node(_net_node("GND"), {"GND": 1})
        assert node.get_int(0) == 1
        assert node.get_string(1) == "GND"

    def test_name_only_numeric_only_produces_number(self):
        """numeric_only=True (segments/vias): name-only -> (net N)."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        node = _canonicalize_net_node(_net_node("GND"), {"GND": 1}, numeric_only=True)
        assert node.get_int(0) == 1
        assert node.get_string(1) is None

    def test_already_numeric_unchanged(self):
        """A node with numeric ID is returned unchanged regardless of flag."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        original = _net_node(5)
        result = _canonicalize_net_node(original, {"X": 5}, numeric_only=True)
        assert result is original

    def test_dual_atom_stripped_when_numeric_only(self):
        """numeric_only=True strips trailing name from (net N "name")."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        node = _canonicalize_net_node(_net_node(79, "GNDA"), {"GNDA": 79}, numeric_only=True)
        assert node.get_int(0) == 79
        assert node.get_string(1) is None

    def test_dual_atom_preserved_when_not_numeric_only(self):
        """Default mode preserves (net N "name") format."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        original = _net_node(79, "GNDA")
        result = _canonicalize_net_node(original, {"GNDA": 79})
        assert result is original

    def test_none_returns_none(self):
        from kicad_tools.cli.runner import _canonicalize_net_node

        assert _canonicalize_net_node(None, {}) is None

    def test_unknown_name_returns_original(self):
        from kicad_tools.cli.runner import _canonicalize_net_node

        original = _net_node("UNKNOWN")
        result = _canonicalize_net_node(original, {"GND": 1})
        assert result is original


# ---------------------------------------------------------------------------
# validate_net_format
# ---------------------------------------------------------------------------


class TestValidateNetFormat:
    """Verify ``validate_net_format`` detects corruption."""

    @staticmethod
    def _write_pcb(tmp_path: Path, segments: list[str], footprints: str = "") -> Path:
        """Write a minimal ``.kicad_pcb`` with custom segments/footprints."""
        pcb = tmp_path / "board.kicad_pcb"
        seg_text = "\n".join(segments)
        pcb.write_text(
            f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (net 18 "SYNC_R")
  {footprints}
  {seg_text}
)"""
        )
        return pcb

    def test_valid_pcb(self, tmp_path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = self._write_pcb(
            tmp_path,
            [
                '(segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 1) (uuid "a"))',
                '(via (at 50 60) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 18) (uuid "b"))',
            ],
        )
        report = validate_net_format(pcb)
        assert report.valid is True
        assert report.total_corrupt == 0

    def test_name_only_segment(self, tmp_path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = self._write_pcb(
            tmp_path,
            [
                '(segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "SYNC_R") (uuid "a"))',
            ],
        )
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.name_only_segments == 1

    def test_name_only_via(self, tmp_path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = self._write_pcb(
            tmp_path,
            [
                '(via (at 50 60) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SYNC_R") (uuid "b"))',
            ],
        )
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.name_only_vias == 1

    def test_empty_net_segment(self, tmp_path):
        from kicad_tools.cli.runner import validate_net_format

        pcb = self._write_pcb(
            tmp_path,
            [
                '(segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "") (uuid "a"))',
            ],
        )
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.empty_net_segments == 1

    def test_name_only_pad(self, tmp_path):
        from kicad_tools.cli.runner import validate_net_format

        fp = """(footprint "R_0603"
    (property "Reference" "R1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND"))
  )"""
        pcb = self._write_pcb(tmp_path, [], footprints=fp)
        report = validate_net_format(pcb)
        assert report.valid is False
        assert report.name_only_pads == 1


# ---------------------------------------------------------------------------
# _restore_net_declarations with name-only corruption
# ---------------------------------------------------------------------------


class TestRestoreNetDeclarations:
    """Verify that name-only net format is overwritten during restore."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_restores_name_only_segment(self, tmp_path):
        """A segment with ``(net "SYNC_R")`` should be restored to ``(net 18)`` numeric-only."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        # PCB with a segment corrupted to name-only format
        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 18 "SYNC_R")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "SYNC_R") (uuid "a"))
)""",
        )

        # Build snapshot data: the canonical net node for this segment
        net_nodes = [_net_node(0, ""), _net_node(18, "SYNC_R")]
        element_nets = {
            "seg:10.0,20.0:30.0,40.0:F.Cu": [_net_node(18)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        # Verify the segment now has numeric-only format (KiCad 9 requirement)
        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                net_num = net_node.get_int(0)
                assert net_num == 18, f"Expected (net 18) but got {net_node}"
                # Verify no net name string is present (numeric-only)
                assert net_node.get_string(1) is None, "Segment net should be numeric-only"
                break
        else:
            pytest.fail("No segment found in restored PCB")

    def test_restores_empty_string_segment(self, tmp_path):
        """A segment with ``(net "")`` should be restored."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "") (uuid "a"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(1, "GND")]
        element_nets = {
            "seg:10.0,20.0:30.0,40.0:F.Cu": [_net_node(1)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                net_num = net_node.get_int(0)
                assert net_num == 1, f"Expected (net 1) but got {net_node}"
                # Verify no net name string is present (numeric-only)
                assert net_node.get_string(1) is None, "Segment net should be numeric-only"
                break
        else:
            pytest.fail("No segment found in restored PCB")

    def test_preserves_canonical_net(self, tmp_path):
        """A segment with canonical ``(net 18)`` should NOT be overwritten."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 18 "SYNC_R")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 18) (uuid "a"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(18, "SYNC_R")]
        element_nets = {
            "seg:10.0,20.0:30.0,40.0:F.Cu": [_net_node(18)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                net_num = net_node.get_int(0)
                assert net_num == 18
                break
        else:
            pytest.fail("No segment found in restored PCB")


# ---------------------------------------------------------------------------
# _make_segment_via_key coordinate rounding (issue #1822)
# ---------------------------------------------------------------------------


class TestMakeSegmentViaKeyRounding:
    """Verify coordinate rounding absorbs float drift in key generation."""

    def test_identical_keys_within_rounding_threshold(self):
        """Coordinates differing by < 0.00005 must produce identical keys.

        With precision=4, round(100.00004, 4) == round(100.0, 4) == 100.0.
        """
        from kicad_tools.cli.runner import _make_segment_via_key

        seg_a = SExp.list(
            "segment",
            SExp.list("start", 100.0, 200.0),
            SExp.list("end", 300.0, 400.0),
            SExp.list("layer", "F.Cu"),
        )
        seg_b = SExp.list(
            "segment",
            SExp.list("start", 100.00004, 200.0),
            SExp.list("end", 300.0, 399.99996),
            SExp.list("layer", "F.Cu"),
        )
        key_a = _make_segment_via_key(seg_a)
        key_b = _make_segment_via_key(seg_b)
        assert key_a is not None
        assert key_a == key_b

    def test_distinct_keys_beyond_rounding_threshold(self):
        """Coordinates differing by > 0.001 must produce distinct keys."""
        from kicad_tools.cli.runner import _make_segment_via_key

        seg_a = SExp.list(
            "segment",
            SExp.list("start", 100.0, 200.0),
            SExp.list("end", 300.0, 400.0),
            SExp.list("layer", "F.Cu"),
        )
        seg_b = SExp.list(
            "segment",
            SExp.list("start", 100.005, 200.0),
            SExp.list("end", 300.0, 400.0),
            SExp.list("layer", "F.Cu"),
        )
        key_a = _make_segment_via_key(seg_a)
        key_b = _make_segment_via_key(seg_b)
        assert key_a is not None
        assert key_b is not None
        assert key_a != key_b

    def test_via_rounding(self):
        """Via coordinates must also be rounded for consistent keys."""
        from kicad_tools.cli.runner import _make_segment_via_key

        via_a = SExp.list(
            "via",
            SExp.list("at", 50.0, 75.0),
            SExp.list("size", 0.8),
            SExp.list("layers", "F.Cu", "B.Cu"),
        )
        via_b = SExp.list(
            "via",
            SExp.list("at", 50.00004, 74.99996),
            SExp.list("size", 0.8),
            SExp.list("layers", "F.Cu", "B.Cu"),
        )
        key_a = _make_segment_via_key(via_a)
        key_b = _make_segment_via_key(via_b)
        assert key_a is not None
        assert key_a == key_b


# ---------------------------------------------------------------------------
# Restore with drifted coordinates (issue #1822)
# ---------------------------------------------------------------------------


class TestRestoreWithDriftedCoordinates:
    """Verify restore succeeds when kicad-cli drifts coordinates slightly."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_drifted_segment_restored_by_rounded_key(self, tmp_path):
        """Segment at (100.0001, 200.0) with (net "") should be restored
        when snapshot was taken at (100.0, 200.0)."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "VCC")
  (segment (start 100.0001 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "drift1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(5, "VCC")]
        # Snapshot taken at exact coordinates (before kicad-cli drift)
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(5)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 5, f"Expected (net 5) but got {net_node}"
                break
        else:
            pytest.fail("No segment found in restored PCB")

    def test_drifted_via_restored_by_rounded_key(self, tmp_path):
        """Via at (50.00005, 75.0) with (net "") should match snapshot at (50.0, 75.0)."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 3 "GND")
  (via (at 50.00005 75.0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "") (uuid "vdrift"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(3, "GND")]
        element_nets = {
            "via:50.0,75.0:0.8:F.Cu,B.Cu": [_net_node(3)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "via":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 3, f"Expected (net 3) but got {net_node}"
                break
        else:
            pytest.fail("No via found in restored PCB")


# ---------------------------------------------------------------------------
# Fallback proximity restore for wholly unmatched elements (issue #1822)
# ---------------------------------------------------------------------------


class TestFallbackProximityRestore:
    """Verify the fallback mechanism for segments/vias with no snapshot match."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_unmatched_segment_restored_by_proximity(self, tmp_path):
        """A segment with (net "") and no exact key match should be restored
        via spatial proximity to a nearby snapshotted segment on the same layer."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        # Post-fill PCB: segment coordinates drifted beyond rounding tolerance
        # but within proximity threshold (0.1 mm)
        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 7 "MOSI")
  (segment (start 100.009 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "prox1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(7, "MOSI")]
        # Snapshot at exact coords -- key won't match due to rounding difference
        # (100.009 rounds to 100.009, not 100.0)
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(7)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 7, f"Expected (net 7) but got {net_node}"
                break
        else:
            pytest.fail("No segment found in restored PCB")

    def test_unmatched_segment_not_restored_beyond_threshold(self, tmp_path):
        """A segment too far from any snapshot should NOT be proximity-matched."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        # Segment at (200, 200) -- far from snapshot at (100, 200)
        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 7 "MOSI")
  (segment (start 200 200) (end 300 400) (width 0.25) (layer "F.Cu") (net "") (uuid "far1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(7, "MOSI")]
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(7)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                # Should still be empty -- too far for proximity match
                if net_node is not None:
                    net_str = net_node.get_string(0)
                    if net_str == "":
                        pass  # Expected: still empty
                    else:
                        net_num = net_node.get_int(0)
                        assert net_num != 7, "Segment too far should not be proximity-matched"
                break

    def test_segment_restored_after_fix_drc_nudge(self, tmp_path):
        """Segment nudged up to 0.05 mm by fix-drc must still be proximity-matched.

        fix-drc's repair_clearance can nudge segments up to max_displacement=0.1 mm.
        With the old 0.01 mm threshold, a nudge of even 0.006 mm per endpoint would
        exceed the combined distance metric and cause 130+ segments to lose their
        net assignments (issue #1842).  The widened 0.1 mm threshold covers this.
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        # Segment nudged 0.05 mm on start-x -- well beyond old 0.01 threshold
        # but within new 0.1 threshold
        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 18 "SCK")
  (segment (start 100.05 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "nudge1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(18, "SCK")]
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(18)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 18, (
                    f"Segment nudged 0.05 mm should be proximity-matched; got {net_node}"
                )
                break
        else:
            pytest.fail("No segment found in restored PCB")

    def test_segment_not_restored_beyond_widened_threshold(self, tmp_path):
        """Segment displaced > 0.5 mm must NOT be proximity-matched.

        The proximity threshold is 0.5 mm (to cover drill clearance
        max_displacement).  Segments displaced beyond that should not match
        to avoid cross-net mismatches.
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        # Segment displaced 0.6 mm on start-x -- beyond the 0.5 threshold
        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 18 "SCK")
  (segment (start 100.6 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "far2"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(18, "SCK")]
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(18)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                if net_node is not None:
                    net_str = net_node.get_string(0)
                    if net_str == "":
                        pass  # Expected: still empty
                    else:
                        net_num = net_node.get_int(0)
                        assert net_num != 18, "Segment 0.6 mm away should not be proximity-matched"
                break


# ---------------------------------------------------------------------------
# _run_fill_zones_native snapshot/restore
# ---------------------------------------------------------------------------


class TestRunFillZonesNativeProtection:
    """Verify ``_run_fill_zones_native`` snapshots and restores nets."""

    def test_snapshots_and_restores(self, tmp_path):
        """Native fill path must call snapshot and restore functions."""
        from kicad_tools.cli.runner import _run_fill_zones_native

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
)"""
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with (
            patch(f"{_RUNNER}.subprocess.run", return_value=mock_result),
            patch(f"{_RUNNER}._snapshot_net_declarations") as mock_snap,
            patch(f"{_RUNNER}._snapshot_element_nets") as mock_elem_snap,
            patch(f"{_RUNNER}._restore_net_declarations") as mock_restore,
        ):
            mock_snap.return_value = [_net_node(0, ""), _net_node(1, "GND")]
            mock_elem_snap.return_value = {}

            result = _run_fill_zones_native(pcb, None, Path("/usr/bin/kicad-cli"))

            assert result.success is True
            mock_snap.assert_called_once_with(pcb)
            mock_elem_snap.assert_called_once_with(pcb)
            mock_restore.assert_called_once()

    def test_no_restore_on_failure(self, tmp_path):
        """Native fill should NOT restore nets when kicad-cli fails."""
        from kicad_tools.cli.runner import _run_fill_zones_native

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240108) (generator test))")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"

        with (
            patch(f"{_RUNNER}.subprocess.run", return_value=mock_result),
            patch(f"{_RUNNER}._snapshot_net_declarations", return_value=[]),
            patch(f"{_RUNNER}._snapshot_element_nets", return_value={}),
            patch(f"{_RUNNER}._restore_net_declarations") as mock_restore,
        ):
            result = _run_fill_zones_native(pcb, None, Path("/usr/bin/kicad-cli"))

            assert result.success is False
            mock_restore.assert_not_called()


# ---------------------------------------------------------------------------
# Issue #1845: snapshot includes (net 0) segments
# ---------------------------------------------------------------------------


class TestSnapshotIncludesNetZero:
    """Verify ``_snapshot_element_nets`` captures (net 0) segments."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_net_zero_segment_is_snapshotted(self, tmp_path):
        """A segment with (net 0) must appear in the snapshot."""
        from kicad_tools.cli.runner import _snapshot_element_nets

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 0) (uuid "z1"))
)""",
        )

        snapshot = _snapshot_element_nets(pcb)
        key = "seg:10.0,20.0:30.0,40.0:F.Cu"
        assert key in snapshot, "Snapshot must include (net 0) segments"
        net_node = snapshot[key][0]
        assert net_node.get_int(0) == 0, "Snapshot must preserve (net 0)"

    def test_nonzero_segment_still_snapshotted(self, tmp_path):
        """Nonzero net segments must still be captured."""
        from kicad_tools.cli.runner import _snapshot_element_nets

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "VCC")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 5) (uuid "a1"))
)""",
        )

        snapshot = _snapshot_element_nets(pcb)
        key = "seg:10.0,20.0:30.0,40.0:F.Cu"
        assert key in snapshot
        assert snapshot[key][0].get_int(0) == 5


# ---------------------------------------------------------------------------
# Issue #1845: proximity uses max(start, end) metric
# ---------------------------------------------------------------------------


class TestProximityMaxMetric:
    """Verify proximity uses max single-endpoint distance, not sum."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_both_endpoints_shifted_within_threshold(self, tmp_path):
        """Segment with both endpoints shifted 0.06mm should match.

        Old metric (sum): hypot(0.06,0) + hypot(0.06,0) = 0.12 > 0.1 (FAIL).
        New metric (max): max(hypot(0.06,0), hypot(0.06,0)) = 0.06 < 0.1 (PASS).
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 9 "MISO")
  (segment (start 100.06 200.0) (end 300.06 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "shift1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(9, "MISO")]
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(9)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 9, (
                    "Both endpoints shifted 0.06mm should match with max() metric"
                )
                break
        else:
            pytest.fail("No segment found")


# ---------------------------------------------------------------------------
# Issue #1845: remaining (net "") assigned to (net 0)
# ---------------------------------------------------------------------------


class TestAssignEmptyNetsToZero:
    """Verify remaining (net \"\") segments are assigned (net 0)."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_empty_net_segment_becomes_net_zero(self, tmp_path):
        """A segment with (net \"\") and no snapshot match should become (net 0)."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (segment (start 999 999) (end 998 998) (width 0.25) (layer "F.Cu") (net "") (uuid "new1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(1, "GND")]
        # No snapshot entry for this segment at all
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(1)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                net_num = net_node.get_int(0)
                assert net_num == 0, (
                    f'Unmatched (net "") segment should become (net 0), got {net_node}'
                )
                break
        else:
            pytest.fail("No segment found")

    def test_canonical_net_not_overwritten_to_zero(self, tmp_path):
        """A segment with a valid net should NOT be changed to (net 0)."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "VCC")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 5) (uuid "ok1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(5, "VCC")]
        element_nets = {}

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 5, "Valid net should be preserved"
                break
        else:
            pytest.fail("No segment found")

    def test_net_zero_restored_from_snapshot(self, tmp_path):
        """A segment originally (net 0) corrupted to (net \"\") should restore to (net 0)."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "") (uuid "z2"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(1, "GND")]
        # Snapshot captured this segment with (net 0) — original assignment
        element_nets = {
            "seg:10.0,20.0:30.0,40.0:F.Cu": [_net_node(0)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 0, (
                    "Segment originally (net 0) should be restored to (net 0)"
                )
                break
        else:
            pytest.fail("No segment found")


# ---------------------------------------------------------------------------
# _strip_dual_atom_nets
# ---------------------------------------------------------------------------


class TestStripDualAtomNets:
    """Verify ``_strip_dual_atom_nets`` strips trailing names from segments/vias."""

    def test_segment_dual_atom_stripped(self):
        """(net 5 \"GND\") on a segment becomes (net 5)."""
        from kicad_tools.cli.runner import _strip_dual_atom_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            "(kicad_pcb (segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net 5 "GND") (uuid "s1")))'
        )
        changed = _strip_dual_atom_nets(tree)
        assert changed is True
        seg = [c for c in tree.children if c.name == "segment"][0]
        net_node = seg.get("net")
        assert net_node.get_int(0) == 5
        assert net_node.get_string(1) is None

    def test_via_dual_atom_stripped(self):
        """(net 3 \"VCC\") on a via becomes (net 3)."""
        from kicad_tools.cli.runner import _strip_dual_atom_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            "(kicad_pcb (via (at 10 20) (size 0.6) (drill 0.3) "
            '(layers "F.Cu" "B.Cu") (net 3 "VCC") (uuid "v1")))'
        )
        changed = _strip_dual_atom_nets(tree)
        assert changed is True
        via = [c for c in tree.children if c.name == "via"][0]
        net_node = via.get("net")
        assert net_node.get_int(0) == 3
        assert net_node.get_string(1) is None

    def test_pad_dual_atom_preserved(self):
        """(net 5 \"GND\") on a pad is left unchanged (valid in KiCad 9)."""
        from kicad_tools.cli.runner import _strip_dual_atom_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            '(kicad_pcb (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 5 "GND")))'
        )
        changed = _strip_dual_atom_nets(tree)
        assert changed is False

    def test_numeric_only_unchanged(self):
        """(net 5) on a segment is left unchanged."""
        from kicad_tools.cli.runner import _strip_dual_atom_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            "(kicad_pcb (segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net 5) (uuid "s2")))'
        )
        changed = _strip_dual_atom_nets(tree)
        assert changed is False

    def test_no_segments_returns_false(self):
        """A PCB with no segments/vias returns False."""
        from kicad_tools.cli.runner import _strip_dual_atom_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp('(kicad_pcb (net 0 "") (net 1 "GND"))')
        changed = _strip_dual_atom_nets(tree)
        assert changed is False


# ---------------------------------------------------------------------------
# _resolve_name_only_nets
# ---------------------------------------------------------------------------


class TestResolveNameOnlyNets:
    """Verify ``_resolve_name_only_nets`` resolves name-only nets on segments/vias."""

    def test_name_only_segment_resolved(self):
        """(net \"GND\") on a segment becomes (net 2)."""
        from kicad_tools.cli.runner import _resolve_name_only_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            '(kicad_pcb (net 0 "") (net 2 "GND") '
            "(segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net "GND") (uuid "s1")))'
        )
        changed = _resolve_name_only_nets(tree)
        assert changed is True
        seg = [c for c in tree.children if c.name == "segment"][0]
        net_node = seg.get("net")
        assert net_node.get_int(0) == 2
        assert net_node.get_string(1) is None

    def test_name_only_via_resolved(self):
        """(net \"VCC\") on a via becomes (net 3)."""
        from kicad_tools.cli.runner import _resolve_name_only_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            '(kicad_pcb (net 0 "") (net 3 "VCC") '
            "(via (at 10 20) (size 0.6) (drill 0.3) "
            '(layers "F.Cu" "B.Cu") (net "VCC") (uuid "v1")))'
        )
        changed = _resolve_name_only_nets(tree)
        assert changed is True
        via = [c for c in tree.children if c.name == "via"][0]
        net_node = via.get("net")
        assert net_node.get_int(0) == 3

    def test_unknown_name_left_unchanged(self):
        """(net \"MYSTERY\") with no matching declaration is left unchanged."""
        from kicad_tools.cli.runner import _resolve_name_only_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            '(kicad_pcb (net 0 "") (net 1 "GND") '
            "(segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net "MYSTERY") (uuid "s2")))'
        )
        changed = _resolve_name_only_nets(tree)
        assert changed is False

    def test_numeric_net_not_touched(self):
        """(net 5) on a segment is left unchanged."""
        from kicad_tools.cli.runner import _resolve_name_only_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            '(kicad_pcb (net 0 "") (net 5 "VCC") '
            "(segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net 5) (uuid "s3")))'
        )
        changed = _resolve_name_only_nets(tree)
        assert changed is False

    def test_no_net_declarations_returns_false(self):
        """A PCB with no net declarations returns False."""
        from kicad_tools.cli.runner import _resolve_name_only_nets
        from kicad_tools.sexp.parser import parse_string as load_sexp

        tree = load_sexp(
            "(kicad_pcb (segment (start 1 2) (end 3 4) (width 0.25) "
            '(layer "F.Cu") (net "GND") (uuid "s4")))'
        )
        changed = _resolve_name_only_nets(tree)
        assert changed is False


# ---------------------------------------------------------------------------
# Integration: _restore_net_declarations end-to-end
# ---------------------------------------------------------------------------


class TestRestoreNetDeclarationsStripsFormats:
    """Verify the full pipeline strips dual-atom and name-only formats."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_dual_atom_stripped_in_pipeline(self, tmp_path):
        """A segment with (net 5 \"GND\") is stripped to (net 5) by the full pipeline."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 5 "GND") (uuid "d1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(5, "GND")]
        _restore_net_declarations(pcb, net_nodes, element_nets=None)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 5
                assert net_node.get_string(1) is None, (
                    "Dual-atom format should be stripped from segments"
                )
                break
        else:
            pytest.fail("No segment found")

    def test_name_only_resolved_in_pipeline(self, tmp_path):
        """A segment with (net \"GND\") is resolved to (net 2) by the full pipeline."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 2 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net "GND") (uuid "n1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(2, "GND")]
        _restore_net_declarations(pcb, net_nodes, element_nets=None)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 2
                assert net_node.get_string(1) is None, (
                    "Name-only format should be resolved to numeric-only"
                )
                break
        else:
            pytest.fail("No segment found")


# ---------------------------------------------------------------------------
# Issue #1848: UUID-based matching for displaced segments
# ---------------------------------------------------------------------------


class TestUuidBasedMatching:
    """Verify UUID-based snapshot/restore handles DRC displacement."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_snapshot_includes_uuid_keys(self, tmp_path):
        """Snapshot must include uuid:<value> keys for elements with UUIDs."""
        from kicad_tools.cli.runner import _snapshot_element_nets

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "VCC")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 5) (uuid "abc-123"))
)""",
        )

        snapshot = _snapshot_element_nets(pcb)
        # Both geometry and UUID keys should be present
        assert "seg:10.0,20.0:30.0,40.0:F.Cu" in snapshot
        assert "uuid:abc-123" in snapshot
        assert snapshot["uuid:abc-123"][0].get_int(0) == 5

    def test_snapshot_uuid_key_for_via(self, tmp_path):
        """Via with UUID must also get a uuid: key in the snapshot."""
        from kicad_tools.cli.runner import _snapshot_element_nets

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 3 "GND")
  (via (at 50 75) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 3) (uuid "via-uuid-1"))
)""",
        )

        snapshot = _snapshot_element_nets(pcb)
        assert "uuid:via-uuid-1" in snapshot
        assert snapshot["uuid:via-uuid-1"][0].get_int(0) == 3

    def test_displaced_segment_restored_by_uuid(self, tmp_path):
        """Segment displaced 0.3mm by DRC should be restored via UUID match.

        This displacement exceeds the old 0.1mm proximity threshold but the
        UUID is stable, so the segment must be restored correctly.
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 18 "SCK")
  (segment (start 100.3 200.0) (end 300.3 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "displaced-1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(18, "SCK")]
        # Snapshot has UUID key from pre-displacement coordinates
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(18)],
            "uuid:displaced-1": [_net_node(18)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 18, (
                    f"Segment displaced 0.3mm should be restored via UUID; got {net_node}"
                )
                break
        else:
            pytest.fail("No segment found")

    def test_displaced_via_restored_by_uuid(self, tmp_path):
        """Via displaced beyond geometry match threshold restored via UUID."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 7 "MOSI")
  (via (at 50.4 75.0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "") (uuid "via-disp-1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(7, "MOSI")]
        element_nets = {
            "via:50.0,75.0:0.8:F.Cu,B.Cu": [_net_node(7)],
            "uuid:via-disp-1": [_net_node(7)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "via":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 7, (
                    f"Via displaced 0.4mm should be restored via UUID; got {net_node}"
                )
                break
        else:
            pytest.fail("No via found")

    def test_segment_without_uuid_falls_back_to_geometry(self, tmp_path):
        """Segment without UUID should still be restored by geometry key."""
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 5 "VCC")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net ""))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(5, "VCC")]
        element_nets = {
            "seg:10.0,20.0:30.0,40.0:F.Cu": [_net_node(5)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 5, (
                    "Segment without UUID should fall back to geometry key"
                )
                break
        else:
            pytest.fail("No segment found")

    def test_net_zero_segment_snapshot_with_uuid(self, tmp_path):
        """A (net 0) segment with UUID should have both keys in snapshot."""
        from kicad_tools.cli.runner import _snapshot_element_nets

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (segment (start 10 20) (end 30 40) (width 0.25) (layer "F.Cu") (net 0) (uuid "zero-seg"))
)""",
        )

        snapshot = _snapshot_element_nets(pcb)
        assert "uuid:zero-seg" in snapshot
        assert snapshot["uuid:zero-seg"][0].get_int(0) == 0
        assert "seg:10.0,20.0:30.0,40.0:F.Cu" in snapshot


# ---------------------------------------------------------------------------
# Issue #1848: widened proximity threshold covers drill clearance displacement
# ---------------------------------------------------------------------------


class TestWidenedProximityThreshold:
    """Verify proximity threshold covers drill clearance max_displacement (0.5mm)."""

    @staticmethod
    def _write_pcb(tmp_path: Path, content: str) -> Path:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(content)
        return pcb

    def test_segment_displaced_0_3mm_restored_by_proximity(self, tmp_path):
        """Segment displaced 0.3mm (within 0.5mm threshold) should be proximity-matched.

        This displacement exceeds the old 0.1mm threshold but is within the
        new 0.5mm threshold needed for drill clearance repair.
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 12 "SCLK")
  (segment (start 100.3 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "drill-disp-1"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(12, "SCLK")]
        # Only geometry key in snapshot (no UUID key) to test proximity
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(12)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                assert net_node is not None
                assert net_node.get_int(0) == 12, (
                    f"Segment displaced 0.3mm should be proximity-matched with widened threshold; got {net_node}"
                )
                break
        else:
            pytest.fail("No segment found")

    def test_segment_displaced_0_5mm_not_restored_by_proximity(self, tmp_path):
        """Segment displaced exactly at threshold boundary should NOT match.

        The threshold is strict less-than, so 0.5mm displacement should not match.
        """
        from kicad_tools.cli.runner import _restore_net_declarations
        from kicad_tools.core.sexp_file import load_pcb

        pcb = self._write_pcb(
            tmp_path,
            """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 12 "SCLK")
  (segment (start 100.5 200.0) (end 300.0 400.0) (width 0.25) (layer "F.Cu") (net "") (uuid "at-thresh"))
)""",
        )

        net_nodes = [_net_node(0, ""), _net_node(12, "SCLK")]
        element_nets = {
            "seg:100.0,200.0:300.0,400.0:F.Cu": [_net_node(12)],
        }

        _restore_net_declarations(pcb, net_nodes, element_nets)

        sexp = load_pcb(str(pcb))
        for child in sexp.children:
            if child.name == "segment":
                net_node = child.get("net")
                if net_node is not None:
                    net_num = net_node.get_int(0)
                    # Should be (net 0) from assign-empty-nets-to-zero, not (net 12)
                    assert net_num != 12, "Segment at exactly 0.5mm should NOT be proximity-matched"
                break
