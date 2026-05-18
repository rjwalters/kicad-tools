"""Tests for ``Schematic.add_pwr_symbol`` — synthesized power-symbol helper.

Covers issue #3010: KiCad's stock ``power:`` library bakes the published
global-net name into each symbol (``power:+24V`` always publishes ``+24V``).
When project convention uses rail labels like ``VMOTOR`` (no stock analogue)
or ``+3.3V`` (KiCad uses ``+3V3``), placing a stock symbol on the rail
creates two electrically distinct nets that look visually unified.

``add_pwr_symbol(net_name=...)`` fixes this at the block level by
synthesizing a one-pin power-input symbol whose Value field AND pin name
both equal ``net_name``, so KiCad publishes the requested global net.

Test surface:

* The synthesized lib_symbol contains a ``pin power_in`` whose ``name``
  matches the requested ``net_name``, and a ``Value`` property that also
  matches.
* The instantiated :class:`PowerSymbol` carries the synthesized ``lib_id``.
* The synthesized lib_symbol is registered in ``_embedded_lib_symbols`` so
  ``_build_lib_symbols_node`` emits it on save.
* A schematic written-then-reloaded preserves the synthesized lib_symbol
  (round-trip stability through ``_embedded_lib_symbols``).
* Repeated calls with the same ``net_name`` reuse the cached lib_symbol
  rather than creating duplicates.
* Edge-case net names (``+3.3V`` with a dot, ``GND``) work as well as
  alphanumeric names like ``VMOTOR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schematic.models.elements import PowerSymbol
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# ---------------------------------------------------------------------------
# Synthesized lib_symbol structure
# ---------------------------------------------------------------------------


class TestSynthesizedLibSymbolStructure:
    """Verify the lib_symbol entry produced by ``add_pwr_symbol``."""

    def test_pin_name_matches_net_name_vmotor(self):
        """For ``VMOTOR``, the power_in pin's name field must equal ``VMOTOR``."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)

        lib_node = sch._embedded_lib_symbols["kicad_tools_pwr:VMOTOR"]
        out = lib_node.to_string(indent=0)
        # The pin name appears inside the inner unit symbol VMOTOR_1_1.
        assert "(pin power_in line" in out
        assert '(name "VMOTOR"' in out

    def test_pin_name_matches_net_name_with_dot(self):
        """``+3.3V`` (with the dot — project convention) must be preserved."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("+3.3V", x=100, y=100)

        lib_node = sch._embedded_lib_symbols["kicad_tools_pwr:+3.3V"]
        out = lib_node.to_string(indent=0)
        assert '(name "+3.3V"' in out

    def test_value_property_matches_net_name(self):
        """The ``Value`` property must equal the net name.

        KiCad determines the published global net from the ``Value`` field
        of a ``(power)`` symbol — this is the single most load-bearing
        invariant.
        """
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)

        lib_node = sch._embedded_lib_symbols["kicad_tools_pwr:VMOTOR"]
        # Walk properties and find Value
        value_prop = None
        for prop in lib_node.find_all("property"):
            atoms = prop.get_atoms()
            if len(atoms) >= 2 and str(atoms[0]) == "Value":
                value_prop = str(atoms[1])
                break
        assert value_prop == "VMOTOR", f"Value property must equal net name; got {value_prop!r}"

    def test_symbol_has_power_flag(self):
        """The synthesized symbol must carry the ``(power)`` flag.

        Without it, KiCad treats the symbol as an ordinary component
        instead of as a global-net driver.
        """
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)

        lib_node = sch._embedded_lib_symbols["kicad_tools_pwr:VMOTOR"]
        assert lib_node.get("power") is not None, (
            "Synthesized lib_symbol missing (power) flag — KiCad will not "
            "treat it as a global-net symbol"
        )


# ---------------------------------------------------------------------------
# PowerSymbol instance returned by add_pwr_symbol
# ---------------------------------------------------------------------------


class TestAddPwrSymbolReturnValue:
    def test_returns_power_symbol(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr = sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        assert isinstance(pwr, PowerSymbol)

    def test_lib_id_uses_synth_prefix(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr = sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        assert pwr.lib_id == "kicad_tools_pwr:VMOTOR"

    def test_lib_id_with_dot_net_name(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr = sch.add_pwr_symbol("+3.3V", x=100, y=100)
        assert pwr.lib_id == "kicad_tools_pwr:+3.3V"

    def test_reference_uses_pwr_counter(self):
        """First call gets #PWR01, second gets #PWR02, etc."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr1 = sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        pwr2 = sch.add_pwr_symbol("+5V", x=120, y=100)
        assert pwr1.reference == "#PWR01"
        assert pwr2.reference == "#PWR02"

    def test_appended_to_power_symbols_list(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        assert len(sch.power_symbols) == 0
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        assert len(sch.power_symbols) == 1


# ---------------------------------------------------------------------------
# Caching: repeated net names reuse the same lib_symbol
# ---------------------------------------------------------------------------


class TestSynthesizedLibSymbolCaching:
    def test_repeated_net_name_creates_one_lib_symbol(self):
        """Two #PWR symbols on the same net share one lib_symbol entry."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        sch.add_pwr_symbol("VMOTOR", x=200, y=100)

        assert len(sch._synthesized_pwr_defs) == 1
        assert "VMOTOR" in sch._synthesized_pwr_defs
        # The instances both reference the same lib_id
        assert len(sch.power_symbols) == 2
        assert all(p.lib_id == "kicad_tools_pwr:VMOTOR" for p in sch.power_symbols)

    def test_different_net_names_create_distinct_lib_symbols(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        sch.add_pwr_symbol("+3.3V", x=120, y=100)
        sch.add_pwr_symbol("+5V", x=140, y=100)
        sch.add_pwr_symbol("GND", x=160, y=100)

        assert len(sch._synthesized_pwr_defs) == 4
        assert set(sch._synthesized_pwr_defs.keys()) == {"VMOTOR", "+3.3V", "+5V", "GND"}


# ---------------------------------------------------------------------------
# Round-trip: synthesized lib_symbols survive write -> load -> write -> load
# ---------------------------------------------------------------------------


class TestSynthesizedLibSymbolRoundTrip:
    def test_save_and_reload_preserves_lib_symbol(self, tmp_path: Path):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        sch.add_pwr_symbol("+3.3V", x=120, y=100)

        p = tmp_path / "test.kicad_sch"
        sch.write(p)

        sch2 = Schematic.load(str(p))
        # The reload must recover both #PWR instances
        assert len(sch2.power_symbols) == 2
        lib_ids = {p.lib_id for p in sch2.power_symbols}
        assert lib_ids == {"kicad_tools_pwr:VMOTOR", "kicad_tools_pwr:+3.3V"}

        # The lib_symbols block in the reloaded schematic must still carry
        # the synthesized entries (these come back via _embedded_lib_symbols).
        assert "kicad_tools_pwr:VMOTOR" in sch2._embedded_lib_symbols
        assert "kicad_tools_pwr:+3.3V" in sch2._embedded_lib_symbols

    def test_double_round_trip_is_stable(self, tmp_path: Path):
        """Write -> load -> write -> load preserves the lib_symbols block."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)

        p1 = tmp_path / "test1.kicad_sch"
        sch.write(p1)

        sch2 = Schematic.load(str(p1))
        p2 = tmp_path / "test2.kicad_sch"
        sch2.write(p2)

        sch3 = Schematic.load(str(p2))
        assert len(sch3.power_symbols) == 1
        assert sch3.power_symbols[0].lib_id == "kicad_tools_pwr:VMOTOR"
        assert "kicad_tools_pwr:VMOTOR" in sch3._embedded_lib_symbols

        # The lib_symbol structure on second reload must still have the
        # right Value property and pin name.
        lib_node = sch3._embedded_lib_symbols["kicad_tools_pwr:VMOTOR"]
        out = lib_node.to_string(indent=0)
        assert '(name "VMOTOR"' in out

    def test_pin_name_preserved_across_round_trip(self, tmp_path: Path):
        """The load-bearing invariant: pin name equals net_name after reload."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_pwr_symbol("+3.3V", x=100, y=100)

        p = tmp_path / "test.kicad_sch"
        sch.write(p)

        sch2 = Schematic.load(str(p))
        lib_node = sch2._embedded_lib_symbols["kicad_tools_pwr:+3.3V"]
        out = lib_node.to_string(indent=0)
        assert '(name "+3.3V"' in out


# ---------------------------------------------------------------------------
# Grid snapping
# ---------------------------------------------------------------------------


class TestSnapping:
    def test_snap_off_preserves_coordinates(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr = sch.add_pwr_symbol("VMOTOR", x=100.5, y=100.7, snap=False)
        # snap=False AND SnapMode.OFF means just round to 2dp
        assert pwr.x == 100.5
        assert pwr.y == 100.7

    def test_snap_with_explicit_false_skips_snap(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.AUTO)
        pwr = sch.add_pwr_symbol("VMOTOR", x=100.5, y=100.7, snap=False)
        assert pwr.x == 100.5
        assert pwr.y == 100.7

    def test_rotation_passes_through(self):
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        pwr = sch.add_pwr_symbol("GND", x=100, y=100, rotation=180)
        assert pwr.rotation == 180


# ---------------------------------------------------------------------------
# Integration: kicad-cli loads the synthesized schematic
# ---------------------------------------------------------------------------


def _find_kicad_cli() -> str | None:
    """Locate kicad-cli on macOS or fall back to PATH."""
    import shutil

    mac_path = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if Path(mac_path).exists():
        return mac_path
    return shutil.which("kicad-cli")


@pytest.mark.skipif(_find_kicad_cli() is None, reason="kicad-cli not found")
class TestKicadCliIntegration:
    """End-to-end: write a schematic with a synthesized symbol and run ERC.

    The load-bearing assertion is that kicad-cli accepts the file (no
    parse errors) AND that the ERC violation, if any, reports the net
    name we asked for — confirming the synthesized symbol unifies with
    the rail label on the KiCad side, not just on ours.
    """

    def _run_erc(self, sch_path: Path, out_dir: Path) -> dict:
        """Run kicad-cli sch erc and return the parsed JSON report."""
        import json
        import subprocess

        cli = _find_kicad_cli()
        report = out_dir / "erc.json"
        subprocess.run(
            [
                cli,
                "sch",
                "erc",
                str(sch_path),
                "-o",
                str(report),
                "--format",
                "json",
                "--severity-error",
            ],
            capture_output=True,
            check=False,
        )
        return json.loads(report.read_text())

    def test_synthesized_symbol_publishes_requested_net(self, tmp_path: Path):
        """ERC violation must reference net ``VMOTOR``, not ``+24V``."""
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        # Stub wire + label so the symbol pin meets a real wire endpoint
        # and the rail carries the matching label.
        sch.add_wire((100, 100), (100, 110), warn_on_collision=False)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        sch.add_label("VMOTOR", x=100, y=110, validate_connection=False)

        p = tmp_path / "t.kicad_sch"
        sch.write(p)
        erc = self._run_erc(p, tmp_path)

        # The synthesized symbol publishes VMOTOR.  Without a PWR_FLAG
        # the net is undriven, so ERC emits power_pin_not_driven naming
        # the symbol — and the pin description must include "VMOTOR" if
        # the synthesized pin name took effect.
        descriptions = []
        for sheet in erc.get("sheets", []):
            for v in sheet.get("violations", []):
                for item in v.get("items", []):
                    descriptions.append(item.get("description", ""))
        # At least one violation item description should include the net name
        assert any("VMOTOR" in d for d in descriptions), (
            f"Expected VMOTOR in ERC item descriptions; got {descriptions!r}"
        )

    def test_pwr_flag_drives_synthesized_net(self, tmp_path: Path):
        """Net unification check: PWR_FLAG on the rail clears ERC.

        If the synthesized symbol unified the wrong net (e.g., ``+24V``
        instead of ``VMOTOR``), then a ``PWR_FLAG`` on the ``VMOTOR``
        rail would leave the symbol's net undriven.  Zero ERC errors
        proves unification works.
        """
        sch = Schematic(title="Test", snap_mode=SnapMode.OFF)
        sch.add_wire((100, 100), (100, 110), warn_on_collision=False)
        sch.add_wire((100, 110), (200, 110), warn_on_collision=False)
        sch.add_pwr_symbol("VMOTOR", x=100, y=100)
        sch.add_label("VMOTOR", x=150, y=110, validate_connection=False)
        sch.add_pwr_flag(100, 110)

        p = tmp_path / "t.kicad_sch"
        sch.write(p)
        erc = self._run_erc(p, tmp_path)

        total = sum(len(s.get("violations", [])) for s in erc.get("sheets", []))
        assert total == 0, (
            f"Expected zero ERC errors with PWR_FLAG driving the synthesized "
            f"VMOTOR symbol; got {total}. Report: {erc}"
        )
