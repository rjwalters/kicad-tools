"""Shared S-expression builders for the ``test_sch_validate_*`` test family.

Each validator test file used to carry its own copy of these helpers.  They
are imported under the files' historical local names, e.g.::

    from tests._sch_sexp_builders import make_lib_symbol as _make_lib_symbol
"""

from __future__ import annotations


def make_lib_symbol(
    lib_id: str,
    pins: list[tuple[str, str, str]],
) -> str:
    """Generate a lib_symbols entry.

    Args:
        lib_id: e.g. "Device:R_Small" or "IC:DAC"
        pins: list of (pin_number, pin_name, pin_type) tuples
    """
    part_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    pin_blocks = []
    for i, (num, name, ptype) in enumerate(pins):
        y = i * 2.54
        pin_blocks.append(
            f"""(pin {ptype} line
                    (at 0 {y:.2f} 0)
                    (length 2.54)
                    (name "{name}")
                    (number "{num}")
                )"""
        )
    pin_str = "\n".join(pin_blocks)
    return f"""(symbol "{lib_id}"
            (pin_names (offset 0.254))
            (symbol "{part_name}_0_1"
                (rectangle
                    (start -5.08 -{(len(pins) * 2.54) + 1.27:.2f})
                    (end 5.08 1.27)
                    (stroke (width 0.254))
                    (fill (type background))
                )
            )
            (symbol "{part_name}_1_1"
                {pin_str}
            )
        )"""


def make_symbol_instance(
    ref: str,
    lib_id: str,
    pins: list[tuple[str, str, str]],
    x: float = 100.0,
    y: float = 50.0,
    dnp: bool = False,
    in_bom: bool = True,
    on_board: bool = True,
    rotation: float = 0,
    *,
    footprint: str = "",
    value: str | None = None,
) -> str:
    """Generate a symbol instance S-expression."""
    pin_entries = "\n".join(f'(pin "{num}" (uuid "pin-{ref.lower()}-{num}"))' for num, _, _ in pins)
    dnp_str = "yes" if dnp else "no"
    in_bom_str = "yes" if in_bom else "no"
    on_board_str = "yes" if on_board else "no"
    value_str = value if value is not None else lib_id.split(":")[-1]
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} {rotation})
        (unit 1)
        (in_bom {in_bom_str})
        (on_board {on_board_str})
        (dnp {dnp_str})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{value_str}"
            (at {x + 2} {y} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" "{footprint}"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        {pin_entries}
    )"""
