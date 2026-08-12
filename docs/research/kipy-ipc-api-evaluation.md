# Evaluation: kipy (official KiCad Python API) as a Parser Cross-Check — via kicad-mcp-kipy

**Issue**: #4779
**Date**: 2026-08-11
**Packages evaluated**:
- `kicad-python` (`kipy`) 0.7.1 — https://gitlab.com/kicad/code/kicad-python (official, The KiCad Development Team)
- `kicad-mcp-kipy` 0.1.3 — https://github.com/mnori0211/kicad-mcp-kipy (third-party MCP wrapper; **metadata inspection only, never installed or run**)

**Method**: PyPI JSON API metadata for both packages; the official
`kicad_python-0.7.1-py3-none-any.whl` wheel downloaded and unpacked in a
scratch directory outside the repo; `kicad-python==0.7.1` installed into a
throwaway venv (also outside the repo) for one empirical connection probe.
Nothing was added to this repository's environment or `pyproject.toml`.

## TL;DR

**Do not adopt `kipy` in any form. Do not adopt `kicad-mcp-kipy` at all.**

`kipy` is a live-editor RPC client, not a file parser: a `kipy.board.Board`
is a handle to a document *currently open in a running KiCad editor* (with
**Preferences → Scripting → Enable IPC API** ticked), and every accessor is
an RPC over an NNG socket at `/tmp/kicad/api.sock`. There is no offline
path — no `from_file`, no `load`, no `open_document` anywhere in the
package (verified against the unpacked 0.7.1 wheel). It therefore cannot
participate in anything this project actually runs: headless board
generation (`boards/*/generate_design.py`), CI board regression (Board
00–07 E2E, Match-Group, Diff-Pair), or unattended `kct route` runs on
display-less runners.

**No `kipy`-based check can ever be a CI gate**, because the IPC API it
depends on only exists inside a running, operator-attended KiCad editor
session with the API preference enabled and a document open — CI runners
have none of these.

**`kicad-mcp-kipy` itself has no role for this project**: it is a
read-mostly MCP wrapper around `kipy` that inherits the live-editor
constraint wholesale, duplicates a capability we already ship natively
(`src/kicad_tools/mcp/` — see Option C), and at evaluation time was
hours-old single-author code with four releases published within ~85
minutes. Its only value to us was pointing at `kipy`, and that evaluation
is now done.

`kipy` also cannot run DRC (F2 below), so it does not augment — let alone
replace — the manufacturing sign-off cross-gate `kicad-cli pcb drc
--refill-zones`, which remains the only headless second-opinion path and is
strictly more capable for the cross-check job this issue originally
pitched.

## Verified Facts

### Package metadata (PyPI JSON API, re-verified 2026-08-11)

The original issue body's facts table conflated the wrapper's metadata with
`kipy`'s. Correctly separated:

| Field | `kicad-python` (`kipy`) 0.7.1 | `kicad-mcp-kipy` 0.1.3 |
|---|---|---|
| Author | The KiCad Development Team | Noriko Miyazaki (single author) |
| Released | 2026-04-18 (14 releases since 0.0.1) | 2026-08-11 — all 4 releases (0.1.0→0.1.3) within ~85 min, 17:05–18:29 UTC |
| License | MIT ("Copyright The KiCad Developers") | MIT |
| Requires-Python | >=3.9 | >=3.11 |
| Dependencies | `jsonschema>=4.23,<5`, `protobuf>=5.29,<6`, `pynng>=0.9,<0.10`, `typing_extensions>=4.13.2; py<3.13` | `kicad-python==0.7.1` (exact pin), `mcp[cli]>=1.3.0,<2.0.0`, `psutil>=6.0.0` |
| Canonical home | https://gitlab.com/kicad/code/kicad-python | https://github.com/mnori0211/kicad-mcp-kipy |

`kipy` is the mature, official piece; the wrapper is a days-old
third-party shim. Everything below evaluates `kipy`; the wrapper adds
nothing on top of it that we could use (see Option-by-Option Disposition).

