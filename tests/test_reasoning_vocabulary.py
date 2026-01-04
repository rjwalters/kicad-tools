"""Tests for reasoning/vocabulary.py module to increase coverage.

Tests for:
- SpatialRelation enum and from_positions
- SpatialRegion dataclass
- NetType classification
- RoutingPriority
- ComponentGroup
- Helper functions
"""

from kicad_tools.reasoning.vocabulary import (
    ComponentGroup,
    NetType,
    RoutingPriority,
    SpatialRegion,
    SpatialRelation,
    create_hat_regions,
    describe_distance,
    describe_net_type,
    describe_position,
)


class TestSpatialRelation:
    """Tests for SpatialRelation enum."""

    def test_enum_values(self):
        """Enum values are correct."""
        assert SpatialRelation.NORTH_OF.value == "north_of"
        assert SpatialRelation.NEAR.value == "near"
        assert SpatialRelation.ADJACENT.value == "adjacent"

    def test_from_positions_adjacent(self):
        """Adjacent objects (within 2mm)."""
        relations = SpatialRelation.from_positions(10, 10, 11, 10)
        assert SpatialRelation.ADJACENT in relations

    def test_from_positions_near(self):
        """Near objects (within 5mm)."""
        relations = SpatialRelation.from_positions(10, 10, 14, 10)
        assert SpatialRelation.NEAR in relations

    def test_from_positions_far(self):
        """Far objects (over 20mm)."""
        relations = SpatialRelation.from_positions(10, 10, 50, 10)
        assert SpatialRelation.FAR in relations

    def test_from_positions_north_of(self):
        """Object 2 is north of object 1."""
        # In KiCad, Y increases downward, so "north" means smaller Y
        relations = SpatialRelation.from_positions(50, 50, 50, 40)
        assert SpatialRelation.NORTH_OF in relations

    def test_from_positions_south_of(self):
        """Object 2 is south of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 50, 60)
        assert SpatialRelation.SOUTH_OF in relations

    def test_from_positions_east_of(self):
        """Object 2 is east of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 60, 50)
        assert SpatialRelation.EAST_OF in relations

    def test_from_positions_west_of(self):
        """Object 2 is west of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 40, 50)
        assert SpatialRelation.WEST_OF in relations

    def test_from_positions_northeast_of(self):
        """Object 2 is northeast of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 60, 40)
        assert SpatialRelation.NORTHEAST_OF in relations

    def test_from_positions_northwest_of(self):
        """Object 2 is northwest of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 40, 40)
        assert SpatialRelation.NORTHWEST_OF in relations

    def test_from_positions_southeast_of(self):
        """Object 2 is southeast of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 60, 60)
        assert SpatialRelation.SOUTHEAST_OF in relations

    def test_from_positions_southwest_of(self):
        """Object 2 is southwest of object 1."""
        relations = SpatialRelation.from_positions(50, 50, 40, 60)
        assert SpatialRelation.SOUTHWEST_OF in relations

    def test_from_positions_aligned_horizontal(self):
        """Horizontally aligned objects."""
        relations = SpatialRelation.from_positions(10, 50, 60, 50)
        assert SpatialRelation.ALIGNED_HORIZONTAL in relations

    def test_from_positions_aligned_vertical(self):
        """Vertically aligned objects."""
        relations = SpatialRelation.from_positions(50, 10, 50, 60)
        assert SpatialRelation.ALIGNED_VERTICAL in relations


