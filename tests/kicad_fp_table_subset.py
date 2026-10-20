"""Give each test ``kicad-cli pcb drc`` only the footprint libraries its board names.

Issue #5240. ``kicad-cli pcb drc`` loads every library in the global
``fp-lib-table`` before checking a board, whichever footprints the board
uses. With the pinned table the Test job installs (155 stock libraries,
``src/kicad_tools/data/kicad-config/10.0/fp-lib-table``) that costs about
5 CPU-seconds and 2-3 wall seconds per launch, measured in the CI KiCad image
on a board whose only footprints are synthetic ``Test:`` ones:

    pinned table   real 3.5-4.4 s   user 5.8-6.0 s
    no fp libs     real 1.0 s       user 0.7 s

and the bulk pool launches ``pcb drc`` about 770 times per run. That library
load is most of the CPU a typical test's DRC costs, on a runner where those
DRCs already queue for two native slots.

So, for the test session only, each ``kicad-cli pcb drc`` launch is pointed
at a copy of its config home whose ``fp-lib-table`` keeps exactly the entries
whose nickname a footprint on the board uses (``Resistor_SMD`` for
``(footprint "Resistor_SMD:R_0603_1608Metric" ...)``). KiCad resolves a
footprint's library through its nickname alone, so every lookup DRC makes
finds what it found before: a stock footprint still meets its library (and
reports the same ``lib_footprint_mismatch``), and a nickname the table lacks
still gets the same ``lib_footprint_issues`` text. Everything else in the
config home (the sym-lib-table, ``kicad_common.json`` path variables, ...) is
copied unchanged, and a project-local ``fp-lib-table`` beside the board is
not touched at all.

It deliberately leaves a launch alone -- full table, original environment --
whenever it cannot be sure of that:

* the argv is not ``kicad-cli pcb drc`` with exactly one ``.kicad_pcb`` input;
* the launch's ``KICAD_CONFIG_HOME`` is not the session's own (a test or
  producer that chose its own config home, e.g. an empty one or a scratch
  pinned one, gets exactly that);
* the board cannot be read, or uses a legacy ``(module ...)`` footprint;
* a table entry spans lines, is not a plain ``(lib (name "..."))`` entry, or
  is a nested ``(type "Table")``.

Set ``KCT_TEST_FULL_FP_LIB_TABLE=1`` to turn it off (every launch then sees
the full table, as before).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

DISABLE_ENV = "KCT_TEST_FULL_FP_LIB_TABLE"
CONFIG_HOME_ENV = "KICAD_CONFIG_HOME"

_FOOTPRINT_LIB = re.compile(r'\(footprint\s+"((?:[^"\\]|\\.)*)"')
_LEGACY_MODULE = re.compile(r"\(module[\s)]")
_LIB_NAME = re.compile(r'^\s*\(lib\s+\(name\s+"((?:[^"\\]|\\.)*)"\)')
_VERSION_DIR = re.compile(r"^\d+\.\d+$")

_lock = threading.Lock()
_cache: dict[tuple, Path] = {}
_root: list[Path] = []
_session_home: list[str | None] = []
_installed: list[bool] = []


def default_config_home() -> Path:
    """KiCad's config home when ``KICAD_CONFIG_HOME`` is unset (as runner.py)."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Preferences/kicad"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "kicad"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "kicad"


def board_nicknames(text: str) -> frozenset[str] | None:
    """Library nicknames the board's footprints name; ``None`` when unsure."""
    if _LEGACY_MODULE.search(text):
        return None
    nicknames = set()
    for lib_id in _FOOTPRINT_LIB.findall(text):
        if ":" in lib_id:
            nicknames.add(lib_id.split(":", 1)[0])
    return frozenset(nicknames)


