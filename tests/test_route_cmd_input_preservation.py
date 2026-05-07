"""Tests that ``kct route INPUT -o OUTPUT`` does not modify INPUT (issue #2548).

Auto-pour writes zone definitions in-place to its target PCB.  Before
this fix, ``kct route`` passed the user's INPUT path to auto-pour, which
silently mutated the input file -- producing a confusing two-file diff
for what users naturally read as a single read-INPUT/write-OUTPUT
operation.

The fix stages a copy at OUTPUT first (when INPUT != OUTPUT) and runs
auto-pour against the copy, leaving INPUT byte-identical.  When INPUT
== OUTPUT (the ``kct build`` pipeline case), the copy is a no-op and
in-place behavior is preserved.

This module covers:

1. The ``_stage_input_for_auto_pour`` helper directly (path semantics).
2. A behavioral test that exercises the auto-pour code path through
   the helper plus ``auto_pour_if_missing`` and confirms the user's
   INPUT file is unchanged across the full operation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal PCB fixture (mirrors the fixture in tests/test_auto_pour.py so
# this test stays self-contained even if that one is refactored).
# ---------------------------------------------------------------------------

_PCB_HEADER = """\
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
"""

_PCB_FOOTER = """\
  (gr_line (start 0 0) (end 50 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 0) (end 50 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 50) (end 0 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 0 50) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _make_pcb_with_pour_candidates() -> str:
    """Build a PCB that has *both* power and signal nets (auto-pour eligible).

    The board-level guard in ``auto_pour_if_missing`` skips boards where
    *every* net is power/ground; including SDA/SCL ensures pours actually
    get created so the test exercises the in-place write that motivated
    issue #2548.
    """
    parts = [_PCB_HEADER]
    parts.append('  (net 0 "")\n')
    parts.append('  (net 1 "GND")\n')
    parts.append('  (net 2 "VCC")\n')
    parts.append('  (net 3 "SDA")\n')
    parts.append('  (net 4 "SCL")\n')
    parts.append('  (footprint "TestLib:TestPkg" (layer "F.Cu") (at 10 10)\n')
    for idx, (nid, name) in enumerate([(1, "GND"), (2, "VCC"), (3, "SDA"), (4, "SCL")]):
        x_off = idx * 2.0
        parts.append(
            f'    (pad "{idx + 1}" smd roundrect (at {x_off} 0) '
            f'(size 1.0 1.3) (layers "F.Cu" "F.Paste" "F.Mask") '
            f'(roundrect_rratio 0.25) (net {nid} "{name}"))\n'
        )
    parts.append("  )\n")
    parts.append(_PCB_FOOTER)
    return "".join(parts)


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


class TestStageInputForAutoPour:
    """Direct tests of the ``_stage_input_for_auto_pour`` helper."""

    def test_input_equals_output_returns_input_unchanged(self, tmp_path: Path):
        """Pipeline case (``kct build``): no copy when input == output."""
        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_make_pcb_with_pour_candidates())
        original_hash = _sha256(pcb)

        # Same path passed for both arguments -- pipeline mode.
        returned = _stage_input_for_auto_pour(pcb, pcb)

        assert returned == pcb
        # No copy was made; the file is byte-identical.
        assert _sha256(pcb) == original_hash

    def test_input_equals_output_via_distinct_path_objects(self, tmp_path: Path):
        """Same file referenced through two Path objects still hits the no-op branch."""
        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_make_pcb_with_pour_candidates())

        # Build a second Path object that resolves to the same file.
        same_file_via_other_object = Path(str(pcb))
        returned = _stage_input_for_auto_pour(pcb, same_file_via_other_object)

        assert returned == pcb

    def test_distinct_paths_copies_input_to_output(self, tmp_path: Path):
        """``kct route IN -o OUT`` case: input is copied to output."""
        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour

        input_pcb = tmp_path / "input.kicad_pcb"
        output_pcb = tmp_path / "output.kicad_pcb"
        input_pcb.write_text(_make_pcb_with_pour_candidates())
        input_hash = _sha256(input_pcb)
        assert not output_pcb.exists()

        returned = _stage_input_for_auto_pour(input_pcb, output_pcb)

        # Helper rebinds the working path to OUTPUT.
        assert returned == output_pcb
        # Output was created and contains identical bytes to input.
        assert output_pcb.exists()
        assert _sha256(output_pcb) == input_hash
        # Input is byte-identical (not even mtime-touched in the
        # absence of a write -- shutil.copy2 only writes to dst).
        assert _sha256(input_pcb) == input_hash