class TestSpatialRegion:
    """Tests for SpatialRegion dataclass."""

    def test_region_creation(self):
        """Create region with all attributes."""
        region = SpatialRegion(
            name="power_section",
            description="Power supply components area",
            bounds=(10, 10, 50, 30),
            is_keepout=False,
            is_routing_channel=False,
            priority=1,
        )
        assert region.name == "power_section"
        assert region.bounds == (10, 10, 50, 30)

    def test_region_center(self):
        """Region center property."""
        region = SpatialRegion(name="test", description="test", bounds=(10, 10, 50, 30))
        assert region.center == (30, 20)

    def test_region_width(self):
        """Region width property."""
        region = SpatialRegion(name="test", description="test", bounds=(10, 10, 50, 30))
        assert region.width == 40

    def test_region_height(self):
        """Region height property."""
        region = SpatialRegion(name="test", description="test", bounds=(10, 10, 50, 30))
        assert region.height == 20

    def test_region_contains(self):
        """Region contains point."""
        region = SpatialRegion(name="test", description="test", bounds=(10, 10, 50, 30))
        assert region.contains(25, 20) is True
        assert region.contains(5, 20) is False
        assert region.contains(25, 5) is False

    def test_region_contains_edge(self):
        """Region contains point on edge."""
        region = SpatialRegion(name="test", description="test", bounds=(10, 10, 50, 30))
        assert region.contains(10, 10) is True  # Corner
        assert region.contains(50, 30) is True  # Opposite corner
        assert region.contains(10, 20) is True  # Edge

    def test_region_overlaps(self):
        """Region overlaps detection."""
        region1 = SpatialRegion(name="r1", description="", bounds=(10, 10, 50, 30))
        region2 = SpatialRegion(name="r2", description="", bounds=(40, 20, 80, 50))
        region3 = SpatialRegion(name="r3", description="", bounds=(60, 40, 100, 70))

        assert region1.overlaps(region2) is True
        assert region1.overlaps(region3) is False
        assert region2.overlaps(region3) is True

    def test_region_keepout(self):
        """Region keepout flag."""
        keepout = SpatialRegion(name="test", description="", bounds=(0, 0, 10, 10), is_keepout=True)
        assert keepout.is_keepout is True

    def test_region_routing_channel(self):
        """Region routing channel flag."""
        channel = SpatialRegion(
            name="test", description="", bounds=(0, 0, 10, 10), is_routing_channel=True
        )
        assert channel.is_routing_channel is True


class TestNetType:
    """Tests for NetType enum."""

    def test_enum_values(self):
        """Enum values are correct."""
        assert NetType.GROUND.value == "ground"
        assert NetType.POWER.value == "power"
        assert NetType.CLOCK.value == "clock"

    def test_classify_ground(self):
        """Classify ground nets."""
        assert NetType.classify("GND") == NetType.GROUND
        assert NetType.classify("AGND") == NetType.GROUND
        assert NetType.classify("DGND") == NetType.GROUND
        assert NetType.classify("VSS") == NetType.GROUND

    def test_classify_power(self):
        """Classify power nets."""
        assert NetType.classify("VCC") == NetType.POWER
        assert NetType.classify("VDD") == NetType.POWER
        assert NetType.classify("+3.3V") == NetType.POWER
        assert NetType.classify("+5V") == NetType.POWER
        assert NetType.classify("+12V") == NetType.POWER

    def test_classify_clock(self):
        """Classify clock nets."""
        assert NetType.classify("CLK") == NetType.CLOCK
        assert NetType.classify("MCLK") == NetType.CLOCK
        assert NetType.classify("BCLK") == NetType.CLOCK
        assert NetType.classify("LRCLK") == NetType.CLOCK
        assert NetType.classify("XTAL_IN") == NetType.CLOCK

    def test_classify_i2c(self):
        """Classify I2C nets."""
        assert NetType.classify("SDA") == NetType.I2C
        assert NetType.classify("SCL") == NetType.I2C
        assert NetType.classify("I2C_SDA") == NetType.I2C

    def test_classify_spi(self):
        """Classify SPI nets."""
        assert NetType.classify("MOSI") == NetType.SPI
        assert NetType.classify("MISO") == NetType.SPI
        assert NetType.classify("SCK") == NetType.SPI
        assert NetType.classify("CS") == NetType.SPI

    def test_classify_analog(self):
        """Classify analog nets."""
        assert NetType.classify("AIN1") == NetType.ANALOG
        assert NetType.classify("AOUT") == NetType.ANALOG
        assert NetType.classify("ANALOG_IN") == NetType.ANALOG
        assert NetType.classify("VREF") == NetType.ANALOG

    def test_classify_gpio(self):
        """Classify GPIO nets."""
        assert NetType.classify("GPIO5") == NetType.GPIO
        assert NetType.classify("GPIO_PIN") == NetType.GPIO

    def test_classify_signal(self):
        """Classify generic signals."""
        assert NetType.classify("DATA") == NetType.SIGNAL
        assert NetType.classify("NET1") == NetType.SIGNAL
        assert NetType.classify("SOME_NET") == NetType.SIGNAL


