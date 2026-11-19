"""``kct parts search`` ranks exact resistance matches above other decades (#9474)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kicad_tools.cli import parts_cmd
from kicad_tools.parts.models import Part, SearchResult


def _part(lcsc: str, mfr: str, description: str, stock: int) -> Part:
    return Part(
        lcsc_part=lcsc,
        mfr_part=mfr,
        description=description,
        package="2512",
        stock=stock,
    )


CATALOG = [
    _part("C35219", "25121WF820KT4E", "820kΩ ±1% 1W 2512 Chip Resistor", 3551),
    _part("C48277", "25121WF680KT4E", "680kΩ ±1% 1W 2512 Chip Resistor", 7659),
    _part("C999", "MYSTERY2512", "Thick film resistor", 100),
    _part("C175238", "CR2512F8R20E04S", "8.2Ω ±1% 1W 2512 Chip Resistor", 2250),
    _part("C777", "RC2512JK-074R7L", "4.7Ω ±5% 1W 2512 Chip Resistor", 50),
    _part("C778", "RC2512JK-074K7L", "4.7kΩ ±5% 1W 2512 Chip Resistor", 5000),
]


class _FakeClient:
    def search(self, query, page_size=20, in_stock=False, basic_only=False):
        return SearchResult(
            query=query,
            parts=list(CATALOG),
            total_count=len(CATALOG),
            source="test",
            coverage="live",
        )


def _search_json(query: str, capsys) -> list[str]:
    with patch("kicad_tools.parts.LCSCClient", _FakeClient):
        rc = parts_cmd.main(["search", query, "--format", "json"])
    assert rc == 0
    return [p["lcsc_part"] for p in json.loads(capsys.readouterr().out)["parts"]]


def test_ohm_query_ranks_exact_value_above_kilo_ohm(capsys):
    order = _search_json("8.2Ω 2512", capsys)
    assert order[0] == "C175238"
    # Unparseable part kept right after exact matches; mismatches keep original order.
    assert order[1:] == ["C999", "C35219", "C48277", "C777", "C778"]


@pytest.mark.parametrize(
    ("query", "first"),
    [("4R7 2512", "C777"), ("4k7 2512", "C778"), ("4.7kΩ 2512", "C778"), ("4.7 ohm", "C777")],
)
def test_short_and_unit_spellings(query, first, capsys):
    assert _search_json(query, capsys)[0] == first


def test_query_without_resistance_token_keeps_order(capsys):
    order = _search_json("resistor 2512", capsys)
    assert order == [p.lcsc_part for p in CATALOG]


def test_rank_helper_is_stable_and_flags_target():
    ranked, has_target = parts_cmd._rank_by_resistance(CATALOG, "8.2Ω")
    assert has_target
    assert [p.lcsc_part for p in ranked][0] == "C175238"
    unchanged, has_target = parts_cmd._rank_by_resistance(CATALOG, "2512")
    assert not has_target
    assert unchanged is CATALOG


def test_table_output_shows_description(capsys):
    with patch("kicad_tools.parts.LCSCClient", _FakeClient):
        rc = parts_cmd.main(["search", "8.2Ω 2512"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    first_row = next(line for line in lines if line.startswith("C"))
    assert first_row.startswith("C175238")
    assert "8.2Ω" in first_row


def test_no_exact_match_in_page_prints_note(capsys):
    with patch("kicad_tools.parts.LCSCClient", _FakeClient):
        rc = parts_cmd.main(["search", "1Ω 2512"])
    assert rc == 0
    assert "no result in this page" in capsys.readouterr().err
