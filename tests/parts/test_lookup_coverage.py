"""Supplier outages must not be represented as verified catalog absence."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kicad_tools.parts.lcsc import LCSCClient, LCSCUnavailableError
from kicad_tools.parts.models import Part


@pytest.fixture
def client():
    return LCSCClient(
        use_cache=False, use_local_catalog=False, use_official_api=False, rate_limit=0
    )


@pytest.mark.parametrize("status", [403, 404])
def test_transport_status_is_not_no_match(client, monkeypatch, status):
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError("sensitive server body", response=response)
    monkeypatch.setattr(client, "_make_request", Mock(side_effect=error))
    result = client.lookup_result("C1987701")
    assert result.status == "unavailable"
    assert str(status) in " ".join(result.diagnostics)
    assert "sensitive" not in " ".join(result.diagnostics)
    with pytest.raises(LCSCUnavailableError):
        client.lookup("C1987701")
    with pytest.raises(LCSCUnavailableError):
        client.search("exact MPN")


@pytest.mark.parametrize(
    "response",
    [None, {"code": 403, "message": "private"}, {"code": 200}, {"code": 200, "data": "invalid"}],
)
def test_business_and_malformed_lookup_are_unavailable(client, monkeypatch, response):
    monkeypatch.setattr(client, "_make_request", Mock(return_value=response))
    with pytest.raises(LCSCUnavailableError):
        client.lookup("C1")


def test_verified_empty_is_distinct(client, monkeypatch):
    request = Mock(return_value={"code": 200, "data": None})
    monkeypatch.setattr(client, "_make_request", request)
    assert client.lookup("C1") is None
    assert client.lookup_result("C1").status == "not_found"
    request.return_value = {"code": 200, "data": {"componentPageInfo": {"list": None, "total": 0}}}
    result = client.search("missing")
    assert result.parts == []
    assert result.coverage == "live"


def test_offline_hit_and_miss_preserve_coverage_and_part(client, monkeypatch):
    part = Part("C1", mfr_part="EXACT")
    catalog = SimpleNamespace(
        available=True,
        lookup=lambda number: part if number == "C1" else None,
        search=lambda *a, **kw: [part],
    )
    monkeypatch.setattr(client, "_get_catalog", lambda: catalog)
    monkeypatch.setattr(client, "_make_request", Mock(side_effect=requests.Timeout("private")))
    result = client.lookup_result("C1")
    assert result.part is part
    assert result.source == "offline"
    assert client.lookup_result("C2").status == "offline_miss"
    with pytest.raises(LCSCUnavailableError):
        client.lookup("C2")
    search = client.search("EXACT")
    assert search.parts == [part]
    assert search.coverage == "offline"
    assert any("current inventory" in d for d in search.diagnostics)
    # Business failure must take the same useful fallback path.
    monkeypatch.setattr(client, "_make_request", Mock(return_value={"code": 500}))
    assert client.search("EXACT").parts == [part]


def test_batch_retains_partial_results_and_unknown_ids(client, monkeypatch):
    part = Part("C1", mfr_part="EXACT")
    monkeypatch.setattr(client, "_fetch_part", Mock(side_effect=[part, requests.Timeout()]))
    with pytest.raises(LCSCUnavailableError) as failure:
        client.lookup_many(["C1", "C2"])
    assert failure.value.partial_results["C1"] is part
    assert failure.value.unavailable_parts == {"C2"}
    assert failure.value.partial_results.sources == {"C1": "anonymous"}


def test_official_no_match_survives_anonymous_outage(client, monkeypatch):
    official = SimpleNamespace(get_component_detail_by_codes=lambda numbers: {})
    monkeypatch.setattr(client, "_get_official_client", lambda: official)
    monkeypatch.setattr(client, "_make_request", Mock(side_effect=requests.Timeout()))
    assert client.lookup_result("C1").status == "not_found"
    assert client.lookup_many(["C1"]) == {}


def test_cli_json_distinguishes_outage_and_offline_empty(client, monkeypatch, capsys):
    import kicad_tools.parts
    from kicad_tools.cli.parts_cmd import main

    monkeypatch.setattr(kicad_tools.parts, "LCSCClient", lambda: client)
    monkeypatch.setattr(client, "_make_request", Mock(side_effect=requests.Timeout()))
    assert main(["lookup", "C1", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["lookup"]["status"] == "unavailable"
    assert main(["search", "missing", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    catalog = SimpleNamespace(available=True, search=lambda *a, **kw: [])
    monkeypatch.setattr(client, "_get_catalog", lambda: catalog)
    assert main(["search", "missing", "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["coverage"] == "offline"
    assert result["parts"] == []


def test_batch_availability_keeps_known_parts(client, monkeypatch):
    from kicad_tools.cost.availability import LCSCAvailabilityChecker

    part = Part("C1", stock=20)
    failure = LCSCUnavailableError(
        "unavailable", partial_results={"C1": part}, unavailable_parts={"C2"}
    )
    monkeypatch.setattr(client, "lookup_many", Mock(side_effect=failure))
    items = [
        SimpleNamespace(
            reference="R1",
            value="10k",
            footprint="0402",
            lcsc="C1",
            quantity=1,
            dnp=False,
            is_virtual=False,
            mpn="",
        ),
        SimpleNamespace(
            reference="R2",
            value="10k",
            footprint="0402",
            lcsc="C2",
            quantity=1,
            dnp=False,
            is_virtual=False,
            mpn="",
        ),
    ]
    basic = client.check_bom(items)
    assert basic.items[0].part is part
    assert "unavailable" in basic.items[1].error
    checker = LCSCAvailabilityChecker(find_alternatives=False)
    monkeypatch.setattr(checker, "_get_client", lambda: client)
    result = checker.check_items(items)
    assert result.items[0].quantity_available == 20
    assert result.items[1].status.value == "unavailable"
    assert "not verified" in result.items[1].error


def test_cost_estimator_keeps_partial_prices_and_marks_unavailable(client, monkeypatch):
    import kicad_tools.parts
    from kicad_tools.cost.estimator import ManufacturingCostEstimator
    from kicad_tools.parts.models import PartPrice

    part = Part("C1", stock=20, prices=[PartPrice(1, 0.12)])
    failure = LCSCUnavailableError(
        "unavailable", partial_results={"C1": part}, unavailable_parts={"C2"}
    )
    monkeypatch.setattr(client, "lookup_many", Mock(side_effect=failure))
    monkeypatch.setattr(kicad_tools.parts, "LCSCClient", lambda: client)
    groups = [
        SimpleNamespace(
            lcsc=code, items=[], references=ref, value="10k", footprint="0402", mpn="", quantity=1
        )
        for code, ref in [("C1", "R1"), ("C2", "R2")]
    ]
    estimator = ManufacturingCostEstimator()
    costs = estimator._estimate_component_costs(SimpleNamespace(grouped=lambda: groups), 1)
    assert costs[0].unit_cost == 0.12
    assert costs[0].pricing_source == "lcsc"
    assert not costs[0].lookup_unavailable
    assert costs[1].pricing_source == "estimated"
    assert costs[1].lookup_unavailable
    assert not costs[1].in_stock


def test_offline_search_miss_cannot_become_bom_no_match(client, monkeypatch):
    from kicad_tools.cost.suggest import PartSuggester
    from kicad_tools.parts.models import SearchResult

    monkeypatch.setattr(
        client, "search", lambda *a, **kw: SearchResult(query="10k", coverage="offline")
    )
    suggester = PartSuggester()
    monkeypatch.setattr(suggester, "_get_client", lambda: client)
    with pytest.raises(LCSCUnavailableError):
        suggester.suggest_for_component(
            reference="R1", value="10k", footprint="Resistor_SMD:R_0402_1005Metric"
        )
