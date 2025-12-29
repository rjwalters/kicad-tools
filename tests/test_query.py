"""Tests for the query module."""

import pytest
from dataclasses import dataclass
from typing import Optional

from kicad_tools.query.base import BaseQuery
from kicad_tools.query.symbols import SymbolList, SymbolQuery
from kicad_tools.query.footprints import FootprintList, FootprintQuery
from kicad_tools.schema.schematic import Schematic
from kicad_tools.schema.pcb import PCB


# Test data class for BaseQuery tests
@dataclass
class MockItem:
    name: str
    value: int
    category: str
    active: bool = True


class TestBaseQuery:
    """Tests for BaseQuery filter logic."""

    @pytest.fixture
    def items(self):
        return [
            MockItem("alpha", 10, "A"),
            MockItem("beta", 20, "B"),
            MockItem("gamma", 30, "A"),
            MockItem("delta", 40, "B", active=False),
            MockItem("epsilon", 50, "C"),
        ]

    def test_all_returns_all_items(self, items):
        query = BaseQuery(items)
        assert query.all() == items

    def test_filter_exact_match(self, items):
        query = BaseQuery(items)
        result = query.filter(name="alpha").all()
        assert len(result) == 1
        assert result[0].name == "alpha"

    def test_filter_contains(self, items):
        query = BaseQuery(items)
        result = query.filter(name__contains="a").all()
        # alpha, beta, gamma, delta all contain 'a'
        assert len(result) == 4

    def test_filter_startswith(self, items):
        query = BaseQuery(items)
        result = query.filter(name__startswith="a").all()
        assert len(result) == 1
        assert result[0].name == "alpha"

    def test_filter_endswith(self, items):
        query = BaseQuery(items)
        result = query.filter(name__endswith="a").all()
        # alpha, beta, gamma, delta all end with 'a'
        assert len(result) == 4

    def test_filter_in(self, items):
        query = BaseQuery(items)
        result = query.filter(category__in=["A", "C"]).all()
        assert len(result) == 3
        assert all(item.category in ["A", "C"] for item in result)

    def test_filter_gt(self, items):
        query = BaseQuery(items)
        result = query.filter(value__gt=25).all()
        assert len(result) == 3
        assert all(item.value > 25 for item in result)

    def test_filter_lt(self, items):
        query = BaseQuery(items)
        result = query.filter(value__lt=25).all()
        assert len(result) == 2
        assert all(item.value < 25 for item in result)

    def test_filter_gte(self, items):
        query = BaseQuery(items)
        result = query.filter(value__gte=30).all()
        assert len(result) == 3
        assert all(item.value >= 30 for item in result)

    def test_filter_lte(self, items):
        query = BaseQuery(items)
        result = query.filter(value__lte=30).all()
        assert len(result) == 3
        assert all(item.value <= 30 for item in result)

    def test_filter_regex(self, items):
        query = BaseQuery(items)
        result = query.filter(name__regex=r"^[a-d]").all()
        # alpha, beta, delta match
        assert len(result) == 3

    def test_filter_icontains(self, items):
        query = BaseQuery(items)
        result = query.filter(name__icontains="A").all()
        # alpha, beta, gamma, delta all contain 'a' (case-insensitive)
        assert len(result) == 4

    def test_filter_chain(self, items):
        query = BaseQuery(items)
        result = query.filter(category="A").filter(value__gt=15).all()
        assert len(result) == 1
        assert result[0].name == "gamma"

    def test_filter_multiple_kwargs(self, items):
        query = BaseQuery(items)
        result = query.filter(category="B", active=True).all()
        assert len(result) == 1
        assert result[0].name == "beta"

    def test_exclude(self, items):
        query = BaseQuery(items)
        result = query.exclude(category="A").all()
        assert len(result) == 3
        assert all(item.category != "A" for item in result)

    def test_first_returns_first_match(self, items):
        query = BaseQuery(items)
        result = query.filter(category="A").first()
        assert result is not None
        assert result.name == "alpha"

    def test_first_returns_none_when_empty(self, items):
        query = BaseQuery(items)
        result = query.filter(category="Z").first()
        assert result is None

    def test_count(self, items):
        query = BaseQuery(items)
        assert query.filter(category="A").count() == 2

    def test_exists(self, items):
        query = BaseQuery(items)
        assert query.filter(category="A").exists() is True
        assert query.filter(category="Z").exists() is False

    def test_iter(self, items):
        query = BaseQuery(items)
        result = list(query.filter(category="A"))
        assert len(result) == 2

    def test_len(self, items):
        query = BaseQuery(items)
        assert len(query.filter(category="A")) == 2

    def test_bool(self, items):
        query = BaseQuery(items)
        assert bool(query.filter(category="A")) is True
        assert bool(query.filter(category="Z")) is False

    def test_getitem(self, items):
        query = BaseQuery(items)
        result = query.filter(category="A")
        assert result[0].name == "alpha"
        assert result[1].name == "gamma"

    def test_values_list_flat(self, items):
        query = BaseQuery(items)
        result = query.filter(category="A").values_list("name", flat=True)
        assert result == ["alpha", "gamma"]


