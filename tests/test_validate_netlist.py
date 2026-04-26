"""Tests for the netlist integrity DRC rule (net_undeclared)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.validate.rules.netlist import NetlistRule, _absolute_pad_position


# ---------------------------------------------------------------------------
# Lightweight stubs that satisfy the rule without loading a real PCB file
# ---------------------------------------------------------------------------


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubPad:
    number: str
    net_number: int = 0
    net_name: str = ""
    position: tuple[float, float] = (0.0, 0.0)
    type: str = "smd"


@dataclass
class _StubFootprint:
    reference: str = "U1"
    name: str = "Package_SO:SOIC-8"
    position: tuple[float, float] = (100.0, 50.0)
    rotation: float = 0.0
    pads: list[_StubPad] = field(default_factory=list)


@dataclass
class _StubPCB:
    """Minimal PCB stub implementing the subset used by NetlistRule."""

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _footprints: list[_StubFootprint] = field(default_factory=list)

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    @property
    def footprints(self) -> list[_StubFootprint]:
        return self._footprints


# Stub design rules -- the netlist rule ignores them, but the interface
# requires the argument.
class _StubDesignRules:
    pass


RULES = _StubDesignRules()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNetlistRule:
    """Tests for NetlistRule.check()."""

    def test_all_nets_declared_no_violations(self):
        """No violations when every pad net is declared."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                1: _StubNet(1, "VCC"),
                2: _StubNet(2, "GND"),
            },
            _footprints=[
                _StubFootprint(
                    reference="C1",
                    pads=[
                        _StubPad(number="1", net_number=1, net_name="VCC"),
                        _StubPad(number="2", net_number=2, net_name="GND"),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_undeclared_net_flagged(self):
        """A pad referencing an undeclared net produces a warning."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                1: _StubNet(1, "VCC"),
            },
            _footprints=[
                _StubFootprint(
                    reference="C3",
                    pads=[
                        _StubPad(number="1", net_number=1, net_name="VCC"),
                        _StubPad(
                            number="2",
                            net_number=0,
                            net_name='Net-(C3-Pad2)',
                        ),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 1

        v = results.violations[0]
        assert v.rule_id == "net_undeclared"
        assert v.severity == "warning"
        assert "C3-2" in v.message
        assert 'Net-(C3-Pad2)' in v.message
        assert v.items == ("C3-2",)

    def test_empty_net_name_skipped(self):
        """Pads with an empty net name (unconnected) are not flagged."""
        pcb = _StubPCB(
            _nets={0: _StubNet(0, "")},
            _footprints=[
                _StubFootprint(
                    reference="R1",
                    pads=[
                        _StubPad(number="1", net_number=0, net_name=""),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_net_zero_empty_name_skipped(self):
        """The conventional (net 0 '') unconnected net is not flagged."""
        pcb = _StubPCB(
            _nets={0: _StubNet(0, "")},
            _footprints=[
                _StubFootprint(
                    reference="R1",
                    pads=[
                        _StubPad(number="1", net_number=0, net_name=""),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_multiple_undeclared_nets(self):
        """Multiple pads on different footprints can all be flagged."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                1: _StubNet(1, "VCC"),
            },
            _footprints=[
                _StubFootprint(
                    reference="C1",
                    pads=[
                        _StubPad(number="1", net_number=0, net_name="MISSING_A"),
                    ],
                ),
                _StubFootprint(
                    reference="C2",
                    pads=[
                        _StubPad(number="2", net_number=0, net_name="MISSING_B"),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 2
        names = {v.items[0] for v in results.violations}
        assert names == {"C1-1", "C2-2"}

    def test_location_accounts_for_rotation(self):
        """Pad absolute position respects footprint rotation."""
        fp = _StubFootprint(
            reference="U1",
            position=(100.0, 50.0),
            rotation=90.0,
        )
        pad = _StubPad(number="1", position=(1.0, 0.0))

        x, y = _absolute_pad_position(fp, pad)
        # 90-degree rotation: (1,0) -> (0,1) relative, so absolute = (100, 51)
        assert x == pytest.approx(100.0, abs=1e-6)
        assert y == pytest.approx(51.0, abs=1e-6)

    def test_declared_net_with_nonzero_number_not_flagged(self):
        """A pad whose net_number > 0 with a declared name is fine."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                5: _StubNet(5, "SDA"),
            },
            _footprints=[
                _StubFootprint(
                    reference="U1",
                    pads=[
                        _StubPad(number="3", net_number=5, net_name="SDA"),
                    ],
                ),
            ],
        )
        results = NetlistRule().check(pcb, RULES)
        assert len(results.violations) == 0
