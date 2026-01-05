"""
Component address registry for layout preservation.

Provides hierarchical address-based component matching to enable
stable component identification across schematic regenerations.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from kicad_tools.schema.hierarchy import HierarchyBuilder, HierarchyNode
from kicad_tools.sexp import parse_file

from .types import ComponentAddress


class AddressRegistry:
    """
    Registry for hierarchical component addresses.

    Builds a mapping between hierarchical addresses and component UUIDs
    from a schematic hierarchy.

    Example:
        >>> registry = AddressRegistry("/path/to/board.kicad_sch")
        >>> addr = registry.get_address("abc-123-uuid")
        >>> print(addr)  # "power.ldo.C1"
        >>> comp = registry.resolve("power.ldo.C1")
        >>> print(comp.uuid)  # "abc-123-uuid"
    """

    def __init__(self, schematic_path: str | Path):
        """
        Initialize registry from a schematic file.

        Args:
            schematic_path: Path to the root .kicad_sch file
        """
        self._schematic_path = Path(schematic_path)
        self._addresses: dict[str, ComponentAddress] = {}
        self._uuid_to_address: dict[str, str] = {}
        self._build_from_schematic()

    def _build_from_schematic(self) -> None:
        """Build address registry from schematic hierarchy."""
        if not self._schematic_path.exists():
            return

        # Build the hierarchy tree
        builder = HierarchyBuilder(str(self._schematic_path.parent))
        root = builder.build(str(self._schematic_path))

        # Visit each node and extract symbols
        self._visit_node(root, "")

    def _visit_node(self, node: HierarchyNode, path: str) -> None:
        """
        Visit a hierarchy node and extract component addresses.

        Args:
            node: The hierarchy node to process
            path: Current hierarchical path (e.g., "power.ldo")
        """
        # Load the schematic file to get symbols
        schematic_path = Path(node.path)
        if not schematic_path.exists():
            return

        try:
            doc = parse_file(schematic_path)
        except Exception:
            return

        # Find all symbol instances (those with lib_id, excluding power symbols)
        for child in doc.children:
            if child.name != "symbol":
                continue

            lib_id_node = child.get("lib_id")
            if not lib_id_node:
                continue

            lib_id = str(lib_id_node.get_first_atom() or "")

            # Skip power symbols (they start with power: and have #PWR reference)
            if lib_id.startswith("power:"):
                continue

            # Get UUID
            uuid_node = child.get("uuid")
            if not uuid_node:
                continue
            uuid_str = str(uuid_node.get_first_atom() or "")

            # Get reference from properties
            reference = ""
            for prop_node in child.find_all("property"):
                atoms = prop_node.get_atoms()
                if len(atoms) >= 2 and str(atoms[0]) == "Reference":
                    reference = str(atoms[1])
                    break

            if not reference or reference.startswith("#"):
                # Skip power references like #PWR01
                continue

            # Create component address
            address = ComponentAddress.from_parts(
                sheet_path=path,
                local_ref=reference,
                uuid=uuid_str,
            )

            self._addresses[address.full_path] = address
            self._uuid_to_address[uuid_str] = address.full_path

        # Recursively visit children
        for sheet in node.sheets:
            # Build child path
            child_path = f"{path}.{sheet.name}" if path else sheet.name

            # Find the child node that corresponds to this sheet
            for child_node in node.children:
                if child_node.name == sheet.name:
                    self._visit_node(child_node, child_path)
                    break

    def resolve(self, address: str) -> ComponentAddress | None:
        """
        Resolve a hierarchical address to a ComponentAddress.

        Args:
            address: Hierarchical address (e.g., "power.ldo.C1")

        Returns:
            ComponentAddress if found, None otherwise
        """
        return self._addresses.get(address)

    def get_address(self, uuid: str) -> str | None:
        """
        Get the hierarchical address for a component UUID.

        Args:
            uuid: KiCad component UUID

        Returns:
            Hierarchical address string if found, None otherwise
        """
        return self._uuid_to_address.get(uuid)

    def get_component(self, uuid: str) -> ComponentAddress | None:
        """
        Get ComponentAddress by UUID.

        Args:
            uuid: KiCad component UUID

        Returns:
            ComponentAddress if found, None otherwise
        """
        address = self.get_address(uuid)
        if address:
            return self._addresses.get(address)
        return None

    def match_by_pattern(self, pattern: str) -> list[ComponentAddress]:
        """
        Match components by glob pattern.

        Supports standard glob patterns:
        - `*` matches any characters within a path segment
        - `**` matches any characters across path segments
        - `?` matches any single character

        Args:
            pattern: Glob pattern (e.g., "power.*.C*", "**.C1")

        Returns:
            List of matching ComponentAddresses

        Examples:
            >>> registry.match_by_pattern("power.*.C*")
            [ComponentAddress("power.ldo.C1"), ComponentAddress("power.buck.C2")]
            >>> registry.match_by_pattern("*.R1")
            [ComponentAddress("power.R1"), ComponentAddress("logic.R1")]
        """
        # Handle ** pattern for matching across segments
        if "**" in pattern:
            # Convert ** to match anything including dots
            regex_pattern = pattern.replace(".", r"\.").replace("**", ".*")
            import re

            compiled = re.compile(f"^{regex_pattern}$")
            return [addr for addr in self._addresses.values() if compiled.match(addr.full_path)]

        # Use fnmatch for standard glob patterns
        return [
            addr for addr in self._addresses.values() if fnmatch.fnmatch(addr.full_path, pattern)
        ]

    def all_addresses(self) -> list[ComponentAddress]:
        """
        Get all registered component addresses.

        Returns:
            List of all ComponentAddresses in the registry
        """
        return list(self._addresses.values())

    def components_in_sheet(self, sheet_path: str) -> list[ComponentAddress]:
        """
        Get all components in a specific sheet.

        Args:
            sheet_path: Sheet path (e.g., "power.ldo") or empty for root

        Returns:
            List of ComponentAddresses in that sheet
        """
        return [addr for addr in self._addresses.values() if addr.sheet_path == sheet_path]

    def __len__(self) -> int:
        """Return number of registered addresses."""
        return len(self._addresses)

    def __contains__(self, address: str) -> bool:
        """Check if an address exists in the registry."""
        return address in self._addresses

    def __iter__(self):
        """Iterate over all addresses."""
        return iter(self._addresses.values())
