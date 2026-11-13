"""Tests for ``kct bom --check-distributor-stock`` (Issue #8893)."""

import json
from types import SimpleNamespace

from kicad_tools.cli.bom_cmd import check_distributor_stock
from kicad_tools.parts.distributors.base import DistributorError, DistributorOffer


def _item(ref, mpn):
    return SimpleNamespace(reference=ref, mpn=mpn)


def _offer(mpn, name, stock):
    return DistributorOffer(
        mpn=mpn,
        distributor=name,
        distributor_pn="x",
        stock=stock,
        currency="USD",
        region="US",
        fetched_at="2026-01-01T00:00:00Z",
    )


class FakeClient:
    daily_quota = 10_000
    region = "US"

    def __init__(self, name, stock_by_mpn):
        self.name = name
        self.stock_by_mpn = stock_by_mpn

    def search(self, mpn):
        if mpn not in self.stock_by_mpn:
            return []
        return [_offer(mpn, self.name, self.stock_by_mpn[mpn])]


class CasingClient(FakeClient):
    """Mirrors the real parsers: matches the query case-insensitively but
    reports the *distributor's* casing (``mouser.py`` / ``digikey.py`` compare
    ``mpn.upper() != query_mpn.upper()`` and then pass ``mpn=mpn``).

    ``FakeClient`` echoes the queried string back, which is why the rest of
    the suite cannot see a casing mismatch.
    """

    def search(self, mpn):
        for canonical, stock in self.stock_by_mpn.items():
            if canonical.upper() == mpn.upper():
                return [_offer(canonical, self.name, stock)]
        return []


class RaisingClient(FakeClient):
    """Raises ``DistributorError`` for the MPNs in ``fail`` (HTTP 429 etc.)."""

    def __init__(self, name, stock_by_mpn, fail):
        super().__init__(name, stock_by_mpn)
        self.fail = set(fail)

    def search(self, mpn):
        if mpn in self.fail:
            raise DistributorError("Mouser rate limit reached (HTTP 429)")
        return super().search(mpn)


def _run(items, qty, clients, tmp_path, monkeypatch, fmt="table", capsys=None):
    monkeypatch.setenv("KCT_CACHE_DIR", str(tmp_path))
    from kicad_tools.parts.distributors import service
    from kicad_tools.parts.distributors.cache import DistributorCache

    real = service.query_stock

    def qs(*a, **k):
        k["cache"] = DistributorCache(tmp_path / "c.db")
        return real(*a, **k)

    monkeypatch.setattr(service, "query_stock", qs)
    rc = check_distributor_stock(items, qty, fmt, bypass_cache=True, clients=clients)
    return rc, capsys.readouterr()


def test_all_in_stock(tmp_path, monkeypatch, capsys):
    rc, _ = _run(
        [_item("U1", "A")],
        10,
        [FakeClient("mouser", {"A": 50})],
        tmp_path,
        monkeypatch,
        capsys=capsys,
    )
    assert rc == 0


def test_under_stocked_flagged(tmp_path, monkeypatch, capsys):
    rc, out = _run(
        [_item("U1", "A")],
        100,
        [FakeClient("mouser", {"A": 50})],
        tmp_path,
        monkeypatch,
        capsys=capsys,
    )
    assert rc == 2
    assert "U1" in out.out and "50" in out.out


def test_not_found_flagged(tmp_path, monkeypatch, capsys):
    rc, out = _run(
        [_item("U1", "A")], 1, [FakeClient("mouser", {})], tmp_path, monkeypatch, capsys=capsys
    )
    assert rc == 2
    assert "not found" in out.out


def test_no_mpn_not_checked(tmp_path, monkeypatch, capsys):
    rc, out = _run(
        [_item("R1", ""), _item("U1", "A")],
        1,
        [FakeClient("m", {"A": 5})],
        tmp_path,
        monkeypatch,
        capsys=capsys,
    )
    assert rc == 0
    assert "R1" in out.out and "not checked" in out.out


def test_no_credentials_notice_exit_zero(tmp_path, monkeypatch, capsys):
    for v in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    rc, out = _run([_item("U1", "A")], 1, None, tmp_path, monkeypatch, capsys=capsys)
    assert rc == 0
    assert "OUT OF STOCK" not in out.out


