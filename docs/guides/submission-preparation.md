# Offline assembly submission preparation

`kicad_tools.export.submission_plan` implements milestone A of #5142: a local,
read-only handoff of an existing manufacturing bundle. It does not export a
board again, choose parts, contact a supplier, or submit an order. Local integrity
means the supplied evidence matches the input bytes; it does **not** establish
board readiness, human approval, factory matching, available inventory, reserved
stock, or feeder/attrition requirements.

Milestone B (exact-ID inventory observations) remains outstanding and depends on
#5033/#5034, tracked by PRs #5115/#5090. `refresh_inventory(plan)` currently raises
`NotImplementedError` before accessing anything. Preparation has no client,
credential, or transport argument, and does not read supplier environment
variables. No live availability evidence is produced.

## Python API

```python
from pathlib import Path
from kicad_tools.export.submission_plan import (
    FactorySettings,
    SourceEvidence,
    prepare_submission,
    verify_submission,
)

# Obtain these hashes/sizes from your independently retained frozen-source
# evidence. Do not silently recompute trusted evidence from changed inputs.
source_records = {
    "pcb": SourceEvidence("board.kicad_pcb", expected_pcb_sha256, expected_pcb_size),
    "schematic": SourceEvidence("board.kicad_sch", expected_sch_sha256, expected_sch_size),
}
plan = prepare_submission(
    bundle_root=Path("release/manufacturing"),
    manifest_path="manifest.json",
    # Explicit complete set: include every file in manifest['files'], not only
    # the three selected uploads. The manifest itself must not list itself.
    bundle_files=("gerbers/gerbers.zip", "assembly/bom.csv", "assembly/cpl.csv"),
    artifacts={
        "gerber": "gerbers/gerbers.zip",
        "bom": "assembly/bom.csv",
        "cpl": "assembly/cpl.csv",
    },
    source_root=Path("release/source"),
    source_evidence=source_records,
    board_quantity=10,
    settings=FactorySettings(factory="jlcpcb", board_layers=4, assembly_sides=("top",)),
    destination=Path("handoffs/board-run-1"),  # Parent exists; destination does not.
    exclusions={"J1": "tht", "R99": "dnp"},  # Optional explicit absent population.
)
print(plan.sha256)
verified = verify_submission(plan.directory, plan.sha256)
assert verified.plan_bytes == plan.plan_bytes
```

All three `FactorySettings` fields are required. Factory is currently `jlcpcb`,
layer count is a positive integer (at most 64), and sides are an explicit tuple
containing `top`, `bottom`, or both, without duplicates. These are planning intent,
not vendor API enum values. Other order settings, such as finish, assembly class,
shipping, previews and quotes, are not represented or guessed. This module cannot
produce a complete factory order payload. Quantity must be a positive integer;
booleans, strings and fractional values are rejected.

Both PCB and schematic evidence are required and mapped explicitly relative to
`source_root`, including when source files live outside the fabrication bundle.
The API binds their exact bytes, not their file modification times. It does not
assert that an untrusted manifest/source record is an approval or that a Gerber
ZIP was generated from those sources; the caller supplies the frozen provenance.

## Strict inputs

Use the current export manifest's `files` mapping, with canonical bundle-relative
POSIX paths as keys and **both** lowercase SHA256 and integer byte `size` in each
record. Every declared file is verified, including unselected reports or images.
The declared allowlist must equal the manifest file keys. The only additional file
permitted in the bundle is the selected manifest. Its subdirectories must be
implied by listed paths. Missing/changed/extra files, extra directories, self-listed
manifests, duplicate JSON keys and case aliases fail validation. Legacy basename
search is intentionally unsupported: migrate the manifest paths explicitly,
without changing the fabrication bytes, before preparing a submission.

Relative artifact/source paths cannot contain `.`/`..`, empty components, backslash,
colon, control characters, or non-NFC Unicode. Symlinks anywhere in the root's
ancestry or the selected paths are rejected, even if they point inside the root.
Every entry in the bundle must be a regular file or an implied directory; source
roots may contain other files because their binding is an explicit role mapping.
All selected artifacts must be different declared files. Destination must be
outside both input roots, with an existing nonsymlink parent.

The supported CSV dialect is the existing JLCPCB export:

- BOM: `Comment`, `Designator`, `Footprint`, `LCSC Part #`; optional `Quantity`.
- CPL: `Designator`, `Val`, `Package`, `Mid X`, `Mid Y`, `Rotation`, `Layer`.

