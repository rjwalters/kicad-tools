"""Regression test for ``boards/00-simple-led/`` power-net stranding.

Issue #3148 — Board 00 (the "Hello World" LED board) routed only the
single signal net ``LED_ANODE`` and left both power nets (``VCC`` and
``GND``) stranded, producing 2 connectivity DRC errors.

The board defines **3 nets**: ``VCC``, ``LED_ANODE``, ``GND``.  ``VCC``
and ``GND`` are power/pour nets (``is_pour_net=True`` in
``DEFAULT_NET_CLASS_MAP``) that are meant to be connected via copper
zones, not traces.  The router correctly auto-skips them, but the
board's hand-rolled ``route_pcb()`` never created copper pours -- so
both power nets ended up with neither a trace nor a zone and DRC
reported "1 of 2 pads stranded" for each.

This was NOT a router regression (it reproduced on commits predating
all suspected router PRs).  The fix routes board 00 through the same
pour-aware path the official ``kct route`` CLI uses:
``auto_pour_if_missing()`` creates the VCC/GND zone outlines and
``_fill_zones_after_route()`` fills them.

This test pins the post-fix behavior by running the generator
end-to-end and asserting:

- The routable signal net ``LED_ANODE`` is routed (>= 1 segment).
- The routed PCB has at least 2 filled copper zones (VCC + GND).
- ``kct check`` reports 0 connectivity errors (no stranded pads).

It FAILS against the unpatched ``route_pcb()`` (0 zones, 2 connectivity
errors) and PASSES after the fix.

Requires ``kicad-cli`` for ERC + zone fill; skipped otherwise.  Marked
``@pytest.mark.slow`` (full generate + route is ~15-20s).  The nightly
slow-tests workflow (``.github/workflows/slow-tests.yml``, ``-m slow``)
picks this up; PR-time CI excludes it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "00-simple-led"
GENERATOR = BOARD_DIR / "generate_design.py"

# Net 2 is the only routable signal net on board 00 (R1.2 -> D1.1).
# Nets 1 (VCC) and 3 (GND) are pour nets connected via copper zones.
LED_ANODE_NET_NUM = 2
LED_ANODE_NET_NAME = "LED_ANODE"


def _kicad_cli_available() -> bool:
    """Whether kicad-cli is installed (needed for ERC + zone fill)."""
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli() is not None


def _count_filled_zones(pcb_text: str) -> int:
    """Count ``(zone ...)`` blocks that contain a ``filled_polygon``.

    A zone with ``fill enabled`` but no ``filled_polygon`` is an empty
    outline that contributes no plane copper -- it does NOT connect the
    net.  We only count zones that were actually filled.
    """
    # Split on zone openings (both single-line and newline-wrapped forms).
    chunks = re.split(r"\(zone[\s\(]", pcb_text)
    return sum(1 for chunk in chunks[1:] if "filled_polygon" in chunk)


def _count_segments_on_net(pcb_text: str, net_num: int) -> int:
    """Count routed copper segments belonging to ``net_num``.

    Segments serialize as ``(segment ... (net N) ...)``.  We match the
    ``(net N)`` reference form used inside segment/via blocks (distinct
    from the ``(net N "name")`` header declarations).
    """
    return len(re.findall(rf"\(segment\b[^()]*?(?:\([^)]*\)[^()]*?)*\(net {net_num}\)", pcb_text))


def _connectivity_error_count(check_stdout: str) -> int:
    """Count connectivity DRC errors in ``kct check`` output.

    The pre-fix failure mode prints lines like::

        [X] connectivity
            Net 'GND' is partially routed: 1 of 2 pads stranded
    """
    return len(
        re.findall(
            r"\bpartially routed\b|\bpads stranded\b|\bstranded\b",
            check_stdout,
        )
    )


class TestBoard00D1Polarity:
    """Regression test for #3747: schematic↔PCB D1 polarity disagreement.

    The schematic generator originally wired R1.2 to D1 pin 1 (cathode, K)
    and D1 pin 2 (anode, A) to GND, leaving the LED reverse-biased per the
    schematic. The PCB generator at ``generate_led("D1", ..., "LED_ANODE",
    "GND")`` already binds pad 1 → GND and pad 2 → LED_ANODE (forward
    biased). The two halves of the recipe disagreed.

    This test runs the generator end-to-end and asserts that:

    - Schematic: D1 pin 1 (K) is on net ``GND``; pin 2 (A) is on net
      ``LED_ANODE``.
    - PCB: pad 1 is on net ``GND`` (net 3); pad 2 is on net ``LED_ANODE``
      (net 2).

    Fails on ``origin/main`` at the pre-fix commit; passes after the fix
    in ``boards/00-simple-led/generate_design.py``. Sub-second, no
    ``@pytest.mark.slow`` -- runs in PR CI.
    """

    @pytest.fixture(scope="class")
    def generated_design(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Run ``generate_design.py`` and return the output directory."""
        out_dir = tmp_path_factory.mktemp("board00_polarity")
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), str(out_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        sch = out_dir / "simple_led.kicad_sch"
        pcb = out_dir / "simple_led.kicad_pcb"
        if not sch.exists() or not pcb.exists():
            pytest.fail(
                f"Generator did not produce expected artifacts "
                f"(exit {proc.returncode}).\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}"
            )
        return out_dir

    def test_schematic_d1_cathode_on_gnd(self, generated_design: Path) -> None:
        """D1 pin 1 (K, cathode) must be on the GND net in the schematic."""
        from kicad_tools.schematic.models.schematic import Schematic

        sch = Schematic.load(generated_design / "simple_led.kicad_sch")
        net = sch.get_net_for_pin("D1", "1")
        assert net == "GND", (
            f"D1 pin 1 (cathode K) is on net {net!r}, expected 'GND'. "
            "The schematic generator wired the LED reverse-biased "
            "(see #3747)."
        )

    def test_schematic_d1_anode_on_led_anode(self, generated_design: Path) -> None:
        """D1 pin 2 (A, anode) must be on the LED_ANODE net in the schematic."""
        from kicad_tools.schematic.models.schematic import Schematic

        sch = Schematic.load(generated_design / "simple_led.kicad_sch")
        net = sch.get_net_for_pin("D1", "2")
        assert net == "LED_ANODE", (
            f"D1 pin 2 (anode A) is on net {net!r}, expected 'LED_ANODE'. "
            "The schematic generator wired the LED reverse-biased "
            "(see #3747)."
        )

    def test_pcb_d1_pad_to_net_mapping(self, generated_design: Path) -> None:
        """The PCB must agree with the schematic on D1's pin-to-net mapping.

        ``generate_led("D1", D1_POS, "LED_ANODE", "GND")`` binds pad 1 to
        GND (net 3) and pad 2 to LED_ANODE (net 2). The schematic must
        match this mapping for LVS sanity.
        """
        pcb_text = (generated_design / "simple_led.kicad_pcb").read_text()
        # Net header declarations (id-to-name binding):
        assert '(net 2 "LED_ANODE")' in pcb_text, (
            "PCB net 2 is not LED_ANODE; net numbering changed."
        )
        assert '(net 3 "GND")' in pcb_text, "PCB net 3 is not GND; net numbering changed."
        # Locate D1's footprint and verify each pad's net. The KiCad
        # serializer uses either ``(fp_text reference "D1" ...)`` (the form
        # this repo's PCB generator emits) or ``(property "Reference" "D1"
        # ...)`` (KiCad 8 stable form); accept either.
        d1_marker = re.search(
            r'\(fp_text\s+reference\s+"D1"|\(property\s+"Reference"\s+"D1"',
            pcb_text,
        )
        assert d1_marker is not None, "Could not find D1 footprint block in PCB."
        # Walk back to the enclosing ``(footprint`` opening, then bound the
        # block to its matching close paren so the pad search is scoped to D1.
        start = pcb_text.rfind("(footprint", 0, d1_marker.start())
        assert start >= 0, "Could not find enclosing (footprint for D1."
        depth = 0
        end = start
        for i in range(start, len(pcb_text)):
            ch = pcb_text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        d1_block = pcb_text[start:end]
        # Match each pad and its (net N "NAME") binding.
        pad_to_net: dict[str, tuple[str, str]] = {
            m.group(1): (m.group(2), m.group(3))
            for m in re.finditer(
                r'\(pad\s+"(\d+)"[^()]*(?:\([^()]*\)[^()]*)*?\(net\s+(\d+)\s+"([^"]+)"\)',
                d1_block,
            )
        }
        pad1 = pad_to_net.get("1")
        pad2 = pad_to_net.get("2")
        assert pad1 is not None and pad1[1] == "GND", (
            f"D1 pad 1 is on net {pad1!r}, expected GND. "
            "PCB net assignment changed; the schematic↔PCB agreement (#3747) "
            "now needs re-verification."
        )
        assert pad2 is not None and pad2[1] == "LED_ANODE", (
            f"D1 pad 2 is on net {pad2!r}, expected LED_ANODE."
        )


@pytest.mark.slow
@pytest.mark.skipif(
    not _kicad_cli_available(),
    reason="kicad-cli not installed (required for ERC + zone fill)",
)
class TestBoard00SimpleLed:
    """Pin board 00's pour-aware routing against the #3148 fix.

    Runs the generator script as a subprocess (the same path the fleet
    audit / a developer invokes), then inspects the routed artifact and
    runs ``kct check`` on it.  The fixture runs once per class; each test
    asserts a distinct aspect for sharp failure attribution.
    """

    @pytest.fixture(scope="class")
    def routed_pcb(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        """Run ``generate_design.py`` and return the routed PCB path."""
        out_dir = tmp_path_factory.mktemp("board00")
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), str(out_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        routed = out_dir / "simple_led_routed.kicad_pcb"
        if not routed.exists():
            pytest.fail(
                f"Generator did not produce {routed.name} "
                f"(exit {proc.returncode}).\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}"
            )
        # The generator's exit gate is ``0 if erc_success and drc_success``.
        # Post-fix, DRC must pass, so a non-zero exit is itself a regression.
        assert proc.returncode == 0, (
            f"generate_design.py exited {proc.returncode} (expected 0; "
            "ERC + DRC must both pass after the #3148 pour fix).\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
        return routed

    def test_led_anode_routed(self, routed_pcb: Path) -> None:
        """The only routable signal net (LED_ANODE) must have a trace."""
        text = routed_pcb.read_text()
        # Sanity: confirm the net numbering matches our assumption.
        assert f'(net {LED_ANODE_NET_NUM} "{LED_ANODE_NET_NAME}")' in text, (
            f"Expected net {LED_ANODE_NET_NUM} to be '{LED_ANODE_NET_NAME}' "
            "in the routed PCB header; the board's net map may have changed. "
            "Update LED_ANODE_NET_NUM in this test if so."
        )
        segments = _count_segments_on_net(text, LED_ANODE_NET_NUM)
        assert segments >= 1, (
            f"LED_ANODE (net {LED_ANODE_NET_NUM}) has no routed segments. "
            "The only routable signal net on board 00 was not routed."
        )

    def test_power_nets_have_filled_zones(self, routed_pcb: Path) -> None:
        """VCC + GND must be connected via filled copper zones.

        Pre-fix, ``route_pcb()`` created zero zones (it never called
        ``auto_pour_if_missing()``), so this asserts the fix wired the
        pour step in.  Empty zone outlines (fill enabled, no
        ``filled_polygon``) do NOT count -- they carry no copper.
        """
        text = routed_pcb.read_text()
        filled = _count_filled_zones(text)
        assert filled >= 2, (
            f"Routed PCB has only {filled} filled copper zone(s); expected "
            ">= 2 (one VCC, one GND).  This is the #3148 failure: the board "
            "script bypassed auto_pour_if_missing()/zone-fill, leaving both "
            "power nets without copper."
        )

    def test_no_connectivity_errors(self, routed_pcb: Path) -> None:
        """``kct check`` must report 0 connectivity errors.

        This is the user-visible DRC failure from #3148 ("Net 'GND'/'VCC'
        is partially routed: 1 of 2 pads stranded").
        """
        proc = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(routed_pcb)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        conn_errors = _connectivity_error_count(proc.stdout)
        assert conn_errors == 0, (
            f"kct check reported {conn_errors} connectivity issue(s) "
            "(stranded / partially-routed pads).  VCC/GND should be fully "
            "connected via filled copper zones after the #3148 fix.\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
        assert proc.returncode == 0, (
            f"kct check exited {proc.returncode} (expected 0 -- DRC clean).\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
