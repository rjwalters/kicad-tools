"""Unit tests for the wire-stub ERC rule.

The wire-stub rule detects wires whose dangling endpoint is an exact
integer multiple of the schematic grid short of a real pin -- the
canonical "off-by-N-grids" defect class found in the chorus-test-revA
schematic.  See :mod:`kicad_tools.validate.sch_wire_stub` for the
algorithm description.

These tests build synthetic in-memory "schematics" using lightweight
namespace objects (no S-expression parsing).
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.validate.sch_wire_stub import (
    DEFAULT_GRID_MM,
    WireStubFinding,
    find_wire_stubs,
)

# ---------------------------------------------------------------------------
# Synthetic schematic helpers (parallel to the orphan-label tests)
# ---------------------------------------------------------------------------


class _FakeLibSymbol:
    def __init__(self, pin_positions_abs: dict[str, tuple[float, float]]):
        self._abs = pin_positions_abs

    def get_all_pin_positions(
        self, instance_pos, instance_rot=0, mirror="", snap_to_grid=True
    ):
        return dict(self._abs)


def _wire(x1: float, y1: float, x2: float, y2: float):
    return SimpleNamespace(start=(x1, y1), end=(x2, y2))


def _symbol(reference: str, lib_id: str, dnp: bool = False):
    return SimpleNamespace(
        reference=reference,
        lib_id=lib_id,
        position=(0.0, 0.0),
        rotation=0.0,
        mirror="",
        dnp=dnp,
    )


def _junction(x: float, y: float):
    return SimpleNamespace(position=(x, y))


def _label_at(x: float, y: float):
    return SimpleNamespace(text="", position=(x, y))


def _no_connect(x: float, y: float):
    return SimpleNamespace(position=(x, y))


def _sheet(symbols=None, wires=None, junctions=None, labels=None,
           global_labels=None, hierarchical_labels=None, no_connects=None):
    return SimpleNamespace(
        symbols=symbols or [],
        wires=wires or [],
        junctions=junctions or [],
        labels=labels or [],
        global_labels=global_labels or [],
        hierarchical_labels=hierarchical_labels or [],
        no_connects=no_connects or [],
    )


# ---------------------------------------------------------------------------
# find_wire_stubs
# ---------------------------------------------------------------------------


class TestWireStubDetection:
    """End-to-end synthetic-schematic tests."""

    def test_one_grid_short_horizontal_stub(self):
        """The canonical chorus-test pattern: wire ends 2.54mm short of J2 pin.

        Setup: a wire from (60.0, 80.0) to (81.28, 80.0); the target
        pin J2.8 is at (83.82, 80.0).  Endpoint x=81.28 is exactly
        one 2.54mm grid step short of x=83.82.
        """
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        w = _wire(60.0, 80.0, 81.28, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("connectors.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert len(findings) == 1
        f = findings[0]
        assert isinstance(f, WireStubFinding)
        assert f.candidate_pin_ref == "J2.8"
        assert f.grid_steps_short == 1
        assert f.axis == "x"
        assert f.dangling_endpoint == (81.28, 80.0)
        assert f.candidate_pin_position == (83.82, 80.0)

    def test_two_grids_short_horizontal_stub(self):
        """A 2-grid (5.08mm) gap is still detected and reported correctly."""
        lib = _FakeLibSymbol({"1": (88.9, 60.0)})
        j = _symbol("J1", "Conn:Test")
        w = _wire(60.0, 60.0, 78.74, 60.0)  # 88.9 - 78.74 = 10.16mm = 4 grids
        sheet = _sheet(symbols=[j], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert len(findings) == 1
        assert findings[0].grid_steps_short == 4

    def test_vertical_stub_detected(self):
        """A wire ending one grid above its target pin fires the rule."""
        lib = _FakeLibSymbol({"3": (50.0, 80.0)})
        j = _symbol("J3", "Conn:Test")
        # Wire ends one grid above the pin: y = 77.46 vs pin y=80.0
        w = _wire(50.0, 70.0, 50.0, 77.46)
        sheet = _sheet(symbols=[j], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert len(findings) == 1
        assert findings[0].axis == "y"
        assert findings[0].grid_steps_short == 1

    def test_endpoint_on_pin_not_flagged(self):
        """A wire that already terminates on a pin is NOT flagged."""
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        w = _wire(60.0, 80.0, 83.82, 80.0)  # exact match -- no stub
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_endpoint_on_junction_not_flagged(self):
        """A wire endpoint at a junction is connected, not a stub."""
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        w = _wire(60.0, 80.0, 81.28, 80.0)
        jct = _junction(81.28, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w], junctions=[jct])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_endpoint_on_label_not_flagged(self):
        """A wire endpoint at a label is connected (label is a real anchor)."""
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        w = _wire(60.0, 80.0, 81.28, 80.0)
        lbl = _label_at(81.28, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w], global_labels=[lbl])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_endpoint_on_no_connect_not_flagged(self):
        """A wire endpoint at a no_connect flag is intentional, not a stub."""
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        w = _wire(60.0, 80.0, 81.28, 80.0)
        nc = _no_connect(81.28, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w], no_connects=[nc])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_sub_grid_offset_not_flagged(self):
        """An endpoint less than one grid step from a pin is NOT flagged.

        These are caught by KiCad's built-in ``endpoint_off_grid`` rule
        and would duplicate the report.
        """
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        # 0.5mm short -- below one grid step (2.54mm).
        w = _wire(60.0, 80.0, 83.32, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_non_integer_grid_offset_not_flagged(self):
        """An endpoint not an integer multiple of the grid is NOT flagged.

        These are also caught by ``endpoint_off_grid`` separately.
        """
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        # Off-grid: 80.0 + 1.5mm = neither integer multiple of 2.54.
        w = _wire(60.0, 80.0, 82.32, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_max_grids_limit_respected(self):
        """Stubs beyond max_stub_grids are NOT flagged."""
        lib = _FakeLibSymbol({"8": (100.0, 80.0)})
        j2 = _symbol("J2", "Conn:Test")
        # 6 grid steps away: would be flagged if we allowed it.
        w = _wire(60.0, 80.0, 84.76, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
            max_stub_grids=4,
        )

        assert findings == []

    def test_multiple_stubs_each_reported(self):
        """Replicates the chorus-test pattern: many wires all 1 grid short."""
        lib = _FakeLibSymbol({
            "8": (83.82, 72.39),
            "10": (83.82, 74.93),
            "12": (83.82, 85.09),
        })
        j2 = _symbol("J2", "Conn:Test")
        w_tx = _wire(66.04, 72.39, 81.28, 72.39)   # UART_TX
        w_rx = _wire(66.04, 74.93, 81.28, 74.93)   # UART_RX
        w_bclk = _wire(54.61, 85.09, 81.28, 85.09)  # I2S_BCLK
        sheet = _sheet(symbols=[j2], wires=[w_tx, w_rx, w_bclk])

        findings = find_wire_stubs(
            sheets=[("connectors.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert len(findings) == 3
        pins = sorted(f.candidate_pin_ref for f in findings)
        assert pins == ["J2.10", "J2.12", "J2.8"]
        for f in findings:
            assert f.grid_steps_short == 1
            assert f.axis == "x"

    def test_dnp_symbol_pins_skipped(self):
        """A DNP symbol's pins are not used as stub candidates."""
        lib = _FakeLibSymbol({"8": (83.82, 80.0)})
        j2 = _symbol("J2", "Conn:Test", dnp=True)
        w = _wire(60.0, 80.0, 81.28, 80.0)
        sheet = _sheet(symbols=[j2], wires=[w])

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Conn:Test": lib},
        )

        assert findings == []

    def test_uses_custom_grid_mm(self):
        """Caller can override the grid step (e.g., 1.27mm fine grid)."""
        lib = _FakeLibSymbol({"1": (10.0, 10.0)})
        u1 = _symbol("U1", "Test:Test")
        # 1.27mm short -- one grid on a 1.27mm grid.
        w = _wire(0.0, 10.0, 8.73, 10.0)
        sheet = _sheet(symbols=[u1], wires=[w])

        # On the default 2.54mm grid, 1.27 isn't an integer multiple.
        findings_default = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
        )
        assert findings_default == []

        # On a 1.27mm grid, the stub is detected.
        findings_fine = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Test:Test": lib},
            grid_mm=1.27,
        )
        assert len(findings_fine) == 1
        assert findings_fine[0].grid_steps_short == 1


