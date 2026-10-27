"""Tests for kct check finding scope (Issue #8016)."""

import pytest

from kicad_tools.validate.scope import CheckScope, parse_bbox
from kicad_tools.validate.violations import DRCViolation


def _v(loc=None, items=(), nets=()):
    return DRCViolation(
        rule_id="r",
        severity="error",
        message="m",
        location=loc,
        items=tuple(items),
        nets=tuple(nets),
    )


def test_bbox_inclusive_and_swapped_corners():
    scope = CheckScope.from_args(bbox="10,10,0,0")
    assert scope.bbox == (0, 0, 10, 10)
    assert scope.in_scope(_v(loc=(10, 0)))
    assert not scope.in_scope(_v(loc=(10.01, 5)))


def test_refs_only_and_nets_only():
    assert CheckScope.from_args(refs="R1").in_scope(_v(loc=(99, 99), items=("R1",)))
    assert not CheckScope.from_args(refs="R1").in_scope(_v(loc=(0, 0), items=("R2",)))
    assert CheckScope.from_args(nets="GND").in_scope(_v(nets=("GND",)))
    assert not CheckScope.from_args(nets="GND").in_scope(_v(nets=("VCC",)))


def test_union_and_unscopable_kept():
    scope = CheckScope.from_args(bbox="0,0,1,1", refs="R1", nets="GND")
    findings = [
        _v(loc=(0.5, 0.5)),
        _v(loc=(50, 50), items=("R1",)),
        _v(loc=(50, 50), nets=("GND",)),
        _v(loc=(50, 50), items=("R9",)),
        _v(),  # board-level
        _v(items=("R9",)),  # items set, no location: scoped by items
    ]
    kept, rec = scope.apply(findings)
    assert len(kept) == 4
    assert rec["findings_total"] == 6
    assert rec["findings_in_scope"] == 4
    assert rec["unscopable_kept"] == 1
    assert rec["signoff_eligible"] is False


def test_no_flags_is_none():
    assert CheckScope.from_args() is None


@pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", "1,2,3,nan", ""])
def test_malformed_bbox(bad):
    with pytest.raises(ValueError):
        parse_bbox(bad)
