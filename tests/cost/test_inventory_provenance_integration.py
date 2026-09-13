"""Inventory observations and backend failures remain distinct after batch lookup."""

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicad_tools.cli.parts_cmd import _availability_table
from kicad_tools.cost.availability import (
    AvailabilityStatus,
    BOMAvailabilityResult,
    LCSCAvailabilityChecker,
    PartAvailabilityResult,
)
from kicad_tools.parts.lcsc import LCSCUnavailableError
from kicad_tools.parts.models import Part, SearchResult
from kicad_tools.schema.bom import BOMItem


def test_partial_lookup_retains_unverified_and_unavailable_counts():
    observed = datetime.now()
    parts = {
        "C1": Part(lcsc_part="C1", stock=1000, stock_source="offline"),
        "C2": Part(lcsc_part="C2", stock=1000, stock_source="live", fetched_at=observed),
        "C3": Part(lcsc_part="C3", stock=0, stock_source="live", fetched_at=observed),
    }
    client = Mock()
    client.lookup_many.side_effect = LCSCUnavailableError(
        "Backend unavailable", partial_results=parts, unavailable_parts={"C4"}
    )
    checker = LCSCAvailabilityChecker(find_alternatives=False)
    checker._client = client
    items = [
        BOMItem(reference=f"R{i}", value="10k", footprint="0402", lib_id="Device:R", lcsc=f"C{i}")
        for i in range(1, 6)
    ]

    report = checker.check_items(items).to_dict()

    assert [item["status"] for item in report["items"]] == [
        "unknown",
        "available",
        "out_of_stock",
        "unavailable",
        "not_found",
    ]
    assert [item["sufficient_stock"] for item in report["items"]] == [
        False,
        True,
        False,
        False,
        False,
    ]
    assert report["summary"]["unverified"] == 1
    assert report["summary"]["unavailable"] == 1
    assert report["summary"]["out_of_stock"] == 1
    assert report["summary"]["missing"] == 1
    assert report["summary"]["all_available"] is False
    assert report["items"][0]["inventory"]["observed_at"] is None
    assert report["items"][1]["inventory"]["observed_at"] == observed.isoformat()
    client.lookup_many.assert_called_once()


@pytest.mark.parametrize("status", [AvailabilityStatus.LOW_STOCK, AvailabilityStatus.OUT_OF_STOCK])
@pytest.mark.parametrize("source", ["live", "offline_catalog"])
def test_availability_table_preserves_alternative_stock_provenance(status, source, capsys):
    original = Part(lcsc_part="C1", stock=0, stock_source="live", fetched_at=datetime.now())
    alternative = Part(
        lcsc_part="C2",
        stock=1000,
        stock_source=source,
        fetched_at=datetime.now() if source == "live" else None,
    )
    checker = LCSCAvailabilityChecker()
    checker._client = Mock()
    checker._client.search.return_value = SearchResult(query="TEST", parts=[alternative])
    alternatives = checker._find_alternatives("TEST", original, 10)
    item = PartAvailabilityResult(
        reference="R1",
        value="TEST",
        footprint="0603",
        mpn="TEST",
        lcsc_part="C1",
        quantity_needed=10,
        quantity_available=5 if status == AvailabilityStatus.LOW_STOCK else 0,
        status=status,
        in_stock=status == AvailabilityStatus.LOW_STOCK,
        alternatives=alternatives,
        inventory=original.inventory_provenance(),
    )
    result = BOMAvailabilityResult(items=[item])

    _availability_table(result, [item], Path("board.kicad_sch"), 1)

    output = capsys.readouterr().out
    if source == "live":
        assert "C2: 1,000 in stock" in output
        assert "Stock unverified" not in output
    else:
        assert "C2: 1,000 reported (stock unverified)" in output
        assert "C2: 1,000 in stock" not in output
        assert (
            "Stock unverified for alternatives: refresh live inventory before ordering." in output
        )
    serialized = result.to_dict()["items"][0]["alternatives"][0]
    assert serialized["stock"] == 1000
    assert serialized["inventory"]["source"] == source
    assert serialized["inventory"]["stock_verified"] == (source == "live")
