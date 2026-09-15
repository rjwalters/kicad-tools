"""Shared numeric defaults for silkscreen clearance/search geometry.

Issue #5240: these plain float constants are needed both by the real
silkscreen logic (:mod:`kicad_tools.validate.rules.silkscreen`,
:mod:`kicad_tools.silkscreen.place_refs`) and by
:mod:`kicad_tools.cli.parser`'s argparse default/help-text construction --
the CLI parser is built on *every* ``kct <anything>`` invocation via
``create_parser()``, regardless of which subcommand actually runs.

Before this module existed, ``parser.py`` obtained these values via
``from kicad_tools.silkscreen.place_refs import (...)``, which forces
Python to fully execute ``place_refs.py``'s own top-level import of
``kicad_tools.validate.rules.silkscreen`` -- and importing anything under
``kicad_tools.validate`` (a package) runs ``validate/__init__.py`` and
``validate/rules/__init__.py`` first, which eagerly aggregate every DRC
rule module including ``via_in_pad.py``, which itself imports the whole
``kicad_tools.router`` package.  Measured locally: this one 4-constant
lookup added ~1.6s of CPU time (~3-5s wall) to *every* CLI invocation,
including ones with no connection to routing or silkscreen at all (e.g.
``kct zones fill --help``).

Locating these dependency-free literals in their own leaf module lets the
parser pull just the numbers it needs without paying for the real
modules' transitive import chains, which are only needed when
``place-silk-refs`` (or the silkscreen edge-clearance DRC rule) actually
executes.  Behavior is unchanged -- every name below is still importable
at its historical location too (each is re-exported there), so existing
callers (:mod:`kicad_tools.cli.place_silk_refs_cmd`,
``tests/test_validate_silkscreen.py``, etc.) are unaffected.
"""

from __future__ import annotations

# Default silk-to-obstacle clearance (mm), used by
# ``kicad_tools.silkscreen.place_refs``.  Deliberately a *dedicated*
# solver parameter, distinct from ``DesignRules.min_solder_mask_clearance_mm``
# (used only to size pad mask apertures, matching
# ``validate.rules.silkscreen``) -- it is the "how far apart do labels
# need to be to stay legible" knob a fab-specific silkscreen policy
# tightens or loosens.  0.15mm matches the tightened value from the
# concrete Chorus v25 result ``place_refs.py`` targets.
DEFAULT_CLEARANCE_MM = 0.15

# Search geometry defaults for ``kicad_tools.silkscreen.place_refs``.  A
# candidate ring step of 0.25mm across an 8mm radius (32 rings x 8 points =
# 256 candidates/reference, worst case) is comfortably fast for real
# boards while covering "just outside this 0402's courtyard" through
# "clear across a dense connector row".
DEFAULT_MAX_OFFSET_MM = 8.0
DEFAULT_STEP_MM = 0.25

# Minimum silk-to-board-edge clearance (mm), used by the
# ``check_silk_edge_clearance`` DRC rule in
# ``kicad_tools.validate.rules.silkscreen``.
SILK_EDGE_CLEARANCE_MM = 0.2