# ---------------------------------------------------------------------------
# Behavioral test: auto-pour does not mutate INPUT when staged via helper
# ---------------------------------------------------------------------------


class TestAutoPourDoesNotModifyInput:
    """Composes the helper with auto_pour_if_missing to verify input-preservation."""

    def test_input_preserved_when_output_differs(self, tmp_path: Path):
        """``kct route IN -o OUT`` (IN != OUT) leaves IN byte-identical even though
        auto-pour writes zones."""
        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        input_pcb = tmp_path / "input.kicad_pcb"
        output_pcb = tmp_path / "output.kicad_pcb"
        input_pcb.write_text(_make_pcb_with_pour_candidates())
        input_hash_before = _sha256(input_pcb)

        # Mirror the call-site pattern: stage, then auto-pour on the staged path.
        working_path = _stage_input_for_auto_pour(input_pcb, output_pcb)
        count, names = auto_pour_if_missing(working_path, quiet=True)

        # Auto-pour did create zones (the test would be trivially passing otherwise).
        assert count >= 1, "Expected auto-pour to create at least one zone for GND/VCC"
        assert set(names).issubset({"GND", "VCC"})

        # OUTPUT now contains the auto-poured zones.
        assert "(zone" in output_pcb.read_text()

        # CRITICAL: INPUT is byte-identical -- no zones leaked into the user's input.
        input_hash_after = _sha256(input_pcb)
        assert input_hash_after == input_hash_before, (
            "INPUT.kicad_pcb was modified despite -o pointing at a different file"
        )
        assert "(zone" not in input_pcb.read_text()

    def test_input_mutated_when_input_equals_output(self, tmp_path: Path):
        """Pipeline case (``kct build``): input == output, in-place auto-pour
        is the desired behavior; zones must end up in the file."""
        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text(_make_pcb_with_pour_candidates())
        hash_before = _sha256(pcb)

        # Same path for input and output -- pipeline mode.
        working_path = _stage_input_for_auto_pour(pcb, pcb)
        count, _names = auto_pour_if_missing(working_path, quiet=True)

        assert working_path == pcb
        assert count >= 1
        # The file *should* have changed (zones were added).
        assert _sha256(pcb) != hash_before
        assert "(zone" in pcb.read_text()

    def test_command_level_idempotency_across_fresh_outputs(self, tmp_path: Path):
        """Running the staged auto-pour twice on the same fresh INPUT (each time
        to a fresh OUTPUT) leaves INPUT unchanged and produces structurally
        equivalent OUTPUTs.

        Locks in command-level idempotency, which was broken before #2548:
        run 1 mutated INPUT so run 2 saw a different starting state.

        Note: zone UUIDs are random (zones/generator.py uses uuid4), so the
        outputs are not byte-identical -- we compare the *set of zoned net
        names*, which is the user-visible idempotency property."""
        import re

        from kicad_tools.cli.route_cmd import _stage_input_for_auto_pour
        from kicad_tools.router.auto_pour import auto_pour_if_missing

        input_pcb = tmp_path / "input.kicad_pcb"
        input_pcb.write_text(_make_pcb_with_pour_candidates())
        input_hash_before = _sha256(input_pcb)

        def _zoned_nets(text: str) -> set[str]:
            """Return the set of net names that have at least one zone."""
            nets: set[str] = set()
            for m in re.finditer(r'\(zone\s+.*?\(net_name\s+"([^"]+)"\)', text, re.DOTALL):
                nets.add(m.group(1))
            for m in re.finditer(r'\(zone\s[^)]*\(net\s+"([^"]+)"\)', text):
                nets.add(m.group(1))
            return nets

        # Run 1: auto-pour into a fresh output path.
        out1 = tmp_path / "out1.kicad_pcb"
        working1 = _stage_input_for_auto_pour(input_pcb, out1)
        count1, names1 = auto_pour_if_missing(working1, quiet=True)

        # INPUT must still be unchanged.
        assert _sha256(input_pcb) == input_hash_before

        # Run 2: same INPUT, different fresh output path.
        out2 = tmp_path / "out2.kicad_pcb"
        working2 = _stage_input_for_auto_pour(input_pcb, out2)
        count2, names2 = auto_pour_if_missing(working2, quiet=True)

        # INPUT *still* unchanged -- two independent runs against a
        # pristine input.
        assert _sha256(input_pcb) == input_hash_before

        # Both runs must report the same set of pour-net names.
        assert count1 == count2
        assert set(names1) == set(names2)

        # Both outputs must zone the same set of nets.
        assert _zoned_nets(out1.read_text()) == _zoned_nets(out2.read_text())
