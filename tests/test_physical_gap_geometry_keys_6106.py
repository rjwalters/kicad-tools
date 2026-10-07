"""physical_copper_gap names copper by geometry, not UUID (Issue #6106)."""

from __future__ import annotations

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.physical_gap import check_physical_copper_gap
from kicad_tools.validate.rules.waivers import Waiver, _rewaive_candidates


def _board(a_uuid: str, b_uuid: str, via_uuid: str, arc_uuid: str) -> PCB:
    return PCB(
        parse_string(
            f"""(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "GND") (net 2 "VCC")
      (segment (start -4 0) (end 0 0) (width 0.2) (layer "F.Cu") (net 1) (uuid "{a_uuid}"))
      (segment (start -4 0.3) (end 0 0.3) (width 0.2) (layer "F.Cu") (net 2) (uuid "{b_uuid}"))
      (via (at 3 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "{via_uuid}"))
      (via (at 3 0.8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "{via_uuid}2"))
      (arc (start 6 0) (mid 7 1) (end 8 0) (width 0.2) (layer "F.Cu") (net 1)
        (uuid "{arc_uuid}"))
      (arc (start 6 0.5) (mid 7 1.5) (end 8 0.5) (width 0.2) (layer "F.Cu") (net 2)
        (uuid "{arc_uuid}2"))
    )"""
        )
    )


def _gaps(pcb):
    return sorted(
        (v for v in check_physical_copper_gap(pcb, 0.25).violations),
        key=lambda v: v.items,
    )


def test_same_copper_different_uuids_gives_identical_keys_and_evidence():
    first = _gaps(_board("a1", "b1", "v1", "r1"))
    second = _gaps(_board("a2", "b2", "v2", "r2"))
    assert first and len(first) == len(second)
    assert [v.key for v in first] == [v.key for v in second]
    assert [v.evidence_hash for v in first] == [v.evidence_hash for v in second]
    for v in first:
        assert all(item.startswith(("Trace@", "Via@", "Arc@")) for item in v.items)
    kinds = {item.split("@")[0] for v in first for item in v.items}
    assert {"Trace", "Via", "Arc"} <= kinds
    # Negative coordinates survive in the Trace@ names.
    assert any("-4/0" in item for v in first for item in v.items)


def test_uuid_keyed_waiver_hints_new_geometry_keys():
    findings = _gaps(_board("a1", "b1", "v1", "r1"))
    trace = next(v for v in findings if all(i.startswith("Trace@") for i in v.items))
    legacy_items = Waiver(
        rule="physical_copper_gap",
        items=frozenset({"a1", "b1"}),
        nets=frozenset(trace.nets),
        reason="r",
        issue="x#1",
    )
    assert trace.key in _rewaive_candidates(legacy_items, findings)
    legacy_key = Waiver(
        rule="physical_copper_gap",
        items=frozenset(),
        nets=frozenset(),
        reason="r",
        issue="",
        key=f"physical_copper_gap|a1,b1|{','.join(trace.nets)}|F.Cu",
    )
    assert trace.key in _rewaive_candidates(legacy_key, findings)
    # A current geometry-keyed waiver is not a legacy name.
    current = Waiver(
        rule="physical_copper_gap",
        items=frozenset(trace.items),
        nets=frozenset(trace.nets),
        reason="r",
        issue="x#1",
    )
    assert _rewaive_candidates(current, findings) == []