### F1 — `kipy` has no offline path; it cannot read a `.kicad_pcb` from disk

Verified twice — once by the curator during enrichment, and independently
re-verified for this write-up from the unpacked 0.7.1 wheel:

- `kipy/board.py:257` — `class Board` with
  `def __init__(self, kicad: KiCadClient, document: DocumentSpecifier)`.
  A `Board` is a handle to an *open editor document*; every accessor
  (`get_tracks`, `get_footprints`, `get_nets`, `get_as_string`, …) issues
  an RPC over the socket.
- `grep -rn "from_file\|def load\|open_document" kipy/*.py` across the
  wheel finds **no** file-loading entry point. The only file-shaped verbs
  are `Board.save_as` (`kipy/board.py:290`) and `Schematic.save_as`, which
  write *out of* a live editor. The sole board entry point is
  `KiCad.get_board()` (`kipy/kicad.py:225`), which resolves whatever
  document the running editor already has open via
  `get_open_documents(DOCTYPE_PCB)`.
- Empirical (throwaway venv, no KiCad running): `KiCad(timeout_ms=3000)`
  raises `ConnectionError: Failed to connect to KiCad: Connection
  refused`; `/tmp/kicad/api.sock` does not exist.

**Consequence**: any `kipy`-based capability is local/operator-attended
only. It can never run in CI, in `boards/*/generate_design.py`, or in the
Board 00–07 E2E jobs. This repo already keeps local-only fixtures (chorus,
softstart) deliberately out of CI, so "useful locally, never a gate" is a
recognised shape — but here even the local utility is thin (see F2 and the
rules-probe section).

### F2 — `kipy` cannot run DRC

The full board command surface in the wheel's
`kipy/proto/board/board_commands_pb2.pyi` (all 40 command/response classes
enumerated) contains `GetNets`, `GetBoardStackup`, `GetItemsByNet`,
`GetItemsByNetClass`, `RefillZones`, `InjectDrcError`, `DrcSeverity`,
`UpdateBoardStackup`, layer/appearance getters and setters — and **no
run-DRC command**. `InjectDrcError` pushes a marker *into* the editor; it
does not evaluate rules.

