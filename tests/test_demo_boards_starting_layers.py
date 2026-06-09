"""Per-board ``starting_layers`` audit (Issue #3402).

PR #3405 (Issue #3400) added ``EscalationPolicy.starting_layers`` so a
board can opt out of the 2L probe when 2L cannot meet the
"manufacturable = 100% LVS + 0 DRC" bar (user direction 2026-06-09).

Issue #3402 audited every demo board (``boards/00`` ... ``boards/07``)
against that bar and set ``starting_layers`` per board:

  - Boards 00, 01, 02 stay at the default (2L probe enabled): they
    route 100% with 0 DRC on 2L in production.
  - Boards 03 and 04 also stay at the default: their 2L reach gap is
    a *placement / topology* gap, not a layer-count gap (4L doesn't
    improve completion).  Residual gaps tracked as separate issues.
  - Boards 05, 06, 07 set ``starting_layers: 4``: 06 and 07 are
    inherent 4-layer PCBs (inner GND / PWR planes), and 05 (DRV8301 +
    STM32G431 + 3-phase power) shows a material reach gap at 2L vs
    4L (46% vs 60%+) — probing 2L is a waste of routing budget.

This regression test pins the audit decisions so a future spec edit
that silently flips a board's starting layer (e.g., dropping ``05``
back to the default 2L probe) is caught loudly.

The test loads each board's ``project.kct`` through the spec parser
and asserts the resolved ``EscalationPolicy.starting_layers`` value
matches the audit table.  It does NOT re-route — that's covered by
the per-board routing tests already in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.spec.parser import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS_DIR = REPO_ROOT / "boards"


# Audit table from Issue #3402 (2026-06-09).  ``None`` means no
# ``escalation`` block (default 2L probe), an int means an explicit
# ``starting_layers`` value in the spec.
AUDIT_TABLE: dict[str, int | None] = {
    "00-simple-led": None,
    "01-voltage-divider": None,
    "02-charlieplex-led": None,
    "03-usb-joystick": None,
    "04-stm32-devboard": None,
    "05-bldc-motor-controller": 4,
    "06-diffpair-test": 4,
    "07-matchgroup-test": 4,
}


@pytest.mark.parametrize("board,expected", sorted(AUDIT_TABLE.items()))
def test_starting_layers_matches_audit(board: str, expected: int | None) -> None:
    """``EscalationPolicy.starting_layers`` matches the Issue #3402 audit.

    For boards in the "stays at default" group the test asserts the
    ``escalation`` block is absent (or its ``starting_layers`` field
    is the schema default ``2``).  For the 4L-opt-in group it asserts
    the explicit value is present.
    """
    spec_path = BOARDS_DIR / board / "project.kct"
    assert spec_path.is_file(), f"Missing project.kct for {board}: {spec_path}"

    spec = load_spec(spec_path)
    requirements = getattr(spec, "requirements", None)
    manufacturing = (
        getattr(requirements, "manufacturing", None) if requirements else None
    )
    escalation = (
        getattr(manufacturing, "escalation", None) if manufacturing else None
    )

    if expected is None:
        # Default group: either no escalation block, or escalation
        # present but starting_layers at the schema default (2).
        if escalation is None:
            return
        assert escalation.starting_layers == 2, (
            f"{board}: per Issue #3402 audit this board should keep the "
            f"default 2L probe, but its project.kct now sets "
            f"starting_layers={escalation.starting_layers}.  If the audit "
            f"decision changed, update AUDIT_TABLE in this test."
        )
    else:
        assert escalation is not None, (
            f"{board}: per Issue #3402 audit this board should declare "
            f"starting_layers={expected}, but project.kct has no "
            f"requirements.manufacturing.escalation block."
        )
        assert escalation.starting_layers == expected, (
            f"{board}: per Issue #3402 audit starting_layers should be "
            f"{expected}, got {escalation.starting_layers}.  If the audit "
            f"decision changed, update AUDIT_TABLE in this test."
        )


def test_all_demo_boards_present() -> None:
    """Sanity: every demo board directory is covered by the audit.

    Guards against a new ``boards/0N-...`` being added without the
    Issue #3402 audit decision being recorded here.
    """
    on_disk = sorted(
        p.name
        for p in BOARDS_DIR.iterdir()
        if p.is_dir() and p.name[:2].isdigit() and (p / "project.kct").is_file()
    )
    audited = sorted(AUDIT_TABLE.keys())
    assert on_disk == audited, (
        "Demo boards on disk diverge from the Issue #3402 audit table.\n"
        f"  On disk:  {on_disk}\n"
        f"  Audited:  {audited}\n"
        "Add the new board to AUDIT_TABLE in this test (and pick a "
        "starting_layers value per the methodology in Issue #3402)."
    )
