"""Regression guard: board-05 schematic ERC error count stays at-or-below 4.

Issue #3004 (PR-B, follow-up to #2994 cascade) drops board-05's KiCad
ERC error count from 17 to 1 by:

* Adding ``sch.add_no_connect()`` markers for the 8 unused STM32G431K8
  GPIO pins (PA3–PA5, PA11–PA15, PB3–PB5 — LQFP-32 pins 8, 9, 10, 21,
  22, 25, 27, 28).  Closes 8 ``pin_not_connected`` errors.
* Wiring each #PWR power-input symbol (``power:+24V``, ``power:+5V``,
  ``power:+3V3``, ``power:GND``) down to its rail's left endpoint so
  the symbol pin meets a real wire endpoint AND so the symbol's global
  net unifies with the rail's labelled net.  Closes 4
  ``pin_not_connected`` errors on the power symbols.
* Extending the +3.3V rail westward (x_start from X_LDO+25=165 to
  X_LDO+7=147) so the LDO output cap C6 at x=160.02 has a rail endpoint
  to meet, plus a junction at the original rail start so the +3V3
  symbol's vertical wire still connects via a valid T-connection.
  Closes the C6 ``pin_not_connected`` error.
* Extending the GND rail at x=309.88 (with a short bridge wire and
  junction) so C19's pin-2 wire endpoint lands on a real GND wire
  endpoint.  Closes the C19 ``pin_not_connected`` error.

The residual 1 error (``power_pin_not_driven`` on U1.VIN) is a
known-shape demo-board churn item (curator's acceptance allows ≤4) and
stems from a pre-existing label-placement issue in the buck-block (the
U1.FB ``+5V`` label lands on a C2-bulk-cap vertical wire that bridges
+5V and VMOTOR globally; adding an explicit PWR_FLAG on VMOTOR triggers
a worse ``pin_to_pin`` Output<->Power-output conflict against U1.OUT,
so the bridge is preferable to the alternative).

This test pins the post-#3004 ERC state so a future regression that
re-introduces any of the four categories trips CI.  The actual ERC
invocation lives at ``boards/05-bldc-motor-controller/design.py``'s
``run_erc`` helper; this test runs the same KiCad-CLI ERC against the
*committed* ``output/bldc_controller.kicad_sch`` artifact so the slow
end-to-end ``design.py`` rebuild is not required.

If you regenerate the schematic and ERC errors increase above
``MAX_ALLOWED_ERC_ERRORS``, re-evaluate whether the increase is a
fixable structural defect (preferred) or an acceptance-criteria churn
adjustment (rare — bump the constant with a referenced commit/issue).
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
SCH_PATH = BOARD_DIR / "output" / "bldc_controller.kicad_sch"

# Issue #3004 acceptance criterion: ERC errors drop from 17 to ≤4.  The
# current shipped state is 1; allow slack up to 4 to cover the curator's
# "expected demo board churn" floor while still failing loudly if a
# regression re-introduces a chunk of the 13 fixed errors.
MAX_ALLOWED_ERC_ERRORS = 4


@pytest.fixture(scope="module")
def schematic_path() -> Path:
    """Resolve the committed schematic or skip if absent.

    The schematic lives under ``boards/05-bldc-motor-controller/output/``
    and is committed to git so this test is self-contained.  If the
    file is missing (someone wiped output/), skip with a regen hint
    rather than fail spuriously.
    """
    if not SCH_PATH.exists():
        pytest.skip(
            f"Board 05 schematic not found at {SCH_PATH!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return SCH_PATH


def _run_erc_count_errors(sch_path: Path) -> tuple[int, list[dict]]:
    """Invoke KiCad-CLI ERC on *sch_path*, return ``(error_count, items)``.

    Skips the calling test if ``kicad-cli`` is not on PATH; that lets
    the test run on developer machines without KiCad installed without
    failing CI (the CI image has KiCad).  ``items`` is the list of
    error-severity violation dicts straight from the JSON report — used
    by per-category assertions below.
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


