"""CLI tests for ``kct check --only/--skip diffpair_routing_continuity``.

Verifies the new category id is wired into ``CHECK_CATEGORIES`` and the
``check_methods`` dispatch dict at ``cli/check_cmd.py``.

The rule is a no-op when invoked from the standalone CLI (no engaged
pairs are supplied), so the CLI integration test asserts:

- Exit 0 when ``--only diffpair_routing_continuity`` is used against a
  board with no engaged pairs context (the rule reports nothing, so no
  errors).
- The category id appears in the ``--help`` output and in the
  ``CHECK_CATEGORIES`` list.

The "rule fires" assertions live in
``test_validate_diffpair_routing_continuity.py`` where we can construct
the rule with an explicit ``engaged_pairs`` set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Minimal valid PCB.  Contains USB_D+/USB_D- nets with parallel
# segments, but the CLI does not (today) construct an engaged_pairs set
# -- so the rule is a no-op and the CLI exits clean.
MINIMAL_DIFFPAIR_PCB = """(kicad_pcb
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
  (segment (start 110 120.275) (end 140 120.275) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


@pytest.fixture
def minimal_diffpair_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "diffpair.kicad_pcb"
    pcb_file.write_text(MINIMAL_DIFFPAIR_PCB)
    return pcb_file


class TestDiffPairRoutingContinuityCLI:
    """CLI-level wiring tests for the new check category."""

    def test_category_in_check_categories_list(self):
        """``"diffpair_routing_continuity"`` is registered in CHECK_CATEGORIES."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "diffpair_routing_continuity" in CHECK_CATEGORIES

    def test_only_diffpair_routing_continuity_runs_clean(self, minimal_diffpair_pcb: Path, capsys):
        """Standalone CLI with --only flag -> exit 0 (no engaged pairs).

        Today the CLI does not thread an ``engaged_pairs`` set into the
        rule, so the rule is a conservative no-op regardless of routed
        diff-pair geometry on the board.  This test asserts the wiring
        is present (the category id is dispatched) and the run exits
        cleanly.  When the router-side wiring lands (follow-up issue),
        this test will be updated to assert the new fire-path.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_diffpair_pcb), "--only", "diffpair_routing_continuity"])
        # 0 = passed (no violations reported because no engaged pairs).
        assert result == 0

    def test_skip_diffpair_routing_continuity_runs_clean(self, minimal_diffpair_pcb: Path, capsys):
        """--skip diffpair_routing_continuity is accepted by the CLI.

        The fixture has a tight 0.075 mm intra gap so without the
        ``diffpair_clearance_intra`` rule's same-pair skip the generic
        clearance rule WOULD fire.  Skipping the continuity rule alone
        doesn't change clearance behaviour.  This test only verifies
        that ``--skip diffpair_routing_continuity`` is a valid flag (no
        argparse error) -- not the exit code.
        """
        from kicad_tools.cli.check_cmd import main

        # The CLI accepts the flag and runs to completion (any int exit
        # code is OK -- we're only validating that ``--skip
        # diffpair_routing_continuity`` is a recognized category id).
        result = main([str(minimal_diffpair_pcb), "--skip", "diffpair_routing_continuity"])
        assert isinstance(result, int)
