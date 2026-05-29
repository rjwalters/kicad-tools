"""Regression guard: board-04 schematic ERC error count stays at 0.

Issue #3149.  Board 04 (STM32 devboard) shipped with 11 long-standing
KiCad ERC errors (the fleet-audit "0 -> 11" delta was a false regression:
the M-C closure narrative recorded an inaccurate "ERC: 0 PASS"; the board
had exactly these 11 errors since PR #3116 / commit 071961ef).  The 11
errors split into two independent defects in
``boards/04-stm32-devboard/generate_design.py`` -- both fixed by mirroring
sister board 05's PR #3004 pattern:

* **Cluster A (6 errors):** the three ``add_power()`` symbols (+5V, +3V3,
  GND) were placed 10 mm from their rails with NO bridging wire, so each
  symbol pin floated (``pin_not_connected`` on #PWR01/#PWR02/#PWR03).
  Because no power source ever reached the +5V/GND rails (the AMS1117 VI
  and GND pins are ``power_input``), U1.VI, U1.GND and the +3V3 symbol
  also fired ``power_pin_not_driven``.  A secondary net-name mismatch
  (rail label "+3.3V" vs stock symbol ``power:+3V3`` net "+3V3") meant the
  +3V3 symbol would not unify even once wired.  Fixed by: wiring each
  symbol pin to its rail, adding a PWR_FLAG on +5V and GND (no
  ``power_output`` driver), and renaming the 3.3V rail label to "+3V3" to
  match the stock symbol.
* **Cluster B (5 issues):** the SWD ``DebugHeader`` (J1) was created
  WITHOUT ``pin_nets``, so ``connect_to_rails`` only wired pin 1 (VCC) and
  the first GND (pin 3).  Pins 2/4/5/6 (SWDIO/SWCLK/GND-key/NRST) floated
  (``pin_not_connected``), the MCU-side SWDIO/SWCLK/NRST labels had no
  J1-side match (``isolated_pin_label``) and NRST had no driver
  (``pin_not_driven`` on U2.7).  Fixed by passing
  ``pin_nets={"2": "SWDIO", "4": "SWCLK", "5": "GND", "6": "NRST"}``.

The single residual warning -- ``isolated_pin_label`` on the SWO label --
is design intent: PB3/SWO has no SWD-6 header pin, so it is an intentional
single-pin test-point label (per #3149).

This test pins the post-fix ERC state so a future regression that
re-introduces any of the two clusters trips CI.  It runs the same
KiCad-CLI ERC against the *committed* ``output/stm32_devboard.kicad_sch``
artifact (modeled on ``tests/test_board_05_erc_regression.py``) so the
slow end-to-end ``generate_design.py`` rebuild is not required.  It skips
gracefully when ``kicad-cli`` is not on PATH.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"
SCH_PATH = BOARD_DIR / "output" / "stm32_devboard.kicad_sch"

# Issue #3149 acceptance criterion: ERC errors drop from 11 to 0.
MAX_ALLOWED_ERC_ERRORS = 0


@pytest.fixture(scope="module")
def schematic_path() -> Path:
    """Resolve the committed schematic or skip if absent.

    The schematic lives under ``boards/04-stm32-devboard/output/`` and is
    committed to git so this test is self-contained.  If the file is
    missing (someone wiped output/), skip with a regen hint rather than
    fail spuriously.
    """
    if not SCH_PATH.exists():
        pytest.skip(
            f"Board 04 schematic not found at {SCH_PATH!s}; "
            "regenerate via "
            "`uv run python boards/04-stm32-devboard/generate_design.py`"
        )
    return SCH_PATH


def _run_erc_count_errors(sch_path: Path) -> tuple[int, list[dict]]:
    """Invoke KiCad-CLI ERC on *sch_path*, return ``(error_count, items)``.

    Skips the calling test if ``kicad-cli`` is not on PATH; that lets the
    test run on developer machines without KiCad installed without failing
    CI (the CI image has KiCad).  ``items`` is the list of error-severity
    violation dicts straight from the JSON report -- used by per-cluster
    assertions below.
    """
    from kicad_tools.cli.runner import find_kicad_cli, run_erc

    if find_kicad_cli() is None:
        pytest.skip(
            "kicad-cli not found; ERC regression test requires KiCad 8+ "
            "(install from https://www.kicad.org/download/)"
        )

    result = run_erc(sch_path)
    if not result.success or result.output_path is None:
        pytest.fail(f"ERC run failed: stderr={result.stderr!r}")

    with open(result.output_path) as f:
        data = json.load(f)
    with contextlib.suppress(Exception):
        result.output_path.unlink(missing_ok=True)

    errors: list[dict] = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            if v.get("severity") == "error":
                errors.append(v)
    return len(errors), errors


class TestBoard04ERCRegression:
    """Pin the post-#3149 ERC state of board 04's committed schematic."""

    def test_erc_error_count_is_zero(self, schematic_path: Path) -> None:
        """Acceptance: ERC error count drops from 11 (pre-#3149) to 0.

        A failure here means a regression re-introduced one of the two
        clusters fixed in #3149 (power-symbol-to-rail bridging wires +
        PWR_FLAG / +3V3 rename, or the DebugHeader ``pin_nets``).
        Regenerate the schematic and diff the JSON ERC report against this
        zero-error baseline to identify which cluster regressed.
        """
        count, errors = _run_erc_count_errors(schematic_path)
        assert count <= MAX_ALLOWED_ERC_ERRORS, (
            f"Board 04 schematic has {count} ERC error(s), expected "
            f"<={MAX_ALLOWED_ERC_ERRORS}.  #3149 dropped this from 11 to 0; "
            f"a regression here means the power-symbol bridging wires / "
            f"PWR_FLAG / +3V3 rail rename (Cluster A) or the DebugHeader "
            f"pin_nets (Cluster B) were undone.  Error types: "
            + ", ".join(sorted({v.get("type", "?") for v in errors}))
        )

    def test_no_power_symbol_pin_not_connected(self, schematic_path: Path) -> None:
        """Cluster A: no ``pin_not_connected`` on the #PWR0* power symbols.

        #3149 wired each ``add_power()`` symbol pin (+5V #PWR01, +3V3
        #PWR02, GND #PWR03) down/up to its rail endpoint.  A regression
        that drops the bridging wires re-introduces 3
        ``pin_not_connected`` errors at the symbol pins.
        """
        _, errors = _run_erc_count_errors(schematic_path)
        pwr_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_connected"
            and any("#PWR" in it.get("description", "") for it in v.get("items", []))
        ]
        assert not pwr_errors, (
            f"Board 04 has {len(pwr_errors)} pin_not_connected error(s) on "
            f"#PWR0* power symbols; #3149's symbol-to-rail bridging wires "
            f"were dropped.  Items: "
            + ", ".join(it.get("description", "") for v in pwr_errors for it in v.get("items", []))
        )

    def test_no_u1_power_pin_not_driven(self, schematic_path: Path) -> None:
        """Cluster A: no ``power_pin_not_driven`` on the AMS1117 (U1).

        #3149 added a PWR_FLAG on +5V and GND (neither rail has a genuine
        ``power_output`` source -- U1.VI and U1.GND are both
        ``power_input``).  A regression that drops the flags re-introduces
        ``power_pin_not_driven`` on U1.VI (pin 3) and U1.GND (pin 1).
        """
        _, errors = _run_erc_count_errors(schematic_path)
        u1_errors = [
            v
            for v in errors
            if v.get("type") == "power_pin_not_driven"
            and any("U1" in it.get("description", "") for it in v.get("items", []))
        ]
        assert not u1_errors, (
            f"Board 04 has {len(u1_errors)} power_pin_not_driven error(s) "
            f"on U1 (AMS1117); #3149's PWR_FLAG on +5V/GND was dropped.  "
            f"Items: "
            + ", ".join(it.get("description", "") for v in u1_errors for it in v.get("items", []))
        )

    def test_no_j1_pin_not_connected(self, schematic_path: Path) -> None:
        """Cluster B: no ``pin_not_connected`` on the SWD header (J1).

        #3149 passed ``pin_nets`` to the DebugHeader so pins 2 (SWDIO),
        4 (SWCLK), 5 (GND-key) and 6 (NRST) get label stubs.  A regression
        that drops ``pin_nets`` re-introduces 4 ``pin_not_connected``
        errors on those J1 pins.
        """
        _, errors = _run_erc_count_errors(schematic_path)
        j1_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_connected"
            and any("J1" in it.get("description", "") for it in v.get("items", []))
        ]
        assert not j1_errors, (
            f"Board 04 has {len(j1_errors)} pin_not_connected error(s) on "
            f"the SWD header J1; #3149's DebugHeader pin_nets was dropped.  "
            f"Items: "
            + ", ".join(it.get("description", "") for v in j1_errors for it in v.get("items", []))
        )

    def test_no_nrst_pin_not_driven(self, schematic_path: Path) -> None:
        """Cluster B: no ``pin_not_driven`` on the MCU NRST pin (U2.7).

        The MCU-side NRST label only unifies with a driver once J1's pin 6
        publishes a matching NRST label (via ``pin_nets``).  A regression
        re-introduces ``pin_not_driven`` on U2 pin 7.
        """
        _, errors = _run_erc_count_errors(schematic_path)
        nrst_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_driven"
            and any("U2" in it.get("description", "") for it in v.get("items", []))
        ]
        assert not nrst_errors, (
            f"Board 04 has {len(nrst_errors)} pin_not_driven error(s) on "
            f"U2 (NRST); #3149's DebugHeader NRST label stub was dropped."
        )
