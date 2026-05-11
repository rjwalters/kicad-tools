"""CLI tests for ``kct check --only/--skip diffpair_length_skew``.

Verifies the new category id is wired into ``CHECK_CATEGORIES`` and the
``check_methods`` dispatch dict at ``cli/check_cmd.py``.

The rule is a no-op when invoked from the standalone CLI (no router
context supplies per-pair skew data), so the CLI integration test
asserts:

- Exit 0 when ``--only diffpair_length_skew`` is used against a board
  with no router context (the rule reports nothing, so no errors).
- The category id appears in the ``CHECK_CATEGORIES`` list and is
  accepted by both ``--only`` and ``--skip`` argparse paths.

The "rule fires" assertions live in
``test_validate_diffpair_length_skew.py`` where the rule can be
constructed with explicit ``skew_data`` and ``engaged_pairs`` sets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Minimal valid PCB with USB_D+/USB_D- nets.  The CLI cannot today
# construct a ``skew_data`` dict (there is no router-side context on
# disk to populate it), so the rule is a no-op and the CLI exits clean.
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


class TestDiffPairLengthSkewCLI:
    """CLI-level wiring tests for the new check category."""

    def test_category_in_check_categories_list(self):
        """``"diffpair_length_skew"`` is registered in CHECK_CATEGORIES."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "diffpair_length_skew" in CHECK_CATEGORIES

    def test_only_diffpair_length_skew_runs_clean(self, minimal_diffpair_pcb: Path, capsys):
        """Standalone CLI with --only flag -> exit 0 (no router context).

        Today the CLI does not thread a ``skew_data`` dict into the
        rule, so the rule is a conservative no-op regardless of the
        board's routed geometry.  This test asserts the wiring is
        present (the category id is dispatched) and the run exits
        cleanly.  When the router-side wiring lands as a follow-up
        issue, this test will be updated to assert the new fire-path.

        This is also the "Freerouting / external router" graceful-
        degradation case from the issue body: a board that was routed
        externally (no kicad-tools tracker context) MUST NOT have
        spurious skew violations -- because the caller had no way to
        compute the skew.
        """
        from kicad_tools.cli.check_cmd import main

        result = main([str(minimal_diffpair_pcb), "--only", "diffpair_length_skew"])
        # 0 = passed (no violations reported because no skew_data).
        assert result == 0

    def test_skip_diffpair_length_skew_runs_clean(self, minimal_diffpair_pcb: Path, capsys):
        """--skip diffpair_length_skew is accepted by the CLI.

        Verifies that ``--skip diffpair_length_skew`` is a recognized
        category id (no argparse error).
        """
        from kicad_tools.cli.check_cmd import main

        # The CLI accepts the flag and runs to completion (any int
        # exit code is OK -- we're only validating that ``--skip
        # diffpair_length_skew`` is a recognized category id).
        result = main([str(minimal_diffpair_pcb), "--skip", "diffpair_length_skew"])
        assert isinstance(result, int)

    def test_unknown_category_rejected_by_only(self, minimal_diffpair_pcb: Path):
        """Unknown category id is rejected with exit code 1.

        Regression guard: ensures the ``CHECK_CATEGORIES`` validation
        path is wired correctly (it should still reject typos like
        ``diffpair_skew`` -- without the underscore-length-skew).
        """
        from kicad_tools.cli.check_cmd import main

        # ``diffpair_skew`` (without the underscore-length-) is NOT a
        # valid category id.
        result = main([str(minimal_diffpair_pcb), "--only", "diffpair_skew"])
        assert result == 1