class TestSymbolQueryIntegration:
    """Integration tests for SymbolQuery with real schematic."""

    def test_symbols_returns_symbol_list(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        assert isinstance(sch.symbols, SymbolList)

    def test_backward_compatible_iteration(self, minimal_schematic):
        """Existing code that iterates over symbols should still work."""
        sch = Schematic.load(minimal_schematic)
        count = 0
        for sym in sch.symbols:
            count += 1
            assert hasattr(sym, "reference")
        assert count >= 1

    def test_backward_compatible_len(self, minimal_schematic):
        """len(sch.symbols) should still work."""
        sch = Schematic.load(minimal_schematic)
        assert len(sch.symbols) >= 1

    def test_backward_compatible_indexing(self, minimal_schematic):
        """sch.symbols[0] should still work."""
        sch = Schematic.load(minimal_schematic)
        sym = sch.symbols[0]
        assert hasattr(sym, "reference")

    def test_by_reference(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        r1 = sch.symbols.by_reference("R1")
        assert r1 is not None
        assert r1.reference == "R1"

    def test_by_reference_not_found(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        result = sch.symbols.by_reference("NOTFOUND")
        assert result is None

    def test_filter_by_value(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        result = sch.symbols.filter(value="10k")
        assert len(result) >= 1
        assert all(s.value == "10k" for s in result)

    def test_query_method(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        query = sch.symbols.query()
        assert isinstance(query, SymbolQuery)

    def test_query_chain(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        result = sch.symbols.query().filter(reference__startswith="R").all()
        assert len(result) >= 1
        assert all(s.reference.startswith("R") for s in result)

    def test_resistors_shortcut(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        result = sch.symbols.resistors()
        assert len(result) >= 1
        assert all(s.reference.startswith("R") for s in result)

    def test_references_method(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        refs = sch.symbols.references()
        assert "R1" in refs
        assert refs == sorted(refs)  # Should be sorted


class TestFootprintQueryIntegration:
    """Integration tests for FootprintQuery with real PCB."""

    def test_footprints_returns_footprint_list(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        assert isinstance(pcb.footprints, FootprintList)

    def test_backward_compatible_iteration(self, minimal_pcb):
        """Existing code that iterates over footprints should still work."""
        pcb = PCB.load(str(minimal_pcb))
        count = 0
        for fp in pcb.footprints:
            count += 1
            assert hasattr(fp, "reference")
        assert count >= 1

    def test_backward_compatible_len(self, minimal_pcb):
        """len(pcb.footprints) should still work."""
        pcb = PCB.load(str(minimal_pcb))
        assert len(pcb.footprints) >= 1

    def test_by_reference(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        r1 = pcb.footprints.by_reference("R1")
        assert r1 is not None
        assert r1.reference == "R1"

    def test_by_reference_not_found(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        result = pcb.footprints.by_reference("NOTFOUND")
        assert result is None

    def test_filter_by_layer(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        result = pcb.footprints.filter(layer="F.Cu")
        assert len(result) >= 1
        assert all(fp.layer == "F.Cu" for fp in result)

    def test_query_method(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        query = pcb.footprints.query()
        assert isinstance(query, FootprintQuery)

    def test_on_top_shortcut(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        result = pcb.footprints.on_top()
        assert len(result) >= 1
        assert all(fp.layer == "F.Cu" for fp in result)

    def test_resistors_shortcut(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        result = pcb.footprints.resistors()
        assert len(result) >= 1
        assert all(fp.reference.startswith("R") for fp in result)

    def test_references_method(self, minimal_pcb):
        pcb = PCB.load(str(minimal_pcb))
        refs = pcb.footprints.references()
        assert "R1" in refs
        assert refs == sorted(refs)  # Should be sorted


class TestRoutingPCBQueries:
    """Test queries on the routing test PCB with multiple components."""

    def test_multiple_footprints(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        assert len(pcb.footprints) == 3

    def test_filter_ics(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        ics = pcb.footprints.ics()
        assert len(ics) == 1
        assert ics[0].reference == "U1"

    def test_filter_connectors(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        connectors = pcb.footprints.connectors()
        assert len(connectors) == 1
        assert connectors[0].reference == "J1"

    def test_filter_resistors(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        resistors = pcb.footprints.resistors()
        assert len(resistors) == 1
        assert resistors[0].reference == "R1"

    def test_query_with_prefix(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        result = pcb.footprints.query().with_prefix("U").all()
        assert len(result) == 1

    def test_exclude(self, routing_test_pcb):
        pcb = PCB.load(str(routing_test_pcb))
        result = pcb.footprints.exclude(reference__startswith="U")
        assert len(result) == 2
        assert all(not fp.reference.startswith("U") for fp in result)


class TestSymbolListMethods:
    """Test SymbolList convenience methods."""

    def test_non_power_excludes_power_symbols(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        # Minimal schematic doesn't have power symbols, so all should be returned
        non_power = sch.symbols.non_power()
        all_symbols = list(sch.symbols)
        assert len(non_power) == len(all_symbols)

    def test_in_bom_filters_correctly(self, minimal_schematic):
        sch = Schematic.load(minimal_schematic)
        in_bom = sch.symbols.in_bom()
        # R1 should be in BOM
        refs = [s.reference for s in in_bom]
        assert "R1" in refs
