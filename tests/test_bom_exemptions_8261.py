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
