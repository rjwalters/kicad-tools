"""Pin-lookup helpers for blocks whose default symbols name pins unevenly.

Several stock blocks were written against pin names that the KiCad 10 symbol
libraries do not use (issue #7399):

* ``Device:D_TVS`` is bidirectional and names its pins ``A1``/``A2``, so the
  ``"A"``/``"K"`` lookups the blocks used for a unidirectional diode fail.
* ``Amplifier_Current:INA240A1D`` leaves its output pin unnamed, and the
  output's pin *number* differs between packages (5 on SOIC, 8 on TSSOP).

These helpers resolve such roles by name first, so a caller-supplied symbol
that does use the conventional names keeps resolving exactly as before, and
only fall back to the KiCad library convention when the name is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..models.symbol import SymbolInstance

# KiCad ``Device`` diode convention: pin 1 is the cathode, pin 2 the anode.
# ``Device:D_TVS`` keeps the same numbering (pin 1 = A1, pin 2 = A2), so a
# block laid out around a diode's K/A geometry lands on the same pin ends.
_DIODE_PIN_NUMBERS = {"K": "1", "A": "2"}


def _has_pin(symbol: SymbolInstance, name: str) -> bool:
    """True when ``symbol`` resolves ``name`` without the lookup having to fail.

    Checking up front (rather than catching the miss) keeps a probe for an
    optional name from looking like a failed lookup.  Symbols without a
    pin list (test doubles) are assumed to have the pin.
    """
    pins = getattr(getattr(symbol, "symbol_def", None), "pins", None)
    if not isinstance(pins, list):
        return True
    wanted = name.lower()
    return any(p.name.lower() == wanted or p.number.lower() == wanted for p in pins)


def diode_pin_position(diode: SymbolInstance, role: Literal["A", "K"]) -> tuple[float, float]:
    """Return the position of a diode's anode (``"A"``) or cathode (``"K"``).

    Tries the pin name first.  When the symbol has no such name -- the
    bidirectional ``Device:D_TVS`` (``A1``/``A2``) is the common case -- it
    falls back to KiCad's diode pin numbering (``K`` = 1, ``A`` = 2).
    """
    if _has_pin(diode, role):
        return diode.pin_position(role)
    return diode.pin_position(_DIODE_PIN_NUMBERS[role])


def output_pin_position(symbol: SymbolInstance, name: str = "OUT") -> tuple[float, float]:
    """Return the position of a symbol's output pin.

    Tries ``name`` first, then the symbol's single pin whose electrical type is
    ``output`` (KiCad's INA240 symbols leave the output unnamed, and its pin
    number depends on the package).
    """
    if _has_pin(symbol, name):
        return symbol.pin_position(name)
    outputs = [p for p in symbol.symbol_def.pins if p.pin_type == "output"]
    if len(outputs) != 1:
        # Let the normal lookup raise its PinNotFoundError with suggestions.
        return symbol.pin_position(name)
    return symbol.pin_position(outputs[0].number)


def first_pin_position(symbol: SymbolInstance, *names: str) -> tuple[float, float]:
    """Return the position of the first of ``names`` that ``symbol`` has.

    Lets a block accept both its historical pin names (still used by custom
    symbols) and the KiCad 10 library's names.  If none match, the lookup of
    the last name raises the usual ``PinNotFoundError`` with suggestions.
    """
    for name in names[:-1]:
        if _has_pin(symbol, name):
            return symbol.pin_position(name)
    return symbol.pin_position(names[-1])
