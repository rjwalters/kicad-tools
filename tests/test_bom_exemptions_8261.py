"""Declared BOM exemptions (issue #8261)."""

from __future__ import annotations

import pytest

from kicad_tools.export.bom_spec_overlay import declared_exemptions
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig
from kicad_tools.parts.cache import PartsCache
from kicad_tools.schema.bom import BOM, BOMItem
from kicad_tools.spec.schema import BOMEntry


def test_exempt_requires_reason():
    with pytest.raises(ValueError, match="exempt_reason"):
        BOMEntry(ref="J1", part="X", exempt="hand_solder")


def test_exempt_rejects_lcsc():
    with pytest.raises(ValueError, match="must not set lcsc"):
        BOMEntry(ref="J1", part="X", exempt="consigned", exempt_reason="r", lcsc="C1")


def test_reason_without_exempt_rejected():
    with pytest.raises(ValueError):
        BOMEntry(ref="J1", part="X", exempt_reason="r")


def test_declared_exemptions_expand_ranges():
    e = BOMEntry(ref="Q1-Q2", part="X", exempt="consigned", exempt_reason="owner supplies")
    assert declared_exemptions([e, BOMEntry(ref="U1", part="Y")]) == {
        "Q1": ("consigned", "owner supplies"),
        "Q2": ("consigned", "owner supplies"),
    }


def _checker(tmp_path, monkeypatch, exemptions):
    items = [BOMItem(reference="J1", value="HDR", footprint="Conn:X", lib_id="C:J", lcsc="C999999")]
    checker = PreflightChecker(
        pcb_path=tmp_path / "b.kicad_pcb",
        schematic_path=None,
        config=PreflightConfig(),
        parts_cache=PartsCache(db_path=tmp_path / "c.db"),
    )
    monkeypatch.setattr(checker, "_load_bom", lambda: BOM(items=items))
    monkeypatch.setattr(checker, "_items_with_spec_overlay", lambda bom: list(bom.items))
    monkeypatch.setattr(checker, "_spec_exemptions", lambda: exemptions)
    monkeypatch.setattr(checker, "_parts_catalog", None, raising=False)
    return checker


def test_unverified_warns_when_undeclared(tmp_path, monkeypatch):
    r = _checker(tmp_path, monkeypatch, {}).check_bom_lcsc_values()
    assert r.status == "WARN"


def test_declared_exempt_is_ok_and_reported(tmp_path, monkeypatch):
    r = _checker(
        tmp_path, monkeypatch, {"J1": ("hand_solder", "owner solders")}
    ).check_bom_lcsc_values()
    assert r.status == "OK"
    assert "exempt (declared): J1 [hand_solder: owner solders]" in r.details


# ---------------------------------------------------------------------------
# Review follow-ups (PR #8265): real spec load path, conflicts, stale refs
# ---------------------------------------------------------------------------

_KCT = """kct_version: '1.0'
project:
  name: exempt demo
  revision: A
requirements:
  manufacturing:
    target_fab: jlcpcb-tier1
    layers:
      preferred: 2
    assembly: both
bom_entries:
{entries}
"""


def _write_spec(tmp_path, *, refs):
    entries = "\n".join(
        f"- ref: {r}\n  part: X\n  exempt: consigned\n  exempt_reason: owner supplies" for r in refs
    )
    (tmp_path / "project.kct").write_text(_KCT.format(entries=entries))


def _real_checker(tmp_path, items):
    from datetime import datetime, timezone

    from kicad_tools.parts.models import Part

    cache = PartsCache(db_path=tmp_path / "c.db")
    cache.put(
        Part(
            lcsc_part="C1234",
            value="10k",
            package="0402",
            stock=0,
            stock_source="live",
            fetched_at=datetime.now(tz=timezone.utc),
        )
    )
    checker = PreflightChecker(
        pcb_path=tmp_path / "b.kicad_pcb",
        schematic_path=None,
        config=PreflightConfig(),
        parts_cache=cache,
    )
    checker._load_bom = lambda: BOM(items=items)  # type: ignore[method-assign]
    checker._items_with_spec_overlay = lambda bom: list(bom.items)  # type: ignore[method-assign]
    checker._parts_catalog = None
    return checker


