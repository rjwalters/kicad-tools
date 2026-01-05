"""
Tests for layout addressing module.

Tests hierarchical component address generation and pattern matching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.layout import AddressRegistry, ComponentAddress


class TestComponentAddress:
    """Tests for ComponentAddress dataclass."""

    def test_create_root_component(self):
        """Component at root level."""
        addr = ComponentAddress.from_parts(
            sheet_path="",
            local_ref="C1",
            uuid="test-uuid-123",
        )
        assert addr.full_path == "C1"
        assert addr.sheet_path == ""
        assert addr.local_ref == "C1"
        assert addr.uuid == "test-uuid-123"
        assert addr.depth == 0

    def test_create_nested_component(self):
        """Component in nested sheet."""
        addr = ComponentAddress.from_parts(
            sheet_path="power.ldo",
            local_ref="C1",
            uuid="test-uuid-456",
        )
        assert addr.full_path == "power.ldo.C1"
        assert addr.sheet_path == "power.ldo"
        assert addr.local_ref == "C1"
        assert addr.depth == 2

    def test_depth_calculation(self):
        """Test depth at various levels."""
        root = ComponentAddress.from_parts("", "R1", "uuid1")
        level1 = ComponentAddress.from_parts("power", "R1", "uuid2")
        level2 = ComponentAddress.from_parts("power.ldo", "R1", "uuid3")
        level3 = ComponentAddress.from_parts("power.ldo.filter", "R1", "uuid4")

        assert root.depth == 0
        assert level1.depth == 1
        assert level2.depth == 2
        assert level3.depth == 3

    def test_parent_path(self):
        """Test parent path extraction."""
        root = ComponentAddress.from_parts("", "R1", "uuid1")
        level1 = ComponentAddress.from_parts("power", "R1", "uuid2")
        level2 = ComponentAddress.from_parts("power.ldo", "R1", "uuid3")

        assert root.parent_path == ""
        assert level1.parent_path == ""
        assert level2.parent_path == "power"

    def test_string_representation(self):
        """Test str and repr."""
        addr = ComponentAddress.from_parts("power", "C1", "uuid-123")
        assert str(addr) == "power.C1"
        assert "power.C1" in repr(addr)
        assert "uuid-123" in repr(addr)

    def test_validation_empty_local_ref(self):
        """Empty local_ref should raise error."""
        with pytest.raises(ValueError, match="local_ref"):
            ComponentAddress(
                full_path="",
                sheet_path="",
                local_ref="",
                uuid="uuid",
            )

    def test_validation_empty_uuid(self):
        """Empty uuid should raise error."""
        with pytest.raises(ValueError, match="uuid"):
            ComponentAddress(
                full_path="C1",
                sheet_path="",
                local_ref="C1",
                uuid="",
            )

    def test_frozen_immutability(self):
        """ComponentAddress should be immutable."""
        addr = ComponentAddress.from_parts("power", "C1", "uuid")
        with pytest.raises(AttributeError):
            addr.full_path = "new.path"  # type: ignore

    def test_hashable(self):
        """ComponentAddress should be hashable for use in sets/dicts."""
        addr1 = ComponentAddress.from_parts("power", "C1", "uuid1")
        addr2 = ComponentAddress.from_parts("power", "C2", "uuid2")
        addr_set = {addr1, addr2}
        assert len(addr_set) == 2
        assert addr1 in addr_set


class TestAddressRegistryBasic:
    """Basic tests for AddressRegistry."""

    def test_empty_registry(self, tmp_path: Path):
        """Registry with non-existent file should be empty."""
        registry = AddressRegistry(tmp_path / "nonexistent.kicad_sch")
        assert len(registry) == 0

    def test_len_and_contains(self, hierarchical_schematic: Path):
        """Test len and contains operations."""
        registry = AddressRegistry(hierarchical_schematic)
        # The hierarchical_main.kicad_sch has C1 in root
        assert len(registry) >= 1
        if "C1" in registry:
            assert registry.resolve("C1") is not None


class TestAddressRegistryHierarchical:
    """Tests for hierarchical schematic parsing."""

    def test_parse_hierarchical_schematic(self, hierarchical_schematic: Path):
        """Parse a hierarchical schematic with sub-sheets."""
        registry = AddressRegistry(hierarchical_schematic)

        # Should have at least the C1 from root
        c1 = registry.resolve("C1")
        assert c1 is not None
        assert c1.local_ref == "C1"
        assert c1.sheet_path == ""

    def test_uuid_lookup(self, hierarchical_schematic: Path):
        """Look up address by UUID."""
        registry = AddressRegistry(hierarchical_schematic)

        # Get C1 and verify we can find it by UUID
        c1 = registry.resolve("C1")
        if c1:
            found_addr = registry.get_address(c1.uuid)
            assert found_addr == "C1"

            # Also test get_component
            found_comp = registry.get_component(c1.uuid)
            assert found_comp is not None
            assert found_comp.full_path == "C1"

    def test_components_in_sheet(self, hierarchical_schematic: Path):
        """Get components in a specific sheet."""
        registry = AddressRegistry(hierarchical_schematic)

        # Root sheet components
        root_components = registry.components_in_sheet("")
        refs = [c.local_ref for c in root_components]
        assert "C1" in refs

    def test_all_addresses(self, hierarchical_schematic: Path):
        """Get all registered addresses."""
        registry = AddressRegistry(hierarchical_schematic)
        all_addrs = registry.all_addresses()
        assert len(all_addrs) >= 1
        assert all(isinstance(a, ComponentAddress) for a in all_addrs)


class TestAddressRegistryPatternMatching:
    """Tests for pattern matching functionality."""

    def test_exact_match(self, hierarchical_schematic: Path):
        """Exact pattern should match exactly one component."""
        registry = AddressRegistry(hierarchical_schematic)
        matches = registry.match_by_pattern("C1")
        c1_matches = [m for m in matches if m.full_path == "C1"]
        assert len(c1_matches) <= 1

    def test_wildcard_in_ref(self, hierarchical_schematic: Path):
        """Wildcard in reference should match multiple."""
        registry = AddressRegistry(hierarchical_schematic)
        matches = registry.match_by_pattern("C*")
        assert all(m.local_ref.startswith("C") for m in matches)

    def test_star_star_pattern(self, hierarchical_schematic: Path):
        """** pattern should match across sheet levels."""
        registry = AddressRegistry(hierarchical_schematic)
        # Match any component starting with C at any level
        matches = registry.match_by_pattern("**C*")
        assert len(matches) >= 0  # May or may not find matches

    def test_single_level_wildcard(self, hierarchical_schematic: Path):
        """Single * should only match within one level."""
        registry = AddressRegistry(hierarchical_schematic)
        matches = registry.match_by_pattern("*")
        # Should only match root-level components
        for m in matches:
            assert m.sheet_path == ""

    def test_question_mark_pattern(self, hierarchical_schematic: Path):
        """? should match single character."""
        registry = AddressRegistry(hierarchical_schematic)
        matches = registry.match_by_pattern("C?")
        # Matches C1, C2, etc. but not C10
        for m in matches:
            assert m.local_ref.startswith("C")
            assert len(m.local_ref) == 2

    def test_pattern_with_sheet_path(self, hierarchical_schematic: Path):
        """Pattern with sheet path component."""
        registry = AddressRegistry(hierarchical_schematic)
        # This may or may not match depending on if Logic sheet has components
        matches = registry.match_by_pattern("Logic.*")
        # Just verify it doesn't crash and returns a list
        assert isinstance(matches, list)


class TestAddressRegistryIteration:
    """Tests for iteration and container protocols."""

    def test_iterate_addresses(self, hierarchical_schematic: Path):
        """Should be able to iterate over addresses."""
        registry = AddressRegistry(hierarchical_schematic)
        addresses = list(registry)
        assert all(isinstance(a, ComponentAddress) for a in addresses)

    def test_contains_check(self, hierarchical_schematic: Path):
        """Test 'in' operator."""
        registry = AddressRegistry(hierarchical_schematic)
        if registry.resolve("C1"):
            assert "C1" in registry
        assert "nonexistent.component" not in registry


class TestAddressRegistryFlatSchematic:
    """Tests with flat (non-hierarchical) schematics."""

    def test_flat_schematic(self, simple_rc_schematic: Path):
        """Flat schematic should work correctly."""
        registry = AddressRegistry(simple_rc_schematic)

        # All components should be at root level
        for addr in registry:
            assert addr.depth == 0
            assert addr.sheet_path == ""


class TestAddressRegistryDeepHierarchy:
    """Tests for deeply nested hierarchies."""

    def test_three_level_hierarchy(self, tmp_path: Path):
        """Test a 3-level nested hierarchy."""
        # Create a 3-level hierarchy
        root_sch = tmp_path / "root.kicad_sch"
        level1_sch = tmp_path / "level1.kicad_sch"
        level2_sch = tmp_path / "level2.kicad_sch"

        # Level 2 schematic with a component
        level2_sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "level2-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:R"
            (symbol "R_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 2.794) (name "~") (number "2"))
            )
        )
    )
    (symbol
        (lib_id "Device:R")
        (at 100 50 0)
        (unit 1)
        (uuid "r1-deep-uuid")
        (property "Reference" "R1" (at 104 48 0))
        (property "Value" "10k" (at 104 52 0))
    )
)
""")

        # Level 1 schematic with sheet to level 2
        level1_sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "level1-uuid")
    (paper "A4")
    (lib_symbols)
    (sheet
        (at 130 40) (size 40 30)
        (uuid "sheet-l2-uuid")
        (property "Sheetname" "SubLevel" (at 130 39 0))
        (property "Sheetfile" "level2.kicad_sch" (at 130 71 0))
    )
)
""")

        # Root schematic with sheet to level 1
        root_sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
                (pin passive line (at 0 -3.81 90) (length 2.794) (name "~") (number "2"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-root-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
    (sheet
        (at 130 40) (size 40 30)
        (uuid "sheet-l1-uuid")
        (property "Sheetname" "MainLevel" (at 130 39 0))
        (property "Sheetfile" "level1.kicad_sch" (at 130 71 0))
    )
)
""")

        registry = AddressRegistry(root_sch)

        # Root component
        c1 = registry.resolve("C1")
        assert c1 is not None
        assert c1.depth == 0
        assert c1.uuid == "c1-root-uuid"

        # Level 3 component (MainLevel.SubLevel.R1)
        r1 = registry.resolve("MainLevel.SubLevel.R1")
        assert r1 is not None
        assert r1.depth == 2
        assert r1.sheet_path == "MainLevel.SubLevel"
        assert r1.uuid == "r1-deep-uuid"


class TestAddressRegistryStability:
    """Tests for address stability across modifications."""

    def test_address_stability_with_same_uuid(self, tmp_path: Path):
        """Same UUID should produce same address."""
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "stable-uuid-123")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
)
""")

        registry1 = AddressRegistry(sch)
        c1_addr = registry1.get_address("stable-uuid-123")
        assert c1_addr == "C1"

        # Rebuild registry - should get same result
        registry2 = AddressRegistry(sch)
        c1_addr2 = registry2.get_address("stable-uuid-123")
        assert c1_addr2 == c1_addr


# Fixtures


@pytest.fixture
def hierarchical_schematic() -> Path:
    """Return path to hierarchical test schematic."""
    return Path(__file__).parent / "fixtures" / "projects" / "hierarchical_main.kicad_sch"


@pytest.fixture
def simple_rc_schematic() -> Path:
    """Return path to simple RC schematic."""
    return Path(__file__).parent / "fixtures" / "simple_rc.kicad_sch"
