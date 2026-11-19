"""Regression tests for ungrouped BOM formatter output (issue #9382).

``group_by_value=False`` must emit one row per item. The JLCPCB and PCBWay
formatters keyed their non-grouping branch on field values, so two parts
sharing ``(value, footprint[, lcsc])`` collided and every designator but the
last was silently dropped from the fab BOM. The Seeed and Generic formatters
key on the item's reference and were never affected; they are guarded here so
they stay that way.
"""

from __future__ import annotations

import csv
import io

from kicad_tools.export.bom_formats import (
    BOMExportConfig,
    GenericBOMFormatter,
    JLCPCBBOMFormatter,
    PCBWayBOMFormatter,
    SeeedBOMFormatter,
)
from kicad_tools.schema.bom import BOMItem


def _make_item(
    ref: str,
    value: str = "10k",
    footprint: str = "0402",
    lcsc: str = "C1",
    dnp: bool = False,
) -> BOMItem:
    return BOMItem(
        reference=ref,
        value=value,
        footprint=footprint,
        lib_id="Device:R",
        lcsc=lcsc,
        dnp=dnp,
    )


def _duplicate_items() -> list[BOMItem]:
    """Two identical 10k/0402/C1 resistors plus two identical no-LCSC 4k7s."""
    return [
        _make_item("R1"),
        _make_item("R2"),
        _make_item("R3", value="4k7", lcsc=""),
        _make_item("R4", value="4k7", lcsc=""),
    ]


def _rows(output: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(output)))


class TestJLCPCBUngrouped:
    def test_every_designator_reaches_the_csv(self) -> None:
        formatter = JLCPCBBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(formatter.format(_duplicate_items()))

        # Header + one row per item (the bug collapsed each pair to one row).
        assert len(rows) == 5
        assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]
        designators = {row[1] for row in rows[1:]}
        assert designators == {"R1", "R2", "R3", "R4"}

    def test_empty_lcsc_items_do_not_collide(self) -> None:
        formatter = JLCPCBBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(
            formatter.format(
                [_make_item("R3", value="4k7", lcsc=""), _make_item("R4", value="4k7", lcsc="")]
            )
        )

        assert len(rows) == 3
        assert {row[1] for row in rows[1:]} == {"R3", "R4"}
        assert all(row[3] == "" for row in rows[1:])

    def test_ordering_is_deterministic_regardless_of_input_order(self) -> None:
        formatter = JLCPCBBOMFormatter(BOMExportConfig(group_by_value=False))
        items = _duplicate_items()

        forward = formatter.format(items)
        backward = formatter.format(list(reversed(items)))

        assert forward == backward

    def test_dnp_items_are_still_filtered_ungrouped(self) -> None:
        formatter = JLCPCBBOMFormatter(BOMExportConfig(group_by_value=False))
        items = [
            _make_item("R1"),
            _make_item("R2", dnp=True),
            _make_item("R3", value="4k7", lcsc=""),
            _make_item("R4", value="4k7", lcsc="", dnp=True),
        ]

        rows = _rows(formatter.format(items))

        # Only R1 and R3 survive the DNP filter; one row each.
        assert len(rows) == 3
        assert {row[1] for row in rows[1:]} == {"R1", "R3"}

    def test_grouped_output_is_unchanged(self) -> None:
        formatter = JLCPCBBOMFormatter()

        assert formatter.format(_duplicate_items()) == (
            'Comment,Designator,Footprint,LCSC Part #\n10k,"R1,R2",0402,C1\n4k7,"R3,R4",0402,\n'
        )


class TestPCBWayUngrouped:
    def test_every_designator_reaches_the_csv(self) -> None:
        formatter = PCBWayBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(formatter.format(_duplicate_items()))

        assert len(rows) == 5
        assert rows[0] == [
            "Item",
            "Designator",
            "Qty",
            "Manufacturer",
            "Mfr. Part #",
            "Description/Value",
            "Package/Footprint",
        ]
        # One row per item, qty 1, sequential item numbers.
        assert [row[1] for row in rows[1:]] == ["R1", "R2", "R3", "R4"]
        assert all(row[2] == "1" for row in rows[1:])
        assert [row[0] for row in rows[1:]] == ["1", "2", "3", "4"]

    def test_empty_lcsc_items_do_not_collide(self) -> None:
        formatter = PCBWayBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(
            formatter.format(
                [_make_item("R3", value="4k7", lcsc=""), _make_item("R4", value="4k7", lcsc="")]
            )
        )

        assert len(rows) == 3
        assert {row[1] for row in rows[1:]} == {"R3", "R4"}

    def test_ordering_is_deterministic_regardless_of_input_order(self) -> None:
        formatter = PCBWayBOMFormatter(BOMExportConfig(group_by_value=False))
        items = _duplicate_items()

        assert formatter.format(items) == formatter.format(list(reversed(items)))

    def test_dnp_items_are_still_filtered_ungrouped(self) -> None:
        formatter = PCBWayBOMFormatter(BOMExportConfig(group_by_value=False))
        items = [
            _make_item("R1"),
            _make_item("R2", dnp=True),
            _make_item("R3", value="4k7", lcsc=""),
            _make_item("R4", value="4k7", lcsc="", dnp=True),
        ]

        rows = _rows(formatter.format(items))

        assert len(rows) == 3
        assert [row[1] for row in rows[1:]] == ["R1", "R3"]

    def test_grouped_output_is_unchanged(self) -> None:
        formatter = PCBWayBOMFormatter()

        assert formatter.format(_duplicate_items()) == (
            "Item,Designator,Qty,Manufacturer,Mfr. Part #,Description/Value,Package/Footprint\n"
            '1,"R1,R2",2,,,10k,0402\n'
            '2,"R3,R4",2,,,4k7,0402\n'
        )


class TestSeeedUngroupedGuard:
    """Seeed keys ungrouped rows on the reference: no collision by design."""

    def test_every_designator_reaches_the_csv(self) -> None:
        formatter = SeeedBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(formatter.format(_duplicate_items()))

        assert len(rows) == 5
        assert [row[0] for row in rows[1:]] == ["R1", "R2", "R3", "R4"]
        assert all(row[2] == "1" for row in rows[1:])


class TestGenericUngroupedGuard:
    """Generic keys ungrouped rows on the reference: no collision by design."""

    def test_every_designator_reaches_the_csv(self) -> None:
        formatter = GenericBOMFormatter(BOMExportConfig(group_by_value=False))
        rows = _rows(formatter.format(_duplicate_items()))

        assert len(rows) == 5
        assert [row[0] for row in rows[1:]] == ["R1", "R2", "R3", "R4"]
        assert all(row[3] == "1" for row in rows[1:])