def filter_fp_lib_table(text: str, keep: frozenset[str]) -> str | None:
    """*text* with only the ``(lib ...)`` entries named in *keep*; ``None`` when unsure."""
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("(lib"):
            match = _LIB_NAME.match(line)
            # One complete entry per line is what KiCad writes; anything else
            # is not ours to rewrite.
            if (
                match is None
                or stripped.count("(") != stripped.count(")")
                or '(type "Table")' in stripped
            ):
                return None
            if match.group(1) not in keep:
                continue
        out.append(line)
    return "".join(out)


def _drc_board(args: Any, cwd: Any) -> Path | None:
    """The input board of a ``kicad-cli pcb drc`` argv, else ``None``."""
    if isinstance(args, (str, bytes, os.PathLike)):
        return None
    try:
        words = [os.fspath(a) for a in args]
    except TypeError:
        return None
    if len(words) < 4 or Path(words[0]).name not in ("kicad-cli", "kicad-cli.exe"):
        return None
    if words[1:3] != ["pcb", "drc"]:
        return None
    boards = [
        w
        for i, w in enumerate(words[3:], start=3)
        if isinstance(w, str)
        and w.endswith(".kicad_pcb")
        and words[i - 1] not in ("-o", "--output")
    ]
    if len(boards) != 1:
        return None
    board = Path(boards[0])
    if not board.is_absolute() and cwd is not None:
        board = Path(os.fspath(cwd)) / board
    return board


def _subset_home(source: Path, keep: frozenset[str]) -> Path | None:
    """A copy of config home *source* with every fp-lib-table cut to *keep*."""
    tables = sorted(
        p
        for p in source.glob("*/fp-lib-table")
        if p.is_file() and _VERSION_DIR.match(p.parent.name)
    )
    if not tables:
        return None
    key = (
        str(source),
        keep,
        tuple((str(t), t.stat().st_mtime_ns, t.stat().st_size) for t in tables),
    )
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
        filtered = {}
        for table in tables:
            text = filter_fp_lib_table(table.read_text(encoding="utf-8"), keep)
            if text is None:
                return None
            filtered[table.relative_to(source)] = text
        if not _root:
            _root.append(Path(tempfile.mkdtemp(prefix="kct-fp-subset-")))
        target = Path(tempfile.mkdtemp(prefix="home-", dir=_root[0]))
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        for rel, text in filtered.items():
            (target / rel).write_text(text, encoding="utf-8")
        _cache[key] = target
        return target


def subset_env(args: Any, env: Any, cwd: Any) -> dict[str, str] | None:
    """The environment to launch *args* with, or ``None`` to leave it alone."""
    if os.environ.get(DISABLE_ENV) or not _session_home:
        return None
    board = _drc_board(args, cwd)
    if board is None:
        return None
    base = dict(os.environ if env is None else env)
    if base.get(CONFIG_HOME_ENV) != _session_home[0]:
        return None
    source = Path(base[CONFIG_HOME_ENV]) if base.get(CONFIG_HOME_ENV) else default_config_home()
    try:
        keep = board_nicknames(board.read_text(encoding="utf-8", errors="replace"))
        if keep is None:
            return None
        home = _subset_home(source, keep)
    except OSError:
        return None
    if home is None:
        return None
    base[CONFIG_HOME_ENV] = str(home)
    return base


def install() -> bool:
    """Wrap ``subprocess.Popen`` for the session. Idempotent; returns whether active."""
    if _installed:
        return True
    _session_home.append(os.environ.get(CONFIG_HOME_ENV))
    base = subprocess.Popen
    while base.__module__ != "subprocess":  # under the native gate's subclass
        base = base.__mro__[1]
    original = base.__init__

    def __init__(self, args, *rest, **kwargs):  # noqa: N807
        env = None
        try:
            env = subset_env(args, kwargs.get("env"), kwargs.get("cwd"))
        except Exception:  # never let the speed-up break a launch
            env = None
        if env is not None:
            kwargs["env"] = env
        original(self, args, *rest, **kwargs)

    __init__.__wrapped__ = original  # type: ignore[attr-defined]
    base.__init__ = __init__
    _installed.append(True)
    return True
