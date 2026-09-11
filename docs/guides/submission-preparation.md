# Offline assembly submission preparation

`kicad_tools.export.submission_plan` implements milestone A of #5142: a local,
read-only handoff of an existing manufacturing bundle. It does not export a
board again, choose parts, contact a supplier, or submit an order. Local integrity
means the supplied evidence matches the input bytes; it does **not** establish
board readiness, human approval, factory matching, available inventory, reserved
stock, or feeder/attrition requirements.

Milestone B (exact-ID inventory observations) has a **narrow, partial** increment:
`refresh_inventory` observes exact-ID stock through a caller-supplied adapter and
preserves unknown-vs-zero evidence, but it targets only the current, pre-merge
`kicad_tools.parts.jlcpcb_api` contract. Full milestone B depends on #5033/#5034
(tracked by PRs #5115/#5090) landing their result/provenance/freshness contracts;
until then this module does not claim source/observation-age provenance beyond a
caller-supplied timestamp, and #5142 stays open. Preparation itself
(`prepare_submission`) has no client, credential, or transport argument and does
not read supplier environment variables — it never produces live availability
evidence.

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

## Inventory refresh (narrow milestone B increment)

```python
from kicad_tools.export.submission_plan import refresh_inventory
from kicad_tools.parts import JLCCredentials, JLCOpenAPIClient

creds = JLCCredentials.from_env()  # or construct explicitly; never silent-fallback
assert creds is not None
with JLCOpenAPIClient(creds) as client:
    snapshot = refresh_inventory(
        plan,
        source=client,
        destination=Path("inventory/board-run-1-2026-01-01.json"),
        observed_at="2026-01-01T00:00:00Z",  # caller-supplied clock, not module-generated
    )
print(snapshot.sha256)
```

`refresh_inventory` never constructs a client, reads credentials, or falls back to
an anonymous/offline source — it requires an already-built `source` object (the
official `JLCOpenAPIClient`, or any object exposing a structurally compatible
`get_component_detail_raw(codes) -> list[dict]`, per the `InventorySource`
protocol). All local plan-shape validation (schema version, demand entries, bound
BOM hash, explicit `observed_at`, destination outside the published handoff)
happens **before** `source` is touched, so a malformed plan or destination never
reaches the network.

The deduplicated, sorted approved-ID set from the plan's `demand` list is queried
once. Each ID is classified independently:

| status | meaning |
|---|---|
| `verified` | Adapter returned a genuine non-negative int `stockCount`; `raw_stock` holds it (0 is valid and distinct from unknown). |
| `missing-field` | The matched component object had no `stockCount` key. |
| `malformed` | `stockCount` was present but not a non-negative int (string, negative, bool, float, …). |
| `not-returned` | No component with this exact code appeared in the adapter's response. |
| `forbidden` | Auth, permission, or IP-whitelist failure for the whole batch. |
| `quota-error` | Rate limit/quota failure for the whole batch. |
| `transport-error` | Any other adapter/API failure for the whole batch. |
| `dependency-error` | The adapter's transport dependency (e.g. `requests`) was unavailable. |
| `incomplete-response` | The response envelope succeeded but its payload shape was unusable. |

Only `verified` ever carries a non-`None` `raw_stock`; every other status leaves
it `None` rather than defaulting to zero. A component the adapter returns for an
ID that was never requested is read and discarded — it can never satisfy a
different code's demand ("no automatic substitution").

The resulting `InventorySnapshot` is written once to an explicit `destination`
**outside the published handoff directory** (which stays read-only) as a new,
non-overwritable file, then bound to `plan_sha256`, the bound BOM output hash,
`board_quantity`, and the exact `demand` mapping copied from the plan. Refreshing
never mutates `plan.plan_bytes` or any file inside the handoff directory; calling
it again with the same plan and `observed_at` reproduces byte-identical output,
while a different `observed_at`, plan, or adapter response changes it. The
snapshot's own `states` block never claims human review, factory matching, upload,
reservation, or feeder/attrition evidence — those remain out of scope here as in
milestone A.

**What remains blocked on #5033/#5034/#5115/#5090:** those PRs add a
`lookup_result()`/provenance contract (source identity, original observation
time, snapshot revision/time, a documented freshness policy) to the shared parts
layer. Until they land, `refresh_inventory` cannot safely surface that richer
provenance without inventing it, so `observed_at` is caller-supplied and
per-adapter source identity is not recorded beyond the coarse status above. This
module also does not add a parallel signer/HTTP client, and does not confirm the
live signing variant documented in `jlcpcb_api.py` actually works against the
real API — no current supplier availability is claimed by any test in this repo.

## Human review record and local review page (#5143)

`kicad_tools.export.submission_review` implements the second slice of #5056: a
hash-bound human review record and a portable, local review page over an
already-prepared `SubmissionPlan`. It performs no order/purchase operation of
any kind, no rendering of fabrication bytes, and never launches a browser.

```python
from kicad_tools.export.submission_review import (
    ReviewAsset,
    render_review_page,
    submit_review,
    verify_review,
)

REQUIRED_CHECKLIST = (
    "gerber-visually-inspected",
    "bom-cross-checked",
    "quantities-confirmed",
)

record = submit_review(
    plan,
    reviewer="alice@example.com",
    reviewed_at="2026-01-01T00:00:00Z",  # caller-supplied clock, not module-generated
    policy_version="policy-v1",
    checklist=dict.fromkeys(REQUIRED_CHECKLIST, True),
    required_items=REQUIRED_CHECKLIST,
    destination=Path("reviews/board-run-1.json"),
)
verify_review(record, plan)  # raises ReviewError on any hash mismatch

page = render_review_page(
    plan,
    record=record,
    destination=Path("reviews/board-run-1-page"),
    assets={
        "top-copper.png": ReviewAsset("top-copper.png", layer_sha256, layer_size, "manufacturing"),
    },
    assets_root=Path("exports/layer-views"),
)
```

`submit_review` is the **only** function that writes a review record, and it
only runs after every item in the caller-supplied `required_items` (the
review policy's own checklist, never hardcoded here) is present in
`checklist` mapped to exactly `True`. A missing item, an unrecognized extra
item, or a `False`/falsy item all leave the checklist incomplete, and no
record is published — a missing checklist is invalid, never an implicit
pass. The record binds the reviewer identity, timestamp and policy version to
the exact SHA-256 hashes of the plan document itself
(`SubmissionPlan.sha256`), the overall bundle output set, each of the
Gerber/BOM/CPL outputs individually, every declared bundle file, and the
declared source evidence.

`verify_review` recomputes every one of those hashes from the current plan
and raises `ReviewError` on the first mismatch — a bundle re-prepared into a
new plan (different hashes) invalidates a review bound to the old one. Pass
`source_root` to additionally reread the current bytes of the declared PCB/
schematic source files on disk and catch a **source edited after the
review was recorded**, even though the plan and bundle themselves were never
regenerated (the plan only pins the source hash observed at `prepare_submission`
time, not a live watch on the source path).

`render_review_page` publishes a portable directory: `index.md` (states,
checklist, reviewer/timestamp/policy version, and links), a human-readable
`inventory-report.txt` (the plan's own per-catalog-ID assembly demand, not a
live stock observation), byte-exact copies of the plan's declared upload/
download file set under `uploads/`, and any caller-supplied `ReviewAsset`s
under `layer-views/` (`kind="manufacturing"`) or `test-evidence/`
(`kind="synthetic-test-evidence"`). If `record` is omitted the page is
rendered with an explicit `human_approval: not-established` state and a
"NOT REVIEWED" banner — human approval is never inferred from a prepared
plan alone. If `record` is supplied it is independently reverified first; a
stale or malformed record raises rather than silently rendering an
"approved" page.

Every `ReviewAsset` is reread from `assets_root` and reverified against its
declared `sha256`/`size` before being copied — a caller cannot claim a layer
view is bound to bytes it does not actually match. `kind` must be exactly
`"manufacturing"` or `"synthetic-test-evidence"`; anything captured by a
headless/automated browser (for example, a Playwright/Selenium screenshot
used only to test that this page itself renders) must use
`"synthetic-test-evidence"` and is always kept in its own, clearly labeled,
"NOT MANUFACTURING EVIDENCE" section — it never counts as proof that any
factory portal received, matched, or accepted an upload.

States reuse `submission_plan.py`'s own vocabulary
(`preparation`/`inventory`/`human_approval`/`factory_matching`/`upload`/
`order`). This module only ever establishes `human_approval` (to
`"reviewed"`, once and only once a checklist submission validates);
`upload`, `factory_matching` and `order` remain untouched placeholders —
no stage is ever inferred from an earlier or later stage's success, and this
slice adds no upload, factory-matching, or order/purchase operation. Full
factory-matching/upload/order tracking is left to later slices (#5145,
#5146).
