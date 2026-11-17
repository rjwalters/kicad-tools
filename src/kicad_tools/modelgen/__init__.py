"""Parametric 3D body generation for footprints with no stock model (issue #7687).

``kct models generate`` derives a STEP body from a footprint's fab/courtyard
outline and pad field plus a few parameters:

* **families** (:mod:`.families`): chip R/C/L, rectangular boxes (shunts,
  power inductors), gull-wing SO/SOT/QFP, QFN/DFN, and 2.00/2.54 mm headers
  (JST PH/XH style shrouded or plain).  Building them needs the optional
  ``[models]`` extra (CadQuery, Apache-2.0); it is never a core dependency.
* **placeholder mode**: a courtyard-extruded box written by a dependency-free
  STEP writer (:mod:`.step_writer`).  Its file name carries ``placeholder``,
  so the coverage audit still counts the footprint as a gap.
* **bridge** (:mod:`.bridge`): runs KiCad's GPL-3.0 ``kicad-library-tools``
  generator as an external subprocess only -- never imported or vendored.

Every body records its generator, version and parameters in a
``<stem>.provenance.json`` sidecar and in the STEP header.
"""

from __future__ import annotations

from .families import FAMILIES, BodySpec, ParamError, family_for_lib_id, parse_params
from .footprint_geom import FootprintGeometry, load_footprint_file, load_footprint_from_pcb
from .generate import ExtraMissingError, GenerateResult, default_output_dir, generate_model

__all__ = [
    "FAMILIES",
    "BodySpec",
    "ExtraMissingError",
    "FootprintGeometry",
    "GenerateResult",
    "ParamError",
    "default_output_dir",
    "family_for_lib_id",
    "generate_model",
    "load_footprint_file",
    "load_footprint_from_pcb",
    "parse_params",
]
