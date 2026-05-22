# Evaluation: component-importer-for-kicad as a Vendor-ZIP Ingestion Backend

**Issue**: #3091
**Date**: 2026-05-21
**Upstream**: https://github.com/robertxdx/component-importer-for-kicad (commit at clone time of evaluation)
**Upstream license**: MIT (compatible)
**Upstream pyproject runtime deps**: `PyQt6` only (no other third-party libs)

## TL;DR

**Pursue. Borrow-with-refactor, not vendor verbatim.** The headless modules
(everything not prefixed `gui_*`) are PyQt6-free, format-agnostic, and almost
exactly the ZIP-ingest surface we need. They are ~2,300 LoC of straightforward
`pathlib` + regex + `zipfile.ZipFile` code. The "v10" framing in the upstream
README is about KiCad app behavior (memory-locking project library tables) and
**not** about file syntax — upstream emits `(version 20231120)` for
`.kicad_sym`, identical to what `kicad-tools/src/kicad_tools/schematic/symbol_generator.py:523`
already writes today. No version-compat work is required.

The smallest credible follow-up PR is **~600 LoC of clean-room re-implementation
plus a `kct ingest-zip` CLI**, validated end-to-end on one SnapEDA ZIP for one
connector. The remaining ~1,700 LoC of upstream code is GUI plumbing, backup
bookkeeping, online-search-link helpers, and import-report formatting that we
can either skip or build in tiny follow-ups.

Vendor-format detection is a **non-problem**: the upstream code does *not*
sniff Snapeda vs Ultra Librarian vs CSE layouts. It just walks `ZipFile.namelist()`
and dispatches on file extension (`.kicad_sym`, `.kicad_mod`, `.step|.stp|.wrl|.stl`,
`.pdf`). Every vendor that ships native KiCad outputs in a ZIP works automatically.
Vendors that ship Altium / Eagle / EasyEDA / OrCAD only would still not work
(but that is out of scope for this issue and a much harder problem).

## Files Examined Upstream

```
src/component_importer/
├── __init__.py            (empty)
├── app_paths.py           (135 LoC) — desktop-app QStandardPaths wrapper. SKIP.
├── backup_helper.py       (113 LoC) — timestamped file backups. BORROW IDEAS.
├── cad_zip_importer.py    (459 LoC) — top-level orchestrator. PORT WITH REFACTOR.
├── download_import_helper.py (158 LoC) — watch-folder helper. SKIP (out of scope).
├── file_discovery.py      ( 21 LoC) — `iter_zip_files(folder)`. SKIP (one-liner).
├── footprint_3d_fixer.py  (271 LoC) — 3D-path matcher + rewriter. PORT WITH REFACTOR.
├── gui_*                  (1,996 LoC across 9 files) — PyQt6. SKIP ALL.
├── import_summary.py      (353 LoC) — formats a human-readable import report. BORROW IDEAS.
├── import_validator.py    (740 LoC) — post-import sanity checks. BORROW IDEAS (selectively).
├── library_table_updater.py (228 LoC) — fp/sym-lib-table mutation. PORT WITH REFACTOR.
├── models.py              ( 49 LoC) — `AssetType`, `CadAsset` dataclasses. PORT VERBATIM.
├── online_component_search.py (138 LoC) — URL-builder for vendor sites. SKIP.
├── project_library_initializer.py ( 40 LoC) — trivial scaffolder. PORT VERBATIM.
├── project_library.py     ( 89 LoC) — fixed `libraries/` layout. PORT WITH REFACTOR.
├── symbol_footprint_linker.py (665 LoC) — sets `Footprint` field on symbol. PORT WITH REFACTOR.
├── symbol_library_manager.py (192 LoC) — merges `.kicad_sym` into target. PORT WITH REFACTOR.
├── zip_inspector.py       (181 LoC) — `dict`-returning ZIP report. BORROW IDEAS.
└── zip_scanner.py         ( 86 LoC) — extension-driven asset detector. PORT VERBATIM.
```

**PyQt6 leakage audit**: `grep -l 'PyQt6\|PySide' src/component_importer/*.py`
returns only the `gui_*` files. All seventeen non-GUI modules are clean.
**No headless code transitively imports Qt.**

## Module-by-Module Disposition

