"""Issue #3911: fleet-level enforcement of the memory-forced-unsafe-grid gate.

The unit suite (``test_grid_auto_selection.py`` /
``test_route_grid_safety_gate.py``) exercises the flag and the CLI gate on
synthetic pad sets.  This file closes the gap the judge flagged on PR #3945:
the "no regression on boards 00-04, 06, 07" claim must be enforced by a test,
not just asserted in the PR description.

It runs the *real* auto-grid selector over every committed demo-board input
PCB (the pre-route ``*.kicad_pcb`` each recipe hands to ``kct route``) at the
route default clearance (0.15mm) and asserts exactly which boards trip the
gate:

* Board 05 (bldc-motor-controller) is the ONLY board that both (a) is coerced
  onto a grid > clearance/2 by the memory budget and (b) is fine-pitch (0.5mm
  DRV8301) -- so it is the only board the gate refuses by default.  Its recipe
  passes ``--allow-unsafe-grid`` to opt in explicitly.
* Every OTHER board must leave ``memory_forced_unsafe_grid`` False, so
  ``kct route`` keeps producing their routed artifacts (the four CI board jobs
  the judge saw go red on the un-narrowed gate: board 01 / 05 / 07 end-to-end
  and match-group regression).

The selector is evaluated at BOTH the gate's default budget (500k, what
``route_cmd`` passes) and the multi-resolution routing budget (2M) so a future
budget change on either path cannot silently re-introduce the over-fire.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from kicad_tools.router.io import (
    auto_select_grid_resolution,
    extract_board_dimensions,
    extract_pad_positions,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Route default --clearance (src/kicad_tools/cli/parser.py: route --clearance).
# None of the demo recipes override it, so this is the clearance the gate sees.
ROUTE_DEFAULT_CLEARANCE = 0.15

#: (board dir, input-PCB stem, expected memory_forced_unsafe_grid).
#: Board 05 is the sole gated board (fine-pitch + memory-coerced); every other
#: board must stay False so its CI end-to-end / regression job keeps routing.
FLEET: list[tuple[str, str, bool]] = [
    ("00-simple-led", "simple_led", False),
    ("01-voltage-divider", "voltage_divider", False),
    ("02-charlieplex-led", "charlieplex_3x3", False),
    ("03-usb-joystick", "usb_joystick", False),
    ("04-stm32-devboard", "stm32_devboard", False),
    ("05-bldc-motor-controller", "bldc_controller", True),
    ("06-diffpair-test", "diffpair_test", False),
    ("07-matchgroup-test", "matchgroup_test", False),
]


def _input_pcb(board_dir: str, stem: str) -> Path:
    return REPO_ROOT / "boards" / board_dir / "output" / f"{stem}.kicad_pcb"


@pytest.mark.parametrize("budget", [500_000, 2_000_000])
@pytest.mark.parametrize("board_dir,stem,expected", FLEET)
def test_fleet_gate_signal(board_dir: str, stem: str, expected: bool, budget: int) -> None:
    """Only board 05 trips ``memory_forced_unsafe_grid`` -- at either budget."""
    pcb = _input_pcb(board_dir, stem)
    if not pcb.exists():
        pytest.skip(f"input PCB not committed: {pcb}")

    pads = extract_pad_positions(pcb)
    dims = extract_board_dimensions(pcb)
    assert dims is not None, f"{board_dir}: no board dimensions"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = auto_select_grid_resolution(
            pads=pads,
            clearance=ROUTE_DEFAULT_CLEARANCE,
            board_width=dims[0],
            board_height=dims[1],
            max_cells=budget,
        )

    assert result.memory_forced_unsafe_grid is expected, (
        f"{board_dir} @ max_cells={budget:,}: "
        f"memory_forced_unsafe_grid={result.memory_forced_unsafe_grid} "
        f"(expected {expected}); selected grid {result.resolution}mm, "
        f"clearance/2={ROUTE_DEFAULT_CLEARANCE / 2}mm, "
        f"memory_capped={result.memory_capped}. "
        "A True here on a non-05 board is the over-fire that refuses a board "
        "which routes cleanly; a False on board 05 lets the predicted shorts "
        "ship silently."
    )


def test_exactly_one_board_is_gated() -> None:
    """Guard the fleet map itself: precisely one board opts into the gate."""
    gated = [b for b, _, expected in FLEET if expected]
    assert gated == ["05-bldc-motor-controller"], (
        f"Expected board 05 to be the sole gated board, got {gated}"
    )


#: The verbatim substring of the alarming auto-grid warning (issue #3942).
#: It literally claims risk "at fine-pitch pads", so it must fire ONLY on the
#: fine-pitch memory-coerced board (05) -- never on a clean, no-fine-pitch board
#: that routes DRC-clean (boards 01 / 07 tripped it before the #3942 gate fix).
_MEMORY_CAP_WARNING = "memory budget cap forces grid"


@pytest.mark.parametrize("budget", [500_000, 2_000_000])
@pytest.mark.parametrize("board_dir,stem,expected", FLEET)
def test_fleet_memory_cap_warning_matches_gate(
    board_dir: str, stem: str, expected: bool, budget: int
) -> None:
    """Issue #3942: the alarming "may produce clearance violations at fine-pitch
    pads" warning fires iff the board actually trips the gate.

    Before the fix the warning fired on *every* memory-coerced board -- even
    boards 01 (2.54mm divider) and 07 (0.8mm match group) that carry no
    fine-pitch pads and route DRC-clean -- because it was gated on the looser
    ``grid_unsafe_by_memory_cap`` predicate instead of the ``has_fine_pitch``
    term that ``memory_forced_unsafe_grid`` carries.  The warning's own
    precondition ("at fine-pitch pads") was unmet, so users of provably-clean
    boards were told the board was at risk.  This locks the warning to the same
    condition as the gate: emitted for board 05 only.
    """
    pcb = _input_pcb(board_dir, stem)
    if not pcb.exists():
        pytest.skip(f"input PCB not committed: {pcb}")

    pads = extract_pad_positions(pcb)
    dims = extract_board_dimensions(pcb)
    assert dims is not None, f"{board_dir}: no board dimensions"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        auto_select_grid_resolution(
            pads=pads,
            clearance=ROUTE_DEFAULT_CLEARANCE,
            board_width=dims[0],
            board_height=dims[1],
            max_cells=budget,
        )

    warning_texts = [str(w.message) for w in caught]
    fired = any(_MEMORY_CAP_WARNING in t for t in warning_texts)

    assert fired is expected, (
        f"{board_dir} @ max_cells={budget:,}: memory-cap clearance warning "
        f"fired={fired} (expected {expected}). The warning claims risk 'at "
        f"fine-pitch pads' and must track the gate exactly -- a True on a "
        f"non-05 board is the #3942 false alarm that tells a DRC-clean board "
        f"it is at risk; a False on board 05 drops a genuine warning. "
        f"Warnings seen: {warning_texts}"
    )


# ---------------------------------------------------------------------------
# Issue #4875: the fleet must be structurally immune to board-derived rules
# ---------------------------------------------------------------------------
#
# #4875 lets ``kct route`` derive ``args.clearance`` from a board's own legacy
# top-level ``(net_class …)`` block.  The gate assertions above are all keyed
# to ``ROUTE_DEFAULT_CLEARANCE`` (0.15mm), so they only stay meaningful while
# no fleet board can reach that derivation path.  It cannot: this repo's PCB
# writer emits no ``net_class`` node at all, and the derivation is gated on
# one being present.  These cases enforce both halves of that argument.

#: The ``--manufacturer`` each recipe's ``kct route`` invocation actually
#: passes, or ``None`` where it passes none (boards 00 and 06 route through
#: the library API rather than the CLI).  Verified against the recipe
#: sources.  An explicit manufacturer is #4875's operator override, so these
#: invocations must resolve to the unchanged 0.15mm default too.
FLEET_MANUFACTURER: dict[str, str | None] = {
    "00-simple-led": None,
    "01-voltage-divider": None,
    "02-charlieplex-led": "jlcpcb",
    "03-usb-joystick": "jlcpcb-tier1",
    "04-stm32-devboard": "jlcpcb-tier1",
    "05-bldc-motor-controller": "jlcpcb-tier1",
    "06-diffpair-test": None,
    "07-matchgroup-test": "jlcpcb",
}


@pytest.mark.parametrize("board_dir,stem,_expected", FLEET)
def test_fleet_board_declares_no_legacy_net_class(board_dir: str, stem: str, _expected: bool):
    """No fleet input carries the token that unlocks board-derived rules."""
    from kicad_tools.cli.route_cmd import _board_declared_net_classes

    pcb = _input_pcb(board_dir, stem)
    if not pcb.exists():
        pytest.skip(f"input PCB not committed: {pcb}")

    assert "(net_class" not in pcb.read_text(errors="replace"), (
        f"{board_dir} now carries a legacy net_class block; the 0.15mm "
        "clearance every assertion in this file assumes no longer holds."
    )
    assert _board_declared_net_classes(pcb) == {}


@pytest.mark.parametrize("board_dir,stem,_expected", FLEET)
def test_fleet_clearance_is_unchanged_by_rule_derivation(
    board_dir: str, stem: str, _expected: bool
):
    """``args.clearance`` still resolves to 0.15mm for every fleet recipe.

    Run both with no flags and with the recipe's real ``--manufacturer``, so
    neither #4875 branch (board-derived, or manufacturer-as-override) can
    silently move a fleet board off the default the gate map is keyed to.
    """
    from types import SimpleNamespace

    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    pcb = _input_pcb(board_dir, stem)
    if not pcb.exists():
        pytest.skip(f"input PCB not committed: {pcb}")

    mfr = FLEET_MANUFACTURER[board_dir] or "jlcpcb"
    invocations: list[list[str]] = [
        [str(pcb)],
        [str(pcb), "--manufacturer", mfr],
    ]

    for argv in invocations:
        args = SimpleNamespace(
            clearance=ROUTE_DEFAULT_CLEARANCE,
            manufacturer=mfr,
            quiet=True,
        )
        resolved = _resolve_route_clearance(args, pcb, argv, quiet=True)
        assert resolved == ROUTE_DEFAULT_CLEARANCE, (
            f"{board_dir} @ {argv}: clearance moved to {resolved}mm. "
            "Every gate expectation in this file (and the committed routed "
            "artifacts) assumes the flat 0.15mm route default."
        )
        assert args.clearance == ROUTE_DEFAULT_CLEARANCE
        assert args._clearance_rule_source == "default"
