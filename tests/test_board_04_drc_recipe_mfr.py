"""Regression guard: board-04 ``generate_design.py`` runs ``kct check``
against the ``jlcpcb-tier1`` manufacturer profile.

Issue #3208.  Board 04 (STM32 devboard) ships against ``jlcpcb-tier1``
(see ``.github/routed-drc-tolerance.yml``'s ``manufacturers:`` override
for ``boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb``)
because its GND stitching uses ``--micro-via`` in-pad vias that the
default ``jlcpcb`` (tier-0) profile forbids.  Before #3208,
``generate_design.py::run_drc`` invoked ``kct check <pcb>`` with no
``--mfr`` argument, so the local recipe summary defaulted to tier-0 and
reported 3 spurious ``via_in_pad`` DRC errors -- errors the CI gate did
NOT see (CI passes ``--mfr jlcpcb-tier1`` per the tolerance YAML).

The fix is local to the recipe: hardcode ``"--mfr", "jlcpcb-tier1"`` in
the ``subprocess.run([...])`` argv that invokes ``kct check``.  This
test pins the post-fix invocation by scanning the recipe source so a
future hand-edit that drops the flag (or switches to a different tier)
trips immediately at PR time.

This is a precedent-mirror of the #3150 fix for board 03's recipe at
``boards/03-usb-joystick/generate_design.py``; board 03's
``run_drc`` carries the same hardcoded ``--mfr jlcpcb-tier1`` argv,
which we also pin here so the precedent itself is regression-guarded.

References:
- Issue #3208 -- the board-04 gap and curator-recommended fix.
- Issue #3150 / board-03 -- the precedent the fix mirrors.
- ``.github/routed-drc-tolerance.yml`` -- the per-board
  ``manufacturers:`` override block that declares both boards against
  ``jlcpcb-tier1`` and is the authoritative source of truth the recipe
  is being aligned to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_04_RECIPE = REPO_ROOT / "boards" / "04-stm32-devboard" / "generate_design.py"
BOARD_03_RECIPE = REPO_ROOT / "boards" / "03-usb-joystick" / "generate_design.py"


def _find_run_drc_block(source: str) -> str:
    """Return the source text of the ``run_drc`` function definition.

    Brittle-on-purpose: if ``run_drc`` is renamed or relocated, this test
    should fail loudly so we re-pin against the new entry point rather
    than silently passing on an unrelated argv.
    """
    match = re.search(
        r"def\s+run_drc\b[^\n]*:\s*\n(?P<body>(?:[ \t]+[^\n]*\n|[ \t]*\n)+)",
        source,
    )
    assert match is not None, (
        "Could not locate the ``run_drc`` function in the recipe.  "
        "If the function was renamed or refactored, update this test to "
        "match the new entry point.  This pin exists because the kct "
        "check invocation must continue to pass --mfr jlcpcb-tier1 "
        "(issue #3208 / board-03 #3150)."
    )
    return match.group("body")


def _assert_run_drc_passes_tier1(recipe_path: Path, issue_ref: str) -> None:
    """Shared assertion: ``run_drc`` argv to ``kct check`` carries ``--mfr jlcpcb-tier1``.

    Uses a source-text scan rather than executing the recipe so the test
    runs in milliseconds and produces a precise failure message pointing
    at the offending recipe.
    """
    assert recipe_path.exists(), (
        f"Recipe file missing: {recipe_path}.  This pin assumes the "
        f"board recipe lives at this path; update the constant if the "
        f"board was relocated."
    )

    source = recipe_path.read_text()
    body = _find_run_drc_block(source)

    # The argv must contain "check" followed (with possibly other tokens
    # in between, e.g. ``str(pcb_path)``) by "--mfr" and "jlcpcb-tier1".
    # We use a soft check that the three relevant strings all appear in
    # the run_drc body in a "check" subprocess invocation -- not a strict
    # ordering check -- so reasonable code-style variations (e.g.,
    # listing args on multiple lines) don't break the pin.
    assert '"check"' in body, (
        f"{recipe_path.name}::run_drc no longer invokes the ``check`` "
        f"subcommand.  {issue_ref} pinned this invocation; if you "
        f"intentionally switched to a different DRC entry point, update "
        f"this test."
    )
    assert '"--mfr"' in body, (
        f"{recipe_path.name}::run_drc is missing the ``--mfr`` argument "
        f"in its kct check invocation.  {issue_ref} requires this flag "
        f"so the local recipe DRC summary matches the CI gate (which "
        f"reads the manufacturer from .github/routed-drc-tolerance.yml). "
        f"Without it, kct check defaults to the jlcpcb (tier-0) profile "
        f"and reports spurious via_in_pad errors on a board that ships "
        f"with --micro-via in-pad vias."
    )
    assert '"jlcpcb-tier1"' in body, (
        f"{recipe_path.name}::run_drc no longer passes the "
        f"``jlcpcb-tier1`` manufacturer tier to kct check.  {issue_ref} "
        f"pinned this tier explicitly because the board's "
        f".github/routed-drc-tolerance.yml entry declares it against "
        f"jlcpcb-tier1.  If the board was retargeted to a different tier "
        f"(e.g., jlcpcb-tier2 or a different fab), update both this test "
        f"AND the tolerance YAML manufacturers: block in lockstep."
    )


def test_board_04_run_drc_passes_jlcpcb_tier1() -> None:
    """Issue #3208: board 04 ``run_drc`` must pass ``--mfr jlcpcb-tier1``.

    Acceptance criterion: ``boards/04-stm32-devboard/generate_design.py``'s
    ``run_drc`` function invokes ``kct check`` with the
    ``--mfr jlcpcb-tier1`` flag.  Without this flag, the local recipe
    summary defaults to tier-0 jlcpcb and reports 3 spurious
    ``via_in_pad`` errors on the board's --micro-via GND stitching --
    errors the CI gate (which reads ``jlcpcb-tier1`` from
    ``.github/routed-drc-tolerance.yml``) does not see.

    This is the #3208 fix's primary regression guard.  A future hand-edit
    that removes the ``--mfr`` flag (e.g., during a recipe cleanup) would
    silently reintroduce the local/CI divergence; this test prevents
    that.
    """
    _assert_run_drc_passes_tier1(BOARD_04_RECIPE, "Issue #3208")


def test_board_03_run_drc_passes_jlcpcb_tier1() -> None:
    """Issue #3150 precedent guard: board 03 ``run_drc`` keeps ``--mfr jlcpcb-tier1``.

    Board 03's recipe was fixed in #3150 with the same one-line pattern
    that #3208 applies to board 04.  Pinning both recipes here means a
    future cleanup that drops the flag from either board surfaces in the
    same place, with a single explanation of why both boards need it.

    If board 03 is ever retargeted off jlcpcb-tier1 (e.g., a new fab) the
    canonical sibling update is the ``manufacturers:`` block in
    ``.github/routed-drc-tolerance.yml`` -- only then should this test
    be updated.
    """
    _assert_run_drc_passes_tier1(BOARD_03_RECIPE, "Issue #3150")


def test_routed_drc_tolerance_yaml_still_declares_board_04_tier1() -> None:
    """The recipe pin is meaningful only if the tolerance YAML still
    declares board 04 against jlcpcb-tier1.

    If the YAML is changed (e.g., the board is retargeted to a different
    fab), this test should fail FIRST -- forcing the maintainer to look
    at the recipe pin above and update both in lockstep.  Without this
    sibling assertion, a YAML-only retarget would leave the recipe pinned
    to the wrong tier and the local DRC summary would once again drift
    from the CI gate.
    """
    tolerance_yaml = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"
    assert tolerance_yaml.exists(), (
        f"Tolerance YAML missing at {tolerance_yaml}; the CI gate cannot "
        f"function without it.  Restore the file or update this test."
    )

    text = tolerance_yaml.read_text()
    expected_line = "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb: jlcpcb-tier1"
    assert expected_line in text, (
        f"{tolerance_yaml.name} no longer declares board 04 against "
        f"jlcpcb-tier1 in its manufacturers: block.  If the board was "
        f"retargeted to a different manufacturer or tier, also update "
        f"``boards/04-stm32-devboard/generate_design.py::run_drc`` to "
        f"pass the same tier to ``kct check`` -- otherwise the recipe's "
        f"local DRC summary will drift from the CI gate (the bug #3208 "
        f"was opened to fix)."
    )


if __name__ == "__main__":  # pragma: no cover -- manual debugging convenience.
    pytest.main([__file__, "-v"])
