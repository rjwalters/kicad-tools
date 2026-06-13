"""Tests for composition-based part model (Unit, Entity, ComposedPart)."""

from __future__ import annotations

import pytest

from kicad_tools.parts.composition import (
    ComposedPart,
    Entity,
    PinDirection,
    Unit,
    UnitPin,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def passive_unit() -> Unit:
    """A two-pin passive unit (e.g., resistor)."""
    return Unit(
        id="passive",
        name="Passive",
        pins=[
            UnitPin(name="1", number="1", direction=PinDirection.PASSIVE),
            UnitPin(name="2", number="2", direction=PinDirection.PASSIVE),
        ],
    )


@pytest.fixture
def opamp_unit() -> Unit:
    """A single op-amp gate unit."""
    return Unit(
        id="opamp-gate",
        name="Op-Amp Gate",
        pins=[
            UnitPin(name="IN+", number="3", direction=PinDirection.INPUT),
            UnitPin(name="IN-", number="2", direction=PinDirection.INPUT),
            UnitPin(name="OUT", number="1", direction=PinDirection.OUTPUT),
        ],
    )


@pytest.fixture
def power_unit() -> Unit:
    """A power-supply unit."""
    return Unit(
        id="power",
        name="Power",
        pins=[
            UnitPin(name="VCC", number="8", direction=PinDirection.POWER_IN),
            UnitPin(name="GND", number="4", direction=PinDirection.POWER_IN),
        ],
    )


@pytest.fixture
def resistor_entity(passive_unit: Unit) -> Entity:
    return Entity(id="resistor", name="Resistor", units=[passive_unit])


@pytest.fixture
def quad_opamp_entity(opamp_unit: Unit, power_unit: Unit) -> Entity:
    """Four identical op-amp gates plus one power unit."""
    gates = [
        Unit(id=f"opamp-gate-{i}", name=f"Gate {i}", pins=list(opamp_unit.pins))
        for i in range(1, 5)
    ]
    return Entity(id="lm324", name="LM324 Quad Op-Amp", units=[*gates, power_unit])


# ---------------------------------------------------------------------------
# UnitPin tests
# ---------------------------------------------------------------------------


class TestUnitPin:
    def test_defaults(self):
        pin = UnitPin(name="A", number="1")
        assert pin.direction == PinDirection.UNSPECIFIED
        assert pin.alternate_names == []

    def test_alternate_names(self):
        pin = UnitPin(name="SDA", number="5", alternate_names=["I2C_DATA"])
        assert "I2C_DATA" in pin.alternate_names


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestUnit:
    def test_pin_count(self, passive_unit: Unit):
        assert passive_unit.pin_count == 2

    def test_get_pin(self, passive_unit: Unit):
        p = passive_unit.get_pin("1")
        assert p is not None
        assert p.name == "1"

    def test_get_pin_not_found(self, passive_unit: Unit):
        assert passive_unit.get_pin("99") is None

    def test_get_pins_by_name(self, opamp_unit: Unit):
        ins = opamp_unit.get_pins_by_name("IN+")
        assert len(ins) == 1
        assert ins[0].number == "3"


# ---------------------------------------------------------------------------
# Entity tests
# ---------------------------------------------------------------------------


class TestEntity:
    def test_single_unit_entity(self, resistor_entity: Entity):
        assert len(resistor_entity.units) == 1
        assert resistor_entity.total_pins == 2

    def test_multi_unit_entity(self, quad_opamp_entity: Entity):
        # 4 gates x 3 pins + 1 power unit x 2 pins = 14
        assert len(quad_opamp_entity.units) == 5
        assert quad_opamp_entity.total_pins == 14


# ---------------------------------------------------------------------------
# ComposedPart tests
# ---------------------------------------------------------------------------


class TestComposedPart:
    def test_standalone_part(self, resistor_entity: Entity):
        part = ComposedPart(
            id="r-10k-0402",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            mpn="RC0402FR-0710KL",
            manufacturer="Yageo",
            params={"resistance": "10000", "tolerance": "1%"},
            tags=["smd", "basic"],
        )
        assert part.base_part is None
        assert part.params["resistance"] == "10000"

    def test_resolve_standalone(self, resistor_entity: Entity):
        part = ComposedPart(
            id="r-10k",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            params={"resistance": "10000"},
            tags=["smd"],
        )
        resolved = part.resolve()
        assert resolved["package"] == "0402"
        assert resolved["category"] == "resistor"
        assert resolved["params"] == {"resistance": "10000"}
        assert resolved["tags"] == ["smd"]
        assert resolved["entity"] is resistor_entity

    def test_inheritance_basic(self, resistor_entity: Entity):
        base = ComposedPart(
            id="r-0402-base",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            manufacturer="Yageo",
            description="0402 resistor",
            tags=["smd"],
        )
        variant = ComposedPart(
            id="r-10k-0402",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            mpn="RC0402FR-0710KL",
            base_part=base,
            params={"resistance": "10000", "tolerance": "1%"},
            tags=["basic"],
        )

        resolved = variant.resolve()
        # mpn comes from variant
        assert resolved["mpn"] == "RC0402FR-0710KL"
        # manufacturer inherited from base
        assert resolved["manufacturer"] == "Yageo"
        # tags are unioned
        assert set(resolved["tags"]) == {"smd", "basic"}
        # params merged
        assert resolved["params"]["resistance"] == "10000"

    def test_inheritance_override(self, resistor_entity: Entity):
        base = ComposedPart(
            id="base",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            description="Generic 0402",
            params={"tolerance": "5%"},
        )
        variant = ComposedPart(
            id="variant",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            description="Precision 0402",
            base_part=base,
            params={"tolerance": "1%"},
        )

        resolved = variant.resolve()
        # Description overridden by variant
        assert resolved["description"] == "Precision 0402"
        # Param overridden by variant
        assert resolved["params"]["tolerance"] == "1%"

    def test_inheritance_chain_three_levels(self, resistor_entity: Entity):
        grandparent = ComposedPart(
            id="gp",
            entity=resistor_entity,
            category="resistor",
            manufacturer="Yageo",
            tags=["passive"],
        )
        parent = ComposedPart(
            id="p",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            base_part=grandparent,
            tags=["smd"],
        )
        child = ComposedPart(
            id="c",
            entity=resistor_entity,
            package="0402",
            category="resistor",
            mpn="CHILD-MPN",
            base_part=parent,
            params={"resistance": "10000"},
        )

        resolved = child.resolve()
        assert resolved["manufacturer"] == "Yageo"
        assert resolved["mpn"] == "CHILD-MPN"
        assert set(resolved["tags"]) == {"passive", "smd"}
        assert resolved["params"]["resistance"] == "10000"

    def test_circular_inheritance_detected(self, resistor_entity: Entity):
        a = ComposedPart(id="a", entity=resistor_entity)
        b = ComposedPart(id="b", entity=resistor_entity, base_part=a)
        # Create cycle: a -> b -> a
        a.base_part = b

        with pytest.raises(ValueError, match="Circular inheritance"):
            a.resolve()

    def test_pad_to_pin_mapping(self, resistor_entity: Entity):
        part = ComposedPart(
            id="r-mapped",
            entity=resistor_entity,
            package="0402",
            pad_to_pin={"1": "1", "2": "2"},
        )
        assert part.pad_to_pin["1"] == "1"


# ---------------------------------------------------------------------------
# Serialisation round-trip tests
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_unit_round_trip(self):
        from kicad_tools.parts.composition import _unit_from_dict, _unit_to_dict

        unit = Unit(
            id="test",
            name="Test Unit",
            pins=[
                UnitPin(
                    name="VCC",
                    number="1",
                    direction=PinDirection.POWER_IN,
                    alternate_names=["V+"],
                ),
                UnitPin(name="GND", number="2", direction=PinDirection.POWER_IN),
            ],
        )
        d = _unit_to_dict(unit)
        restored = _unit_from_dict(d)
        assert restored.id == unit.id
        assert restored.name == unit.name
        assert len(restored.pins) == 2
        assert restored.pins[0].alternate_names == ["V+"]
        assert restored.pins[0].direction == PinDirection.POWER_IN

    def test_entity_round_trip(self):
        from kicad_tools.parts.composition import _entity_from_dict, _entity_to_dict

        entity = Entity(
            id="ent",
            name="Entity",
            units=[
                Unit(id="u1", name="U1", pins=[UnitPin(name="A", number="1")]),
                Unit(id="u2", name="U2", pins=[]),
            ],
        )
        d = _entity_to_dict(entity)
        restored = _entity_from_dict(d)
        assert restored.id == "ent"
        assert len(restored.units) == 2
        assert restored.units[0].pins[0].name == "A"
