"""Gate-parity ratchet for native-evidence producers (Issue #7358).

Any ``scripts/boards/*.py`` that writes ``readiness.json`` /
``native-drc*.json`` / ``fill-consistency.json`` must do so through
``refresh_readiness.repin_staged`` or ``readiness_cmd.main`` so the shared
gates run.  Today's offenders sit in ``ALLOWLIST``; it can only shrink.  A
second test pins exactly one re-pin owner per ``ready`` board, which settles the
00-02 double-producer ambiguity recorded in #7353.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOARDS_SCRIPTS = REPO / "scripts" / "boards"

#: A string literal that *is* a gated evidence path (not prose that mentions one).
_EVIDENCE_PATH = re.compile(r"^[\w./-]*(readiness|native-drc[\w-]*|fill-consistency)\.json$")
_SANCTIONED = re.compile(r"\brepin_staged\b|\breadiness_cmd\.main\b")

#: Scripts that still write gated evidence without the gated path.  Shrink only.
ALLOWLIST = {
    "annotate_pintypes_6025.py",
    "regenerate_boards_03_04_6076.py",
}

#: Board dir -> the one producer that owns its ``readiness.json`` re-pin.
READINESS_REPIN_OWNER = {
    "00-simple-led": "refresh_readiness",
    "01-voltage-divider": "refresh_readiness",
    "02-charlieplex-led": "refresh_readiness",
    "03-usb-joystick": "kct readiness",
    "04-stm32-devboard": "refresh_readiness",
    "05-bldc-motor-controller": "refresh_readiness",
    "06-diffpair-test": "refresh_readiness",
    "07-matchgroup-test": "refresh_readiness",
}


def _evidence_scripts() -> list[Path]:
    return sorted(p for p in BOARDS_SCRIPTS.glob("*.py") if not p.name.startswith("_"))


def _names_evidence_path(text: str) -> bool:
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _EVIDENCE_PATH.match(node.value)
        for node in ast.walk(ast.parse(text))
    )


def _offenders() -> set[str]:
    found = set()
    for path in _evidence_scripts():
        text = path.read_text()
        if _names_evidence_path(text) and not _SANCTIONED.search(text):
            found.add(path.name)
    return found


def test_evidence_scripts_write_through_the_gated_repin_path():
    new = _offenders() - ALLOWLIST
    assert not new, (
        f"{sorted(new)} write readiness/native-drc/fill-consistency evidence without "
        "refresh_readiness.repin_staged or readiness_cmd.main; route them through the "
        "gated path (do not extend ALLOWLIST)"
    )


def test_allowlist_has_no_stale_entries():
    stale = ALLOWLIST - _offenders()
    assert not stale, f"{sorted(stale)} no longer offend; remove them from ALLOWLIST"


def test_scanner_flags_a_bypassing_script_and_skips_prose():
    assert _names_evidence_path('x = out / "native-drc.json"\n')
    assert _names_evidence_path('p = "output/readiness/fill-consistency.json"\n')
    assert not _names_evidence_path(
        '"""Re-pin readiness.json."""\nh = "ISO stamp for readiness.json"\n'
    )


def _refresh_readiness_boards() -> set[str]:
    spec = importlib.util.spec_from_file_location(
        "_refresh_readiness_7358", BOARDS_SCRIPTS / "refresh_readiness.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return set(module.BOARDS)


def test_every_ready_board_has_exactly_one_repin_owner():
    boards = REPO / "boards"
    ready = set()
    for report in boards.glob("*/output/readiness.json"):
        if json.loads(report.read_text()).get("status") == "ready":
            ready.add(report.parents[1].name)
    assert ready, "no ready boards found -- guard is vacuous"
    assert ready == set(READINESS_REPIN_OWNER), (
        f"ready boards without an owner: {sorted(ready - set(READINESS_REPIN_OWNER))}; "
        f"owners for non-ready/missing boards: {sorted(set(READINESS_REPIN_OWNER) - ready)}"
    )
    for name in READINESS_REPIN_OWNER:
        assert (boards / name).is_dir()
    registered = {n for n, o in READINESS_REPIN_OWNER.items() if o == "refresh_readiness"}
    assert registered == _refresh_readiness_boards(), (
        "refresh_readiness.BOARDS and READINESS_REPIN_OWNER disagree"
    )
