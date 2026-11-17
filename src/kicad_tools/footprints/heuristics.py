"""Library-independent footprint heuristics for missing-footprint auto-assign.

The :mod:`kicad_tools.cli.sch_suggest_footprint` machinery resolves a
footprint by *scanning the installed KiCad ``.kicad_mod`` libraries* — it
needs the standard libraries on disk (or a project ``fp-lib-table``). That
path is the right one when libraries are available, but it cannot help on a
fresh checkout / CI runner with no KiCad install, and it cannot resolve a
standard passive whose package is only implied by its symbol.

This module fills that gap with a **deterministic, disk-free** mapping from a
symbol's *value + package hint + library id* to a canonical
``Library:Footprint`` string that names a stock KiCad footprint (e.g.
``Resistor_SMD:R_0402_1005Metric``). It never touches the filesystem, so it
produces the same answer everywhere.

The mapping is intentionally narrow and high-confidence:

* **Two-pin SMD passives** — resistors, capacitors, inductors, ferrite
  beads, LEDs, diodes — whose package is a standard chip size
  (``0201/0402/0603/0805/1206/1210/2010/2512``). These are the parts that
  most often arrive footprint-less from value-only schematic capture, and
  whose footprint is fully determined by *component class + chip size*.

The function returns ``None`` for anything it cannot resolve with high
confidence (multi-pin ICs, unknown packages, ambiguous classes). Callers
treat ``None`` as **fail-loud**: a part that cannot be auto-assigned must be
surfaced to a human, never silently guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "HeuristicMatch",
    "guess_chip_footprint",
    "CHIP_PASSIVE_LIBRARIES",
    "STANDARD_CHIP_SIZES",
]


# Canonical KiCad SMD chip-size code -> metric body suffix used in the stock
# footprint names (e.g. "0402" -> "1005Metric" yields "*_0402_1005Metric").
STANDARD_CHIP_SIZES: dict[str, str] = {
    "0201": "0603Metric",
    "0402": "1005Metric",
    "0603": "1608Metric",
    "0805": "2012Metric",
    "1206": "3216Metric",
    "1210": "3225Metric",
    "2010": "5025Metric",
    "2512": "6332Metric",
}


# Component class -> (library nickname, footprint-name prefix). Each entry
# names the stock KiCad SMD library and the footprint stem prefix used by
# that library's chip parts.
CHIP_PASSIVE_LIBRARIES: dict[str, tuple[str, str]] = {
    "R": ("Resistor_SMD", "R"),
    "C": ("Capacitor_SMD", "C"),
    "L": ("Inductor_SMD", "L"),
    "FB": ("Inductor_SMD", "L"),  # ferrite bead -> inductor chip footprint
    "LED": ("LED_SMD", "LED"),
    "D": ("Diode_SMD", "D"),
}


@dataclass(frozen=True)
class HeuristicMatch:
    """A high-confidence footprint guess for a footprint-less symbol.

    Attributes:
        footprint: The canonical ``Library:Footprint`` string.
        component_class: The inferred class (``"R"``, ``"C"``, ...).
        chip_size: The inferred chip size code (``"0402"``, ...).
        reason: Human-readable explanation of how the guess was made.
    """

    footprint: str
    component_class: str
    chip_size: str
    reason: str


# Reference-prefix -> component class. Used as a fallback when the lib_id does
# not make the class obvious (e.g. a generic ``Device:R`` is unambiguous, but
# some libraries use bespoke symbol names).
_REF_PREFIX_CLASS: dict[str, str] = {
    "R": "R",
    "C": "C",
    "L": "L",
    "FB": "FB",
    "LED": "LED",
    "D": "D",
}


# lib_id stem (lowercased) substrings -> component class. The check is ordered:
# more specific names first so "ferrite" wins over a bare "l".
_LIBID_CLASS_HINTS: tuple[tuple[str, str], ...] = (
    ("ferrite", "FB"),
    ("polarized", "C"),
    ("capacitor", "C"),
    ("resistor", "R"),
    ("inductor", "L"),
    ("ledd", "LED"),
    ("led", "LED"),
    ("schottky", "D"),
    ("zener", "D"),
    ("diode", "D"),
)


def _classify(lib_id: str, reference: str) -> str | None:
    """Infer the two-pin passive class from lib_id then reference prefix.

    Returns one of the keys of :data:`CHIP_PASSIVE_LIBRARIES`, or ``None``
    when the symbol is not recognisably a standard two-pin passive.
    """
    raw_stem = lib_id.split(":")[-1] if lib_id else ""
    # Scan the WHOLE lib_id (library + stem) for class hints so that
    # library-name conventions like ``Diode:1N4148W`` (stem carries no
    # "diode" token, but the library does) and ``Device:C_Polarized`` are
    # both classified.
    full = (lib_id or "").lower()
    for needle, cls in _LIBID_CLASS_HINTS:
        if needle in full:
            return cls

    # Exact short-name match for the canonical generic library symbols
    # (``Device:R``, ``Device:C``, ``Device:L``, ``Device:LED``,
    # ``Device:D``, ``Device:FerriteBead`` is handled by the hints above).
    # These stems are too short to appear in the substring hints, so match
    # them exactly against the reference-prefix class table.
    if raw_stem.upper() in _REF_PREFIX_CLASS:
        return _REF_PREFIX_CLASS[raw_stem.upper()]

    # Fall back to the reference designator prefix (letters before digits).
    m = re.match(r"^([A-Za-z]+)", reference or "")
    if m:
        prefix = m.group(1).upper()
        if prefix in _REF_PREFIX_CLASS:
            return _REF_PREFIX_CLASS[prefix]
    return None


# A bare chip-size token, optionally with a metric suffix already attached.
_CHIP_SIZE_RE = re.compile(r"(?<!\d)(0201|0402|0603|0805|1206|1210|2010|2512)(?!\d)")


def _extract_chip_size(*candidates: str) -> str | None:
    """Return the first standard chip-size code found in *candidates*.

    Each candidate string (package hint, value, lib_id, ...) is scanned for
    a recognised chip-size token. Returns the imperial code (``"0402"``) or
    ``None`` if none is present.
    """
    for text in candidates:
        if not text:
            continue
        m = _CHIP_SIZE_RE.search(text)
        if m:
            return m.group(1)
    return None


def guess_chip_footprint(
    *,
    value: str | None = None,
    lib_id: str = "",
    reference: str = "",
    package: str | None = None,
    pin_count: int | None = None,
) -> HeuristicMatch | None:
    """Guess a stock footprint for a footprint-less two-pin SMD passive.

    The guess is **deterministic and disk-free**: it maps the symbol's
    component class (resistor / capacitor / inductor / ferrite-bead / LED /
    diode) plus its standard chip size to the canonical KiCad SMD footprint
    name. It does not consult any installed library, so it returns the same
    answer on every machine.

    Args:
        value: The symbol's value field (e.g. ``"10k"``, ``"100nF"``). Used
            only as a place to look for an embedded chip-size token.
        lib_id: The symbol's library id (e.g. ``"Device:R"``). Primary class
            signal.
        reference: The reference designator (e.g. ``"R12"``). Fallback class
            signal via its letter prefix.
        package: An explicit package hint (e.g. ``"0402"``, ``"R_0603"``).
            Strongest chip-size signal when present.
        pin_count: The symbol's pin count. When provided and not equal to 2,
            the symbol is rejected (this heuristic only covers two-pin chip
            passives). ``None`` (unknown) is accepted.

    Returns:
        A :class:`HeuristicMatch` when the symbol is confidently a standard
        two-pin chip passive of a known size, else ``None``.
    """
    # Two-pin only. An unknown pin count (None) is allowed through; a known
    # non-2 count is a hard reject (an IC is never a chip passive).
    if pin_count is not None and pin_count != 2:
        return None

    cls = _classify(lib_id, reference)
    if cls is None:
        return None

    chip_size = _extract_chip_size(package or "", value or "", lib_id or "")
    if chip_size is None:
        return None

    metric = STANDARD_CHIP_SIZES.get(chip_size)
    if metric is None:  # pragma: no cover - regex already constrains the set
        return None

    library, prefix = CHIP_PASSIVE_LIBRARIES[cls]
    footprint = f"{library}:{prefix}_{chip_size}_{metric}"
    reason = (
        f"class={cls} (from {'lib_id' if lib_id else 'reference'}), "
        f"chip size {chip_size} -> {metric}"
    )
    return HeuristicMatch(
        footprint=footprint,
        component_class=cls,
        chip_size=chip_size,
        reason=reason,
    )