def _item(ref, lcsc):
    return BOMItem(reference=ref, value="10k", footprint="R_0402", lib_id="Device:R", lcsc=lcsc)


def test_real_spec_file_is_picked_up(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    checker = _real_checker(tmp_path, [_item("R1", "")])
    assert set(checker._spec_exemptions()) == {"R1"}
    r = checker.check_bom_lcsc_values()
    assert r.status == "OK"
    assert "exempt (declared): R1 [consigned: owner supplies]" in r.details


def test_exempt_unknown_number_skips_stock(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    r = _real_checker(tmp_path, [_item("R1", "C999999")]).check_bom_lcsc_stock()
    assert r.status == "OK"
    assert "No assigned LCSC parts" in r.message


def test_exempt_with_known_part_still_checks_stock(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    r = _real_checker(tmp_path, [_item("R1", "C1234")]).check_bom_lcsc_stock()
    assert r.status == "FAIL"
    assert "C1234" in r.details


def test_exempt_with_known_part_is_reported_conflict(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    r = _real_checker(tmp_path, [_item("R1", "C1234")]).check_bom_lcsc_values()
    assert r.status == "WARN"
    assert "exempt ref carries a verifiable LCSC part: R1 -> C1234" in r.details


def test_stale_exemption_reported(tmp_path):
    _write_spec(tmp_path, refs=["R1", "R9"])
    r = _real_checker(tmp_path, [_item("R1", "")]).check_bom_lcsc_values()
    assert r.status == "OK"
    assert "stale exemption (no such ref): R9" in r.details


def test_gate_bom_surfaces_conflict_and_stale_details(tmp_path):
    """Readiness Gate 4 keeps exemption details visible (behaviour, not a grep)."""
    import json

    from kicad_tools.cli import readiness_cmd

    out = tmp_path / "out"
    out.mkdir()
    (out / "bom_jlcpcb.csv").write_text("Designator,LCSC Part #\nR1,C1234\n")
    (out / "cpl_jlcpcb.csv").write_text("Designator,Mid X\nR1,1\n")
    details = (
        "exempt ref carries a verifiable LCSC part: R1 -> C1234 [consigned: owner supplies]"
        " | stale exemption (no such ref): R9"
    )
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "preflight": [
                    {
                        "name": "bom_lcsc_values",
                        "status": "WARN",
                        "message": "1 of 1 verified",
                        "details": details,
                    }
                ]
            }
        )
    )
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("not a pcb")
    options = readiness_cmd.ReadinessOptions(
        board_dir=tmp_path,
        pcb=pcb,
        schematic=None,
        project=None,
        manufacturer="jlcpcb",
        mode="assembly",
        output_dir=out,
        evidence_dir=tmp_path / "ev",
    )
    outcome = readiness_cmd._gate_bom(options, None, [])  # type: ignore[arg-type]
    assert outcome.status == readiness_cmd.PASSED
    assert "exempt ref carries a verifiable LCSC part: R1 -> C1234" in outcome.detail
    assert "stale exemption (no such ref): R9" in outcome.detail


# ---------------------------------------------------------------------------
# Re-review blockers (PR #8265): an exemption must not weaken other verdicts
# ---------------------------------------------------------------------------

_R0805 = "Resistor_SMD:R_0805_2012Metric"


def _cache_with(tmp_path, *parts):
    cache = PartsCache(db_path=tmp_path / "c.db")
    for p in parts:
        cache.put(p)
    return cache


def _part(lcsc, value, package, desc):
    from datetime import datetime, timezone

    from kicad_tools.parts.models import Part

    return Part(
        lcsc_part=lcsc,
        mfr_part="X",
        description=desc,
        package=package,
        value=value,
        stock=10000,
        stock_source="live",
        fetched_at=datetime.now(tz=timezone.utc),
    )


def _spec_checker(tmp_path, items, parts, strict=False):
    checker = PreflightChecker(
        pcb_path=tmp_path / "b.kicad_pcb",
        schematic_path=None,
        config=PreflightConfig(skip_drc=True, skip_erc=True, strict_report_source=strict),
        parts_cache=_cache_with(tmp_path, *parts),
    )
    checker._load_bom = lambda: BOM(items=items)  # type: ignore[method-assign]
    checker._items_with_spec_overlay = lambda bom: list(bom.items)  # type: ignore[method-assign]
    checker._parts_catalog = None
    return checker


_CAP_10U = ("C5555", "10uF", "0603", "10uF 0603 ceramic capacitor")
_RES_137K = ("C1234", "137k\u03a9", "0805", "137k 1% 0805 resistor")


def test_exempt_ref_with_wrong_part_still_fails(tmp_path):
    """#8113 guard: a wrong part on an exempt row stays FAIL."""
    _write_spec(tmp_path, refs=["R1"])
    item = BOMItem(reference="R1", value="137k", footprint=_R0805, lib_id="Device:R", lcsc="C5555")
    r = _spec_checker(tmp_path, [item], [_part(*_CAP_10U)]).check_bom_lcsc_values()
    assert r.status == "FAIL"
    assert "R1 (137k) -> C5555" in r.details
    assert "does not match requested '137k'" in r.details
    assert "exempt ref carries a verifiable LCSC part: R1 -> C5555" in r.details
    assert PreflightChecker.has_failures([r])


def test_conflict_does_not_hide_undeclared_unverified(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    items = [
        BOMItem(reference="R1", value="137k", footprint=_R0805, lib_id="Device:R", lcsc="C1234"),
        BOMItem(reference="R2", value="10k", footprint=_R0805, lib_id="Device:R", lcsc="C999999"),
    ]
    r = _spec_checker(tmp_path, items, [_part(*_RES_137K)]).check_bom_lcsc_values()
    assert r.status == "WARN"
    assert "unverified: R2 -> C999999" in r.details
    assert "exempt ref carries a verifiable LCSC part: R1 -> C1234" in r.details


def test_conflict_does_not_hide_strict_header_fail(tmp_path):
    from kicad_tools.parts.models import Part

    _write_spec(tmp_path, refs=["R1"])
    header = Part(
        lcsc_part="C999998",
        mfr_part="ZX-HDR-ABC",
        package="Through Hole,P=2.54mm",
        description="Pin Header 2.54mm Pitch Single Row Through Hole",
    )
    items = [
        BOMItem(reference="R1", value="137k", footprint=_R0805, lib_id="Device:R", lcsc="C1234"),
        BOMItem(
            reference="J1",
            value="Conn",
            footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
            lib_id="Connector:Conn",
            lcsc="C999998",
        ),
    ]
    r = _spec_checker(tmp_path, items, [_part(*_RES_137K), header], strict=True)
    result = r.check_bom_lcsc_values()
    assert result.status == "FAIL"
    assert "pin count not verifiable" in result.message
    assert "J1 -> C999998 (pin count not stated by part)" in result.details
    assert "exempt ref carries a verifiable LCSC part: R1 -> C1234" in result.details


def test_conflict_alone_is_never_ok(tmp_path):
    _write_spec(tmp_path, refs=["R1"])
    item = BOMItem(reference="R1", value="137k", footprint=_R0805, lib_id="Device:R", lcsc="C1234")
    r = _spec_checker(tmp_path, [item], [_part(*_RES_137K)]).check_bom_lcsc_values()
    assert r.status == "WARN"
    assert "spec and BOM disagree" in r.message