class TestRoutingPriority:
    """Tests for RoutingPriority dataclass."""

    def test_priority_creation(self):
        """Create routing priority with all attributes."""
        priority = RoutingPriority(
            priority=1,
            trace_width=0.3,
            clearance=0.2,
            preferred_layers=["F.Cu"],
            avoid_regions=["analog"],
            via_preference="minimize",
        )
        assert priority.priority == 1
        assert priority.trace_width == 0.3
        assert priority.clearance == 0.2

    def test_for_net_type_ground(self):
        """Priority for ground nets."""
        priority = RoutingPriority.for_net_type(NetType.GROUND)
        assert priority.priority == 1
        assert priority.trace_width == 0.3
        assert priority.via_preference == "allow"

    def test_for_net_type_power(self):
        """Priority for power nets."""
        priority = RoutingPriority.for_net_type(NetType.POWER)
        assert priority.priority == 2
        assert priority.trace_width == 0.4

    def test_for_net_type_clock(self):
        """Priority for clock nets."""
        priority = RoutingPriority.for_net_type(NetType.CLOCK)
        assert priority.priority == 3
        assert priority.clearance == 0.3
        assert "analog" in priority.avoid_regions
        assert priority.via_preference == "minimize"

    def test_for_net_type_analog(self):
        """Priority for analog nets."""
        priority = RoutingPriority.for_net_type(NetType.ANALOG)
        assert priority.priority == 4
        assert "digital" in priority.avoid_regions
        assert "power" in priority.avoid_regions

    def test_for_net_type_spi(self):
        """Priority for SPI nets."""
        priority = RoutingPriority.for_net_type(NetType.SPI)
        assert priority.priority == 5

    def test_for_net_type_i2c(self):
        """Priority for I2C nets."""
        priority = RoutingPriority.for_net_type(NetType.I2C)
        assert priority.priority == 6

    def test_for_net_type_signal(self):
        """Priority for generic signals."""
        priority = RoutingPriority.for_net_type(NetType.SIGNAL)
        assert priority.priority == 10


class TestComponentGroup:
    """Tests for ComponentGroup dataclass."""

    def test_group_creation(self):
        """Create component group."""
        group = ComponentGroup(
            name="power_supply",
            description="3.3V power supply components",
            components=["U1", "C1", "C2", "L1"],
            function="power",
            preferred_region="power_section",
        )
        assert group.name == "power_supply"
        assert len(group.components) == 4
        assert group.function == "power"

    def test_group_contains(self):
        """Check component membership."""
        group = ComponentGroup(
            name="power_supply",
            description="",
            components=["U1", "C1", "C2"],
            function="power",
        )
        assert "U1" in group
        assert "C1" in group
        assert "R1" not in group