`kicad-cli pcb drc --refill-zones` therefore remains the only
second-opinion DRC path. It is headless, it evaluates KiCad's full
resolved-rule engine, and manufacturing sign-off already mandates it
(it has caught a real defect — an H11AA1 opto input tied straight to AC
with its limiters bypassed — that `kct check` filed under "LVS
artifacts"). `kipy` is strictly less capable than the gate we already run.

### F3 — this repo already ships its own MCP server; "zero MCP surface" was false

The original issue body asserted zero MCP surface in the repo. In fact
`src/kicad_tools/mcp/` ships a complete FastMCP server —
`create_fastmcp_server` and `run_server` in
`src/kicad_tools/mcp/server.py`, a tool registry under
`src/kicad_tools/mcp/tools/`, docs under `docs/mcp/`, and an `mcp` extra
in `pyproject.toml`. It wraps *our* differentiators (routing, DRC,
manufacturing analysis) — exactly the capabilities `kicad-mcp-kipy` does
not and cannot offer.

### F4 — the repo's existing `kipy` mention pointed at the wrong project

`README.md` § "Related Projects" listed `kipy` with a link to
`https://github.com/zeodotdev/zeo-python`, described it as "KiCad 9.0+"
bindings, and implied it could push results into KiCad without stating the
live-editor constraint. The canonical home is
`https://gitlab.com/kicad/code/kicad-python` (per the package's own
`project_urls`). Corrected in the same PR as this document.

### F5 — "no mutation tools" is true of the MCP wrapper, false of `kipy`

`kipy` itself exposes mutation: `Board.save_as` (`kipy/board.py:290`),
`begin_commit` (`:310`), `push_commit` (`:325`), `drop_commit` (`:334`),
`create_items` (`:341`), plus setters throughout `kipy.board_rules`. The
"read-mostly, no mutation" observation in the original body is correct
only about `kicad-mcp-kipy`'s exposed MCP tool list. This does not rescue
adoption — F1 still binds — but the record should not repeat the wrong
reason.

### F6 — what `kipy` exposes that we have no other programmatic access to

`kipy/board_rules.py` exposes KiCad's *resolved* design-rule set in
integer nanometres: `MinimumConstraints` (`kipy/board_rules.py:40`) with
sixteen fields — `min_clearance`, `min_groove_width`,
`min_connection_width`, `min_track_width`, `min_via_annular_width`,
`min_via_size`, `min_through_drill`, `min_microvia_size`,
`min_microvia_drill`, `copper_edge_clearance`, `hole_clearance`,
`hole_to_hole_min`, `silk_clearance`, `min_resolved_spokes`,
`min_silk_text_height`, `min_silk_text_thickness` — each a `value_nm`
integer as KiCad resolves it, plus `CustomRule`, `CustomRuleConstraint`,
`DrcSeveritySetting` and `DrcExclusion`.

Our side is a hand-rolled **text** parse of `.kicad_dru`: `import_dru`,
`_extract_design_rules`, `_extract_rule`, `_extract_constraint` and
`_parse_value_with_unit` in `src/kicad_tools/cli/mfr_dru.py`. Notably,
`_parse_value_with_unit` does **not convert units** — it returns
`{"value": 4.0, "unit": "mil"}` as-is, leaving conversion to consumers.
Unit conversion at a text-parse boundary is precisely where the
~0.1000-vs-0.1016 mm marginal class lives (0.1016 mm *is* 4 mil), which is
the failure mode the existing `kicad-cli` cross-gate exists to catch.
This is the one hypothesis worth an attended probe (next section) — and
the only surviving argument for touching `kipy` at all.

## The Rules Probe

**Not run — requires operator-attended KiCad session.**

The probe requires KiCad 10 running with **Preferences → Scripting →
Enable IPC API** ticked and one of `boards/*/output/*_routed.kicad_pcb`
open in the PCB editor. No operator session was available for this
evaluation; per the issue's operator-gated preconditions this section
ships marked not-run and the evaluation is complete without it.

For a future attended session, the probe is: read
`BoardDesignRules`/`MinimumConstraints` via `kipy` for one board, tabulate
each of the sixteen `value_nm` fields against the corresponding value
produced by `_extract_design_rules` / `_parse_value_with_unit` in
`src/kicad_tools/cli/mfr_dru.py` (converting units explicitly), and mark
agree/disagree per field. Any confirmed disagreement is a `kicad-tools`
bug to file with a reproducer — the remedy is a parser fix, **not** a
`kipy` dependency.

No confirmed `kicad-tools` defect was found in this evaluation (the probe
that could find one was not run), so no follow-up defect issue is filed.

## Constraint Analysis: can any `kipy`-based check be a CI gate?

**No — no `kipy`-based check can ever be a CI gate, because `kipy` can
only talk to a live, operator-attended KiCad editor (running GUI, IPC API
preference enabled, document open), none of which exists on a headless CI
runner, and it has no file-reading fallback (F1).**

The same constraint bounds local use: a `kipy` cross-check cannot be part
of `kct check`, `kct route`, board regeneration, or any scripted pipeline
this project ships. At most it is a one-off attended diagnostic — and for
the geometry questions (footprint/track/via counts, net names, stackup)
the headless `kicad-cli` round-trip already covers the same ground against
the same official engine, with the board corpus regression-tested on top.

## Relationship to #3803 (Router/DRC fidelity epic)

**A `kipy` resolved-rules read would add nothing to #3803 beyond the
existing `kicad-cli pcb drc --refill-zones` gate: that gate already
evaluates KiCad's full resolved-rule engine headlessly and reports the
actual violations, which strictly subsumes a raw rules dump — so #3803 is
fully served by the existing headless gate.**

In detail: #3803's problem is *evaluation* fidelity (`kct` reports PASS
where native KiCad DRC finds 400+ violations including shorts). `kipy`
cannot evaluate rules at all (F2); the most it could contribute is an
attended-session diagnostic of *which resolved constraint value* differs
between KiCad and our `.kicad_dru` text parse, narrowing a disagreement
the `kicad-cli` gate has already detected. That is a debugging
convenience, not a detection capability, and it cannot run where #3803's
gate needs to run.

