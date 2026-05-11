"""CLI tests for ``kct check --only/--skip match_group_length_skew``.

Verifies the new category id is wired into ``CHECK_CATEGORIES`` and the
``check_methods`` dispatch dict at ``cli/check_cmd.py``.

The rule is a graceful-no-op when invoked from the standalone CLI (no
router context supplies per-group skew data, since the Phase 2.5G
producer wiring is a separate follow-up).  So the CLI integration test
asserts:

- Exit 0 when ``--only match_group_length_skew`` is used against a board
  with no router context (rule reports nothing -> no errors).
- The category id appears in the ``CHECK_CATEGORIES`` list and is
  accepted by both ``--only`` and ``--skip`` argparse paths.

The "rule fires" assertions live in
``test_validate_match_group_length_skew.py`` where the rule can be
constructed with explicit ``group_skew_data`` and
``tracker_match_groups`` arguments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Minimal valid PCB.  The CLI cannot today construct a
# ``group_skew_data`` dict (no producer-side wiring), so the rule is a
# no-op and the CLI exits clean -- this is the AC #1 graceful-
# degradation contract.
MINIMAL_PCB = """(kicad_pcb
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
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)
"""


@pytest.fixture
def minimal_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "min.kicad_pcb"
    pcb_file.write_text(MINIMAL_PCB)
    return pcb_file


class TestMatchGroupLengthSkewCLI:
    """CLI-level wiring tests for the new check category."""

    def test_category_in_check_categories_list(self):
        """``"match_group_length_skew"`` is registered in CHECK_CATEGORIES."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "match_group_length_skew" in CHECK_CATEGORIES

    def test_only_match_group_length_skew_no_router_context(self, minimal_pcb: Path):
        """Standalone CLI with --only flag -> exit 0 (graceful no-op).

        Today the CLI does not thread a ``group_skew_data`` dict into
        the rule, so the rule is a conservative no-op regardless of
        the board's routed geometry.  This test asserts the wiring is
        present (the category id is dispatched) and the run exits
        cleanly.  When the Phase 2.5G producer-side wiring lands as a
        follow-up issue, this test will be updated to assert the new
        fire-path.

        This is the explicit "Freerouting / external router" graceful-
        degradation case from the issue body: a board that was routed
        externally (no kicad-tools tracker context) MUST NOT have
        spurious skew violations because the caller had no way to
        compute the skew.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_pcb), "--only", "match_group_length_skew"])
        # 0 = passed (no violations reported because no group_skew_data).
        assert result == 0

    def test_skip_match_group_length_skew_accepted(self, minimal_pcb: Path):
        """--skip match_group_length_skew is accepted by the CLI.

        Verifies that ``--skip match_group_length_skew`` is a
        recognized category id (no argparse error).
        """
        from kicad_tools.cli.check_cmd import main

        # The CLI accepts the flag and runs to completion (any int
        # exit code is OK -- we're only validating that ``--skip
        # match_group_length_skew`` is a recognized category id).
        result = main([str(minimal_pcb), "--skip", "match_group_length_skew"])
        assert isinstance(result, int)
        # The non-error exit codes are 0 (no violations) or 2 (other
        # violations from other categories on the minimal PCB).  Both
        # are acceptable here -- we're not asserting on the other
        # categories' behaviour, only that the new --skip arg parses.
        assert result in (0, 2)

    def test_unknown_category_rejected_by_only(self, minimal_pcb: Path):
        """Unknown category id is rejected with exit code 1.

        Regression guard: ensures the ``CHECK_CATEGORIES`` validation
        path is wired correctly (it should still reject typos like
        ``match_group_skew`` -- without the ``_length_``).
        """
        from kicad_tools.cli.check_cmd import main

        # ``match_group_skew`` (without ``_length_``) is NOT a valid
        # category id.
        result = main([str(minimal_pcb), "--only", "match_group_skew"])
        assert result == 1
