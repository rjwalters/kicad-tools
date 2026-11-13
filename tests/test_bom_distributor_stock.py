"""Tests for ``kct bom --check-distributor-stock`` (Issue #8893)."""

import json
from types import SimpleNamespace

from kicad_tools.cli.bom_cmd import check_distributor_stock
from kicad_tools.parts.distributors.base import DistributorOffer


def _item(ref, mpn):
    return SimpleNamespace(reference=ref, mpn=mpn)


class FakeClient:
    daily_quota = 10_000
    region = "US"

    def __init__(self, name, stock_by_mpn):
        self.name = name
        self.stock_by_mpn = stock_by_mpn

    def search(self, mpn):
        if mpn not in self.stock_by_mpn:
            return []
        return [
            DistributorOffer(
                mpn=mpn,
                distributor=self.name,
                distributor_pn="x",
                stock=self.stock_by_mpn[mpn],
                currency="USD",
                region="US",
                fetched_at="2026-01-01T00:00:00Z",
            )
        ]


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
