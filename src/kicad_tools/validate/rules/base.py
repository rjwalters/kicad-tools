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
# Issue #9619: set to the largest shortfall pinned KiCad 10.0.6 accepts --
# 500 nm on the black-box probe (tests/test_clearance_slack_kicad_band_9619.py
# re-runs it against the pinned image), so a finding at kct's verdict
# boundary is one kicad-cli DRC agrees on.  Probe detail: parallel 0.2 mm
# tracks under a 0.127 mm rule -- a 500 nm shortfall is accepted, 501 nm is
# flagged; a 45-degree pair and a pad-to-track pair flip at the same edge
# within the 1 nm constructibility of diagonal coordinates.
#
# Deliberately wider than the router's slack (the router package's own
# ``CLEARANCE_EPSILON_MM``, still 1e-4): the checker matches KiCad
# verdict-for-verdict, while copper the router *writes* keeps a margin
# inside KiCad's tolerance so rounding on save cannot tip it into a
# kicad-cli violation (#9619 ruling: a deliberate split, not a parity bug
# -- the generator stays 100 nm inside the band).
#
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
# Duplicated as a literal (like ``manufacturers/fabrication_process.py``'s
# twin) rather than imported because the router's copy deliberately stays
# at 1e-4 (see above), and importing anything under
# ``kicad_tools.router`` from the validate package would break the
# lazy-import contract (``tests/test_validate_lazy_imports.py`` forbids
# ``kicad_tools.validate.*`` from loading the router).  Keep the
# checker-side copies (this and ``manufacturers/fabrication_process.py``)
# in sync.  NOT twins: ``drc/incremental.py`` and ``creepage/engine.py``
# deliberately stay at 1e-4 -- their consumers write copper (see
# :data:`WRITER_CLEARANCE_MARGIN_MM` below) or sit outside the probe's
# scope.
DRC_TOLERANCE: float = 5e-4

# Clearance slack for tools that *write* copper (mm) -- the margin the
# generator side keeps inside KiCad's acceptance band.  #9619 widened the
# *checker* (:data:`DRC_TOLERANCE`) to KiCad's probed 500 nm so verdicts
# match ``kicad-cli`` verdict-for-verdict, but copper a kct command *moves*
# or *places* and then saves must not rely on that band: rounding on save
# can tip an exactly-at-the-band gap into a real kicad-cli violation.  Per
# the #9619 ruling ("split, don't change") the writers keep the pre-#9619
# 1e-4 margin: ``cli/pcb_move_via.py`` and ``cli/relocate_in_pad_vias.py``
# (via moves / relocations that rewrite the board) compare against this
# constant, and ``drc/incremental.py``'s ``_CLEARANCE_EPSILON_MM`` keeps
# the same 1e-4 as a literal (it feeds ``optim/session.py``'s placement
# feedback loop) -- as does the router package's own
# ``CLEARANCE_EPSILON_MM`` and ``creepage/engine.py``'s ``_PASS_TOLERANCE``
# (the probe measured copper clearance, not creepage).
WRITER_CLEARANCE_MARGIN_MM: float = 1e-4

# Band-edge float guard (mm).  KiCad compares in integer nanometres, so its
# acceptance-band edge is exact; the same board read as float64 carries
# ~1e-15 mm of coordinate rounding (a 4-decimal mm coordinate like
# ``20.3265`` is not binary-exact), which would otherwise decide an
# exactly-at-the-band verdict by rounding: the #9619 probe board reads
# ~7e-16 mm below its nominal 500 nm shortfall.  1e-9 mm is six orders of
# magnitude above that dust and six below KiCad's 1 nm IU, so it cannot
# move any verdict a nanometre-resolution check could see.  Folded into
# :func:`drc_clear` -- use that predicate at band-edge verdict sites
# instead of re-deriving the comparison per rule.
DRC_FLOAT_GUARD_MM: float = 1e-9


def drc_clear(measured_mm: float, required_mm: float) -> bool:
    """True unless the measured gap falls short of ``required_mm`` by more
    than :data:`DRC_TOLERANCE` (KiCad's probed band, widened by the
    :data:`DRC_FLOAT_GUARD_MM` float-safety term so a gap KiCad's
    integer-nm math accepts at exactly the band edge is not flagged here
    on float dust alone)."""
    return measured_mm + DRC_TOLERANCE >= required_mm - DRC_FLOAT_GUARD_MM


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
