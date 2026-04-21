"""Unit tests for net restore logic and net format validation in runner.py.

Covers the two failure modes from issue #1812:
1. ``_run_fill_zones_native()`` missing snapshot/restore protection.
2. ``_has_nonzero_net()`` treating name-only format as valid, causing
   ``_restore_net_declarations()`` to skip restoration of corrupted nets.
"""

from __future__ import annotations

import shutil
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

        node = _canonicalize_net_node(
            _net_node("GND"), {"GND": 1}, numeric_only=True
        )
        assert node.get_int(0) == 1
        assert node.get_string(1) is None

    def test_already_numeric_unchanged(self):
        """A node with numeric ID is returned unchanged regardless of flag."""
        from kicad_tools.cli.runner import _canonicalize_net_node

        original = _net_node(5)
        result = _canonicalize_net_node(original, {"X": 5}, numeric_only=True)
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
