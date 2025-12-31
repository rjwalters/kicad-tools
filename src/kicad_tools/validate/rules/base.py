"""Base class for DRC rules.

This module defines the abstract base class that all DRC rule implementations
must inherit from. It provides a consistent interface for rule checking.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ..violations import DRCResults

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB


class DRCRule(ABC):
    """Abstract base class for DRC rule implementations.

    Subclasses must implement the `check` method to perform
    the actual rule validation.

    Attributes:
        rule_id: Unique identifier for this rule category
        name: Human-readable name for the rule
        description: Detailed description of what the rule checks
    """

    rule_id: str = ""
    name: str = ""
    description: str = ""

    @abstractmethod
    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check the PCB against this rule.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing any violations found
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(rule_id={self.rule_id!r})"
