"""Symbol ``extends`` flattening must not depend on PYTHONHASHSEED (#6070)."""

import os
import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    from kicad_tools.schematic.models.symbol import SymbolDef
    from kicad_tools.sexp import parse_sexp

    parent = parse_sexp(
        '(symbol "P" (property "Reference" "U") (property "Value" "P")'
        ' (property "Footprint" "") (property "Datasheet" "")'
        ' (property "Description" "d") (property "ki_keywords" "k"))'
    )
    child = parse_sexp(
        '(symbol "C" (extends "P") (property "Value" "C") (property "Zed" "z")'
        ' (property "Alpha" "a") (property "Description" "cd"))'
    )
    sd = SymbolDef(lib_id="L:C", name="C", raw_sexp="", pins=[])
    print(sd._flatten_with_parent(child, parent).to_string())
    """
)


def _run(seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, check=True
    ).stdout


def test_flatten_property_order_is_hashseed_independent():
    outs = {_run(s) for s in ("0", "1", "2", "12345")}
    assert len(outs) == 1


def test_flatten_property_order_parent_then_child_only():
    out = _run("0")
    order = [out.index(f'"{n}"') for n in ("Reference", "Value", "Footprint", "Description")]
    assert order == sorted(order)
    assert out.index('"ki_keywords"') < out.index('"Zed"') < out.index('"Alpha"')
