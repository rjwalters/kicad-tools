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


# Numerical guard band for DRC clearance comparisons (in mm).
#
# This exists ONLY to suppress IEEE-754 float64 rounding artifacts in the
# geometric distance computation -- NOT to model manufacturing precision.
# Board coordinates live in the ~0-300 mm range; the relative epsilon of
# float64 there is ~1e-14 mm, and a few chained subtract/hypot operations
# accumulate to at most ~1e-9 mm. A guard band of 1e-4 mm (0.1 um) is
# ~5 orders of magnitude above that noise floor while still being far
# below any real manufacturing feature, and it matches
# ``_COLOCATION_EPSILON_MM`` used for the co-location check in
# ``clearance.py``.
#
# The previous value of 0.005 mm (5 um) created a dead band that silently
# passed genuine marginal violations: the entire marginal class this guard
# was masking is on the order of 1.6 um (e.g. actual 0.1000 mm vs a
# 0.1016 mm / 4 mil floor). KiCad's own DRC works at IU granularity
# (1 nm), so a 5 um dead band was ~10^8x wider than float noise requires
# and hid real, fabricable-clearance-violating copper. See issue #3913.
DRC_TOLERANCE: float = 1e-4


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
