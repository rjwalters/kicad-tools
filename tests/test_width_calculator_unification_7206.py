"""Width calculators delegate to the IPC-2221 reference (issue #7206 Part A)."""

import pytest

from kicad_tools.patterns.protection import (
    OvercurrentProtection,
    ReversePolarityProtection,
)
from kicad_tools.pcb.editor import SeeedFusion4Layer
from kicad_tools.physics.ampacity import width_for_current


def test_ipc2221_spot_values():
    assert width_for_current(1.0, 1.0) == pytest.approx(0.300, abs=0.002)
    assert width_for_current(1.0, 1.0, layer="internal") == pytest.approx(0.781, abs=0.002)
    assert width_for_current(3.0, 1.0) == pytest.approx(1.367, abs=0.003)


def test_seeed_power_trace_width_takes_milliamps():
    assert SeeedFusion4Layer.power_trace_width(1000) == pytest.approx(0.300, abs=0.002)
    assert SeeedFusion4Layer.power_trace_width(3000) == pytest.approx(1.367, abs=0.003)
    # Low currents clamp to the fab minimum, never kilometre-scale widths.
    assert SeeedFusion4Layer.power_trace_width(10) == SeeedFusion4Layer.MIN_TRACE_WIDTH


def _width(pattern):
    rules = pattern.get_routing_rules()
    names = ("current_carrying_width", "power_trace_width")
    return next(r.params["min_width_mm"] for r in rules if r.name in names)


@pytest.mark.parametrize("amps,expected", [(3.0, 1.367), (10.0, 7.194)])
def test_overcurrent_width_not_under_ipc(amps, expected):
    w = _width(OvercurrentProtection("fuse", amps))
    assert w == pytest.approx(expected, abs=0.01)
    assert w > amps * 0.3


@pytest.mark.parametrize("amps,expected", [(3.0, 1.367), (10.0, 7.194)])
def test_reverse_polarity_width_not_under_ipc(amps, expected):
    assert _width(ReversePolarityProtection("diode", amps)) == pytest.approx(expected, abs=0.01)


def test_copper_weight_respected():
    w1 = _width(OvercurrentProtection("fuse", 3.0))
    w2 = _width(OvercurrentProtection("fuse", 3.0, copper_oz=2.0))
    assert w2 == pytest.approx(w1 / 2, rel=0.05) or w2 < w1
    assert w2 == pytest.approx(width_for_current(3.0, 2.0))
    assert _width(ReversePolarityProtection("diode", 3.0, copper_oz=2.0)) == pytest.approx(w2)


def test_low_current_keeps_floor():
    assert _width(OvercurrentProtection("fuse", 0.1)) == 0.3


def test_invalid_copper_oz():
    with pytest.raises(ValueError):
        OvercurrentProtection("fuse", 1.0, copper_oz=0)
