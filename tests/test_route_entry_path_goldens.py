"""Golden behaviour-preservation harness for ``kct route``'s four entry paths.

Issue #7377 (steps 2-4) collapses the copy-pasted post-route tails of
``src/kicad_tools/cli/route_cmd.py``.  This harness lands first so every later
refactor has to show it changed nothing it did not mean to change.

``kct route`` dispatches to one of four tails (``_run_main_impl``):

==================  ======================================  =====================================
key                 flags                                   tail
==================  ======================================  =====================================
layer_escalation    (default; ``--auto-layers`` is on)      ``route_with_layer_escalation``
rule_relaxation     ``--adaptive-rules --no-auto-layers``   ``route_with_rule_relaxation``
combined            ``--adaptive-rules``                    ``route_with_combined_escalation``
single_attempt      ``--no-auto-layers``                    ``_run_main_impl`` single attempt
==================  ======================================  =====================================

(``--adaptive-rules --layers 2`` is *not* a way into rule relaxation: it is a
usage error, exit 1 -- pinned by :func:`test_adaptive_rules_with_explicit_layers_is_a_usage_error`.)

Each path runs on two small committed boards in
``tests/fixtures/route_entry_paths/``:

* ``routable`` -- 3 nets, one forced layer change; every path routes 100%.
* ``partial`` -- ``routable`` plus net ``/D`` whose pad is caged by keepouts,
  so every path ends PARTIAL (exit 2) and the incomplete-route half of each
  tail runs.  ``--max-layers 2`` keeps the escalation ladders short and
  ``--export-failed-nets`` pins bug B4 (below).

For every (board, path) the golden in ``goldens.json`` records:

* ``exit_code``
* ``copper`` -- segment / via / arc counts plus a sha256 over the sorted,
  normalised copper items (geometry, layer and net only: UUIDs and
  file-format attributes dropped, net ids resolved to names, numbers rounded
  to 1e-4 mm)
* ``headers`` -- the report section headers printed, in first-seen order
  (``--- X ---``, ``=== X ===`` and ``====`` banner titles; the router's
  per-iteration ``Iteration N`` headers are dropped as noise)
* ``files`` -- every file written into the output directory

Determinism: ``--seed 1 --deterministic-budget --no-cache --backend cpp``, and
kicad-cli is hidden so the native DRC cross-check / zone refill cannot make
the output depend on whether (or which) KiCad is installed.  The test skips
when the C++ router backend is not built (the Python fallback lands
different copper).

Updating the goldens
--------------------
The ``partial`` board's copper digest is architecture-dependent (#7621): the
same topology lands with 0.1 mm-shifted jogs on arm64 (macOS) versus x86_64
(Linux CI), so ``partial.*.copper.sha256`` is a ``{arch: sha256}`` mapping and
the test compares against the host's entry.  ``--update`` rewrites only the
host's key; CI's digest must come from the pinned x86_64 image, e.g. on a Mac
``docker run --platform linux/amd64`` the image in ``.github/workflows/ci.yml``
(``KICAD_IMAGE``), ``uv run kct build-native``, then ``--update``.  ``routable``
digests are identical on both architectures and stay plain strings.

A later #7377 PR must keep every golden identical, or change only the entries
its PR description names as intended.  Unrelated router changes can also move
the copper digests; regenerate on the branch and say why in the PR::

    uv run python tests/test_route_entry_path_goldens.py --update

Divergences fixed by #7377 step 2 (curator re-curation, B1-B4)
--------------------------------------------------------------
These used to be ``xfail(strict=True)`` bugs; the tests below now assert the fix.

* B1 ``--stitch-power-planes`` runs on every tail (``_run_post_save_stitching``).
* B2 ``--progressive-clearance`` and B3 ``--bus-routing`` are strategy
  selectors only the single-attempt path honours; the escalation paths now warn
  on stderr (``_warn_escalation_flags_ignored``) instead of ignoring them silently.
* B4 ``--export-failed-nets`` writes ``failed_nets.json`` on every tail; visible
  in the ``partial`` goldens' ``files``.

Also visible in the goldens (curator item D7, undecided): the
``out_partial.*`` snapshot is written only by the single-attempt path.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "route_entry_paths"
GOLDENS_PATH = FIXTURE_DIR / "goldens.json"

#: Flags shared by every run.  ``--backend cpp`` pins the engine the goldens
#: were generated with; the test skips when it is not built.
#: ``--checkpoint-interval 0`` takes wall-clock time out of the observation:
#: with the default 30 s a slow run archives an extra
#: ``out_checkpoint_unverified_<rand>.kicad_pcb`` and ``files`` differs (#7621).
COMMON_ARGS = (
    "--seed",
    "1",
    "--deterministic-budget",
    "--no-cache",
    "--backend",
    "cpp",
    "--checkpoint-interval",
    "0",
)

ENTRY_PATHS: dict[str, tuple[str, ...]] = {
    "layer_escalation": (),
    "rule_relaxation": ("--adaptive-rules", "--no-auto-layers"),
    "combined": ("--adaptive-rules",),
    "single_attempt": ("--no-auto-layers",),
}

#: Per-board extra flags.  ``{out}`` expands to the run's output directory.
BOARDS: dict[str, tuple[str, ...]] = {
    "routable": (),
    "partial": ("--max-layers", "2", "--export-failed-nets", "{out}/failed_nets.json"),
}

#: The banner each tail prints first -- an independent check that a run
#: really took the path its key names, so a dispatch change cannot quietly
#: re-point an entry at a different tail and regenerate "green".
PATH_BANNERS = {
    "layer_escalation": "KiCad PCB Autorouter - Layer Escalation Mode",
    "rule_relaxation": "KiCad PCB Autorouter - Adaptive Rules Mode",
    "combined": "KiCad PCB Autorouter - Combined Escalation Mode",
    "single_attempt": "KiCad PCB Autorouter",
}

ESCALATION_PATHS = ("layer_escalation", "rule_relaxation", "combined")

_COPPER_KINDS = ("segment", "via", "arc")
_RULE = re.compile(r"^={20,}$")
_DASH_HEADER = re.compile(r"^(-{3}|={3}) (.+?) (-{3}|={3})$")
_NOISE_HEADER = re.compile(r"^Iteration \d+\b")


# ---------------------------------------------------------------------------
# Running one entry path in-process
# ---------------------------------------------------------------------------


def _cpp_available() -> bool:
    try:
        from kicad_tools.router.cpp_backend import is_cpp_available
    except ImportError:
        return False
    return bool(is_cpp_available())


@contextlib.contextmanager
def _without_kicad_cli():
    """Make every kicad-cli lookup report "not installed" for the run."""
    from kicad_tools.cli import runner

    not_found = runner.KiCadCLILookup(None, runner.KICAD_CLI_NOT_FOUND)
    runner.find_kicad_cli.cache_clear()
    try:
        with mock.patch.object(runner, "_locate_kicad_cli_uncached", return_value=not_found):
            yield
    finally:
        runner.find_kicad_cli.cache_clear()


def run_route(board: str, path_key: str, workdir: Path, *extra: str) -> tuple[int, str, Path]:
    """Route ``board`` through ``path_key`` in-process.

    Returns ``(exit_code, combined stdout+stderr with workdir masked, outdir)``.
    The input board is copied into ``workdir`` so nothing can be written next
    to the committed fixture.
    """
    from kicad_tools.cli.route_cmd import main as route_main

    indir = workdir / "in"
    outdir = workdir / "out"
    indir.mkdir(parents=True)
    outdir.mkdir(parents=True)
    pcb = indir / f"{board}.kicad_pcb"
    shutil.copyfile(FIXTURE_DIR / f"{board}.kicad_pcb", pcb)

    board_args = [a.replace("{out}", str(outdir)) for a in BOARDS[board]]
    argv = [
        str(pcb),
        "-o",
        str(outdir / "out.kicad_pcb"),
        *COMMON_ARGS,
        *ENTRY_PATHS[path_key],
        *board_args,
        *extra,
    ]
    buf = io.StringIO()
    with _without_kicad_cli(), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = route_main(argv)
    text = buf.getvalue()
    for root in {str(workdir), str(workdir.resolve())}:
        text = text.replace(root, "<WORK>")
    return rc, text, outdir


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+', text)


def _parse(tokens: list[str]) -> list:
    """Minimal s-expression parser (independent of kicad_tools' own parser,
    so a parser change cannot silently move the digest)."""
    stack: list[list] = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(tok[1:-1] if tok.startswith('"') else tok)
    return stack[0]


def _canon_atom(atom: str) -> str:
    try:
        return f"{float(atom):.4f}"
    except ValueError:
        return atom


#: The geometric / electrical fields of a copper item.  Everything else
#: (``uuid``, ``tstamp``, ``locked``, and the KiCad 10 via ``capping`` /
#: ``covering`` / ``plugging`` / ``filling`` attributes that a kicad-cli
#: re-save adds) is dropped, so the digest moves only when copper moves.
_COPPER_FIELDS = ("start", "mid", "end", "at", "width", "size", "drill", "layer", "layers", "net")


def _canon(item: list, net_names: dict[str, str]) -> list:
    out: list = [item[0]]
    for child in item[1:]:
        if not isinstance(child, list) or not child or child[0] not in _COPPER_FIELDS:
            continue
        if child[0] == "net":
            # ``(net 3)`` -> ``(net "/C")``: robust to net renumbering.
            out.append(["net", *(net_names.get(v, v) for v in child[1:])])
        else:
            out.append([child[0], *(_canon_atom(v) for v in child[1:] if isinstance(v, str))])
    return out


def copper_digest(pcb_path: Path) -> dict:
    """Counts plus a sha256 over the sorted, normalised copper items."""
    if not pcb_path.exists():
        return {"present": False}
    tree = _parse(_tokenize(pcb_path.read_text()))
    root = tree[0]
    net_names = {
        n[1]: n[2] for n in root[1:] if isinstance(n, list) and len(n) >= 3 and n[0] == "net"
    }
    items: dict[str, list[str]] = {k: [] for k in _COPPER_KINDS}
    for child in root[1:]:
        if isinstance(child, list) and child and child[0] in items:
            items[child[0]].append(json.dumps(_canon(child, net_names)))
    blob = "\n".join(line for k in _COPPER_KINDS for line in sorted(items[k]))
    return {
        "present": True,
        "segments": len(items["segment"]),
        "vias": len(items["via"]),
        "arcs": len(items["arc"]),
        "sha256": hashlib.sha256(blob.encode()).hexdigest(),
    }


def report_headers(text: str) -> list[str]:
    """Section headers in first-seen order (see module docstring)."""
    lines = [ln.strip() for ln in text.splitlines()]
    found: list[str] = []
    for i, ln in enumerate(lines):
        title = None
        m = _DASH_HEADER.match(ln)
        if m:
            title = m.group(2)
        elif (
            ln
            and not _RULE.match(ln)
            and 0 < i < len(lines) - 1
            and _RULE.match(lines[i - 1])
            and _RULE.match(lines[i + 1])
        ):
            title = ln
        if title and not _NOISE_HEADER.match(title) and title not in found:
            found.append(title)
    return found


def observe(board: str, path_key: str, workdir: Path) -> dict:
    rc, text, outdir = run_route(board, path_key, workdir)
    return {
        "exit_code": rc,
        "copper": copper_digest(outdir / "out.kicad_pcb"),
        "headers": report_headers(text),
        "files": sorted(p.name for p in outdir.iterdir()),
    }


def _load_goldens() -> dict:
    return json.loads(GOLDENS_PATH.read_text())


def _strip_notes(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def field_diff(observed: dict, golden: dict) -> str:
    """One line per differing leaf, so a drift names its field (issue #7626).

    ``copper`` is compared per sub-key (``sha256`` alone differing with equal
    counts means same topology, different coordinates); ``headers`` and
    ``files`` report what was added/missing rather than dumping both lists.
    """
    lines: list[str] = []
    for key in sorted(set(observed) | set(golden)):
        got, want = observed.get(key), golden.get(key)
        if got == want:
            continue
        if isinstance(got, dict) and isinstance(want, dict):
            for sub in sorted(set(got) | set(want)):
                if got.get(sub) != want.get(sub):
                    lines.append(
                        f"  {key}.{sub}: golden={want.get(sub)!r} observed={got.get(sub)!r}"
                    )
        elif isinstance(got, list) and isinstance(want, list):
            lines.append(
                f"  {key}: missing={[x for x in want if x not in got]} "
                f"unexpected={[x for x in got if x not in want]}"
                + ("" if set(got) != set(want) else " (same members, different order)")
            )
        else:
            lines.append(f"  {key}: golden={want!r} observed={got!r}")
    return "\n".join(lines)


# Boards whose copper digest depends on the host CPU architecture (#7621).
ARCH_DEPENDENT_BOARDS = frozenset({"partial"})


def _host_arch() -> str:
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)


def _resolve_for_host(golden: dict) -> dict:
    """Collapse a per-architecture ``copper.sha256`` mapping to the host's digest."""
    sha = golden.get("copper", {}).get("sha256")
    if not isinstance(sha, dict):
        return golden
    arch = _host_arch()
    resolved = sha.get(arch, f"<no golden for host arch {arch!r}; have {sorted(sha)}>")
    return golden | {"copper": golden["copper"] | {"sha256": resolved}}


# ---------------------------------------------------------------------------
# The golden test
# ---------------------------------------------------------------------------

requires_cpp = pytest.mark.skipif(
    not _cpp_available(),
    reason="C++ router backend not built (uv run kct build-native); goldens pin cpp copper",
)

CASES = [(b, p) for b in BOARDS for p in ENTRY_PATHS]


@requires_cpp
@pytest.mark.parametrize(("board", "path_key"), CASES, ids=[f"{b}-{p}" for b, p in CASES])
def test_entry_path_matches_golden(board: str, path_key: str, tmp_path: Path) -> None:
    golden = _load_goldens()[board][path_key]
    observed = observe(board, path_key, tmp_path)

    # Independent of the golden: the run took the tail its key names.
    assert observed["headers"], "no report headers captured"
    assert PATH_BANNERS[path_key] in observed["headers"], observed["headers"]
    if path_key == "single_attempt":
        assert not set(observed["headers"]) & {PATH_BANNERS[k] for k in ESCALATION_PATHS}, observed[
            "headers"
        ]

    assert observed == _strip_notes(_resolve_for_host(golden)), (
        f"kct route {board}/{path_key} drifted from tests/fixtures/route_entry_paths/"
        f"goldens.json.  Differing fields:\n{field_diff(observed, _strip_notes(_resolve_for_host(golden)))}\n"
        "If the change is intended, regenerate with "
        "`uv run python tests/test_route_entry_path_goldens.py --update` and name the "
        "changed entries in the PR description (issue #7377).\n"
        f"observed: {json.dumps(observed, indent=1)}"
    )


def test_field_diff_names_each_differing_field() -> None:
    golden = {"exit_code": 2, "copper": {"segments": 11, "sha256": "a"}, "files": ["x", "y"]}
    observed = {"exit_code": 2, "copper": {"segments": 11, "sha256": "b"}, "files": ["x", "z"]}
    out = field_diff(observed, golden)
    assert "copper.sha256: golden='a' observed='b'" in out
    assert "copper.segments" not in out
    assert "exit_code" not in out
    assert "files: missing=['y'] unexpected=['z']" in out
    assert field_diff(golden, golden) == ""


def test_goldens_cover_every_case() -> None:
    goldens = _load_goldens()
    assert sorted(goldens) == sorted(BOARDS)
    for board in BOARDS:
        assert sorted(goldens[board]) == sorted(ENTRY_PATHS), board


def test_adaptive_rules_with_explicit_layers_is_a_usage_error(tmp_path: Path) -> None:
    """``--adaptive-rules --layers 2`` exits 1 before routing; rule relaxation
    is reached with ``--adaptive-rules --no-auto-layers`` (see module docstring)."""
    rc, text, outdir = run_route(
        "routable", "layer_escalation", tmp_path, "--adaptive-rules", "--layers", "2"
    )
    assert rc == 1
    assert "--adaptive-rules cannot be used with --layers 2" in text
    assert list(outdir.iterdir()) == []


# ---------------------------------------------------------------------------
# Former divergences (#7377 B1-B4): now fixed or warned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path_key", ESCALATION_PATHS)
def test_export_failed_nets_written_on_escalation_paths(path_key: str) -> None:
    """Reads the committed ``partial`` goldens (no routing).  The run passed
    ``--export-failed-nets <out>/failed_nets.json`` and failed net ``/D``."""
    entry = _load_goldens()["partial"][path_key]
    assert entry["exit_code"] == 2
    assert "failed_nets.json" in entry["files"]


def test_export_failed_nets_written_on_single_attempt_path() -> None:
    """Control: the single-attempt golden has the file."""
    entry = _load_goldens()["partial"]["single_attempt"]
    assert entry["exit_code"] == 2
    assert "failed_nets.json" in entry["files"]


#: (flag, evidence regex on the single-attempt path, evidence regex the
#: default path must show once fixed -- the flag acted on, or a loud warning
#: naming it).
_SINGLE_ONLY_FLAGS = {
    "B1-stitch-power-planes": (
        "--stitch-power-planes",
        r"--- Stitching Power Planes ---",
        r"--- Stitching Power Planes ---",
    ),
    "B2-progressive-clearance": (
        "--progressive-clearance",
        r"=== Progressive Clearance Relaxation ===",
        r"(?i)progressive[ -]clearance",
    ),
    "B3-bus-routing": (
        "--bus-routing",
        r"Bus:\s+enabled",
        r"(?i)\bbus[ -](routing|:|\s+enabled)",
    ),
}


@requires_cpp
@pytest.mark.parametrize("case", sorted(_SINGLE_ONLY_FLAGS))
def test_single_only_flag_acts_on_single_attempt_path(case: str, tmp_path: Path) -> None:
    """Control for B1-B3: on ``--no-auto-layers`` the flag is honoured."""
    flag, single_evidence, _ = _SINGLE_ONLY_FLAGS[case]
    rc, text, _ = run_route("routable", "single_attempt", tmp_path, flag)
    assert rc == 0, text[-3000:]
    assert re.search(single_evidence, text), text[-3000:]


@requires_cpp
@pytest.mark.parametrize("case", sorted(_SINGLE_ONLY_FLAGS))
def test_single_only_flag_acts_or_warns_on_default_path(case: str, tmp_path: Path) -> None:
    flag, _, default_evidence = _SINGLE_ONLY_FLAGS[case]
    rc, text, _ = run_route("routable", "layer_escalation", tmp_path, flag)
    assert rc == 0, text[-3000:]
    assert PATH_BANNERS["layer_escalation"] in text
    assert re.search(default_evidence, text), f"{flag} left no trace on the default path"


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------

_NOTES = {
    ("partial", "single_attempt"): (
        "#7377 D7: only path writing the out_partial.* snapshot (undecided)"
    ),
}


def regenerate() -> dict:
    existing = _load_goldens() if GOLDENS_PATH.exists() else {}
    goldens: dict = {}
    for board in BOARDS:
        goldens[board] = {}
        for path_key in ENTRY_PATHS:
            with tempfile.TemporaryDirectory(prefix="route-golden-") as tmp:
                entry = observe(board, path_key, Path(tmp))
            note = _NOTES.get((board, path_key))
            if board in ARCH_DEPENDENT_BOARDS:
                # Keep the other architectures' digests; replace only this host's.
                prior = existing.get(board, {}).get(path_key, {}).get("copper", {}).get("sha256")
                by_arch = dict(prior) if isinstance(prior, dict) else {}
                by_arch[_host_arch()] = entry["copper"]["sha256"]
                entry["copper"] = entry["copper"] | {"sha256": dict(sorted(by_arch.items()))}
            goldens[board][path_key] = ({"_note": note} if note else {}) | entry
            print(f"{board}/{path_key}: rc={entry['exit_code']} {entry['copper']}", file=sys.stderr)
    return goldens


if __name__ == "__main__":
    if "--update" not in sys.argv[1:]:
        sys.exit("usage: python tests/test_route_entry_path_goldens.py --update")
    if not _cpp_available():
        sys.exit("C++ router backend not built: run `uv run kct build-native` first")
    data = regenerate()
    GOLDENS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {GOLDENS_PATH}", file=sys.stderr)