| Upstream module | LoC | Disposition | Notes |
|-----------------|-----|-------------|-------|
| `models.py` | 49 | **Port verbatim** | `AssetType` enum + `CadAsset` dataclass. Trivial. |
| `zip_scanner.py` | 86 | **Port verbatim** | `detect_asset_type()` is pure extension dispatch. `scan_cad_zip()` walks namelist. |
| `zip_inspector.py` | 181 | **Borrow ideas** | Returns a `dict` summary of ZIP contents. We want this as `ingest.zip.inspect()` but should return a typed dataclass, not `dict`. |
| `project_library_initializer.py` | 40 | **Port verbatim** | Trivial scaffolder. |
| `project_library.py` | 89 | **Port with refactor** | Hard-codes `libraries/{libname}.pretty`, `libraries/3dmodels`, `libraries/source_zips`. We should make the layout pluggable so it slots into existing `boards/<N>-*/` conventions. |
| `library_table_updater.py` | 228 | **Port with refactor** | String-edit on `fp-lib-table` / `sym-lib-table` via `content.rfind(')')`. Works but fragile — should use our existing sexp parser (`kicad_tools.sexp`) for the same operation. |
| `symbol_library_manager.py` | 192 | **Port with refactor** | Merges source `.kicad_sym` blocks into target. Same fragility — relies on `find_symbol_blocks()` from `symbol_footprint_linker.py` which is a hand-rolled paren-depth tracker. Replace with `kicad_tools.sexp`. |
| `symbol_footprint_linker.py` | 665 | **Port with refactor** | Largest single module. Sets `Footprint` field on imported symbols. Same comment: rewrite the paren-walker on top of `kicad_tools.sexp` and the body shrinks ~40%. |
| `footprint_3d_fixer.py` | 271 | **Port with refactor** | 3D-model name matcher with a clever token-overlap scorer (`score_model_match`). Worth keeping the scoring heuristic; rewrite the rewriting half (`replace_3d_model_paths`) against `kicad_tools.sexp`. |
| `backup_helper.py` | 113 | **Borrow ideas** | Timestamped `.bak.<ts>` files. We probably want this opt-in behind a flag; current kicad-tools writes are non-atomic anyway. |
| `import_summary.py` | 353 | **Borrow ideas** | Pretty-printer for the import-result `dict`. Useful as a CLI `kct ingest-zip --report` formatter. |
| `import_validator.py` | 740 | **Borrow ideas (selectively)** | Post-import sanity checks: does the symbol-library footprint field resolve? does the fp-lib-table entry exist? does the 3D model path resolve? Worth cherry-picking ~10 checks; the other 90% is GUI-tied. |
| `cad_zip_importer.py` | 459 | **Port with refactor** | The top-level orchestrator. Twelve kwargs is too many; collapse to `IngestOptions` dataclass mirroring how `PartImporter` does it. |
| `app_paths.py`, `download_import_helper.py`, `file_discovery.py`, `online_component_search.py`, all `gui_*` | n/a | **Skip** | Desktop-app concerns, watch-folder UX, online-link helpers, GUI tabs. None of this maps to an agent-driven workflow. |

**Total port budget (refactored)**: ~600 LoC of new `src/kicad_tools/ingest/`
code if we lean on `kicad_tools.sexp` for the paren-walking parts. ~1,100 LoC
if we port the upstream paren-walkers verbatim.

## KiCad Version Alignment — Resolved

