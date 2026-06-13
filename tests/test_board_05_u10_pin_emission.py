"""Regression test for board-05 U10 (STM32G431K8Tx) pin-label emission (#3379).

PR #3377 fixed the ``kct pcb sync-netlist`` pipeline (kicad-cli ``/``-prefix
strip + unconditional pin-to-pad mapping). Applying that fix to the existing
board-05 schematic exposed a *separate* defect in ``design.py``: the helper
``_connect_mcu_pin_to_label`` placed each U10 pin's stub-and-label without
checking whether the label coordinate landed on the interior of a foreign
wire. Three categories of silent bridge resulted:

1. ``SWDIO`` (U10 pin 23) and ``SWCLK`` (U10 pin 24) ended up on the long
   horizontal wires running from J2 (motor output connector) west to the
   half-bridge phase nodes, bridging into ``PHASE_A`` / ``PHASE_B``.
2. ``GND`` labels for U10 pins 14 / 16 / 32 landed at x=227.33, which sits
   on a vertical HallSensorInput rail that also passes through the
   ``+3.3V`` label row at y=137.16 -- the two labels collapsed onto a
   single net and ``GND`` was reassigned to ``+3V3``.
3. ``ISENSE_A-`` / ``B-`` / ``C-`` (U10 pins 5/6/7) collapsed onto
   ``+3V3`` via the same x=227.33 collision (the labels themselves were
   placed in a row where the +3.3V symbol-stub wire crossed).

The kicad-cli netlist exporter reported ``U10.5/6/7 -> +3V3``,
``U10.14/16/32 -> +3V3``, ``U10.23 -> PHASE_A``, ``U10.24 -> PHASE_B``
-- eight mismatches against the design's intent. Applying
``kct pcb sync-netlist`` then rewrote the committed PCB pad assignments
to match, regressing DRC from 6 to 73 violations.

The fix in ``design.py`` is twofold:

* ``_connect_mcu_pin_to_label`` was rewritten to be collision-aware
  (mirrors the ``_emit_pin_net_stub`` pattern in
  ``schematic/blocks/_stub_helpers.py``).  Candidate label endpoints
  are tried in priority order; the first that does *not* land on the
  interior of an existing wire wins. L-shaped (horizontal + vertical)
  fallbacks cover the dense SWDIO/SWCLK row. ``ValueError`` is raised
  if no candidate clears.
* The U10 pin labels are *deferred* to the end of
  ``create_bldc_controller`` (after every other section has drawn its
  wires), so the collision detector can see all foreign wires that
  could potentially bridge.  Without deferral the future
  ``PHASE_A`` / ``PHASE_B`` / HallSensorInput-rail wires don't exist
  yet at U10-label-emission time.
* The ``CurrentSenseShunt.connect_to_rails(gnd_rail_y=...)`` call was
  removed for board 05; previously it shorted the ``ISENSE_X-`` net
  to ``GND`` by wiring the shunt's IN- pin directly to the GND rail.
  The committed PCB and U10 ADC topology require ``ISENSE_X-`` to be
  a distinct Kelvin-sense net.

This test exercises the full ``design.create_bldc_controller`` pipeline
and asserts that:

* every U10 signal pin lands on the correct labelled net (kicad-cli
  netlist round-trip), and
* the SWDIO/SWCLK labels are *not* electrically connected to PHASE_A
  or PHASE_B (the original silent-bridge symptom).

Acceptance criteria (per #3379):

* AC1: U10.5/6/7 resolve to ISENSE_A-/B-/C- (not +3V3 or GND).
* AC2: U10.23/24 resolve to SWDIO/SWCLK (not PHASE_A/B).
* AC3: U10.14/16/32 resolve to GND (not +3V3).
* AC4: All other U10 signal pins (HALL, PWM, NRST, OSC, SWO) match
  their intended labels.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# The U10 pin -> intended net map.  Power-net names use the global
# "+3V3" form (kicad-cli's serialization of the power:+3V3 symbol);
# the "+3.3V" form is the local label-side name used inside
# ``_connect_mcu_pin_to_label``.  They both refer to the same net.
U10_EXPECTED_NETS: dict[str, set[str]] = {
    # Power supply pins
    "1": {"+3V3", "+3.3V"},  # VDD
    "17": {"+3V3", "+3.3V"},  # VDD
    "15": {"+3V3", "+3.3V"},  # VDDA
    "14": {"GND"},  # VSSA
    "16": {"GND"},  # VSS
    "32": {"GND"},  # VSS
    # Reset and crystal (kicad-cli `/`-prefixes local labels)
    "4": {"NRST", "/NRST"},  # PG10 -> NRST
    "2": {"OSC_IN", "/OSC_IN"},  # PF0
    "3": {"OSC_OUT", "/OSC_OUT"},  # PF1
    # ADC current-sense returns (the original #3379 mismatch set).
    # kicad-cli prefixes the local label with `/` since these are
    # not power symbols; the test allows either form.
    "5": {"ISENSE_A-", "/ISENSE_A-"},  # PA0
    "6": {"ISENSE_B-", "/ISENSE_B-"},  # PA1
    "7": {"ISENSE_C-", "/ISENSE_C-"},  # PA2
    # Hall sensor inputs
    "11": {"HALL_A", "/HALL_A"},  # PA6
    "12": {"HALL_B", "/HALL_B"},  # PA7
    "13": {"HALL_C", "/HALL_C"},  # PB0
    # High-side PWM (to gate driver INH_A/B/C)
    "18": {"PWM_AH", "/PWM_AH"},  # PA8
    "19": {"PWM_BH", "/PWM_BH"},  # PA9
    "20": {"PWM_CH", "/PWM_CH"},  # PA10
    # SWD debug pins (the original #3379 SWDIO/SWCLK mismatch set)
    "23": {"SWDIO", "/SWDIO"},  # PA13
    "24": {"SWCLK", "/SWCLK"},  # PA14
    "26": {"SWO", "/SWO"},  # PB3
    # Low-side PWM (to gate driver INL_A/B/C)
    "29": {"PWM_AL", "/PWM_AL"},  # PB6
    "30": {"PWM_BL", "/PWM_BL"},  # PB7
    "31": {"PWM_CL", "/PWM_CL"},  # PB8
}

# Nets that the original #3379 bug silently bridged INTO. The test
# asserts that none of the U10 signal pins resolve to these "wrong"
# nets after the fix.  Each entry maps a U10 pin to the pre-fix net.
U10_PREFIX_BRIDGE_VICTIMS: dict[str, str] = {
    "5": "+3V3",  # ISENSE_A- was bridged to +3V3
    "6": "+3V3",  # ISENSE_B- was bridged to +3V3
    "7": "+3V3",  # ISENSE_C- was bridged to +3V3
    "14": "+3V3",  # GND was bridged to +3V3
    "16": "+3V3",  # GND was bridged to +3V3
    "32": "+3V3",  # GND was bridged to +3V3
    "23": "/PHASE_A",  # SWDIO was bridged to PHASE_A
    "24": "/PHASE_B",  # SWCLK was bridged to PHASE_B
}


def _find_kicad_cli() -> str | None:
    """Locate the kicad-cli binary; return None if unavailable.

    Used to skip tests cleanly when KiCad is not installed (CI sandbox).
    """
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "/usr/bin/kicad-cli",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _run_design_create_schematic(tmp_path: Path) -> Path:
    """Run ``design.create_bldc_controller`` and return the schematic path.

    Imports ``design`` directly from ``boards/05-bldc-motor-controller``
    so the test exercises the actual emitter, not a copy.
    """
    repo_root = Path(__file__).parent.parent
    board_dir = repo_root / "boards" / "05-bldc-motor-controller"
    sys.path.insert(0, str(board_dir))
    try:
        import design  # noqa: PLC0415 - dynamic load by design

        design.create_project(tmp_path, "bldc_controller")
        sch_path = design.create_bldc_controller(tmp_path)
        return sch_path
    finally:
        sys.path.remove(str(board_dir))


def _parse_u10_pin_assignments(netlist_path: Path) -> dict[str, str]:
    """Parse ``netlist_path`` and return a {pin -> net_name} map for U10.

    Walks the kicad-cli sexpr netlist with a paren-matched scanner
    rather than a regex over the whole file (the format is line-
    folded so a flat regex misses ``(node (ref "U10") (pin "N"))``
    blocks split across lines).
    """
    content = netlist_path.read_text()
    nets_anchor = content.find("(nets")
    if nets_anchor < 0:
        return {}
    text = content[nets_anchor:]

    i = 0
    net_blocks: list[str] = []
    while True:
        j = text.find("(net", i)
        if j < 0:
            break
        # Must be a real "(net (code N) ..." block, not "(nets ..." or
        # "(net_name ...".
        head = text[j : j + 30]
        if not re.match(r"\(net\s+\(code", head):
            i = j + 1
            continue
        depth = 0
        k = j
        while k < len(text):
            c = text[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        net_blocks.append(text[j : k + 1])
        i = k + 1

    u10: dict[str, str] = {}
    for nb in net_blocks:
        m = re.search(r'\(name\s+"([^"]*)"\)', nb)
        if not m:
            continue
        net_name = m.group(1)
        for pm in re.finditer(r'\(ref\s+"U10"\)\s*\(pin\s+"(\d+)"\)', nb):
            u10[pm.group(1)] = net_name
    return u10


@pytest.fixture(scope="module")
def u10_netlist(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Generate the board-05 schematic, export the kicad-cli netlist,
    and parse U10's pin assignments.  Module-scoped so we only pay the
    ~10s pipeline cost once per test session.

    Skips the test module cleanly when kicad-cli is not installed
    (e.g. CI sandboxes without KiCad).
    """
    kicad_cli = _find_kicad_cli()
    if kicad_cli is None:
        pytest.skip("kicad-cli not available; install KiCad to run this test")

    tmp_path = tmp_path_factory.mktemp("board05_u10_pins")
    sch_path = _run_design_create_schematic(tmp_path)
    assert sch_path.exists(), f"Schematic not generated: {sch_path}"

    netlist_path = tmp_path / "netlist.net"
    result = subprocess.run(
        [
            kicad_cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            str(sch_path),
            "-o",
            str(netlist_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Fontconfig warnings on macOS go to stderr but the export
    # still succeeds; only fail if the netlist file is missing or
    # empty.
    assert netlist_path.exists(), f"kicad-cli failed: {result.stderr}"
    assert netlist_path.stat().st_size > 1000, "Netlist file unexpectedly small"

    u10 = _parse_u10_pin_assignments(netlist_path)
    assert len(u10) >= 32, f"Expected >=32 U10 pin entries, got {len(u10)}: {u10}"
    return u10


class TestU10PinEmission:
    """U10 pin-label emission round-trips through kicad-cli (#3379)."""

    @pytest.mark.parametrize(
        "pin, expected_nets",
        sorted(U10_EXPECTED_NETS.items(), key=lambda kv: int(kv[0])),
    )
    def test_pin_resolves_to_intended_net(
        self,
        u10_netlist: dict[str, str],
        pin: str,
        expected_nets: set[str],
    ) -> None:
        """Each U10 signal pin lands on the intended net after the
        kicad-cli netlist round-trip.

        Parameterized for clear failure messages -- a regression on
        SWDIO would fail only ``test_pin_resolves_to_intended_net[23-...]``
        rather than the whole batch.
        """
        actual = u10_netlist.get(pin, "MISSING")
        assert actual in expected_nets, (
            f"U10.{pin} resolved to {actual!r}, expected one of "
            f"{sorted(expected_nets)!r}. "
            f"Likely cause: wire-stub collision in "
            f"_connect_mcu_pin_to_label (issue #3379)."
        )

    def test_swdio_not_bridged_to_phase_a(self, u10_netlist: dict[str, str]) -> None:
        """U10.23 (SWDIO) must NOT be bridged to PHASE_A.

        Pre-fix symptom: the SWDIO stub-and-label at (247.65, 177.80)
        landed on the interior of the long horizontal PHASE_A wire
        from J2 west to HB_A's VOUT, silently merging the SWDIO net
        into PHASE_A.
        """
        assert u10_netlist.get("23") not in {"/PHASE_A", "PHASE_A"}, (
            "U10.23 (SWDIO) is bridged to PHASE_A -- regression of #3379"
        )

    def test_swclk_not_bridged_to_phase_b(self, u10_netlist: dict[str, str]) -> None:
        """U10.24 (SWCLK) must NOT be bridged to PHASE_B.

        Pre-fix symptom: the SWCLK stub-and-label at (247.65, 180.34)
        landed on the long horizontal PHASE_B wire from J2 west to
        HB_B's VOUT, silently merging the SWCLK net into PHASE_B.
        """
        assert u10_netlist.get("24") not in {"/PHASE_B", "PHASE_B"}, (
            "U10.24 (SWCLK) is bridged to PHASE_B -- regression of #3379"
        )

    @pytest.mark.parametrize("pin", ["5", "6", "7"])
    def test_isense_negative_not_bridged_to_power(
        self, u10_netlist: dict[str, str], pin: str
    ) -> None:
        """U10.5/6/7 (ISENSE_A-/B-/C-) must NOT be bridged to +3V3.

        Pre-fix symptom: the ISENSE_* labels at x=247.65, y in
        (144.78, 147.32, 149.86) landed on a vertical
        HallSensorInput rail wire crossing x=227.33 that also touched
        the +3.3V label row at y=137.16, silently merging the ISENSE
        nets into +3V3.
        """
        actual = u10_netlist.get(pin, "MISSING")
        assert actual not in {"+3V3", "+3.3V", "GND"}, (
            f"U10.{pin} (ISENSE) resolved to {actual!r} -- the "
            f"original #3379 silent bridge into a power rail."
        )

    @pytest.mark.parametrize("pin", ["14", "16", "32"])
    def test_gnd_pins_not_bridged_to_power(self, u10_netlist: dict[str, str], pin: str) -> None:
        """U10.14/16/32 (VSS/VSSA) must resolve to GND, not +3V3.

        Pre-fix symptom: the GND label for VDD pin 14 at (227.33,
        190.50) sat on a vertical HallSensorInput rail at x=227.33
        that also crossed the +3.3V label row at y=137.16, silently
        merging GND into +3V3.
        """
        actual = u10_netlist.get(pin, "MISSING")
        assert actual == "GND", (
            f"U10.{pin} (GND) resolved to {actual!r} -- the original #3379 silent bridge into +3V3."
        )

    def test_no_pre_fix_bridge_victims_remain(self, u10_netlist: dict[str, str]) -> None:
        """End-to-end summary check: every pin in the original #3379
        bridge-victim set has moved off the wrong-net it was on
        before the fix.

        Acts as a backstop in case future refactors of the per-pin
        tests above silently weaken individual assertions.
        """
        offenders = []
        for pin, wrong_net in U10_PREFIX_BRIDGE_VICTIMS.items():
            if u10_netlist.get(pin) == wrong_net:
                offenders.append(f"U10.{pin} -> {wrong_net}")
        assert not offenders, (
            f"#3379 regression: the following U10 pins are still "
            f"bridged to their original wrong nets: {offenders}"
        )
