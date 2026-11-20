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


# Numerical guard band for DRC comparisons (in mm).
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
# The pre-#3913 value of 0.005 mm (5 um) created a dead band that silently
# passed genuine marginal violations: the entire marginal class this guard
# was masking is on the order of 1.6 um (e.g. actual 0.1000 mm vs a
# 0.1016 mm / 4 mil floor). KiCad's own DRC works at IU granularity
# (1 nm), so a 5 um dead band was ~10^8x wider than float noise requires
# and hid real, fabricable-clearance-violating copper. See issue #3913.
#
# Issue #9619 review: KiCad's probed 500 nm acceptance band applies to
# *copper clearance verdicts only* -- it lives in
# :data:`KICAD_CLEARANCE_BAND_MM` below, applied via :func:`drc_clear` at
# the probed sites.  It must NOT be folded into this shared constant:
# every other consumer (track width, via drill/diameter, annular ring,
# netclass floors, hole, silk, via-in-pad, via-under-body) is a check the
# clearance probe never covered, and on track width and drill size pinned
# kicad-cli flags a 100 nm shortfall with no band at all -- sharing the
# 500 nm band there would make kct pass boards kicad-cli rejects.
DRC_TOLERANCE: float = 1e-4

# KiCad's probed copper-clearance acceptance band (mm) -- the largest
# shortfall pinned KiCad 10.0.6 accepts on copper-clearance verdicts.
# Black-box probe (tests/test_clearance_slack_kicad_band_9619.py re-runs
# it against the pinned image): parallel 0.2 mm tracks under a 0.127 mm
# rule -- a 500 nm shortfall is accepted, 501 nm is flagged; a 45-degree
# pair and a pad-to-track pair flip at the same edge within the 1 nm
# constructibility of diagonal coordinates.
#
# Scope (#9619 review): this band is *clearance-only* evidence.  Applied
# by :func:`drc_clear`, whose call sites are the probed copper-clearance
# verdicts (``clearance.py``, ``diffpair_clearance_intra.py``,
# ``netclass_clearance_copper.py``) plus -- since #9724 ran its own probe
# (same 500 nm cut-off on a straight edge, pinned in
# ``tests/test_clearance_slack_kicad_band_9619.py``) -- the copper-to-edge
# verdicts in ``edge.py`` (traces, SMD pads, zones).  Every other rule
# keeps :data:`DRC_TOLERANCE`: pinned kicad-cli flags a 100 nm track-width
# or drill shortfall outright (no band), so widening those checks to
# 500 nm would let ``kct check`` pass boards kicad-cli rejects.
# ``edge.py``'s hole-minimum branches (vias, through-hole pads) keep their
# own epsilon: the copper-edge probe cannot establish a tolerance for a
# separate hole minimum.  A rule may join this band only with its own
# probe evidence.
#
# Deliberately wider than the router's slack (the router package's own
# ``CLEARANCE_EPSILON_MM``, still 1e-4): the checker matches KiCad
# verdict-for-verdict on clearances, while copper the router *writes*
# keeps a margin inside KiCad's tolerance so rounding on save cannot tip
# it into a kicad-cli violation (#9619 ruling: a deliberate split, not a
# parity bug -- the generator stays 100 nm inside the band).
KICAD_CLEARANCE_BAND_MM: float = 5e-4

# Clearance slack for tools that *write* copper (mm) -- the margin the
# generator side keeps inside KiCad's acceptance band.  #9619 widened the
# *checker's* clearance verdicts (:data:`KICAD_CLEARANCE_BAND_MM` above)
# to KiCad's probed 500 nm so they match ``kicad-cli``
# verdict-for-verdict, but copper a kct command *moves* or *places* and
# then saves must not rely on that band: rounding on save can tip an
# exactly-at-the-band gap into a real kicad-cli violation.  Per the #9619
# ruling ("split, don't change") the writers keep the pre-#9619 1e-4
# margin: ``cli/pcb_move_via.py`` and ``cli/relocate_in_pad_vias.py``
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


def drc_clear(
    measured_mm: float, required_mm: float, *, band_mm: float = KICAD_CLEARANCE_BAND_MM
) -> bool:
    """True unless the measured gap falls short of ``required_mm`` by more
    than the supplied clearance band (default :data:`KICAD_CLEARANCE_BAND_MM`),
    widened by the :data:`DRC_FLOAT_GUARD_MM` float-safety term so a
    gap KiCad's integer-nm math accepts at exactly the band edge is not
    flagged here on float dust alone.  Clearance verdict sites only --
    dimension, annular, floor, hole and silk checks keep
    :data:`DRC_TOLERANCE`."""
    return measured_mm + band_mm >= required_mm - DRC_FLOAT_GUARD_MM


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
