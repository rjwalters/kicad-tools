"""Cache replay must preserve physical copper, not merely route counts."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.export import find_kicad_cli
from kicad_tools.sexp import SExp, parse_string


def _copper(text: str) -> list[str]:
    def canonical(node: SExp):
        if node.name is None:
            return node.value
        return [
            node.name,
            *[canonical(c) for c in node.children if c.name not in {"uuid", "tstamp"}],
        ]

    return sorted(
        json.dumps(canonical(node))
        for node in parse_string(text).children
        if node.name in {"segment", "via", "arc"}
    )


@pytest.mark.timeout(600)
def test_board02_cache_replays_complete_copper(tmp_path):
    if find_kicad_cli() is None:
        pytest.skip("Native KiCad is required for the final copper comparison")
    root = Path(__file__).resolve().parents[2]
    source = root / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"
    env = {**os.environ, "PYTHONHASHSEED": "42", "XDG_CACHE_HOME": str(tmp_path / "cache")}
    copper = []
    for run in (1, 2):
        output = tmp_path / f"run-{run}.kicad_pcb"
        log = tmp_path / f"run-{run}.log"
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(source),
            "--output",
            str(output),
            "--strategy",
            "negotiated",
            "--iterations",
            "30",
            "--deterministic-budget",
            "--timeout",
            "240",
            "--seed",
            "42",
            "--no-auto-pour",
            "--no-auto-layers",
            "--grid",
            "0.1",
            "--manufacturer",
            "jlcpcb",
        ]
        with log.open("w") as stream:
            subprocess.run(
                cmd,
                cwd=root,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=280,
                check=False,
            )
        evidence = log.read_text()
        assert ("Cache MISS" if run == 1 else "Cache HIT") in evidence, evidence[-2000:]
        if run == 2:
            assert "Using cached result (skipping routing)" in evidence, evidence[-2000:]
        assert output.exists(), evidence[-2000:]
        copper.append(_copper(output.read_text()))
    assert copper[0]
    assert copper[0] == copper[1], f"Cache changed copper; artifacts at {tmp_path}"
