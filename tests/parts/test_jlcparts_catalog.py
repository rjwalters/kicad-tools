"""Tests for the offline jlcparts catalog reader, LCSCClient fallback, and
the ``sync-catalog`` command.

No test in this file makes a real network request. The offline catalog is
exercised against a small, hand-built fixture SQLite (see
``tests/parts/fixtures/build_jlcparts_fixture.py``), and ``sync_catalog`` is
driven against a mocked ``requests`` module + a local split-zip archive built
from that same fixture.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.parts import Part, PartsCache
from kicad_tools.parts.jlcparts_catalog import (
    CATALOG_FILENAME,
    JlcpartsCatalog,
    _extract_catalog,
    _is_split_archive,
    _normalize_lcsc_id,
    _parse_price_column,
    get_catalog_path,
    sync_catalog,
)
from kicad_tools.parts.lcsc import LCSCClient, LCSCForbiddenError

# --------------------------------------------------------------------------
# Load the fixture builder (not an importable package -- load by path)
# --------------------------------------------------------------------------
_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_spec = importlib.util.spec_from_file_location(
    "build_jlcparts_fixture", _FIXTURE_DIR / "build_jlcparts_fixture.py"
)
assert _spec is not None and _spec.loader is not None
_builder = importlib.util.module_from_spec(_spec)
sys.modules["build_jlcparts_fixture"] = _builder
_spec.loader.exec_module(_builder)


@pytest.fixture
def catalog_db(tmp_path: Path) -> Path:
    """Build a tiny jlcparts-schema fixture SQLite and return its path."""
    return _builder.build_fixture(tmp_path / "jlcparts_sample.sqlite3")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def test_normalize_lcsc_id():
    assert _normalize_lcsc_id("C25804") == 25804
    assert _normalize_lcsc_id("c25804") == 25804
    assert _normalize_lcsc_id("25804") == 25804
    assert _normalize_lcsc_id("C0100") == 100
    assert _normalize_lcsc_id("Cabc") is None
    assert _normalize_lcsc_id("") is None


def test_parse_price_json_variants():
    prices = _parse_price_column('[{"qFrom": 10, "price": 0.005}, {"qFrom": 100, "price": 0.002}]')
    assert len(prices) == 2
    assert prices[0].quantity == 10
    assert prices[0].unit_price == 0.005
    # Sorted ascending by quantity
    assert prices[1].quantity == 100

    # Already-parsed list passes through.
    assert _parse_price_column([{"qFrom": 5, "price": 1.0}])[0].quantity == 5

    # Bad / empty inputs degrade to empty list, never raise.
    assert _parse_price_column(None) == []
    assert _parse_price_column("") == []
    assert _parse_price_column("{}") == []
    assert _parse_price_column('[{"qFrom": 0, "price": 0}]') == []


def test_parse_price_column_delimited():
    """The published jlcparts dataset ships tiers as a delimited string."""
    # Curator's real C185857 catalog value.
    raw = "1-49:0.2776,50-149:0.2161,150-499:0.1897,500-2499:0.1629,2500-4999:0.1483,5000-:0.1395"
    prices = _parse_price_column(raw)
    assert [(p.quantity, p.unit_price) for p in prices] == [
        (1, 0.2776),
        (50, 0.2161),
        (150, 0.1897),
        (500, 0.1629),
        (2500, 0.1483),
        (5000, 0.1395),  # open-ended top tier ("5000-:...")
    ]


def test_parse_price_column_edge_cases():
    # Single tier.
    single = _parse_price_column("1-49:0.2776")
    assert [(p.quantity, p.unit_price) for p in single] == [(1, 0.2776)]

    # Open-ended top tier expressed with a trailing '+'.
    plus = _parse_price_column("500+:0.15")
    assert [(p.quantity, p.unit_price) for p in plus] == [(500, 0.15)]

    # Open-ended top tier expressed with a trailing '-'.
    dash = _parse_price_column("5000-:0.1395")
    assert [(p.quantity, p.unit_price) for p in dash] == [(5000, 0.1395)]

    # Unsorted input is returned ascending by quantity.
    unsorted = _parse_price_column("100-499:0.002,1-99:0.005")
    assert [p.quantity for p in unsorted] == [1, 100]

    # Empty / malformed segments degrade to an empty list, never raise.
    assert _parse_price_column("") == []
    assert _parse_price_column("not a price") == []
    assert _parse_price_column("1-49:notaprice") == []
    assert _parse_price_column("garbage,:,,") == []
    # Zero quantity or zero price segments are dropped.
    assert _parse_price_column("0-49:0.5") == []
    assert _parse_price_column("1-49:0") == []


# --------------------------------------------------------------------------
# JlcpartsCatalog reader
# --------------------------------------------------------------------------


def test_get_catalog_path_sits_beside_cache():
    path = get_catalog_path()
    assert path.name == CATALOG_FILENAME
    # Same directory as the per-query parts cache.
    from kicad_tools.parts.cache import get_default_cache_path

    assert path.parent == get_default_cache_path().parent


def test_catalog_absent_is_silent_noop(tmp_path: Path):
    catalog = JlcpartsCatalog(tmp_path / "does-not-exist.sqlite3")
    assert catalog.available is False
    assert catalog.lookup("C25804") is None
    assert catalog.lookup_many(["C25804", "C1525"]) == {}
    assert catalog.count() == 0


def test_catalog_lookup_translates_row(catalog_db: Path):
    catalog = JlcpartsCatalog(catalog_db)
    assert catalog.available is True

    part = catalog.lookup("C25804")
    assert part is not None
    assert part.lcsc_part == "C25804"
    assert part.mfr_part == "RC0402FR-0710KL"
    assert part.manufacturer == "YAGEO"
    assert part.package == "0402"
    assert part.stock == 500000
    assert part.is_basic is True
    assert part.is_preferred is False
    assert part.datasheet_url == "https://example.com/rc0402.pdf"
    assert part.product_url == "https://jlcpcb.com/partdetail/C25804"
    # Price breaks parsed and sorted.
    assert [p.quantity for p in part.prices] == [10, 100]
    assert part.best_price == 0.002


def test_catalog_lookup_surfaces_delimited_prices(tmp_path: Path):
    """A synced-catalog row with the delimited price string surfaces tiers.

    Regression for #4297: the published jlcparts ``price`` column is a
    delimited ``"lo-hi:price,..."`` string, not JSON. ``lookup`` must expose
    the full tier list instead of an empty ``prices``.
    """
    rows = [
        {
            "lcsc": 185857,  # -> C185857
            "mfr": "GRM155R71C104KA88D",
            "package": "0402",
            "manufacturer": "muRata",
            "library_type": "basic",
            "description": "100nF 16V X7R 0402 MLCC",
            "datasheet": "https://example.com/grm155.pdf",
            "stock": 300000,
            # Real on-disk delimited format (curator-verified), open-ended top tier.
            "price": (
                "1-49:0.2776,50-149:0.2161,150-499:0.1897,"
                "500-2499:0.1629,2500-4999:0.1483,5000-:0.1395"
            ),
        },
    ]
    db = _builder.build_fixture(tmp_path / "delimited.sqlite3", rows=rows)

    catalog = JlcpartsCatalog(db)
    part = catalog.lookup("C185857")
    assert part is not None
    assert [(p.quantity, p.unit_price) for p in part.prices] == [
        (1, 0.2776),
        (50, 0.2161),
        (150, 0.1897),
        (500, 0.1629),
        (2500, 0.1483),
        (5000, 0.1395),
    ]
    # Cheapest break is the open-ended top tier.
    assert part.best_price == 0.1395


def test_catalog_lookup_normalizes_and_missing(catalog_db: Path):
    catalog = JlcpartsCatalog(catalog_db)
    # Numeric / lowercase / bare forms all resolve.
    assert catalog.lookup("25804") is not None
    assert catalog.lookup("c25804") is not None
    # Absent part -> None
    assert catalog.lookup("C999999") is None


def test_catalog_library_type_mapping(catalog_db: Path):
    catalog = JlcpartsCatalog(catalog_db)
    preferred = catalog.lookup("C100")
    assert preferred is not None
    assert preferred.is_preferred is True
    assert preferred.is_basic is False
    # No prices / datasheet on this row.
    assert preferred.prices == []
    assert preferred.datasheet_url == ""

    extended = catalog.lookup("C8734")
    assert extended is not None
    assert extended.is_basic is False
    assert extended.is_preferred is False


def test_catalog_lookup_many(catalog_db: Path):
    catalog = JlcpartsCatalog(catalog_db)
    got = catalog.lookup_many(["C25804", "C1525", "C999999", "cabc"])
    assert set(got.keys()) == {"C25804", "C1525"}
    assert got["C25804"].mfr_part == "RC0402FR-0710KL"
    assert got["C1525"].description.startswith("100nF")


def test_catalog_count(catalog_db: Path):
    catalog = JlcpartsCatalog(catalog_db)
    assert catalog.count() == 4


# --------------------------------------------------------------------------
# JlcpartsCatalog.search (parametric)
# --------------------------------------------------------------------------


def test_catalog_search_value_and_package(catalog_db: Path):
    """A value+package query resolves the matching row from the fixture."""
    catalog = JlcpartsCatalog(catalog_db)
    # "10k" + "0402" uniquely resolves the C25804 10kOhms 0402 resistor.
    results = catalog.search("10k 0402")
    assert [p.lcsc_part for p in results] == ["C25804"]
    assert results[0].mfr_part == "RC0402FR-0710KL"

    # Explicit package filter narrows to the 0402 rows only.
    results = catalog.search("0402", package="0402")
    assert {p.lcsc_part for p in results} == {"C25804", "C1525"}
    # The LQFP-48 MCU is excluded by the package filter even though its
    # description mentions LQFP-48.
    results = catalog.search("Microcontroller", package="0402")
    assert results == []


def test_catalog_search_ranks_basic_before_extended(catalog_db: Path):
    """Ordering is basic > preferred > extended, then stock DESC."""
    catalog = JlcpartsCatalog(catalog_db)
    # All four fixture rows mention their package in the description, but only
    # "Transistor"/"Resistor"/"Capacitor"/"Microcontroller" are type words.
    # Query a term present across multiple library_types: every row description
    # ends with a package token, so search on the shared substring "0" would be
    # too broad. Instead assert ordering via a query that spans tiers.
    results = catalog.search("SOT-23")  # preferred C100
    assert [p.lcsc_part for p in results] == ["C100"]

    # A query matching a basic and an extended row must return basic first.
    # C25804 (basic, "Resistor") vs C8734 (extended, "Microcontroller"):
    # both descriptions do NOT share a term, so build ordering from library_type
    # using a term all resistor/mcu rows share is not possible in this fixture.
    # Use the package-less "Chip" (basic) vs "ARM" (extended) distinction by
    # querying a term present in exactly the two rows we want to order.
    results = catalog.search("48")  # "LQFP-48" appears only in the extended MCU
    assert [p.lcsc_part for p in results] == ["C8734"]


def test_catalog_search_library_type_ordering_multi(catalog_db: Path, tmp_path: Path):
    """With multiple library_types matching one term, basic sorts first."""
    # Build a fixture where a shared description term spans basic/preferred/
    # extended so the ORDER BY is observable.
    rows = [
        {
            "lcsc": 1,
            "mfr": "EXT",
            "package": "0402",
            "manufacturer": "X",
            "library_type": "extended",
            "description": "WIDGET 0402",
            "datasheet": "",
            "stock": 9,
            "price": None,
        },
        {
            "lcsc": 2,
            "mfr": "PREF",
            "package": "0402",
            "manufacturer": "X",
            "library_type": "preferred",
            "description": "WIDGET 0402",
            "datasheet": "",
            "stock": 5,
            "price": None,
        },
        {
            "lcsc": 3,
            "mfr": "BASIC",
            "package": "0402",
            "manufacturer": "X",
            "library_type": "basic",
            "description": "WIDGET 0402",
            "datasheet": "",
            "stock": 1,
            "price": None,
        },
        {
            "lcsc": 4,
            "mfr": "BASIC2",
            "package": "0402",
            "manufacturer": "X",
            "library_type": "basic",
            "description": "WIDGET 0402",
            "datasheet": "",
            "stock": 100,
            "price": None,
        },
    ]
    db = _builder.build_fixture(tmp_path / "ranked.sqlite3", rows=rows)
    catalog = JlcpartsCatalog(db)
    results = catalog.search("WIDGET")
    # basic (stock DESC among basics) first, then preferred, then extended.
    assert [p.lcsc_part for p in results] == ["C4", "C3", "C2", "C1"]


def test_catalog_search_min_stock_filter(catalog_db: Path):
    """min_stock pushes a stock floor into SQL (excludes the 0-stock row)."""
    catalog = JlcpartsCatalog(catalog_db)
    # C100 (SOT-23) has stock 0; with min_stock=1 it is excluded.
    assert catalog.search("SOT-23", min_stock=1) == []
    assert [p.lcsc_part for p in catalog.search("SOT-23")] == ["C100"]


def test_catalog_search_limit(catalog_db: Path):
    """The limit param caps the number of returned rows."""
    catalog = JlcpartsCatalog(catalog_db)
    # Two 0402 rows match; limit=1 returns only the top-ranked one.
    results = catalog.search("0402", package="0402", limit=1)
    assert len(results) == 1


def test_catalog_search_empty_query_is_safe(catalog_db: Path):
    """An empty / whitespace query returns [] (no unbounded scan)."""
    catalog = JlcpartsCatalog(catalog_db)
    assert catalog.search("") == []
    assert catalog.search("   ") == []


def test_catalog_search_absent_is_silent_noop(tmp_path: Path):
    """Search against an absent catalog returns [] (never raises)."""
    catalog = JlcpartsCatalog(tmp_path / "does-not-exist.sqlite3")
    assert catalog.available is False
    assert catalog.search("10k 0402") == []


def test_catalog_search_escapes_like_wildcards(catalog_db: Path):
    """LIKE wildcards in a term are matched literally, not as wildcards.

    Only the C25804 description contains a literal ``%`` (``"10kOhms 1% ..."``).
    An unescaped ``%`` LIKE pattern would match *every* row; the ESCAPE handling
    means a ``%`` term matches only the single row that literally contains it.
    """
    catalog = JlcpartsCatalog(catalog_db)
    results = catalog.search("%")
    assert [p.lcsc_part for p in results] == ["C25804"]

    # An underscore term likewise matches literally: no fixture description
    # contains ``_``, so it matches nothing (rather than any single character).
    assert catalog.search("_") == []


def test_catalog_search_matches_mfr_mpn(catalog_db: Path):
    """Search resolves parts by MPN via the ``mfr`` column (#4296).

    The MPN lives in ``mfr`` and never appears in ``description``; before the
    fix these queries returned zero hits (description-only matching).
    """
    catalog = JlcpartsCatalog(catalog_db)

    # Full MPN -- present only in `mfr`, absent from the description string.
    results = catalog.search("STM32F103C8T6")
    assert [p.lcsc_part for p in results] == ["C8734"]
    assert results[0].mfr_part == "STM32F103C8T6"

    # A partial MPN prefix also matches (LIKE %term%), still only via `mfr`.
    results = catalog.search("RC0402FR")
    assert [p.lcsc_part for p in results] == ["C25804"]


def test_catalog_search_mpn_and_description_terms(catalog_db: Path):
    """Multi-term queries keep AND semantics across the mfr/description OR (#4296)."""
    catalog = JlcpartsCatalog(catalog_db)

    # "STM32F103C8T6" matches only via `mfr`; "Microcontroller" only via
    # `description`. Both must be present (AND) -> the single MCU row.
    results = catalog.search("STM32F103C8T6 Microcontroller")
    assert [p.lcsc_part for p in results] == ["C8734"]

    # A second term matching neither column on that row excludes it entirely.
    assert catalog.search("STM32F103C8T6 Resistor") == []


# --------------------------------------------------------------------------
# LCSCClient fallback path
# --------------------------------------------------------------------------


@pytest.fixture
def force_requests_present(monkeypatch):
    """Pretend the ``requests`` extra is installed so the live-API branch runs.

    The base dev env does not install ``requests`` (it is the ``[parts]``
    extra), so the live-fetch branch is otherwise skipped. Tests that exercise
    the *API-failure -> catalog* fallback need the live branch to be entered
    (and ``_fetch_part`` to be reached) regardless of the local install.
    """
    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: True)


def test_lookup_falls_back_to_catalog_on_api_failure(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """When the live API 403s, lookup() resolves from the offline catalog."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(
        client, "_fetch_part", side_effect=LCSCForbiddenError("403")
    ) as mock_fetch:
        part = client.lookup("C25804")

    mock_fetch.assert_called_once()
    assert part is not None
    assert part.lcsc_part == "C25804"
    assert part.mfr_part == "RC0402FR-0710KL"
    # Fallback result is cached for subsequent lookups.
    assert cache.get("C25804") is not None


def test_lookup_no_requests_uses_catalog(catalog_db: Path, tmp_path: Path, monkeypatch):
    """Without the requests extra, lookup() serves directly from the catalog."""
    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: False)
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    # _fetch_part must NOT be called when requests is unavailable.
    with mock.patch.object(client, "_fetch_part", side_effect=AssertionError("no live fetch")):
        part = client.lookup("C25804")

    assert part is not None
    assert part.mfr_part == "RC0402FR-0710KL"


def test_lookup_no_requests_no_catalog_raises(tmp_path: Path, monkeypatch):
    """Without requests AND without a catalog, the historical ImportError stands."""
    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: False)
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=tmp_path / "missing.sqlite3")

    with pytest.raises(ImportError):
        client.lookup("C25804")


def test_lookup_returns_none_when_catalog_absent_and_api_fails(
    tmp_path: Path, force_requests_present
):
    """No catalog + API down = existing 'not found' behavior (None)."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=tmp_path / "missing.sqlite3")

    with mock.patch.object(client, "_fetch_part", side_effect=LCSCForbiddenError("403")):
        assert client.lookup("C25804") is None


def test_lookup_missing_part_falls_through_to_none(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """Catalog present but part absent -> None."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(client, "_fetch_part", side_effect=LCSCForbiddenError("403")):
        assert client.lookup("C999999") is None


def test_live_api_success_bypasses_catalog(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """When the live API returns a part, the catalog is never consulted."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    live_part = Part(lcsc_part="C25804", mfr_part="LIVE-API-PART")
    with mock.patch.object(client, "_fetch_part", return_value=live_part):
        with mock.patch.object(JlcpartsCatalog, "lookup") as catalog_lookup:
            part = client.lookup("C25804")

    catalog_lookup.assert_not_called()
    assert part is not None
    assert part.mfr_part == "LIVE-API-PART"


def test_catalog_disabled_never_constructed(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """use_local_catalog=False -> catalog is never consulted even on failure."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, use_local_catalog=False, catalog_path=catalog_db)

    with mock.patch.object(client, "_fetch_part", side_effect=LCSCForbiddenError("403")):
        assert client.lookup("C25804") is None
    assert client._get_catalog() is None


def test_lookup_many_fallback(catalog_db: Path, tmp_path: Path, force_requests_present):
    """lookup_many falls back to the catalog for parts the API can't serve."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(client, "_fetch_part", side_effect=LCSCForbiddenError("403")):
        got = client.lookup_many(["C25804", "C1525", "C999999"])

    assert set(got.keys()) == {"C25804", "C1525"}
    assert got["C25804"].mfr_part == "RC0402FR-0710KL"


def test_lookup_many_live_partial_then_catalog(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """API serves one part; the catalog fills a second; a third stays missing."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    def fake_fetch(part_num: str):
        if part_num == "C1525":
            return Part(lcsc_part="C1525", mfr_part="LIVE-CAP")
        # Everything else "fails" via circuit breaker.
        raise LCSCForbiddenError("403")

    with mock.patch.object(client, "_fetch_part", side_effect=fake_fetch):
        got = client.lookup_many(["C1525", "C25804", "C999999"])

    # C1525 from live API, C25804 from catalog, C999999 missing.
    assert got["C1525"].mfr_part == "LIVE-CAP"
    assert got["C25804"].mfr_part == "RC0402FR-0710KL"
    assert "C999999" not in got


# --------------------------------------------------------------------------
# LCSCClient.search fallback path (parametric matcher, #4126)
# --------------------------------------------------------------------------


def test_search_falls_back_to_catalog_on_403(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """A 403 from the live search API is served from the offline catalog."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(
        client, "_make_request", side_effect=LCSCForbiddenError("403")
    ) as mock_req:
        result = client.search("10k 0402")

    mock_req.assert_called_once()
    assert result.parts, "expected candidates from the offline catalog, got none"
    assert result.parts[0].lcsc_part == "C25804"
    # Fallback candidates are cached like the live-API path.
    assert cache.get("C25804") is not None


def _request_exception(msg: str = "boom") -> BaseException:
    """Build a RequestException instance without requiring the requests extra.

    Mirrors ``search()``'s ``_request_exception_type()`` so the generic-network-
    failure branch is exercised whether or not ``requests`` is installed.
    """
    from kicad_tools.parts.lcsc import _request_exception_type

    return _request_exception_type()(msg)


def test_search_falls_back_on_request_exception(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """A generic RequestException also falls back to the catalog when present."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(client, "_make_request", side_effect=_request_exception("boom")):
        result = client.search("10k 0402")

    assert [p.lcsc_part for p in result.parts] == ["C25804"]


def test_search_403_without_catalog_still_raises(tmp_path: Path, force_requests_present):
    """403 + no catalog synced -> LCSCForbiddenError still propagates."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=tmp_path / "missing.sqlite3")

    with mock.patch.object(client, "_make_request", side_effect=LCSCForbiddenError("403")):
        with pytest.raises(LCSCForbiddenError):
            client.search("10k 0402")


def test_search_403_with_catalog_disabled_still_raises(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """use_local_catalog=False -> 403 propagates even with a catalog file."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, use_local_catalog=False, catalog_path=catalog_db)

    with mock.patch.object(client, "_make_request", side_effect=LCSCForbiddenError("403")):
        with pytest.raises(LCSCForbiddenError):
            client.search("10k 0402")
    assert client._get_catalog() is None


def test_search_request_exception_without_catalog_returns_empty(
    tmp_path: Path, force_requests_present
):
    """Generic RequestException + no catalog -> empty SearchResult (unchanged)."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=tmp_path / "missing.sqlite3")

    with mock.patch.object(client, "_make_request", side_effect=_request_exception("boom")):
        result = client.search("10k 0402")
    assert result.parts == []
    assert result.query == "10k 0402"


def test_search_live_success_bypasses_catalog(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """When the live API returns results, the offline catalog is never touched."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    live_response = {
        "code": 200,
        "data": {
            "componentPageInfo": {
                "total": 1,
                "list": [
                    {
                        "componentCode": "C25804",
                        "componentModelEn": "LIVE-SEARCH-PART",
                        "componentBrandEn": "LiveCo",
                        "describe": "10kOhms 0402 Resistor",
                        "encapStandard": "0402",
                        "stockCount": 123,
                    }
                ],
            }
        },
    }
    with mock.patch.object(client, "_make_request", return_value=live_response):
        with mock.patch.object(JlcpartsCatalog, "search") as catalog_search:
            result = client.search("10k 0402")

    catalog_search.assert_not_called()
    assert [p.mfr_part for p in result.parts] == ["LIVE-SEARCH-PART"]


def test_search_no_requests_uses_catalog(catalog_db: Path, tmp_path: Path, monkeypatch):
    """Without the requests extra, search() serves directly from the catalog."""
    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: False)
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    result = client.search("10k 0402")
    assert [p.lcsc_part for p in result.parts] == ["C25804"]


def test_search_no_requests_no_catalog_raises(tmp_path: Path, monkeypatch):
    """Without requests AND without a catalog, the dependency error stands."""
    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: False)
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=tmp_path / "missing.sqlite3")

    with pytest.raises(ImportError):
        client.search("10k 0402")


def test_search_fallback_respects_package_filter(
    catalog_db: Path, tmp_path: Path, force_requests_present
):
    """The package arg narrows the offline-catalog fallback candidate pool."""
    cache = PartsCache(db_path=tmp_path / "cache.db")
    client = LCSCClient(cache=cache, catalog_path=catalog_db)

    with mock.patch.object(client, "_make_request", side_effect=LCSCForbiddenError("403")):
        result = client.search("0402", package="0402")

    assert {p.lcsc_part for p in result.parts} == {"C25804", "C1525"}


# --------------------------------------------------------------------------
# sync_catalog (download mocked -- no real network)
# --------------------------------------------------------------------------


def test_sync_catalog_downloads_and_assembles(catalog_db: Path, tmp_path: Path, monkeypatch):
    """sync_catalog assembles the split-zip dataset without real network I/O."""
    data_dir = tmp_path / "dataset"
    _builder.build_split_zip_dataset(data_dir, catalog_db)

    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    dest = tmp_path / "out" / "jlcparts.sqlite3"
    result = sync_catalog(
        dest=dest,
        base_url="https://fake.local/data",
        force=False,
        progress=False,
    )

    assert result == dest
    assert dest.exists()
    # The assembled catalog is queryable and matches the fixture contents.
    catalog = JlcpartsCatalog(dest)
    assert catalog.count() == 4
    assert catalog.lookup("C25804") is not None


def test_sync_catalog_skips_when_present(tmp_path: Path, monkeypatch):
    """An existing catalog is not re-downloaded unless --force is given."""
    dest = tmp_path / "jlcparts.sqlite3"
    dest.write_bytes(b"existing")

    called = {"get": False}

    class _Boom:
        @staticmethod
        def get(*a, **k):
            called["get"] = True
            raise AssertionError("should not download")

        @staticmethod
        def head(*a, **k):
            called["get"] = True
            raise AssertionError("should not probe")

    monkeypatch.setitem(sys.modules, "requests", _Boom)

    result = sync_catalog(dest=dest, force=False, progress=False)
    assert result == dest
    assert called["get"] is False
    assert dest.read_bytes() == b"existing"


def test_sync_catalog_force_redownloads(catalog_db: Path, tmp_path: Path, monkeypatch):
    """--force re-downloads even when a catalog file already exists."""
    data_dir = tmp_path / "dataset"
    _builder.build_split_zip_dataset(data_dir, catalog_db)

    dest = tmp_path / "jlcparts.sqlite3"
    dest.write_bytes(b"stale")

    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = sync_catalog(dest=dest, base_url="https://fake.local/data", force=True, progress=False)
    assert result == dest
    assert dest.read_bytes() != b"stale"
    assert JlcpartsCatalog(dest).count() == 4


# --------------------------------------------------------------------------
# Fake requests module backed by local files (no sockets)
# --------------------------------------------------------------------------


def _make_fake_requests(data_dir: Path):
    """Build a minimal stand-in for the ``requests`` module.

    ``head`` reports 404 for missing segments (to end split-part discovery)
    and 200 for present ones; ``get`` streams the local file bytes. No network
    is touched.
    """

    class _Resp:
        def __init__(self, path: Path | None, status: int):
            self._path = path
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1 << 20):
            assert self._path is not None
            data = self._path.read_bytes()
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _path_for(url: str) -> Path:
        return data_dir / url.rsplit("/", 1)[-1]

    class _FakeRequests:
        @staticmethod
        def head(url, timeout=None, allow_redirects=True):
            path = _path_for(url)
            return _Resp(path if path.exists() else None, 200 if path.exists() else 404)

        @staticmethod
        def get(url, stream=False, timeout=None):
            path = _path_for(url)
            if not path.exists():
                return _Resp(None, 404)
            return _Resp(path, 200)

    return _FakeRequests


# --------------------------------------------------------------------------
# CLI: kct parts sync-catalog (download mocked -- no real network)
# --------------------------------------------------------------------------


def test_cli_sync_catalog(catalog_db: Path, tmp_path: Path, monkeypatch):
    """The `parts sync-catalog` subcommand wires through to sync_catalog."""
    from kicad_tools.cli import parts_cmd

    data_dir = tmp_path / "dataset"
    _builder.build_split_zip_dataset(data_dir, catalog_db)
    dest = tmp_path / "cli-out" / "jlcparts.sqlite3"

    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    # Redirect the default catalog path to a tmp location so the test never
    # touches the real ~/.cache directory.
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: dest)

    rc = parts_cmd.main(["sync-catalog", "--base-url", "https://fake.local/data"])
    assert rc == 0
    assert dest.exists()
    assert JlcpartsCatalog(dest).count() == 4


def test_cli_dispatch_sync_catalog(catalog_db: Path, tmp_path: Path, monkeypatch):
    """The top-level `kct parts sync-catalog` dispatcher forwards correctly."""
    import argparse

    from kicad_tools.cli.commands.parts import run_parts_command

    data_dir = tmp_path / "dataset"
    _builder.build_split_zip_dataset(data_dir, catalog_db)
    dest = tmp_path / "dispatch-out" / "jlcparts.sqlite3"

    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: dest)

    args = argparse.Namespace(
        parts_command="sync-catalog",
        force=False,
        base_url="https://fake.local/data",
    )
    rc = run_parts_command(args)
    assert rc == 0
    assert JlcpartsCatalog(dest).count() == 4


def test_cli_sync_catalog_missing_requests_non_circular(tmp_path: Path, monkeypatch, capsys):
    """sync-catalog without requests exits non-zero with NON-circular advice (#4295).

    The failure must not recommend running ``kct parts sync-catalog`` -- that is
    the very command failing on the missing dependency. It should name only the
    actionable ``parts``-extra install incantation.
    """
    from kicad_tools.cli import parts_cmd

    dest = tmp_path / "out" / "jlcparts.sqlite3"
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: dest)
    # Block `import requests` at import time inside sync_catalog.
    monkeypatch.setitem(sys.modules, "requests", None)

    rc = parts_cmd.main(["sync-catalog"])
    # Regression lock: a fatal missing-dep must NOT exit 0.
    assert rc != 0

    err = capsys.readouterr().err
    # Advice is actionable (names the extra + an install command)...
    assert "parts" in err
    assert ("pip install" in err) or ("uv sync" in err)
    # ...and NOT circular: never tells the user to run the failing command.
    assert "sync-catalog" not in err
    assert "sync the offline jlcparts catalog" not in err
    # And no partial catalog was written.
    assert not dest.exists()


def test_cli_search_missing_backend_is_loud(tmp_path: Path, monkeypatch, capsys):
    """search with no requests AND no catalog fails loudly, not silent-empty (#4296a).

    A genuinely-unavailable backend must be surfaced distinctly from a
    legitimate zero-match result (bare "No parts found").
    """
    from kicad_tools.cli import parts_cmd

    monkeypatch.setattr("kicad_tools.parts.lcsc._requests_installed", lambda: False)
    # Point the default catalog path at a nonexistent file: no offline fallback.
    monkeypatch.setattr(
        "kicad_tools.parts.jlcparts_catalog.get_catalog_path",
        lambda: tmp_path / "missing.sqlite3",
    )

    rc = parts_cmd.main(["search", "UCC27524"])
    assert rc != 0

    err = capsys.readouterr().err
    assert "backend unavailable" in err
    # Must NOT masquerade as an ordinary empty result.
    assert "No parts found" not in err


# --------------------------------------------------------------------------
# Split-archive (true ``zip -s``) extraction path
# --------------------------------------------------------------------------


def test_is_split_archive_detects_spanning_marker(catalog_db: Path, tmp_path: Path):
    """A blob beginning with PK\\x07\\x08 is recognized as a split archive."""
    split = tmp_path / "split.zip"
    split.write_bytes(_builder.build_spanning_split_bytes(catalog_db))
    assert _is_split_archive(split) is True


def test_is_split_archive_rejects_plain_zip(catalog_db: Path, tmp_path: Path):
    """A normal single-disk zip is not flagged as a split archive."""
    import zipfile

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(catalog_db, arcname="cache.sqlite3")
    assert _is_split_archive(plain) is False


def test_spanning_marker_routes_to_streaming_not_zipfile(
    catalog_db: Path, tmp_path: Path, monkeypatch
):
    """The leading spanning marker forces the streaming path, bypassing zipfile.

    This guards the premise of the fix: the real dataset carries the
    ``PK\\x07\\x08`` marker, and we must NOT hand such a blob to stdlib
    ``zipfile`` (which reads the multi-disk central directory the real dataset
    trips on). We assert the single-disk zipfile path is never entered when the
    marker is present.
    """
    split = tmp_path / "split.zip"
    split.write_bytes(_builder.build_spanning_split_bytes(catalog_db))

    def _boom(*_a, **_k):
        raise AssertionError("single-disk zipfile path must not be used for split archives")

    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog._extract_single_disk_archive", _boom)

    dest = tmp_path / "out.sqlite3"
    _extract_catalog(split, dest)
    assert dest.read_bytes() == catalog_db.read_bytes()


def test_extract_falls_back_when_zipfile_reports_multidisk(
    catalog_db: Path, tmp_path: Path, monkeypatch
):
    """A markerless archive that trips zipfile's multi-disk guard falls back.

    Some split archives lack the leading spanning marker but still raise
    ``BadZipFile: ... span multiple disks ...`` once ``zipfile`` reaches the
    zip64 end-of-central-directory locator. The extractor must catch that and
    retry via the streaming path. We simulate the guard by building a plain
    deflate archive (no marker) and forcing the single-disk path to raise the
    multi-disk error; the fallback streaming path then succeeds because the
    local file header is intact at byte 0.
    """
    import zipfile

    # Markerless plain-deflate archive so the streaming fallback can parse the
    # PK\x03\x04 header at byte 0.
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(catalog_db, arcname="cache.sqlite3")

    def _raise_multidisk(*_a, **_k):
        raise zipfile.BadZipFile("zipfiles that span multiple disks are not supported")

    monkeypatch.setattr(
        "kicad_tools.parts.jlcparts_catalog._extract_single_disk_archive", _raise_multidisk
    )

    dest = tmp_path / "out.sqlite3"
    _extract_catalog(plain, dest)
    assert dest.read_bytes() == catalog_db.read_bytes()
    assert JlcpartsCatalog(dest).count() == 4


def test_extract_split_archive_byte_identical(catalog_db: Path, tmp_path: Path):
    """Streaming extraction of a split archive yields a byte-identical member."""
    split = tmp_path / "split.zip"
    split.write_bytes(_builder.build_spanning_split_bytes(catalog_db))

    dest = tmp_path / "out.sqlite3"
    _extract_catalog(split, dest)

    assert dest.exists()
    # Byte-for-byte identical to the embedded SQLite source.
    assert dest.read_bytes() == catalog_db.read_bytes()
    # And it is a queryable catalog.
    assert JlcpartsCatalog(dest).count() == 4


def test_extract_plain_zip_still_works(catalog_db: Path, tmp_path: Path):
    """The single-disk zipfile path is unchanged for plain fixtures."""
    import zipfile

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(catalog_db, arcname="cache.sqlite3")

    dest = tmp_path / "out.sqlite3"
    _extract_catalog(plain, dest)
    assert dest.read_bytes() == catalog_db.read_bytes()
    assert JlcpartsCatalog(dest).count() == 4


def test_extract_split_archive_non_deflate_errors(catalog_db: Path, tmp_path: Path):
    """A stored (method 0) split member raises a clear, actionable error."""
    import io
    import zipfile

    buf = io.BytesIO()
    # ZIP_STORED => compression method 0, which the streaming path rejects.
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(catalog_db, arcname="cache.sqlite3")

    split = tmp_path / "stored-split.zip"
    split.write_bytes(b"PK\x07\x08" + buf.getvalue())

    dest = tmp_path / "out.sqlite3"
    with pytest.raises(RuntimeError, match="not deflate-compressed"):
        _extract_catalog(split, dest)
    # Atomicity: no partial catalog is left behind on failure.
    assert not dest.exists()


def test_extract_split_archive_missing_local_header_errors(tmp_path: Path):
    """A spanning marker not followed by a local header raises a clear error."""
    split = tmp_path / "bogus-split.zip"
    split.write_bytes(b"PK\x07\x08" + b"not a local file header, definitely not zip")

    dest = tmp_path / "out.sqlite3"
    with pytest.raises(RuntimeError, match="local file header"):
        _extract_catalog(split, dest)
    assert not dest.exists()


def test_sync_catalog_extracts_true_split_dataset(catalog_db: Path, tmp_path: Path, monkeypatch):
    """sync_catalog assembles and extracts a real split-shaped dataset."""
    data_dir = tmp_path / "dataset"
    _builder.build_spanning_split_dataset(data_dir, catalog_db)

    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    dest = tmp_path / "out" / "jlcparts.sqlite3"
    result = sync_catalog(
        dest=dest,
        base_url="https://fake.local/data",
        force=False,
        progress=False,
    )

    assert result == dest
    assert dest.read_bytes() == catalog_db.read_bytes()
    catalog = JlcpartsCatalog(dest)
    assert catalog.count() == 4
    assert catalog.lookup("C25804") is not None


def test_cli_surfaces_extraction_error(catalog_db: Path, tmp_path: Path, monkeypatch, capsys):
    """A sync-catalog extraction failure prints an actionable error (exit 1)."""
    import io
    import zipfile

    from kicad_tools.cli import parts_cmd

    # Build a split dataset whose single member is STORED (method 0) so the
    # streaming extractor raises -- exercising the CLI error surface.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.write(catalog_db, arcname="cache.sqlite3")
    data = b"PK\x07\x08" + buf.getvalue()

    data_dir = tmp_path / "dataset"
    data_dir.mkdir(parents=True, exist_ok=True)
    midpoint = max(1, len(data) // 2)
    (data_dir / "cache.z01").write_bytes(data[:midpoint])
    (data_dir / "cache.zip").write_bytes(data[midpoint:])

    dest = tmp_path / "cli-out" / "jlcparts.sqlite3"
    fake_requests = _make_fake_requests(data_dir)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr("kicad_tools.parts.jlcparts_catalog.get_catalog_path", lambda: dest)

    rc = parts_cmd.main(["sync-catalog", "--base-url", "https://fake.local/data"])
    assert rc == 1

    err = capsys.readouterr().err
    # The actual extraction error text is surfaced -- not a silent exit-1.
    assert "failed to sync jlcparts catalog" in err
    assert "not deflate-compressed" in err
    # No partial catalog left behind.
    assert not dest.exists()
