"""Shared KiCad file-format version constants.

KiCad stamps a numeric format ``version`` (a date code) into its files, and a
``generator_version`` string naming the app that wrote them.  PCB
(``.kicad_pcb`` / ``.kicad_mod``), schematic (``.kicad_sch``) and symbol-library
(``.kicad_sym``) files are **independent format-version streams** with
different date codes -- there is no single "KiCad-10 version number" that spans
all three.  Every writer in this package must route its stamp through the
constant for the matching stream so the values cannot drift.

Choosing the date codes -- read before bumping any value
--------------------------------------------------------
KiCad loads *older* format versions fine (it silently auto-upgrades them in
memory), but **rejects a format version newer than the installed release
recognises** ("Failed to load board").  This asymmetry is the whole reason the
constants are pinned conservatively:

* The installed KiCad 10.0.4 writes newer codes than the ones below --
  ``20260206`` for boards, ``20260306`` for schematics, ``20251024`` for symbol
  libraries -- but those *future* codes are rejected by earlier 10.0.x releases
  (empirically, KiCad 10.0.3 rejects the board's ``20260206``).  Emitting the
  newest code would therefore regress users still on 10.0.2 / 10.0.3.
* The codes below are the conservative floor: because KiCad reads older formats
  by design, they load cleanly across the **entire** 10.0.x line.  Do NOT bump
  them to a newer code just because a later point release accepts it -- verify
  load-acceptance across the whole 10.0.x line first (a synthetic future code
  such as ``20991231`` is rejected outright, which is the failure mode to
  avoid).

Keeping single constants here prevents the drift that previously had writers
emit a grab-bag of stale, mismatched codes (``20231014`` / ``20231120`` /
``20240108``) and a zoo of ``generator_version`` strings
(``"0.2.0"`` / ``"9.0"`` / ``"1.0"`` / ``"10.0"``).
"""

# KiCad 10.0.x-compatible board / footprint file-format version (date code).
# Used by all writers that emit a ``.kicad_pcb`` / ``.kicad_mod`` ``(version ...)``
# node so they cannot drift.  20241229 is the conservative floor that loads
# across the whole 10.0.x line (10.0.3 rejects the newer 20260206).
KICAD_BOARD_FORMAT_VERSION = 20241229

# KiCad 10.0.x-compatible schematic (``.kicad_sch``) file-format version.
# Conservative floor: loads across the whole 10.0.x line via backward-read.
# (KiCad 10.0.4 writes the newer 20260306, which earlier 10.0.x would reject.)
KICAD_SCH_FORMAT_VERSION = 20231120

# KiCad 10.0.x-compatible symbol-library (``.kicad_sym``) file-format version.
# Conservative floor: loads across the whole 10.0.x line via backward-read.
# (KiCad 10.0.4 writes the newer 20251024, which earlier 10.0.x would reject.)
KICAD_SYM_FORMAT_VERSION = 20231120

# Shared ``generator_version`` string emitted by every writer, naming the KiCad
# major.minor line this toolkit targets.  Replaces the pre-centralization zoo of
# per-writer values.  Emitted as a quoted atom by the S-expression writers so
# kicad-cli does not downgrade it to a bare number.
KICAD_GENERATOR_VERSION = "10.0"

__all__ = [
    "KICAD_BOARD_FORMAT_VERSION",
    "KICAD_SCH_FORMAT_VERSION",
    "KICAD_SYM_FORMAT_VERSION",
    "KICAD_GENERATOR_VERSION",
]
