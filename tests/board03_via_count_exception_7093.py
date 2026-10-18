"""Board 03's single documented diff-pair exception (Issue #7093). REMOVE WHEN #7093 LANDS.

Operator ruling on #6394 (2026-10-18): board 03 ships the copper re-derived
from a fresh lattice route.  The router gives that board's ``USB_D`` pair
unequal via counts (USB_D+ 2, USB_D- 3): the J1->U2 leg falls back to
uncoupled routing and USB_D- takes one F.Cu<->B.Cu hop there that USB_D+ does
not.  That is router bug #7093.  Until it lands, exactly ONE finding is
excepted:

* board ``boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb``;
* rule ``diffpair_uncoupled_length``, via-count-mismatch finding only
  (``VIA_UNEQUAL_PHRASE``), at ``warning`` severity;
* pair ``USB_D+``/``USB_D-``, with exactly the measured counts 2 / 3.

Every other finding still fails the tests that use this module: an uncoupled
length or per-terminal finding on the same pair, a via-limit finding, any
finding on another pair or board, any change to the counts, and any error.
``tests/test_board03_via_count_exception_7093.py`` pins that narrowness.

When #7093 lands, re-derive board 03, delete this module and its callers'
filters, and the four tests go back to "no warning at all".
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from kicad_tools.validate.rules.diffpair_uncoupled_length import VIA_UNEQUAL_PHRASE

TRACKING_ISSUE = 7093
BOARD = "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"
RULE_ID = "diffpair_uncoupled_length"
PAIR = ("USB_D+", "USB_D-")
SEVERITY = "warning"
MESSAGE = f"Differential pair USB_D+/USB_D- has {VIA_UNEQUAL_PHRASE} (USB_D+: 2, USB_D-: 3)"


def _board_rel(board: str | Path) -> str:
    path = Path(board)
    if path.is_absolute():
        repo = Path(__file__).resolve().parent.parent
        try:
            path = path.resolve().relative_to(repo)
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def is_excepted(board: str | Path, violation) -> bool:
    """True only for board 03's USB_D via-count-mismatch warning (#7093)."""
    return (
        _board_rel(board) == BOARD
        and violation.rule_id == RULE_ID
        and violation.severity == SEVERITY
        and violation.message == MESSAGE
        and tuple(sorted(violation.nets)) == PAIR
        and tuple(sorted(violation.items)) == PAIR
    )


def without_exception(board: str | Path, violations: Iterable) -> list:
    """*violations* minus the one #7093 finding; everything else is kept."""
    return [v for v in violations if not is_excepted(board, v)]
