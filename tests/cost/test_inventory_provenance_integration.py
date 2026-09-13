"""Inventory observations and backend failures remain distinct after batch lookup."""

from datetime import datetime
from unittest.mock import Mock

from kicad_tools.cost.availability import LCSCAvailabilityChecker
from kicad_tools.parts.lcsc import LCSCUnavailableError
from kicad_tools.parts.models import Part
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