Header order is flexible, but duplicate/unknown headers and inconsistent row
lengths are errors. Files must be UTF-8, optionally with a UTF-8 BOM. BOM references
are comma-separated within the quoted designator field. Each reference belongs to
one row only, and every row requires an explicit `C` plus positive integer catalog
ID. Ranges such as `R1-R4` are not expanded. A supplied row quantity must equal the
number of listed distinct references. Empty populations are rejected.

CPL references must match the BOM exactly, once each, and value/footprint fields
must agree. Coordinates accept decimal millimeters with or without `mm`; rotation
is decimal degrees. Finite numeric fields are bounded to an absolute value of one
million. Layers must be `Top`/`Bottom` and included in the explicit assembly sides.
Parsed coordinates are normalized only in the expected matching list; the copied
CSV bytes are unchanged.

The assembly population is exactly the frozen BOM/CPL reference set. Optional
exclusions map references to `dnp` or `tht`; each must **already be absent from both
CSVs**. A populated row cannot be filtered out while leaving it in the byte-exact
upload. No automatic DNP/THT inference or part substitution occurs. Repeated catalog
IDs across different rows are aggregated by distinct references first:

```text
per_board[id] = number of included references assigned to id
required[id]  = per_board[id] * board_quantity
```

There is no extra allowance and no second quantity multiplication.

## Published layout and identity

By default the destination contains exactly:

```text
board-run-1/
  gerbers.zip             # Exact selected input bytes
  bom.csv                 # Exact selected input bytes
  cpl.csv                 # Exact selected input bytes
  bundle-manifest.json    # Exact original manifest bytes
  expected-matches.json   # Sorted planning list; NOT factory-selected evidence
  plan.json               # Canonical versioned core
  plan.sha256             # External SHA256 of plan.json
```

An explicit `output_names={"gerber": ..., "bom": ..., "cpl": ...}` can change the
three artifact filenames. They must be distinct single filenames and cannot use
any of the four reserved metadata names. No glob selection or filename guessing
occurs. The Gerber ZIP is copied as opaque bytes; its contents are not regenerated
or certified by this module. Source files are verified and bound, not copied into
the upload directory. Inputs must be nonsecret manufacturing documents; the
module adds no credentials or raw supplier envelopes to them.

`plan.json` uses UTF-8, sorted object keys, compact separators and a final newline.
Its identity binds exact selected artifacts, every declared bundle file, source
bytes and relative mappings, original manifest bytes, quantity, explicit settings,
exclusions and the deterministic reference/ID/placement list. Input directory
locations and wall-clock timestamps are not part of identity. Reordering settings
or parsed records does not alter their semantic lists; changing the raw CSV bytes
still changes identity because the original byte hash is bound. Reordering the
raw manifest also changes its bound hash. No statement of present-day supplier
availability follows from deterministic replay.

The plan records hashes/sizes of its payload files, **not its own hash**.
`plan.sha256` and `SubmissionPlan.sha256` supply the external digest. Retain the
expected digest outside the directory and use `verify_submission` before reuse;
an attacker replacing both a plan and its adjacent digest cannot satisfy an
independently retained digest. Read-only permissions are accidental-write
protection, not filesystem WORM storage: the owner can change permissions. A
verified handoff never updates in place; changed inputs require a new destination.

## Failure and publication boundary

The implementation requires POSIX descriptor-relative file operations
(`O_NOFOLLOW` and directory file descriptors), supported on Linux and macOS.
Windows is not currently supported. All input bytes are held in memory for the
validation/copy interval, so provision memory for the complete declared bundle.

Preparation opens files without following symlinks, checks identity before/after
reading, verifies hashes/sizes, parses CSVs and validates intent before staging.
It writes a private sibling staging directory, verifies destination bytes, then
rereads all input snapshots and inventories the bundle again. A detected mutation,
write failure or mismatch removes staging and publishes no destination. After
verification, files become read-only and the directory is published with a single
rename on the same filesystem. Concurrent cooperating preparers are fenced by an
exclusive destination lock; an existing destination is rejected, not rewritten.
A process crash may leave a hidden lock/staging directory for explicit inspection
and removal, but not a successfully published partial handoff. The input roots and
destination parent must remain controlled by the caller; this is not protection
against an attacker renaming the filesystem namespace during publication.

Refreshing future stock observations must use separate files outside the frozen
bundle and bind plan/BOM/quantity/demand hashes. That behavior is not implemented
by milestone A. #5142 remains open for milestone B.