class TestCreateHatRegions:
    """Tests for create_hat_regions function."""

    def test_creates_regions(self):
        """Creates standard HAT regions."""
        regions = create_hat_regions()
        assert len(regions) > 0

    def test_has_gpio_header(self):
        """Has GPIO header region."""
        regions = create_hat_regions()
        names = [r.name for r in regions]
        assert "gpio_header" in names

    def test_has_mounting_holes(self):
        """Has mounting hole keepouts."""
        regions = create_hat_regions()
        names = [r.name for r in regions]
        assert "mounting_tl" in names
        assert "mounting_tr" in names
        assert "mounting_bl" in names
        assert "mounting_br" in names

    def test_has_routing_channels(self):
        """Has routing channels."""
        regions = create_hat_regions()
        channels = [r for r in regions if r.is_routing_channel]
        assert len(channels) >= 4

    def test_mounting_holes_are_keepouts(self):
        """Mounting holes are marked as keepouts."""
        regions = create_hat_regions()
        mounting = [r for r in regions if r.name.startswith("mounting_")]
        for m in mounting:
            assert m.is_keepout is True

    def test_custom_dimensions(self):
        """Custom board dimensions."""
        regions = create_hat_regions(width=100, height=80)
        center = [r for r in regions if r.name == "center"][0]
        assert center.bounds[2] == 90  # width - 10


class TestDescribePosition:
    """Tests for describe_position function."""

    def test_west_edge(self):
        """Position at west edge."""
        desc = describe_position(5, 40, 100, 80)
        assert "west edge" in desc

    def test_east_edge(self):
        """Position at east edge."""
        desc = describe_position(95, 40, 100, 80)
        assert "east edge" in desc

    def test_north_edge(self):
        """Position at north edge."""
        desc = describe_position(50, 5, 100, 80)
        assert "north edge" in desc

    def test_south_edge(self):
        """Position at south edge."""
        desc = describe_position(50, 75, 100, 80)
        assert "south edge" in desc

    def test_center(self):
        """Position in center."""
        desc = describe_position(50, 40, 100, 80)
        assert "middle" in desc
        assert "central" in desc

    def test_northwest(self):
        """Position in northwest."""
        desc = describe_position(20, 20, 100, 80)
        assert "northern" in desc
        assert "western" in desc


class TestDescribeDistance:
    """Tests for describe_distance function."""

    def test_very_close(self):
        """Distance < 1mm is very close."""
        assert describe_distance(0.5) == "very close"

    def test_adjacent(self):
        """Distance 1-3mm is adjacent."""
        assert describe_distance(2) == "adjacent"

    def test_nearby(self):
        """Distance 3-10mm is nearby."""
        assert describe_distance(5) == "nearby"

    def test_moderately_far(self):
        """Distance 10-20mm is moderately far."""
        assert describe_distance(15) == "moderately far"

    def test_far(self):
        """Distance > 20mm is far."""
        assert describe_distance(25) == "far"


class TestDescribeNetType:
    """Tests for describe_net_type function."""

    def test_ground_guidance(self):
        """Ground net guidance."""
        desc = describe_net_type(NetType.GROUND)
        assert "Ground" in desc
        assert "plane" in desc.lower() or "star" in desc.lower()

    def test_power_guidance(self):
        """Power net guidance."""
        desc = describe_net_type(NetType.POWER)
        assert "Power" in desc
        assert "wider" in desc.lower()

    def test_clock_guidance(self):
        """Clock net guidance."""
        desc = describe_net_type(NetType.CLOCK)
        assert "Clock" in desc
        assert "analog" in desc.lower()

    def test_analog_guidance(self):
        """Analog net guidance."""
        desc = describe_net_type(NetType.ANALOG)
        assert "Analog" in desc
        assert "noise" in desc.lower()

    def test_i2c_guidance(self):
        """I2C net guidance."""
        desc = describe_net_type(NetType.I2C)
        assert "I2C" in desc

    def test_spi_guidance(self):
        """SPI net guidance."""
        desc = describe_net_type(NetType.SPI)
        assert "SPI" in desc
        assert "clock" in desc.lower()

    def test_signal_guidance(self):
        """Generic signal guidance."""
        desc = describe_net_type(NetType.SIGNAL)
        assert "signal" in desc.lower() or "routing" in desc.lower()
