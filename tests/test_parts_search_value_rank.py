"""``kct parts search`` ranks exact resistance matches above other decades (#9474).

Identity rows (LCSC number, MPN, description, stock) were captured from the
live LCSC catalogue on 2026-11-19. ``C999``/``MYSTERY2512`` and the
``MYSTERYXTAL*`` rows are explicitly synthetic stand-ins: they exercise the
unparseable tier and the note suppression, and claim no catalogue identity.

CATALOG is ordered stock-descending, matching the search API's stock-first
base ordering.
"""

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
    _part(
        "C48277",
        "25121WF680KT4E",
        "-55℃~+155℃ 1W 200V 6.8Ω Thick Film Resistor ±1% ±200ppm/℃ 2512 Chip Resistor - Surface Mount ROHS",
        7659,
    ),
    _part(
        "C784354",
        "25121WF8203T4E",
        "-55℃~+155℃ 1W 200V 820kΩ Thick Film Resistor ±1% ±100ppm/℃ 2512 Chip Resistor - Surface Mount ROHS",
        3692,
    ),
    _part(
        "C35219",
        "25121WF820KT4E",
        "-55℃~+155℃ 1W 200V 8.2Ω Thick Film Resistor ±1% ±200ppm/℃ 2512 Chip Resistor - Surface Mount ROHS",
        3512,
    ),
    _part("C999", "MYSTERY2512", "Thick film resistor", 3000),
    _part(
        "C175238",
        "CR2512F8R20E04S",
        "-55℃~+155℃ 1W 200V 8.2Ω Thick Film Resistor ±1% ±400ppm/℃ 2512 Chip Resistor - Surface Mount ROHS",
        2250,
    ),
    _part(
        "C878295",
        "RC2512JK-074R7L",
        "-55℃~+155℃ 1W 200V 4.7Ω Thick Film Resistor ±200ppm/℃ ±5% 2512 Chip Resistor - Surface Mount ROHS",
        1837,
    ),
    _part(
        "C878291",
        "RC2512JK-074K7L",
        "-55℃~+155℃ 1W 200V 4.7kΩ Thick Film Resistor ±100ppm/℃ ±5% 2512 Chip Resistor - Surface Mount ROHS",
        8,
    ),
]

# A page whose rows never parse to a resistance: the "this isn't a resistor
# query" signal that must suppress the no-match note.
CRYSTAL_CATALOG = [
    _part("C900", "MYSTERYXTAL16M", "SMD Crystal Resonator 16MHz 20ppm", 9000),
    _part("C901", "MYSTERYXTAL8M", "Crystal Unit HC-49/4H 8MHz", 4000),
]


class _FakeClient:
    catalog = CATALOG

    def __init__(self, *args, **kwargs):
        pass

    def search(self, query, page_size=20, in_stock=False, basic_only=False):
        return SearchResult(
            query=query,
            parts=list(self.catalog),
            total_count=len(self.catalog),
            source="test",
            coverage="live",
        )


class _CrystalClient(_FakeClient):
    catalog = CRYSTAL_CATALOG


def _search_json(query: str, capsys, client=_FakeClient) -> list[str]:
    with patch("kicad_tools.parts.LCSCClient", client):
        rc = parts_cmd.main(["search", query, "--format", "json"])
    assert rc == 0
    return [p["lcsc_part"] for p in json.loads(capsys.readouterr().out)["parts"]]


def test_ohm_query_ranks_exact_value_above_other_decades(capsys):
    order = _search_json("8.2Ω 2512", capsys)
    # C784354 (820kΩ) and C48277 (6.8Ω) out-stock both 8.2Ω rows, so the exact
    # matches reaching the front is the re-rank, not the base order.
    assert order == ["C35219", "C175238", "C999", "C48277", "C784354", "C878295", "C878291"]


@pytest.mark.parametrize(
    ("query", "first"),
    [
        ("4R7 2512", "C878295"),
        ("4k7 2512", "C878291"),
        ("4.7kΩ 2512", "C878291"),
        ("4.7 ohm", "C878295"),
    ],
)
def test_short_and_unit_spellings(query, first, capsys):
    assert _search_json(query, capsys)[0] == first


def test_query_without_resistance_token_keeps_order(capsys):
    order = _search_json("resistor 2512", capsys)
    assert order == [p.lcsc_part for p in CATALOG]


def test_rank_helper_is_stable_and_flags_target():
    ranked, value_query, exact, parseable = parts_cmd._rank_by_resistance(CATALOG, "8.2Ω")
    assert value_query
    assert exact == 2
    assert parseable == 6
    assert [p.lcsc_part for p in ranked][:2] == ["C35219", "C175238"]
    unchanged, value_query, exact, parseable = parts_cmd._rank_by_resistance(CATALOG, "2512")
    assert not value_query
    assert unchanged is CATALOG
    assert exact == parseable == 0


def test_table_output_shows_description(capsys):
    with patch("kicad_tools.parts.LCSCClient", _FakeClient):
        rc = parts_cmd.main(["search", "8.2Ω 2512"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    first_row = next(line for line in lines if line.startswith("C"))
    assert first_row.startswith("C35219")
    assert "8.2Ω" in first_row


def test_no_exact_match_in_page_prints_note(capsys):
    with patch("kicad_tools.parts.LCSCClient", _FakeClient):
        rc = parts_cmd.main(["search", "1Ω 2512"])
    assert rc == 0
    assert "no result in this page" in capsys.readouterr().err


def test_note_suppressed_when_nothing_parses_to_a_resistance(capsys):
    order = _search_json("16M crystal", capsys, client=_CrystalClient)
    assert order == [p.lcsc_part for p in CRYSTAL_CATALOG]
    assert "no result in this page" not in capsys.readouterr().err
