"""KiCad schematic operations."""

from .symbol_ops import (
    replace_symbol_lib_id,
    find_symbol_by_reference,
    get_symbol_lib_id,
    get_symbol_pins,
    update_symbol_pins,
    clear_symbol_pins,
    add_symbol_pin,
    create_replacement_symbol,
    SymbolReplacement,
)

from .net_ops import (
    NetTracer,
    Net,
    NetConnection,
    trace_nets,
    find_net,
)
