"""CLI tests for ``kct check --only/--skip diffpair_clearance_intra``.

Verifies the new category id is wired into ``CHECK_CATEGORIES`` and the
``check_methods`` dispatch dict at ``cli/check_cmd.py``.

Exit-code expectations:

- Exit 2 (errors found) when the rule fires.  Default severity is
  ``error``, so this happens automatically once a violation is emitted.
- Exit 0 when ``--skip diffpair_clearance_intra`` removes the only
  violation source AND the same-pair skip in ``ClearanceRule`` already
  drops the corresponding inter-pair clearance violation.

The fixture stages two ``USB_D+`` / ``USB_D-`` segments separated by 0.07 mm
edge-to-edge.  The default JLCPCB inter-pair ``min_clearance_mm`` is
0.1 mm (above 0.07), so without the new rule the ``ClearanceRule``
would fire here.  With the new rule installed:

  - ``ClearanceRule`` is suppressed because the two nets are a detected
    diff pair (same-pair skip).
  - ``DiffPairClearanceIntraRule`` fires using the manufacturer's
    ``min_clearance_mm`` as its fallback threshold (no per-pair map is
    plumbed at the CLI yet -- that will follow in #2559).

So the net effect is the same exit code (2), but the violation now
reports against ``rule_id == "diffpair_clearance_intra"`` instead of
``"clearance_segment_segment"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# A board with two USB_D+/USB_D- segments at a tight 0.07 mm edge-to-edge
# gap.  Default JLCPCB min_clearance_mm is 0.1 mm, so this fires.
DIFFPAIR_TIGHT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "USB_D+")
  (net 2 "USB_D-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 120.27) (end 140 120.27) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


@pytest.fixture
def diffpair_tight_pcb(tmp_path: Path) -> Path:
    """A PCB with USB_D+/USB_D- segments at a tighter-than-min_clearance gap."""
    pcb_file = tmp_path / "diffpair_tight.kicad_pcb"
    pcb_file.write_text(DIFFPAIR_TIGHT_PCB)
    return pcb_file


class TestDiffPairClearanceIntraCLI:
    """CLI-level wiring tests for the new check category."""

    def test_only_diffpair_returns_exit_2(self, diffpair_tight_pcb: Path, capsys):
        """``--only diffpair_clearance_intra`` -> exit 2 when the rule fires."""
        from kicad_tools.cli.check_cmd import main

        result = main([str(diffpair_tight_pcb), "--only", "diffpair_clearance_intra"])
        assert result == 2  # errors found

        captured = capsys.readouterr()
        assert "diffpair_clearance_intra" in captured.out

    def test_skip_diffpair_returns_exit_0(self, diffpair_tight_pcb: Path, capsys):
        """``--skip diffpair_clearance_intra`` -> exit 0.

        With the new rule skipped, ClearanceRule's same-pair skip means
        the inter-pair clearance violation is also suppressed, so the
        run is clean.
        """
        from kicad_tools.cli.check_cmd import main

        # Issue #3750: tmp PCB has no schematic, so the meta rollup is
        # INCOMPLETE; ``--allow-incomplete`` preserves the rule-only
        # assertion (no violations -> 0).
        result = main(
            [
                str(diffpair_tight_pcb),
                "--skip",
                "diffpair_clearance_intra",
                "--allow-incomplete",
            ]
        )
        # No violations reach the user.
        assert result == 0

    def test_only_diffpair_json_carries_correct_type(self, diffpair_tight_pcb: Path, capsys):
        """JSON ``type`` field round-trips to ``diffpair_clearance_intra``.

        This is the cross-check for the to_dict() round-trip trap at the
        CLI level: if the alias entry in drc/violation.py were missing,
        the fuzzy fallback would silently miscategorize the type as
        ``"clearance"`` (because the rule_id contains the substring).
        """
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(diffpair_tight_pcb),
                "--only",
                "diffpair_clearance_intra",
                "--format",
                "json",
            ]
        )
        assert result == 2

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["summary"]["passed"] is False
        assert data["summary"]["errors"] >= 1
        # At least one violation has the EXACT type string.
        types = {v["type"] for v in data["violations"]}
        assert "diffpair_clearance_intra" in types

    def test_errors_only_with_diffpair_returns_exit_2(self, diffpair_tight_pcb: Path, capsys):
        """``--errors-only`` with the new rule firing returns exit 2.

        Default severity is ``error`` so ``--errors-only`` keeps the
        violations and the exit code is 2.
        """
        from kicad_tools.cli.check_cmd import main

        result = main(
            [
                str(diffpair_tight_pcb),
                "--only",
                "diffpair_clearance_intra",
                "--errors-only",
            ]
        )
        assert result == 2
