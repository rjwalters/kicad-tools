"""Shared KiCad file-format version constants.

KiCad stamps a numeric format ``version`` (a date code) into ``.kicad_pcb``
and ``.kicad_mod`` files.  Installed KiCad rejects files stamped with a
*future* format version it does not recognise, so every writer in this
package must emit the same, KiCad-compatible value.

Keeping a single constant here prevents the drift that previously had some
writers emit ``20260206`` (a future code KiCad 10.0.3 rejects) while others
emitted the correct ``20241229``.
"""

# KiCad 10.0.x compatible board / footprint file-format version (date code).
# Used by all writers that emit a ``(version ...)`` node so they cannot drift.
KICAD_BOARD_FORMAT_VERSION = 20241229

__all__ = ["KICAD_BOARD_FORMAT_VERSION"]
