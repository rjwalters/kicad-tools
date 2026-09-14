"""Fleet auto-grid safety: usable safe candidates must precede refusal.

The historical Board05 geometry formerly selected a coarse aligned grid and
tripped the gate despite an affordable clearance-safe candidate. It must now
select the safe grid; genuine no-safe-candidate refusal remains covered by
synthetic selector and CLI gate tests. Other fleet boards retain their prior
ungated behavior at both supported cell budgets.
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
#: Board05 now uses its affordable safe candidate; no fixture needs refusal.
FLEET: list[tuple[str, str, bool]] = [
    ("00-simple-led", "simple_led", False),
    ("01-voltage-divider", "voltage_divider", False),
    ("02-charlieplex-led", "charlieplex_3x3", False),
    ("03-usb-joystick", "usb_joystick", False),
    ("04-stm32-devboard", "stm32_devboard", False),
    ("05-bldc-motor-controller", "bldc_controller", False),
    ("06-diffpair-test", "diffpair_test", False),
    ("07-matchgroup-test", "matchgroup_test", False),
]


def _input_pcb(board_dir: str, stem: str) -> Path:
    # These exact gate expectations describe the pre-redesign pad geometry.
    root = (
        REPO_ROOT / "tests/fixtures/historical_demo_boards"
        if board_dir in {"05-bldc-motor-controller", "07-matchgroup-test"}
        else REPO_ROOT / "boards"
    )
    return root / board_dir / "output" / f"{stem}.kicad_pcb"


@pytest.mark.parametrize("budget", [500_000, 2_000_000])
@pytest.mark.parametrize("board_dir,stem,expected", FLEET)
def test_fleet_gate_signal(board_dir: str, stem: str, expected: bool, budget: int) -> None:
    """Keep fleet routes ungated and historical Board05 on an affordable safe grid."""
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
    )
    if board_dir == "05-bldc-motor-controller":
        assert result.resolution <= ROUTE_DEFAULT_CLEARANCE / 2
        assert dims[0] * dims[1] / result.resolution**2 <= result.memory_budget_used


#: The verbatim substring of the alarming auto-grid warning (issue #3942).
#: It literally claims risk "at fine-pitch pads", so it must fire ONLY on the
#: fine-pitch memory-coerced board -- never on an affordable safe selection
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
        f"fine-pitch pads' and must track the selected grid's gate. "
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