def test_shared_mpn_sums_quantity(tmp_path, monkeypatch, capsys):
    items = [_item("C1", "A"), _item("C2", "A"), _item("C3", "A")]
    rc, out = _run(
        items, 10, [FakeClient("m", {"A": 25})], tmp_path, monkeypatch, fmt="json", capsys=capsys
    )
    data = json.loads(out.out)
    assert rc == 2
    assert data["lines"][0]["needed"] == 30
    assert data["lines"][0]["references"] == ["C1", "C2", "C3"]


def test_distributors_disagree_not_flagged(tmp_path, monkeypatch, capsys):
    clients = [FakeClient("mouser", {"A": 1}), FakeClient("digikey", {"A": 500})]
    rc, _ = _run([_item("U1", "A")], 100, clients, tmp_path, monkeypatch, capsys=capsys)
    assert rc == 0


def test_bom_mpn_case_differs_from_distributor_casing(tmp_path, monkeypatch, capsys):
    """A BOM MPN differing only in case must still match the offer (PR #8898 review)."""
    rc, out = _run(
        [_item("U1", "stm32f103c8t6")],
        1,
        [CasingClient("mouser", {"STM32F103C8T6": 5000})],
        tmp_path,
        monkeypatch,
        fmt="json",
        capsys=capsys,
    )
    data = json.loads(out.out)
    assert rc == 0
    assert data["flagged"] == 0
    assert data["lines"][0]["best_stock"] == 5000
    assert data["lines"][0]["checked"] is True
    assert data["lines"][0]["distributor"] == "mouser"


def test_mpns_differing_only_in_case_share_one_line(tmp_path, monkeypatch, capsys):
    """Two BOM lines whose MPNs differ only in case sum into one group."""
    items = [_item("U1", "stm32f103c8t6"), _item("U2", "STM32F103C8T6")]
    rc, out = _run(
        items,
        10,
        [CasingClient("mouser", {"STM32F103C8T6": 5000})],
        tmp_path,
        monkeypatch,
        fmt="json",
        capsys=capsys,
    )
    data = json.loads(out.out)
    assert rc == 0
    assert len(data["lines"]) == 1
    assert data["lines"][0]["needed"] == 20
    assert data["lines"][0]["references"] == ["U1", "U2"]


def test_failed_lookup_is_not_checked_not_out_of_stock(tmp_path, monkeypatch, capsys):
    """An MPN whose lookup raised is 'not checked', never flagged (PR #8898 review)."""
    rc, out = _run(
        [_item("U1", "A"), _item("U2", "B")],
        1,
        [RaisingClient("mouser", {"A": 5000, "B": 5000}, fail=["B"])],
        tmp_path,
        monkeypatch,
        fmt="json",
        capsys=capsys,
    )
    data = json.loads(out.out)
    assert rc == 0
    assert data["flagged"] == 0
    assert data["not_checked"] == 1
    b = next(ln for ln in data["lines"] if ln["mpn"] == "B")
    assert b["checked"] is False
    assert b["flagged"] is False
    assert any("429" in n for n in data["notices"])


def test_failed_lookup_table_says_not_checked(tmp_path, monkeypatch, capsys):
    rc, out = _run(
        [_item("U1", "A"), _item("U2", "B")],
        1,
        [RaisingClient("mouser", {"A": 5000, "B": 5000}, fail=["B"])],
        tmp_path,
        monkeypatch,
        capsys=capsys,
    )
    assert rc == 0
    assert "NOT CHECKED" in out.out
    assert "1 not checked" in out.out
    assert "OUT OF STOCK" not in out.out


def test_all_lookups_failed_reports_nothing_checked(tmp_path, monkeypatch, capsys):
    """Credentials present but every lookup errored: nothing checked, nothing flagged."""
    rc, out = _run(
        [_item("U1", "A")],
        1,
        [RaisingClient("mouser", {"A": 5000}, fail=["A"])],
        tmp_path,
        monkeypatch,
        capsys=capsys,
    )
    assert rc == 0
    assert "no lines were checked" in out.out
    assert "OUT OF STOCK" not in out.out


def test_genuine_not_found_is_still_flagged(tmp_path, monkeypatch, capsys):
    """A distributor that answered with no match still counts as no stock."""
    rc, out = _run(
        [_item("U1", "A"), _item("U2", "B")],
        1,
        [FakeClient("mouser", {"A": 5000})],
        tmp_path,
        monkeypatch,
        fmt="json",
        capsys=capsys,
    )
    data = json.loads(out.out)
    assert rc == 2
    assert data["flagged"] == 1
    assert data["not_checked"] == 0
    b = next(ln for ln in data["lines"] if ln["mpn"] == "B")
    assert b["checked"] is True
    assert b["flagged"] is True