## Option-by-Option Disposition

Options as framed in the original issue body:

- **Option A (`kipy` as a validation cross-check): do not pursue —**
  F1 makes it operator-attended-only and F2 means it cannot run DRC, so it
  can neither join CI nor add anything over the headless `kicad-cli pcb
  drc --refill-zones` gate we already mandate; the one residual idea (the
  resolved-rules probe) is recorded above as an attended one-off, not a
  capability.
- **Option B (`kipy` as a parser-truth oracle in tests): do not pursue —**
  a test-time oracle that needs a running GUI editor with a document open
  cannot execute under `pytest` in CI or on any unattended runner, which
  is where tests live; the geometry-differential idea it would serve is
  already covered headlessly by `kicad-cli` round-trips over the
  regression-tested board corpus.
- **Option C (an MCP server for kicad-tools): already shipped —** not an
  open option; `src/kicad_tools/mcp/` (see `create_fastmcp_server` and
  `run_server` in `src/kicad_tools/mcp/server.py`, tool registry under
  `src/kicad_tools/mcp/tools/`, docs under `docs/mcp/`, `mcp` extra in
  `pyproject.toml`) already exposes our routing/DRC/manufacturing
  capabilities over MCP, which is precisely the surface `kicad-mcp-kipy`
  lacks.
- **Option D (adopting `kipy` as a general dependency): do not pursue —**
  it requires a live editor for every operation (F1), pins us to the IPC
  API's version coupling for zero headless benefit, and would add
  `protobuf`/`pynng` weight to a toolchain whose entire value proposition
  is unattended operation.

**And `kicad-mcp-kipy` itself: `kicad-mcp-kipy` has no role for this
project — not as a dependency, not as a tool, not as a design source —
because it inherits `kipy`'s live-editor constraint, offers a read-mostly
tool list that overlaps nothing we need, duplicates the MCP surface we
already ship natively, and is an hours-old single-author package whose
only value here was pointing at `kipy`.**

### KiCad-version coupling risk

`kipy` tracks the official IPC API, which is versioned with KiCad itself
(the wheel carries `kipy/kicad_api_version.py`; the wrapper pins
`kicad-python==0.7.1` exactly, illustrating how consumers end up
version-locked). We currently support KiCad 10; KiCad 11 is not expected
until ~Feb 2027. Adopting `kipy` would add a second KiCad-version coupling
surface (IPC protocol compatibility) on top of the file-format coupling we
already manage — and unlike the file format, the IPC protocol offers no
"parse the old format" escape hatch: the client and the running editor
must agree. This is an additional reason options A/B/D stay closed even
for local-only use.

## Recommendation

**Adopt nothing.** Specifically:

1. **No `kipy` dependency** in any form — not runtime, not `dev`, not an
   extra. Options A, B and D are closed by F1/F2; option C was already
   built natively.
2. **No `kicad-mcp-kipy`** — no role at all (verdict sentence above).
3. **The `README.md` "Related Projects" entry is corrected** (same PR) to
   point at the canonical `https://gitlab.com/kicad/code/kicad-python` and
   to state the live-editor/IPC constraint.
4. **If an operator ever wants the rules probe**, the recipe is in "The
   Rules Probe" above; any disagreement it finds is a `kicad-tools` bug to
   file and fix on our side. It does not reopen the adoption question.

This document exists so the next person who finds `kicad-mcp-kipy` (or
`kipy`) does not re-litigate the evaluation: the load-bearing fact is that
**`kipy` is an RPC client to a running editor, not a file parser**, and
that single fact forecloses every use this headless toolchain has for it.
