"""Tests for `kct parts availability` (issue #8208)."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from kicad_tools.cost.availability import (
    AvailabilityStatus,
    BOMAvailabilityResult,
    PartAvailabilityResult,
)
from kicad_tools.export.assembly import AssemblyConfig
from kicad_tools.export.bom_enrich import EnrichmentEntry, EnrichmentReport
from kicad_tools.schema.bom import BOM, BOMItem


def _result(ref: str, status: AvailabilityStatus, stock: int = 0, needed: int = 5):
    return PartAvailabilityResult(
        reference=ref,
        value="v",
        footprint="fp",
        mpn=None,
        lcsc_part="C1" if status != AvailabilityStatus.NO_LCSC else None,
        quantity_needed=needed,
        quantity_available=stock,
        status=status,
        in_stock=status in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.LOW_STOCK)
        and stock > 0,
    )


def _args(tmp_path, **kw):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text("")
    base = {
        "schematic": str(sch),
        "quantity": 1,
        "format": "json",
        "no_alternatives": True,
        "issues_only": False,
        "no_auto_lcsc": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(args, bom, fake_enrich, capsys):
    from kicad_tools.cli import parts_cmd

    seen = {}

    def check_bom(self, b, quantity=1):
        seen["lcsc"] = [i.lcsc for i in b.items]
        return BOMAvailabilityResult(items=[_result("R1", AvailabilityStatus.AVAILABLE, 1000)])

    with (
        patch("kicad_tools.schema.bom.extract_bom", return_value=bom),
        patch("kicad_tools.export.bom_enrich.enrich_bom_lcsc", side_effect=fake_enrich) as m,
        patch("kicad_tools.cost.availability.LCSCAvailabilityChecker.check_bom", check_bom),
    ):
        rc = parts_cmd._availability(args)
    return rc, seen, m, capsys.readouterr().out


def _bom():
    return BOM(items=[BOMItem(reference="R1", value="10k", footprint="0805", lib_id="Device:R")])


def _fake(items, **kw):
    for i in items:
        i.lcsc = "C17414"
    return EnrichmentReport(entries=[EnrichmentEntry("10k", "0805", ["R1"], "C17414", "auto")])


def test_auto_matches_lcsc_like_export(tmp_path, capsys):
    rc, seen, m, out = _run(_args(tmp_path), _bom(), _fake, capsys)
    assert seen["lcsc"] == ["C17414"]
    cfg = AssemblyConfig()
    assert m.call_args.kwargs["prefer_basic"] == cfg.auto_lcsc_prefer_basic
    assert m.call_args.kwargs["min_stock"] == cfg.auto_lcsc_min_stock
    assert json.loads(out)["items"][0]["lcsc_source"] == "auto"


def test_no_auto_lcsc_opt_out(tmp_path, capsys):
    rc, seen, m, out = _run(_args(tmp_path, no_auto_lcsc=True), _bom(), _fake, capsys)
    m.assert_not_called()
    assert seen["lcsc"] == [""]


def test_summary_counts_sum_to_total():
    S = AvailabilityStatus
    res = BOMAvailabilityResult(
        items=[
            _result("R1", S.AVAILABLE, 1000),
            _result("R2", S.LOW_STOCK, 2),
            _result("R3", S.OUT_OF_STOCK),
            _result("R4", S.UNKNOWN),
            _result("R5", S.UNAVAILABLE),
            _result("R6", S.NO_LCSC),
            _result("R7", S.NOT_FOUND),
            _result("R8", S.DISCONTINUED),
        ]
    )
    s = res.summary()
    parts = (
        "available",
        "low_stock",
        "out_of_stock",
        "unverified",
        "unavailable",
        "missing",
        "discontinued",
    )
    assert sum(s[k] for k in parts) == s["total_items"] == 8


def test_table_lists_unverified(tmp_path, capsys):
    from kicad_tools.cli import parts_cmd

    res = BOMAvailabilityResult(items=[_result("R4", AvailabilityStatus.UNKNOWN)])
    parts_cmd._availability_table(res, res.items, tmp_path / "x.kicad_sch", 1)
    out = capsys.readouterr().out
    assert "STOCK UNVERIFIED (1 parts)" in out and "R4" in out


# --- Issue #8518: stale-cache refetch, --no-cache, summary partition ---------


def _part(stock: int, verified: bool):
    from unittest.mock import MagicMock

    p = MagicMock()
    p.stock = stock
    p.stock_verified = verified
    p.prices = []
    p.mfr_part = "MPN"
    p.min_order = 1
    p.inventory_provenance.return_value = {}
    return p


def _checker(client):
    from kicad_tools.cost.availability import LCSCAvailabilityChecker

    c = LCSCAvailabilityChecker(find_alternatives=False)
    c._client = client
    return c


def _one_bom():
    return BOM(
        items=[BOMItem(reference="R1", value="10k", footprint="0805", lib_id="Device:R", lcsc="C1")]
    )


def test_stale_cache_entry_is_refetched_live():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.lookup_many.side_effect = [{"C1": _part(500, False)}, {"C1": _part(500, True)}]
    res = _checker(client).check_bom(_one_bom())
    assert client.lookup_many.call_args_list[1].kwargs == {"bypass_cache": True}
    assert res.items[0].status == AvailabilityStatus.AVAILABLE
    assert res.items[0].error is None


def test_fresh_cache_entry_is_not_refetched():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.lookup_many.return_value = {"C1": _part(500, True)}
    _checker(client).check_bom(_one_bom())
    assert client.lookup_many.call_count == 1


def test_refetch_failure_is_named_per_item():
    from unittest.mock import MagicMock

    from kicad_tools.parts.lcsc import LCSCUnavailableError

    client = MagicMock()
    client.lookup_many.side_effect = [
        {"C1": _part(500, False)},
        LCSCUnavailableError("HTTP 429 rate limited", unavailable_parts={"C1"}),
    ]
    res = _checker(client).check_bom(_one_bom())
    item = res.items[0]
    assert item.status == AvailabilityStatus.UNKNOWN
    assert "Live refetch failed" in item.error and "429" in item.error


def test_bypass_cache_forwarded_to_lookup_many():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.lookup_many.return_value = {"C1": _part(500, True)}
    _checker(client).check_bom(_one_bom(), bypass_cache=True)
    assert client.lookup_many.call_args.kwargs == {"bypass_cache": True}


def test_summary_counts_low_stock_with_sufficient_quantity():
    S = AvailabilityStatus
    res = BOMAvailabilityResult(
        items=[
            _result("R1", S.AVAILABLE, 1000),
            _result("R2", S.LOW_STOCK, 50, needed=5),  # enough, but below threshold
        ]
    )
    s = res.summary()
    assert s["low_stock"] == 1 and s["available"] == 1
    assert s["all_available"] is True


def test_no_cache_flag_reaches_check_bom(tmp_path, capsys):
    from kicad_tools.cli import parts_cmd

    seen = {}

    def check_bom(self, b, quantity=1, bypass_cache=False):
        seen["bypass"] = bypass_cache
        return BOMAvailabilityResult(items=[_result("R1", AvailabilityStatus.AVAILABLE, 1000)])

    with (
        patch("kicad_tools.schema.bom.extract_bom", return_value=_bom()),
        patch("kicad_tools.export.bom_enrich.enrich_bom_lcsc", side_effect=_fake),
        patch("kicad_tools.cost.availability.LCSCAvailabilityChecker.check_bom", check_bom),
    ):
        parts_cmd._availability(_args(tmp_path, no_cache=True))
    assert seen["bypass"] is True
