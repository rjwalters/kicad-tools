"""Tests for the single-pad-net DRC rule (single_pad_net)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.single_pad_net import SinglePadNetRule

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
    """Minimal PCB stub implementing the subset used by SinglePadNetRule."""

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _footprints: list[_StubFootprint] = field(default_factory=list)

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    @property
    def footprints(self) -> list[_StubFootprint]:
        return self._footprints


# Stub design rules -- the single-pad-net rule ignores them, but the
# interface requires the argument.
class _StubDesignRules:
    pass


RULES = _StubDesignRules()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSinglePadNetRule:
    """Tests for SinglePadNetRule.check()."""

    def test_all_multi_pad_nets_no_violations(self):
        """No violations when every net has 2+ pads."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                1: _StubNet(1, "DATA"),
                2: _StubNet(2, "CLK"),
            },
            _footprints=[
                _StubFootprint(
                    reference="U1",
                    pads=[
                        _StubPad(number="1", net_number=1, net_name="DATA"),
                        _StubPad(number="2", net_number=2, net_name="CLK"),
                    ],
                ),
                _StubFootprint(
                    reference="U2",
                    pads=[
                        _StubPad(number="1", net_number=1, net_name="DATA"),
                        _StubPad(number="2", net_number=2, net_name="CLK"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_single_pad_signal_net_flagged(self):
        """A signal net with only one pad produces an error."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                6: _StubNet(6, "SWDIO"),
            },
            _footprints=[
                _StubFootprint(
                    reference="J1",
                    position=(150.0, 100.0),
                    pads=[
                        _StubPad(
                            number="2",
                            net_number=6,
                            net_name="SWDIO",
                            position=(0.0, -3.81),
                        ),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 1

        v = results.violations[0]
        assert v.rule_id == "single_pad_net"
        assert v.severity == "error"
        assert "SWDIO" in v.message
        assert "J1-2" in v.message
        assert "missing footprint" in v.message
        assert v.items == ("J1-2",)
        assert v.nets == ("SWDIO",)
        # Location should reflect footprint+pad combination.
        assert v.location is not None
        assert v.location[0] == 150.0
        assert v.location[1] == 100.0 - 3.81

    def test_single_pad_ground_suppressed(self):
        """A single-pad GND net (testpoint) is silently allowed."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "GND"),
            },
            _footprints=[
                _StubFootprint(
                    reference="TP1",
                    pads=[
                        _StubPad(number="1", net_number=3, net_name="GND"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_single_pad_power_suppressed(self):
        """A single-pad +3.3V marker is silently allowed."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                4: _StubNet(4, "+3.3V"),
            },
            _footprints=[
                _StubFootprint(
                    reference="TP2",
                    pads=[
                        _StubPad(number="1", net_number=4, net_name="+3.3V"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_single_pad_vcc_suppressed(self):
        """A single-pad VCC net is silently allowed (pour-net pattern)."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                5: _StubNet(5, "VCC"),
            },
            _footprints=[
                _StubFootprint(
                    reference="TP3",
                    pads=[
                        _StubPad(number="1", net_number=5, net_name="VCC"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_mixed_signal_and_pour_nets(self):
        """Mix of 2 single-pad signal + 1 single-pad GND -> 2 errors."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                3: _StubNet(3, "GND"),
                6: _StubNet(6, "SWDIO"),
                7: _StubNet(7, "SWCLK"),
            },
            _footprints=[
                _StubFootprint(
                    reference="J1",
                    pads=[
                        _StubPad(number="1", net_number=3, net_name="GND"),
                        _StubPad(number="2", net_number=6, net_name="SWDIO"),
                        _StubPad(number="3", net_number=7, net_name="SWCLK"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 2
        # Both errors are signal nets, neither is GND.
        flagged_nets = {v.nets[0] for v in results.violations}
        assert flagged_nets == {"SWDIO", "SWCLK"}
        for v in results.violations:
            assert v.severity == "error"
            assert v.rule_id == "single_pad_net"

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
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_net_zero_with_empty_name_skipped(self):
        """The conventional (net 0, '') unconnected net is not flagged."""
        pcb = _StubPCB(
            _nets={0: _StubNet(0, "")},
            _footprints=[
                _StubFootprint(
                    reference="R1",
                    pads=[
                        _StubPad(number="1", net_number=0, net_name=""),
                        _StubPad(number="2", net_number=0, net_name=""),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 0

    def test_multiple_single_pad_signal_nets(self):
        """Multiple single-pad signal nets each produce one error."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                6: _StubNet(6, "SWDIO"),
                7: _StubNet(7, "SWCLK"),
                8: _StubNet(8, "SWO"),
                9: _StubNet(9, "NRST"),
            },
            _footprints=[
                _StubFootprint(
                    reference="J1",
                    pads=[
                        _StubPad(number="2", net_number=6, net_name="SWDIO"),
                        _StubPad(number="3", net_number=7, net_name="SWCLK"),
                        _StubPad(number="4", net_number=8, net_name="SWO"),
                        _StubPad(number="5", net_number=9, net_name="NRST"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 4
        flagged_nets = {v.nets[0] for v in results.violations}
        assert flagged_nets == {"SWDIO", "SWCLK", "SWO", "NRST"}
        flagged_items = {v.items[0] for v in results.violations}
        assert flagged_items == {"J1-2", "J1-3", "J1-4", "J1-5"}

    def test_to_dict_resolves_violation_type(self):
        """JSON output 'type' field round-trips to single_pad_net (not unknown)."""
        pcb = _StubPCB(
            _nets={0: _StubNet(0, ""), 6: _StubNet(6, "SWDIO")},
            _footprints=[
                _StubFootprint(
                    reference="J1",
                    pads=[
                        _StubPad(number="2", net_number=6, net_name="SWDIO"),
                    ],
                ),
            ],
        )
        results = SinglePadNetRule().check(pcb, RULES)
        assert len(results.violations) == 1
        d = results.violations[0].to_dict()
        assert d["rule_id"] == "single_pad_net"
        assert d["type"] == "single_pad_net"
        assert d["severity"] == "error"
