"""Zone-only nets must not read ``ampacity: checked`` (Issue #7210).

``AmpacityRule`` measures track segments only.  A net with a declared
current that is carried entirely by a copper pour has nothing measured, so
its coverage is ``unknown`` (blocking) rather than a vacuous ``checked``.
"""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.validate import coverage as cov


def _checker(*, declared: dict[str, float], segment_nets=(), zone_nets=(), keepout_nets=()):
    net_class_map = {name: SimpleNamespace(target_ampacity=amps) for name, amps in declared.items()}
    pcb = SimpleNamespace(
        segments=[SimpleNamespace(net_name=n) for n in segment_nets],
        zones=[SimpleNamespace(net_name=n, keepout=None) for n in zone_nets]
        + [SimpleNamespace(net_name=n, keepout=object()) for n in keepout_nets],
    )
    return SimpleNamespace(net_class_map=net_class_map, pcb=pcb)


def test_pour_only_net_with_declared_current_is_unknown_and_blocking() -> None:
    checker = _checker(declared={"+5V": 2.0, "+3.3V": 1.0}, zone_nets=["+5V", "+3.3V"])
    result = cov.precheck("ampacity", checker)
    assert result is not None
    assert result.state == cov.UNKNOWN
    assert result.blocking
    assert "zone-only, not measured" in result.status
    assert "+5V" in result.status and "+3.3V" in result.status


def test_net_with_tracks_and_pour_stays_checked() -> None:
    checker = _checker(declared={"+5V": 2.0}, segment_nets=["+5V"], zone_nets=["+5V"])
    assert cov.precheck("ampacity", checker) is None


def test_pour_only_net_without_declared_current_stays_non_blocking() -> None:
    checker = _checker(declared={"VBUS": 1.0}, segment_nets=["VBUS"], zone_nets=["GND"])
    assert cov.precheck("ampacity", checker) is None


def test_keepout_zone_is_not_a_pour() -> None:
    checker = _checker(declared={"+5V": 2.0}, keepout_nets=["+5V"])
    assert cov.precheck("ampacity", checker) is None


def test_no_declared_targets_still_skipped() -> None:
    checker = _checker(declared={}, zone_nets=["+5V"])
    result = cov.precheck("ampacity", checker)
    assert result is not None and result.state == cov.SKIPPED
