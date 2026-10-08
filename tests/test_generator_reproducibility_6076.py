"""Fleet generators write byte-identical design files on every run (Issue #6076).

Board 00's schematic + unrouted PCB are generated three times -- twice in one
process (the PCB UUID sequence must restart) and once in another process
with a different ``PYTHONHASHSEED`` -- and must match byte for byte.  Board
06's text-emitting PCB generator, which needs no KiCad libraries, is checked
the same way.  Only the design-capture steps run (no routing), so this is
fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_BOARD00 = textwrap.dedent(
    """
    import contextlib, importlib.util, io, sys
    from pathlib import Path

    gen = Path(sys.argv[1])
    spec = importlib.util.spec_from_file_location("board00_generate_design", gen)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
        for out in sys.argv[2:]:
            mod.create_led_schematic(Path(out))
            mod.create_led_pcb(Path(out))
    """
)

_BOARD06_PCB = textwrap.dedent(
    """
    import importlib.util, sys
    from pathlib import Path

    gen = Path(sys.argv[1])
    sys.path.insert(0, str(gen.parent))
    spec = importlib.util.spec_from_file_location("board06_generate_pcb", gen)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    first = mod.generate_pcb()
    assert mod.generate_pcb() == first, "second in-process build differs"
    sys.stdout.write(first)
    """
)


def _run(script: str, args: list[str], seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        timeout=300,
    ).stdout


def _require_symbol_libs() -> None:
    from kicad_tools.schematic.registry import _default_symbol_paths

    if not _default_symbol_paths():
        pytest.skip("KiCad symbol libraries not installed; board 00 cannot resolve Device:R")


def test_board00_schematic_and_pcb_are_byte_reproducible(tmp_path):
    _require_symbol_libs()
    gen = str(REPO_ROOT / "boards" / "00-simple-led" / "generate_design.py")
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _run(_BOARD00, [gen, str(a), str(b)], seed="1")
    _run(_BOARD00, [gen, str(c)], seed="4242")
    for name in ("simple_led.kicad_sch", "simple_led.kicad_pcb"):
        ref = (a / name).read_bytes()
        assert (b / name).read_bytes() == ref, f"{name}: second in-process run differs"
        assert (c / name).read_bytes() == ref, f"{name}: other process / hash seed differs"
    # Sanity: the files really carry UUIDs (so the comparison means something).
    assert b'(uuid "' in (a / "simple_led.kicad_sch").read_bytes()
    assert b'(uuid "' in (a / "simple_led.kicad_pcb").read_bytes()


def test_board06_pcb_generator_is_byte_reproducible():
    gen = str(REPO_ROOT / "boards" / "06-diffpair-test" / "generate_pcb.py")
    first = _run(_BOARD06_PCB, [gen], seed="0")
    assert first == _run(_BOARD06_PCB, [gen], seed="987")
    assert '(uuid "' in first