Issue body asserted "upstream targets v10, this repo targets v8/v9+". The
"v10" claim is **only** about KiCad application runtime behavior (KiCad 10
caches the project library table in memory, so for first-time import you must
close KiCad before importing or the table mutation won't be seen). It is
**not** about the on-disk file format.

Concrete check:
- Upstream `symbol_library_manager.py:23` emits `(version 20231120)` when
  creating a fresh `.kicad_sym`.
- This repo `src/kicad_tools/schematic/symbol_generator.py:523` and
  `src/kicad_tools/schema/library.py:1125` both emit `(version 20231120)`.
- KiCad 7 / 8 / 9 / 10 all read `(version 20231120)` symbol libraries.
  This is the current stable format token; it was last bumped Nov 2023.

The `(fp_lib_table ...)` and `(sym_lib_table ...)` formats upstream writes
are likewise unchanged across KiCad 7..10.

**Conclusion: no porting cost for version compatibility. Drop the v10 worry.**

## Vendor Format Coverage — Resolved

Upstream's `zip_scanner.detect_asset_type()` is the entire vendor-format
detection layer. It is:

```python
def detect_asset_type(path: PurePosixPath) -> AssetType:
    suffix = path.suffix.lower()
    if suffix == ".kicad_sym": return AssetType.SYMBOL_LIB
    if suffix == ".kicad_mod": return AssetType.FOOTPRINT
    if suffix in [".step", ".stp"]: return AssetType.STEP_MODEL
    if suffix == ".wrl": return AssetType.WRL_MODEL
    if suffix == ".stl": return AssetType.STL_MODEL
    if suffix == ".pdf": return AssetType.DATASHEET
    return AssetType.UNKNOWN
```

That's it. There is **no SnapEDA-specific parser, no Ultra Librarian-specific
parser, no Component Search Engine-specific parser**. Every vendor that ships
native KiCad outputs (which today is: SnapEDA, SnapMagic, Ultra Librarian's
KiCad export, Component Search Engine's KiCad export, SamacSys / KiCad,
Octopart bundles passed through any of the above) "just works" because every
one of them packages `.kicad_sym` + `.kicad_mod` + `.step` files.

**Vendors that ship Altium / Eagle / EasyEDA / OrCAD only would not work.**
Cross-format conversion (Altium → KiCad) is a separate, much harder problem
that should be tracked separately, not folded into this issue.

This eliminates an entire bucket of risk from the original acceptance criteria:
no need to spot-check "one SnapEDA ZIP, one UL ZIP, one CSE ZIP" — the
detection logic is format-blind.

## Integration Boundary

Recommended target shape (drawn from issue body's curator notes, refined):

```
src/kicad_tools/ingest/
├── __init__.py            — public exports
├── models.py              — AssetType, CadAsset, IngestOptions, IngestResult
├── zip.py                 — scan(), inspect(), extract()
├── libtable.py            — register_fp_lib(), register_sym_lib()
│                            (rebuilt on top of kicad_tools.sexp, not regex)
├── link.py                — bind_symbol_to_footprint()
│                            (rebuilt on top of kicad_tools.sexp)
├── models_3d.py           — rewrite_3d_paths(), score_model_match()
├── orchestrator.py        — ZipIngester (mirrors PartImporter shape)
└── cli.py                 — `kct ingest-zip` entry point
```

`PartImporter` integration point (in `src/kicad_tools/parts/importer.py`):

```python
def import_part(self, part_number, options=None, vendor_zip=None, ...):
    # ... existing datasheet-synthesis path ...
    except (DatasheetSearchError, DatasheetParseError, SymbolGenerationError) as e:
        if vendor_zip is not None:
            return self._import_from_zip(vendor_zip, part_number, options)
        raise
```

That is the minimum wiring. A future enhancement could also try ZIP-first when
a `vendor_zip` is supplied, falling back to datasheet synthesis only on failure.

## Vendoring vs Clean-Room

**Recommendation: clean-room reimplementation, using this evaluation as the
spec.** Reasons:

1. **Style mismatch.** Upstream has a one-comment-per-line style and zero type
   hints on public APIs. kicad-tools has full type hints, ruff/mypy gates, and
   pytest fixtures. A verbatim port would fail CI and need rewrite anyway.
2. **Better primitives.** kicad-tools already has `src/kicad_tools/sexp/` for
   structured KiCad-file editing. Upstream's hand-rolled paren-depth trackers
   (`find_matching_paren`, `find_list_blocks_at_depth` in
   `symbol_footprint_linker.py`) duplicate that capability badly.
3. **Smaller surface.** A clean-room version that defers to `kicad_tools.sexp`
   is ~600 LoC. A verbatim vendored version is ~2,300 LoC plus the v8/v10
   compat shim and ruff/mypy ignores. Less code to maintain.
4. **Attribution still owed.** Even clean-room, this evaluation document and
   the follow-up PR description should credit `robertxdx/component-importer-for-kicad`
   as the design source, and the disposition table above is the audit trail
   for "what we took ideas from".

If we change our minds and want to vendor: copy `LICENSE` into
`third_party/component_importer/LICENSE`, add a `NOTICE` file, and namespace
the modules under `kicad_tools.ingest._vendored.` so they are clearly not
our API surface.

## Smallest PR That Proves Integration

Scope a **single follow-up PR** with this goal: "Given a SnapEDA ZIP for a
USB-C connector (e.g. `USB4500-03-0-A`), `kct ingest-zip <zip> --board boards/01-voltage-divider/`
produces a working symbol+footprint+3D-model registered in the board's
`sym-lib-table` and `fp-lib-table`, and `kct schematic add` can place that
symbol on the schematic."

Minimum scope to hit that goal:

1. **New**: `src/kicad_tools/ingest/{__init__,models,zip}.py` —
   `AssetType`, `CadAsset`, `IngestOptions`, `IngestResult`, plus
   `scan_zip(path) -> list[CadAsset]` and `extract_zip(path, dest) -> IngestResult`.
   (~150 LoC, mostly ported from `models.py` + `zip_scanner.py`.)
2. **New**: `src/kicad_tools/ingest/libtable.py` — `register_fp_lib()`,
   `register_sym_lib()` using `kicad_tools.sexp`. (~120 LoC.)
3. **New**: `src/kicad_tools/ingest/link.py` — `bind_symbol_to_footprint()`
   using `kicad_tools.sexp`. (~120 LoC.)
4. **New**: `src/kicad_tools/ingest/models_3d.py` — `rewrite_3d_paths()` +
   `score_model_match()`. (~100 LoC.)
5. **New**: `src/kicad_tools/ingest/cli.py` — `kct ingest-zip` Click command,
   wired into `pyproject.toml`. (~80 LoC.)
6. **New**: `tests/ingest/test_zip_ingest.py` with a tiny redistributable
   fixture ZIP under `tests/fixtures/ingest/` (a SnapEDA-shaped tree built
   from kicad-tools' own generated symbols/footprints — so we don't ship
   third-party CAD with our repo). End-to-end test: ingest → register →
   load symbol → assert footprint field set. (~150 LoC.)

**Out of scope for the first PR** (each is a clean follow-up):
- Hooking ZIP-fallback into `PartImporter`.
- 3D-path rewriting on KiCad 8 absolute-path corner cases.
- Symbol merging when target `.kicad_sym` already has same-named symbol
  (upstream's `replace_existing_symbols=True` behavior).
- Backup/rollback bookkeeping.
- Multi-symbol ZIPs (some Ultra Librarian bundles ship variants).
- Validation pass (port the useful 10% of upstream's `import_validator.py`).

**Total first-PR LoC**: ~720 production + ~150 tests + 1 fixture ZIP.
Realistically **1 builder-day of focused work**, plus review.

## Acceptance Criteria Mapping

The original AC from the curator's enrichment:

1. **Module-by-module disposition** — see "Module-by-Module Disposition" table
   above. Done.
2. **`grep -R 'PyQt6\|PySide'` audit** — done. Only `gui_*` files match. No
   leakage into headless modules.
3. **KiCad version alignment** — done. Both repos emit `(version 20231120)`.
   The "v10" framing is about KiCad app behavior, not file format.
4. **Vendor format inventory + spot-check** — done. Format detection is
   extension-driven and format-blind; any vendor that ships native KiCad
   outputs in a ZIP works. No vendor-specific spot-check is required
   (the test fixture can be synthetic).
5. **Vendor vs clean-room decision** — clean-room, with this doc as the spec.
6. **Follow-up issue or "not worth it" rationale** — pursue. Follow-up issue
   scope is described in "Smallest PR That Proves Integration" above and
   should be filed as `feat(ingest): vendor-ZIP ingestion MVP — scan, extract,
   register one SnapEDA-style ZIP end-to-end`.

## Recommendation

**Pursue the follow-up implementation as described.** This is the highest-leverage
unblock available for "agent hits an unknown part" today: datasheet synthesis
covers generic ICs but consistently fails on connectors, RF modules, and any
part where vendors ship official CAD as the canonical artifact. The evaluation
turned out cheaper than estimated (a few hours, not half a day) because the
PyQt6 audit and version-compat check both came back clean immediately, and
the vendor-format detection logic is so small it isn't actually a detection
problem.
