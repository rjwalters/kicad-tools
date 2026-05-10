"""Unit tests for the orphan-label ERC rule.

The orphan-label rule detects named global / local labels that connect
to exactly one pin in the schematic.  See
:mod:`kicad_tools.validate.sch_orphan_label` for the algorithm
description.

These tests build synthetic in-memory "schematics" using lightweight
namespace objects (no S-expression parsing) so the algorithm can be
exercised in isolation from the KiCad file format.
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.validate.sch_orphan_label import (
    OrphanLabelFinding,
    _is_intended_signal_name,
    find_orphan_labels,
)


# ---------------------------------------------------------------------------
# Helpers for building synthetic Schematic / LibrarySymbol shims.
# ---------------------------------------------------------------------------


class _FakeLibSymbol:
    """A minimal stand-in for :class:`LibrarySymbol`.

    Stores pin positions in absolute schematic coordinates so the
    rule can probe them directly via ``get_all_pin_positions``.  The
    ``instance_pos`` and ``instance_rot`` arguments are ignored --
    callers building this object encode the final position already.
    """

    def __init__(self, pin_positions_abs: dict[str, tuple[float, float]]):
        self._abs = pin_positions_abs

    def get_all_pin_positions(
        self, instance_pos, instance_rot=0, mirror="", snap_to_grid=True
    ):
        # Translate absolute coordinates by subtracting the requested
        # instance position so a test can place the same library
        # symbol at multiple positions.  The synthetic library always
        # stores absolute pin positions.
        return dict(self._abs)


def _label(text: str, x: float, y: float):
    """Build a synthetic Label-like object."""
    return SimpleNamespace(text=text, position=(x, y))


def _wire(x1: float, y1: float, x2: float, y2: float):
    return SimpleNamespace(start=(x1, y1), end=(x2, y2))


def _junction(x: float, y: float):
    return SimpleNamespace(position=(x, y))


def _no_connect(x: float, y: float):
    return SimpleNamespace(position=(x, y))


def _symbol(reference: str, lib_id: str, x: float = 0.0, y: float = 0.0,
            rotation: float = 0.0, mirror: str = "", dnp: bool = False):
    return SimpleNamespace(
        reference=reference,
        lib_id=lib_id,
        position=(x, y),
        rotation=rotation,
        mirror=mirror,
        dnp=dnp,
    )


def _sheet(
    symbols=None,
    labels=None,
    global_labels=None,
    hierarchical_labels=None,
    wires=None,
    junctions=None,
    no_connects=None,
):
    return SimpleNamespace(
        symbols=symbols or [],
        labels=labels or [],
        global_labels=global_labels or [],
        hierarchical_labels=hierarchical_labels or [],
        wires=wires or [],
        junctions=junctions or [],
        no_connects=no_connects or [],
    )


# ---------------------------------------------------------------------------
# _is_intended_signal_name
# ---------------------------------------------------------------------------


class TestIntendedSignalNameFilter:
    """The filter accepts real signal names, rejects power & default nets."""

    def test_accepts_named_signal(self):
        assert _is_intended_signal_name("UART_TX")
        assert _is_intended_signal_name("I2S_BCLK")
        assert _is_intended_signal_name("DBG_LED2")
        assert _is_intended_signal_name("NRST")
        assert _is_intended_signal_name("SCK1")

    def test_rejects_power_names(self):
        assert not _is_intended_signal_name("GND")
        assert not _is_intended_signal_name("VCC")
        assert not _is_intended_signal_name("VDD")
        assert not _is_intended_signal_name("+3V3")
        assert not _is_intended_signal_name("+5V")
        assert not _is_intended_signal_name("-12V")

    def test_rejects_default_nets(self):
        assert not _is_intended_signal_name("Net-(U1-Pad3)")
        assert not _is_intended_signal_name("Net-(J2-11)")


# ---------------------------------------------------------------------------
# find_orphan_labels
# ---------------------------------------------------------------------------


class TestOrphanLabelDetection:
    """End-to-end synthetic-schematic tests for find_orphan_labels."""

    def test_single_pin_global_label_is_orphan(self):
        """A global label ``UART_TX`` connected only to one MCU pin fires.

        Setup: U8 has pin "10" at (100, 80); a wire connects (100, 80)
        to a global label at (110, 80).  No other pin or label exists.
        Expectation: one finding for ``UART_TX``.
        """
        lib = _FakeLibSymbol({"10": (100.0, 80.0)})
        u8 = _symbol("U8", "MCU:Test")
        wire = _wire(100.0, 80.0, 110.0, 80.0)
        lbl = _label("UART_TX", 110.0, 80.0)
        sheet = _sheet(symbols=[u8], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"MCU:Test": lib},
        )

        assert len(findings) == 1
        assert findings[0].label_name == "UART_TX"
        assert findings[0].pin_ref == "U8.10"
        assert findings[0].position_mm == (100.0, 80.0)

    def test_multi_pin_label_is_not_orphan(self):
        """A label reaching two pins is a healthy net, not an orphan."""
        lib = _FakeLibSymbol({"10": (100.0, 80.0)})
        lib2 = _FakeLibSymbol({"8": (200.0, 80.0)})
        u8 = _symbol("U8", "MCU:Test")
        j2 = _symbol("J2", "Conn:Test")
        # Wire from pin to label
        w1 = _wire(100.0, 80.0, 110.0, 80.0)
        # Wire from label to other pin
        w2 = _wire(110.0, 80.0, 200.0, 80.0)
        lbl = _label("UART_TX", 110.0, 80.0)
        sheet = _sheet(
            symbols=[u8, j2],
            global_labels=[lbl],
            wires=[w1, w2],
        )

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"MCU:Test": lib, "Conn:Test": lib2},
        )

        assert findings == []

    def test_power_label_not_flagged(self):
        """A single-pin label named ``GND`` is silently allowed."""
        lib = _FakeLibSymbol({"1": (50.0, 50.0)})
        u1 = _symbol("U1", "Power:Test")
        wire = _wire(50.0, 50.0, 60.0, 50.0)
        lbl = _label("GND", 60.0, 50.0)
        sheet = _sheet(symbols=[u1], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"Power:Test": lib},
        )

        assert findings == []

    def test_no_connect_suppresses_orphan(self):
        """An explicit no_connect at the lone pin silences the finding."""
        lib = _FakeLibSymbol({"4": (30.0, 40.0)})
        u1 = _symbol("U1", "Test:Test")
        wire = _wire(30.0, 40.0, 40.0, 40.0)
        lbl = _label("SPARE_GPIO", 40.0, 40.0)
        nc = _no_connect(30.0, 40.0)
        sheet = _sheet(
            symbols=[u1], global_labels=[lbl], wires=[wire], no_connects=[nc],
        )

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
        )

        assert findings == []

    def test_default_named_net_not_flagged(self):
        """A label named ``Net-(U1-Pad3)`` is silently allowed.

        Default-named nets are categorized by the PCB-side
        single_pad_net DRC rule, not the orphan-label ERC rule.
        """
        lib = _FakeLibSymbol({"3": (30.0, 40.0)})
        u1 = _symbol("U1", "Test:Test")
        wire = _wire(30.0, 40.0, 40.0, 40.0)
        lbl = _label("Net-(U1-Pad3)", 40.0, 40.0)
        sheet = _sheet(symbols=[u1], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
        )

        assert findings == []

    def test_multiple_orphans_reported_individually(self):
        """When multiple labels orphan-fire, each is reported."""
        lib_mcu = _FakeLibSymbol({
            "9": (50.0, 50.0),
            "10": (50.0, 52.54),
        })
        u8 = _symbol("U8", "MCU:Test")
        w1 = _wire(50.0, 50.0, 60.0, 50.0)
        w2 = _wire(50.0, 52.54, 60.0, 52.54)
        l1 = _label("UART_TX", 60.0, 50.0)
        l2 = _label("UART_RX", 60.0, 52.54)
        sheet = _sheet(symbols=[u8], global_labels=[l1, l2], wires=[w1, w2])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"MCU:Test": lib_mcu},
        )

        names = sorted(f.label_name for f in findings)
        assert names == ["UART_RX", "UART_TX"]

    def test_finding_has_error_severity(self):
        """OrphanLabelFinding.kind defaults to ``"error"``."""
        lib = _FakeLibSymbol({"1": (10.0, 10.0)})
        u1 = _symbol("U1", "Test:Test")
        wire = _wire(10.0, 10.0, 20.0, 10.0)
        lbl = _label("TEST_NET", 20.0, 10.0)
        sheet = _sheet(symbols=[u1], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
        )

        assert len(findings) == 1
        assert findings[0].kind == "error"
        assert isinstance(findings[0], OrphanLabelFinding)


class TestDnpAndMissingLibSymbol:
    """Defensive cases: DNP symbols and missing library defs are ignored."""

    def test_dnp_symbol_pins_not_counted(self):
        """A label only attached to a DNP symbol's pin is not flagged.

        (Edge case: DNP components are excluded from the active
        design, so an orphan global label on a DNP pin is vacuously
        ok -- there's no real pin to count.)
        """
        lib = _FakeLibSymbol({"1": (10.0, 10.0)})
        u1 = _symbol("U1", "Test:Test", dnp=True)
        wire = _wire(10.0, 10.0, 20.0, 10.0)
        lbl = _label("DNP_SIG", 20.0, 10.0)
        sheet = _sheet(symbols=[u1], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
        )

        # No pins resolved -> no orphan signal to report.
        assert findings == []

    def test_unresolved_lib_id_skipped(self):
        """A symbol with no resolved lib symbol contributes no pins."""
        u1 = _symbol("U1", "Unknown:Symbol")
        wire = _wire(10.0, 10.0, 20.0, 10.0)
        lbl = _label("MYSTERY_NET", 20.0, 10.0)
        sheet = _sheet(symbols=[u1], global_labels=[lbl], wires=[wire])

        findings = find_orphan_labels(
            sheets=[("root.kicad_sch", sheet)],
            lib_symbols={},  # empty: lib_id not resolvable
        )

        # No pins resolved -> no orphan to flag.
        assert findings == []
