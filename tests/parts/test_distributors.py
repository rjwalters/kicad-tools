"""Distributor stock lookups (issue #8892). Recorded-shape fixtures; no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli import parts_cmd
from kicad_tools.parts.distributors import (
    DistributorCache,
    DistributorSkipped,
    pick_price_break,
    query_stock,
)
from kicad_tools.parts.distributors.digikey import DigiKeyClient
from kicad_tools.parts.distributors.mouser import MouserClient

FIX = Path(__file__).parent / "fixtures" / "distributors"
SECRET = "SENTINEL-SECRET-9f3a"


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload

    def json(self):
        if self._p is None:
            raise ValueError
        return self._p


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # substring of url -> Resp
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        for key, resp in self.routes.items():
            if key in url:
                return resp
        raise AssertionError(url)


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def cache(tmp_path):
    return DistributorCache(tmp_path / "d.db")


def both_session(mouser=None, dk=None):
    return FakeSession(
        {
            "mouser.com": Resp(200, mouser or load("mouser_search.json")),
            "oauth2/token": Resp(200, {"access_token": "tok-" + SECRET}),
            "keyword": Resp(200, dk or load("digikey_search.json")),
        }
    )


def test_price_break_selection():
    br = [(1, 8.5), (10, 7.9), (100, 6.25)]
    assert pick_price_break(br, 1) == (8.5, None)
    assert pick_price_break(br, 50) == (7.9, None)
    assert pick_price_break(br, 1000) == (6.25, None)
    price, flag = pick_price_break([(10, 7.9)], 3)
    assert price == 7.9 and "below minimum" in flag
    assert pick_price_break([], 5)[0] is None


def test_missing_credentials_skip(cache):
    report = query_stock(["X"], 1, cache=cache, session=FakeSession({}))
    assert report.offers == []
    assert any("MOUSER_API_KEY" in n for n in report.notices)
    assert any("DIGIKEY_CLIENT_ID" in n and "DIGIKEY_CLIENT_SECRET" in n for n in report.notices)


def test_partial_digikey_credentials(monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    with pytest.raises(DistributorSkipped) as e:
        DigiKeyClient.from_env(FakeSession({}))
    assert "DIGIKEY_CLIENT_SECRET" in str(e.value) and "DIGIKEY_CLIENT_ID" not in str(e.value)


def test_both_sources_and_fetch_token(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "cid")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", SECRET)
    s = both_session()
    report = query_stock(["STM32F103C8T6"], 100, cache=cache, session=s)
    by = {o.distributor: o for o in report.offers}
    assert by["mouser"].unit_price == 6.25 and by["mouser"].stock == 1234
    assert by["mouser"].lead_time == "84 Days" and by["mouser"].packaging == "Tray"
    assert by["digikey"].unit_price == 7.5 and by["digikey"].stock == 50
    assert by["digikey"].lead_time == "12 weeks" and by["digikey"].distributor_pn == "497-6063-ND"
    assert by["digikey"].fetched_at and by["digikey"].region == "US"
    # token fetched once (one token call, one search call)
    assert sum("oauth2/token" in u for u, _ in s.calls) == 1
    # Mouser's near-match MPN (…TR) is excluded
    assert all(o.mpn == "STM32F103C8T6" for o in report.offers)


def test_sort_available_then_price(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "cid")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "x")
    # qty 100: digikey stock 50 < 100 -> not purchasable -> sorted last despite ordering by price
    report = query_stock(["STM32F103C8T6"], 100, cache=cache, session=both_session())
    assert [o.distributor for o in report.offers] == ["mouser", "digikey"]
    report = query_stock(
        ["STM32F103C8T6"], 20, cache=cache, session=both_session(), use_cache=False
    )
    # both purchasable -> cheaper first (digikey 7.5 @25? qty 20 -> break 1 => 9.1; mouser 7.9)
    assert [o.distributor for o in report.offers] == ["mouser", "digikey"]


def test_not_found(monkeypatch, cache):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "cid")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "x")
    report = query_stock(
        ["NOPE"], 1, cache=cache, session=both_session(dk=load("digikey_empty.json"))
    )
    assert report.not_found == ["NOPE@digikey"]


def test_zero_stock_not_purchasable(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    data = load("mouser_search.json")
    data["SearchResults"]["Parts"][0]["AvailabilityInStock"] = "0"
    report = query_stock(["STM32F103C8T6"], 1, cache=cache, session=both_session(mouser=data))
    assert report.offers[0].stock == 0 and not report.offers[0].purchasable


def test_cache_age_and_reuse(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    s = both_session()
    query_stock(["STM32F103C8T6"], 1, cache=cache, session=s)
    n = len(s.calls)
    report = query_stock(["STM32F103C8T6"], 10, cache=cache, session=s)
    assert len(s.calls) == n  # served from cache
    o = report.offers[0]
    assert o.from_cache and o.age_seconds is not None and o.unit_price == 7.9  # re-priced at qty


def test_quota_exhaustion(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    monkeypatch.setenv("MOUSER_DAILY_QUOTA", "1")
    s = both_session()
    query_stock(["A"], 1, cache=cache, session=s)
    n = len(s.calls)
    report = query_stock(["B"], 1, cache=cache, session=s)
    assert len(s.calls) == n
    assert any("quota" in x and "exhausted" in x for x in report.notices)


def test_http_error_does_not_leak_key(monkeypatch, cache):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    s = FakeSession({"mouser.com": Resp(500, None)})
    report = query_stock(["A"], 1, cache=cache, session=s)
    assert report.offers == [] and any("HTTP 500" in n for n in report.notices)
    assert SECRET not in " ".join(report.notices)


def test_exception_does_not_leak_key(monkeypatch):
    class Boom:
        def post(self, url, **kw):
            raise RuntimeError(f"failed {url} {kw}")

    c = MouserClient(SECRET, Boom())
    from kicad_tools.parts.distributors import DistributorError

    with pytest.raises(DistributorError) as e:
        c.search("A")
    assert SECRET not in str(e.value) and e.value.__cause__ is None


def test_no_credentials_in_outputs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "cid-" + SECRET)
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    s = both_session()
    monkeypatch.setattr("kicad_tools.parts.distributors.service._default_session", lambda: s)
    for fmt in ("json", "table"):
        assert (
            parts_cmd.main(["stock", "STM32F103C8T6", "-q", "10", "--format", fmt, "--no-cache"])
            == 0
        )
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files, "cache file expected"
    assert all(SECRET.encode() not in p.read_bytes() for p in files)
    assert "STM32F103C8T6" in out.out


def test_cli_json_shape(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MOUSER_API_KEY", SECRET)
    s = both_session()
    monkeypatch.setattr("kicad_tools.parts.distributors.service._default_session", lambda: s)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert parts_cmd.main(["stock", "STM32F103C8T6", "-q", "100", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    o = data["offers"][0]
    assert o["distributor"] == "mouser" and o["unit_price"] == 6.25
    assert o["region"] and o["fetched_at"] and o["currency"] == "USD"
