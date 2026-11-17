"""Per-class intra-pair skew limits of the Q16 ruling (Issue #7521, AC4)."""

from __future__ import annotations

import pytest

from kicad_tools.router.pair_protocols import PROTOCOL_DEFAULTS, PairProtocol
from kicad_tools.router.rules import NetClassRouting

Q16_LIMITS_MM = {
    "USB2_HS": 1.0,
    "USB3_SS": 0.127,
    "PCIE_G1": 0.127,
    "PCIE_G2_PLUS": 0.127,
    "HDMI_TMDS": 0.127,
    "SATA": 0.127,
    "MIPI_DPHY": 0.15,
    "DDR_DQS_CK": 0.15,
}


@pytest.mark.parametrize(("protocol", "limit"), sorted(Q16_LIMITS_MM.items()))
def test_resolved_tolerance_per_protocol(protocol, limit):
    nc = NetClassRouting(name="X", pair_protocol=protocol)
    assert nc.effective_skew_tolerance() == pytest.approx(limit)


@pytest.mark.parametrize("protocol", sorted(Q16_LIMITS_MM))
def test_q16_limits_are_cited(protocol):
    skew = PROTOCOL_DEFAULTS[PairProtocol[protocol]].max_skew_mm
    assert skew is not None and skew.cited
    assert "diffpair-skew-compensation" in skew.source


def test_explicit_class_tolerance_still_wins():
    nc = NetClassRouting(name="X", pair_protocol="USB3_SS", skew_tolerance_mm=0.3)
    assert nc.effective_skew_tolerance() == 0.3