class TestBoard05ERCRegression:
    """Pin the post-#3004 ERC state of board 05's committed schematic."""

    def test_erc_error_count_within_budget(self, schematic_path: Path) -> None:
        """Acceptance: ERC error count drops from 17 (pre-#3004) to ≤4.

        A failure here means a regression re-introduced one of the
        category fixes shipped in PR #3004 (MCU NC markers, power-symbol
        wire stubs, +3.3V rail extension, or GND rail extension).
        Regenerate the schematic and diff the JSON ERC report against
        the post-#3004 baseline to identify which category regressed.
        """
        count, _ = _run_erc_count_errors(schematic_path)
        assert count <= MAX_ALLOWED_ERC_ERRORS, (
            f"Board 05 schematic has {count} ERC error(s), expected "
            f"<={MAX_ALLOWED_ERC_ERRORS}.  PR #3004 dropped this from 17 "
            f"to 1; a regression here means one of the four fix categories "
            f"(MCU NC markers, power-symbol wire stubs, +3.3V rail extent, "
            f"or GND rail extent) was undone."
        )

    def test_no_mcu_pin_not_connected_errors(self, schematic_path: Path) -> None:
        """No ``pin_not_connected`` errors on STM32G431K8 GPIO pins.

        PR #3004 added ``sch.add_no_connect()`` markers for the 8
        unused GPIO pins (LQFP-32 pins 8, 9, 10, 21, 22, 25, 27, 28 —
        PA3–PA5, PA11–PA15, PB3–PB5).  A regression that drops the NC
        loop would re-introduce 8 ``pin_not_connected`` errors at the
        MCU's east edge (x≈217–243).
        """
        _, errors = _run_erc_count_errors(schematic_path)
        mcu_pin_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_connected"
            and any(
                # MCU symbol body spans x ∈ [217, 243] approximately
                # (LQFP-32 7x7mm at MCU_X=230); pin positions are in mm
                # divided by 100 in the JSON (KiCad ERC uses meters).
                2.17 <= it.get("pos", {}).get("x", 0) <= 2.43
                for it in v.get("items", [])
            )
        ]
        assert not mcu_pin_errors, (
            f"Board 05 has {len(mcu_pin_errors)} pin_not_connected error(s) "
            f"on STM32G431K8 GPIO pins (PR #3004 NC-marker loop regressed). "
            f"Items: "
            + ", ".join(str(it.get("pos", {})) for v in mcu_pin_errors for it in v.get("items", []))
        )

    def test_no_c19_pin_not_connected_error(self, schematic_path: Path) -> None:
        """No ``pin_not_connected`` error on the gate-driver bypass cap C19.

        PR #3004 added a short GND-rail extension wire from
        (299.72, 279.4) to (309.88, 279.4) so C19's pin-2 vertical wire
        endpoint meets a real wire endpoint.  A regression that drops
        the extension would re-introduce a single pin_not_connected
        error at (309.88, 83.82).
        """
        _, errors = _run_erc_count_errors(schematic_path)
        c19_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_connected"
            and any(
                # C19 pin 2 is at (309.88, 83.82) — JSON pos is in m,
                # so x ≈ 3.0988, y ≈ 0.8382.
                abs(it.get("pos", {}).get("x", 0) - 3.0988) < 0.01
                and abs(it.get("pos", {}).get("y", 0) - 0.8382) < 0.01
                for it in v.get("items", [])
            )
        ]
        assert not c19_errors, (
            "Board 05 has a pin_not_connected error on C19's pin 2 at "
            "(309.88, 83.82); PR #3004's GND-rail extension was dropped."
        )

    def test_no_c6_pin_not_connected_error(self, schematic_path: Path) -> None:
        """No ``pin_not_connected`` error on the LDO output cap C6.

        PR #3004 extended the +3.3V rail westward (x_start from
        X_LDO+25=165 to X_LDO+7=147) so C6's pin-1 vertical wire
        endpoint at (160.02, 64.77) lands on the rail interior.  A
        regression that reverts the rail extent would re-introduce a
        single pin_not_connected error at C6 (160.02, 111.76).
        """
        _, errors = _run_erc_count_errors(schematic_path)
        c6_errors = [
            v
            for v in errors
            if v.get("type") == "pin_not_connected"
            and any(
                # C6 pin 1 at (160.02, 111.76)
                abs(it.get("pos", {}).get("x", 0) - 1.6002) < 0.01
                and abs(it.get("pos", {}).get("y", 0) - 1.1176) < 0.01
                for it in v.get("items", [])
            )
        ]
        assert not c6_errors, (
            "Board 05 has a pin_not_connected error on C6's pin 1 at "
            "(160.02, 111.76); PR #3004's +3.3V rail extension was dropped."
        )