class TestWireStubSharedCorners:
    """Regression: two wires meeting at a corner share an endpoint.

    The original implementation filtered the "other wires' endpoints"
    set by position, which accidentally dropped legitimate corner
    connections from other wires.  An L-shaped path consisting of two
    perpendicular wires meeting at the corner was therefore reported
    as a wire stub for both endpoints of both wires.  The fix tracks
    owning-wire identity by index instead of by position.
    """

    def test_l_shaped_path_to_pin_not_flagged(self):
        """Two wires forming a corner: horizontal segment reaches the
        pin, vertical segment connects elsewhere.  The shared corner
        endpoint is connected via the vertical wire, so neither wire
        should be flagged.
        """
        lib = _FakeLibSymbol({"1": (101.6, 119.38)})
        flg = _symbol("#FLG03", "power:PWR_FLAG")
        horiz = _wire(93.98, 119.38, 101.6, 119.38)  # reaches pin
        vert = _wire(93.98, 107.95, 93.98, 119.38)   # joins corner
        # The (93.98, 107.95) endpoint needs an anchor so it doesn't
        # fire the rule on its own — give it a label.
        lbl = _label_at(93.98, 107.95)
        sheet = _sheet(
            symbols=[flg], wires=[horiz, vert], global_labels=[lbl]
        )

        findings = find_wire_stubs(
            sheets=[("power.kicad_sch", sheet)],
            lib_symbols={"power:PWR_FLAG": lib},
        )

        assert findings == []

    def test_three_wires_at_shared_corner_not_flagged(self):
        """T-junction of three wires meeting at the corner pin.  Two
        approach from perpendicular directions; the third reaches the
        pin.  None should fire.
        """
        lib = _FakeLibSymbol({"1": (50.0, 50.0)})
        u = _symbol("U1", "Device:Test")
        # All three wires share the (50.0, 50.0) corner = pin position.
        horiz = _wire(40.0, 50.0, 50.0, 50.0)  # endpoint at pin
        vert_up = _wire(50.0, 50.0, 50.0, 60.0)  # endpoint at pin
        # vert_down ends 1 grid short — should fire normally
        vert_down = _wire(50.0, 50.0, 50.0, 47.46)
        # Anchor the dangling ends with labels so they don't fire
        lbl_left = _label_at(40.0, 50.0)
        lbl_up = _label_at(50.0, 60.0)
        sheet = _sheet(
            symbols=[u],
            wires=[horiz, vert_up, vert_down],
            global_labels=[lbl_left, lbl_up],
        )

        findings = find_wire_stubs(
            sheets=[("test.kicad_sch", sheet)],
            lib_symbols={"Device:Test": lib},
        )

        # vert_down (50.0, 47.46) is 1 grid short — flagged.
        # horiz and vert_up both reach pin (50.0, 50.0) — not flagged.
        assert len(findings) == 1
        assert findings[0].dangling_endpoint == (50.0, 47.46)


class TestWireStubDefaults:
    """The module exposes documented defaults."""

    def test_default_grid_is_2_54(self):
        assert DEFAULT_GRID_MM == 2.54
