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
# Set to the largest shortfall pinned KiCad 10.0.6 accepts on a
# track-to-track probe -- 500 nm accepted, 600 nm flagged (Issue #9619) --
# so a finding at kct's verdict boundary is one kicad-cli DRC agrees on.
# The band still sits far above the IEEE-754 noise floor it was originally
# introduced for (#3913): float64 coordinates in the ~0-300 mm board range
# accumulate at most ~1e-9 mm of rounding through a few chained
# subtract/hypot operations, five orders of magnitude below 5e-4.
#
# History: the pre-#3913 value of 0.005 mm (5 um) created a dead band that
# silently passed genuine marginal violations: the entire marginal class
# this guard was masking is on the order of 1.6 um (e.g. actual 0.1000 mm
# vs a 0.1016 mm / 4 mil floor).  #3913 tightened it to 1e-4 mm (0.1 um);
# #9619 widened it to 5e-4 mm to match KiCad's probed acceptance band.  It
# remains below the ~1.6 um marginal class, so those still fail.  Note the
# band is wider than a purely-rounding guard needs to be: a shortfall
# between 100 nm and 500 nm now passes where #3913 reported it -- that is
# the KiCad-parity trade-off this issue accepts.
#
# The single shared value lives in ``router.clearance_kernel`` (the Python
# kernel constant, mirrored by the C++ kernel); the router commits gates and
# the validate rules share this number.  It is duplicated as a literal here
# rather than imported because importing anything under ``kicad_tools.router``
# from the validate package would break the lazy-import contract
# (``tests/test_validate_lazy_imports.py`` forbids ``kicad_tools.validate.*``
# from loading the router); keep the two in sync.
DRC_TOLERANCE: float = 5e-4


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
