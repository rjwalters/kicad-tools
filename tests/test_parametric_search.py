"""Tests for ComposedPartStore parametric search."""

from __future__ import annotations

from datetime import datetime

import pytest

from kicad_tools.parts.composition import (
    ComposedPart,
    ComposedPartStore,
    Entity,
    PinDirection,
    Unit,
    UnitPin,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def resistor_entity() -> Entity:
    unit = Unit(
        id="passive",
        name="Passive",
        pins=[
            UnitPin(name="1", number="1", direction=PinDirection.PASSIVE),
            UnitPin(name="2", number="2", direction=PinDirection.PASSIVE),
        ],
    )
    return Entity(id="resistor", name="Resistor", units=[unit])


@pytest.fixture
def capacitor_entity() -> Entity:
    unit = Unit(
        id="passive",
        name="Passive",
        pins=[
            UnitPin(name="1", number="1", direction=PinDirection.PASSIVE),
            UnitPin(name="2", number="2", direction=PinDirection.PASSIVE),
        ],
    )
    return Entity(id="capacitor", name="Capacitor", units=[unit])


@pytest.fixture
def store(tmp_path) -> ComposedPartStore:
    return ComposedPartStore(tmp_path / "composed.db")


@pytest.fixture
def populated_store(
    store: ComposedPartStore,
    resistor_entity: Entity,
    capacitor_entity: Entity,
) -> ComposedPartStore:
    """Store with a handful of test parts."""
    parts = [
        ComposedPart(
            id="r-10k-0402",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            mpn="RC0402FR-0710KL",
            manufacturer="Yageo",
            params={"resistance": "10000", "tolerance": "1%"},
            tags=["smd", "basic"],
        ),
        ComposedPart(
            id="r-10k-0603",
            entity=resistor_entity,
            package="0603",
            category="resistor",
            mpn="RC0603FR-0710KL",
            manufacturer="Yageo",
            params={"resistance": "10000", "tolerance": "1%"},
            tags=["smd", "basic"],
        ),
        ComposedPart(
            id="r-4k7-0402",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            mpn="RC0402FR-074K7L",
            manufacturer="Yageo",
            params={"resistance": "4700", "tolerance": "1%"},
            tags=["smd"],
        ),
        ComposedPart(
            id="c-100n-0402",
            entity=capacitor_entity,
            package="0402",
            category="capacitor",
            mpn="CL05B104KO5NNNC",
            manufacturer="Samsung",
            params={"capacitance": "100nF", "voltage": "16V"},
            tags=["smd", "basic", "mlcc"],
        ),
        ComposedPart(
            id="c-10u-0805",
            entity=capacitor_entity,
            package="0805",
            category="capacitor",
            mpn="CL21A106KAYNNNE",
            manufacturer="Samsung",
            params={"capacitance": "10uF", "voltage": "25V"},
            tags=["smd", "mlcc"],
        ),
    ]
    for p in parts:
        store.save(p)
    return store


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestComposedPartStoreCRUD:
    def test_save_and_get(self, store: ComposedPartStore, resistor_entity: Entity):
        part = ComposedPart(
            id="r1",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            params={"resistance": "10000"},
            tags=["smd"],
        )
        store.save(part)
        retrieved = store.get("r1")

        assert retrieved is not None
        assert retrieved.id == "r1"
        assert retrieved.package == "0402"
        assert retrieved.category == "resistor"
        assert retrieved.params == {"resistance": "10000"}
        assert retrieved.tags == ["smd"]
        assert retrieved.entity.id == "resistor"
        assert retrieved.entity.total_pins == 2

    def test_get_not_found(self, store: ComposedPartStore):
        assert store.get("nonexistent") is None

    def test_save_replaces(self, store: ComposedPartStore, resistor_entity: Entity):
        part = ComposedPart(
            id="r1",
            entity=resistor_entity,
            params={"resistance": "10000"},
            tags=["old"],
        )
        store.save(part)

        # Update
        part.params = {"resistance": "4700"}
        part.tags = ["new"]
        store.save(part)

        retrieved = store.get("r1")
        assert retrieved is not None
        assert retrieved.params == {"resistance": "4700"}
        assert retrieved.tags == ["new"]

    def test_delete(self, store: ComposedPartStore, resistor_entity: Entity):
        part = ComposedPart(id="r1", entity=resistor_entity)
        store.save(part)
        assert store.delete("r1") is True
        assert store.get("r1") is None

    def test_delete_not_found(self, store: ComposedPartStore):
        assert store.delete("nonexistent") is False

    def test_save_with_base_part(self, store: ComposedPartStore, resistor_entity: Entity):
        base = ComposedPart(
            id="base",
            entity=resistor_entity,
            package="0402",
            manufacturer="Yageo",
        )
        store.save(base)

        variant = ComposedPart(
            id="variant",
            entity=resistor_entity,
            package="0402",
            base_part=base,
            mpn="RC0402FR-0710KL",
        )
        store.save(variant)

        retrieved = store.get("variant")
        assert retrieved is not None
        assert retrieved.base_part is not None
        assert retrieved.base_part.id == "base"
        assert retrieved.base_part.manufacturer == "Yageo"

    def test_save_with_pad_to_pin(self, store: ComposedPartStore, resistor_entity: Entity):
        part = ComposedPart(
            id="mapped",
            entity=resistor_entity,
            pad_to_pin={"1": "1", "2": "2"},
        )
        store.save(part)
        retrieved = store.get("mapped")
        assert retrieved is not None
        assert retrieved.pad_to_pin == {"1": "1", "2": "2"}

    def test_created_at_preserved(self, store: ComposedPartStore, resistor_entity: Entity):
        ts = datetime(2026, 1, 15, 12, 0, 0)
        part = ComposedPart(id="ts-test", entity=resistor_entity, created_at=ts)
        store.save(part)
        retrieved = store.get("ts-test")
        assert retrieved is not None
        assert retrieved.created_at == ts


# ---------------------------------------------------------------------------
# Parametric search
# ---------------------------------------------------------------------------


class TestParametricSearch:
    def test_find_by_category(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(category="resistor")
        assert len(results) == 3
        assert all(r.category == "resistor" for r in results)

    def test_find_by_package(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(package="0402")
        assert len(results) == 3  # 2 resistors + 1 cap

    def test_find_by_category_and_package(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(category="resistor", package="0402")
        assert len(results) == 2

    def test_find_by_manufacturer(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(manufacturer="Samsung")
        assert len(results) == 2

    def test_find_by_mpn(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(mpn="RC0402FR-0710KL")
        assert len(results) == 1
        assert results[0].id == "r-10k-0402"

    def test_find_by_param(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(params={"resistance": "10000"})
        assert len(results) == 2  # 0402 and 0603 variants

    def test_find_by_multiple_params(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(params={"resistance": "10000", "tolerance": "1%"})
        assert len(results) == 2

    def test_find_by_tag(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(tags=["basic"])
        assert len(results) == 3  # 2 basic resistors + 1 basic cap

    def test_find_by_multiple_tags(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(tags=["smd", "mlcc"])
        assert len(results) == 2  # both caps

    def test_find_combined_filters(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(
            category="resistor",
            package="0402",
            params={"resistance": "10000"},
            tags=["basic"],
        )
        assert len(results) == 1
        assert results[0].id == "r-10k-0402"

    def test_find_no_results(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(category="inductor")
        assert results == []

    def test_find_no_filters(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts()
        assert len(results) == 5  # all parts

    def test_find_with_limit(self, populated_store: ComposedPartStore):
        results = populated_store.find_parts(limit=2)
        assert len(results) == 2

    def test_find_by_tag_convenience(self, populated_store: ComposedPartStore):
        results = populated_store.find_by_tag("mlcc")
        assert len(results) == 2

    def test_empty_store_returns_empty(self, store: ComposedPartStore):
        results = store.find_parts(category="resistor")
        assert results == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestComposedPartStoreStats:
    def test_stats_empty(self, store: ComposedPartStore):
        s = store.stats()
        assert s["total"] == 0
        assert s["categories"] == {}
        assert s["distinct_tags"] == 0

    def test_stats_populated(self, populated_store: ComposedPartStore):
        s = populated_store.stats()
        assert s["total"] == 5
        assert s["categories"]["resistor"] == 3
        assert s["categories"]["capacitor"] == 2
        assert s["distinct_tags"] > 0


# ---------------------------------------------------------------------------
# Part.to_composed round-trip
# ---------------------------------------------------------------------------


class TestPartToComposed:
    def test_round_trip(self):
        from kicad_tools.parts.models import PackageType, Part, PartCategory

        part = Part(
            lcsc_part="C123456",
            mfr_part="RC0402FR-0710KL",
            manufacturer="Yageo",
            description="10K 1% 0402 Resistor",
            category=PartCategory.RESISTOR,
            package="0402",
            package_type=PackageType.SMD,
            value="10k",
            tolerance="1%",
            voltage_rating="50V",
            is_basic=True,
            datasheet_url="https://example.com/ds.pdf",
        )
        composed = part.to_composed()

        assert composed.id == "C123456"
        assert composed.mpn == "RC0402FR-0710KL"
        assert composed.manufacturer == "Yageo"
        assert composed.package == "0402"
        assert composed.category == "resistor"
        assert composed.lcsc_part == "C123456"
        assert composed.datasheet_url == "https://example.com/ds.pdf"
        assert composed.params["value"] == "10k"
        assert composed.params["tolerance"] == "1%"
        assert composed.params["voltage"] == "50V"
        assert "smd" in composed.tags
        assert "jlcpcb-basic" in composed.tags
        assert "resistor" in composed.tags
        # Entity has default two-pin passive
        assert composed.entity.total_pins == 2

    def test_round_trip_minimal(self):
        from kicad_tools.parts.models import Part

        part = Part(lcsc_part="C1")
        composed = part.to_composed()
        assert composed.id == "C1"
        assert composed.params == {}
        assert composed.entity.total_pins == 2

    def test_round_trip_through_hole(self):
        from kicad_tools.parts.models import PackageType, Part

        part = Part(
            lcsc_part="C999",
            package_type=PackageType.THROUGH_HOLE,
        )
        composed = part.to_composed()
        assert "through-hole" in composed.tags

    def test_round_trip_preferred(self):
        from kicad_tools.parts.models import Part

        part = Part(lcsc_part="C888", is_preferred=True)
        composed = part.to_composed()
        assert "jlcpcb-preferred" in composed.tags

    def test_round_trip_with_specs(self):
        from kicad_tools.parts.models import Part

        part = Part(
            lcsc_part="C777",
            value="100nF",
            specs={"dielectric": "X7R", "esr": "low"},
        )
        composed = part.to_composed()
        assert composed.params["value"] == "100nF"
        assert composed.params["dielectric"] == "X7R"
        assert composed.params["esr"] == "low"

    def test_to_composed_then_store(self, tmp_path):
        """Verify a Part can be projected and stored in ComposedPartStore."""
        from kicad_tools.parts.models import PackageType, Part, PartCategory

        part = Part(
            lcsc_part="C123",
            mfr_part="TEST",
            manufacturer="TestMfr",
            category=PartCategory.CAPACITOR,
            package="0402",
            package_type=PackageType.SMD,
            value="100nF",
        )
        composed = part.to_composed()

        store = ComposedPartStore(tmp_path / "test.db")
        store.save(composed)

        # Search for it
        results = store.find_parts(category="capacitor", package="0402")
        assert len(results) == 1
        assert results[0].id == "C123"
        assert results[0].params["value"] == "100nF"
