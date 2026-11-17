"""Layer-policy contracts for current demo designs and archived routers.

Issue #3402's escalation audit applied to the historical routing fixtures.
The real board-05/06/07 redesigns instead declare reviewed physical layer
counts; board 07 is now six-layer SDRAM hardware. Boards 08/09 declare
four-layer construction. A preferred fabrication layer count is not an
``EscalationPolicy.starting_layers`` setting, and these tests keep that
separation explicit rather than inventing an autorouter policy for them.

The archived board-06/07 specs retain the original default/4L escalation
regressions. Schema coverage below preserves board 05's former 4L opt-in
without tying it to the unrelated revision-B circuit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.spec.parser import load_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS_DIR = REPO_ROOT / "boards"

# Designs still using the original default 2L-probe policy.
DEFAULT_PROBE_BOARDS = (
    "00-simple-led",
    "01-voltage-divider",
    "02-charlieplex-led",
    "03-usb-joystick",
    "04-stm32-devboard",
)

# Reviewed real construction, not a claim about autorouter escalation.
CONSTRUCTION_LAYERS = {
    "05-bldc-motor-controller": 4,
    "06-diffpair-test": 4,
    "07-matchgroup-test": 6,
    "08-precision-acquisition": 4,
    "09-usbc-pd-power": 4,
}


@pytest.mark.parametrize("board", DEFAULT_PROBE_BOARDS)
def test_default_probe_policy(board: str) -> None:
    manufacturing = load_spec(BOARDS_DIR / board / "project.kct").requirements.manufacturing
    escalation = manufacturing.escalation if manufacturing is not None else None
    assert escalation is None or escalation.starting_layers == 2


@pytest.mark.parametrize("board,expected", sorted(CONSTRUCTION_LAYERS.items()))
def test_reviewed_construction_layers(board: str, expected: int) -> None:
    manufacturing = load_spec(BOARDS_DIR / board / "project.kct").requirements.manufacturing
    assert manufacturing.layers["preferred"] == expected
    # Fixed reviewed copper does not inherit the retired synthetic route policy.
    assert manufacturing.escalation is None


@pytest.mark.parametrize("board,expected", [("06-diffpair-test", 2), ("07-matchgroup-test", 4)])
def test_archived_fixture_retains_escalation_audit(board: str, expected: int) -> None:
    path = BOARDS_DIR / board / "regression-fixture" / "project.kct"
    escalation = load_spec(path).requirements.manufacturing.escalation
    assert (escalation.starting_layers if escalation is not None else 2) == expected


def test_explicit_four_layer_opt_in_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "project.kct"
    path.write_text(
        'kct_version: "1.0"\nproject:\n  name: Four-layer route fixture\n'
        "requirements:\n  manufacturing:\n    escalation:\n      starting_layers: 4\n"
    )
    assert load_spec(path).requirements.manufacturing.escalation.starting_layers == 4


def test_all_demo_boards_present() -> None:
    """New numbered demos need an explicit policy or construction contract."""
    on_disk = {
        p.name
        for p in BOARDS_DIR.iterdir()
        if p.is_dir() and p.name[:2].isdigit() and (p / "project.kct").is_file()
    }
    covered = set(DEFAULT_PROBE_BOARDS) | set(CONSTRUCTION_LAYERS)
    assert on_disk == covered, (
        "Demo board layer contracts need updating: "
        f"uncovered={sorted(on_disk - covered)}, missing={sorted(covered - on_disk)}"
    )
