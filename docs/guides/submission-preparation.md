# Offline assembly submission preparation

`kicad_tools.export.submission_plan` implements milestone A of #5142: a local,
read-only handoff of an existing manufacturing bundle. It does not export a
board again, choose parts, contact a supplier, or submit an order. Local integrity
means the supplied evidence matches the input bytes; it does **not** establish
board readiness, human approval, factory matching, available inventory, reserved
stock, or feeder/attrition requirements.

Milestone B composes the landed official parts adapter and shared provenance
contract. Explicit `refresh_inventory` calls preserve exact-ID outcomes and
original observation times in separate snapshots. Preparation itself has no
client, credential, or transport argument and never establishes live availability.
Tests use synthetic responses; no current supplier availability or successful
live signing is established by those tests.

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

## Inventory refresh and snapshot verification

```python
from kicad_tools.export.submission_plan import refresh_inventory, verify_inventory_snapshot
from kicad_tools.parts import JLCCredentials, JLCOpenAPIClient

creds = JLCCredentials.from_env()  # explicit caller action; no silent fallback
assert creds is not None
with JLCOpenAPIClient(creds) as client:
    snapshot = refresh_inventory(
        plan,
        source=client,
        destination=Path("inventory/board-run-1.json"),
        observed_at="2026-01-01T00:00:00Z",  # refresh/read time, NOT stock observation time
    )
verify_inventory_snapshot(snapshot.path, snapshot.sha256, plan)
```

Refresh re-reads and verifies every handoff output, the closed frozen bundle
file set and manifest, and the bound source PCB/schematic before calling the
adapter. It checks them again after the response, rejecting concurrent changes
without publishing a snapshot. The destination must be outside all three roots;
it cannot already exist or be claimed by another refresh. These local failures
cause zero adapter calls. Original root paths are nonserialized context on a
prepared `SubmissionPlan`, so absolute paths do not alter deterministic identity.
Plans loaded with `verify_submission` require explicit `bundle_root` and
`source_root` arguments to refresh; moving files requires caller-supplied roots
whose bytes still verify against the plan.

The injected `InventorySource` exposes `get_component_inventory(codes)` returning
`ComponentInventory`: unfiltered rows, original observation timestamp, source,
and cache flag. `JLCOpenAPIClient` implements this through its existing signed
component-detail transport, with no parallel signer, search fallback, or client
construction inside refresh. It queries only the sorted, deduplicated approved
IDs; the already multiplied demand is copied without another multiplication.

| status | meaning |
|---|---|
| `verified` | Genuine non-negative integer stock, including zero; numeric validity alone is not freshness. |
| `missing-field` | Exact matched row lacks `stockCount`. |
| `malformed` | Present stock is not a supported non-negative integer. |
| `not-returned` | Requested exact ID is absent; this does not prove catalog absence. |
| `incomplete-response` | Unusable response shape or duplicate rows for a requested ID. |
| `forbidden` | Authentication, permission, or IP-whitelist failure. |
| `quota-error` | Quota failure. |
| `transport-error` | Other official API/transport failure. |
| `dependency-error` | Missing transport dependency. |

Useful rows survive partial coverage; duplicate IDs never use first-wins stock.
`coverage` records safe unexpected IDs, duplicate IDs and invalid-row counts.
Unexpected rows never satisfy demand. `stock_field_present` and
`stock_field_value` distinguish missing fields from malformed values; duplicate
row fields are retained in `stock_fields` (at most 100, with `returned_rows`
reporting the total). Integers are bounded to 256 bits. Strings, collections,
floats and oversized integers retain only a fixed type description because
arbitrary server values may contain credentials. No response envelopes, exception
messages, credentials, headers or signatures are serialized.

Snapshot schema version 2 retains original `provenance.observed_at` separately
from caller `read_at`. It reuses `Part.inventory_provenance()` and its canonical
24-hour policy: only live observations of nonfuture age up to 24 hours can have
`stock_verified: true`; offline, unknown, stale or future observations cannot.
Per-ID `stock_verified` additionally requires valid numeric stock. Re-reading or
replaying cached evidence preserves its original timestamp. This is a freshness
assessment at refresh time, not a reservation or ongoing guarantee.

Snapshots are immutable, separately hashed files bound to plan digest, exact BOM
hash, board quantity and complete demand mapping. Retain `snapshot.sha256`
externally and use `verify_inventory_snapshot` before reuse: it verifies the
saved bytes, current handoff and every binding. Changed settings, sources, BOM,
quantity or mapping produce a different plan identity and invalidate reuse.
Verification never rewrites timestamps or renews stock freshness. To assess
freshness later, apply `Part.stock_verified` using retained source and original
observation time, never the snapshot read time. Snapshot freshness is assessed against the current clock, so replaying
the same evidence and read time across the 24-hour boundary can change its
recorded freshness and snapshot digest; genuine new responses also carry new
observation times. The deterministic version-1 plan and fabrication bytes remain untouched.

All observed inventory states leave human approval, factory matching, upload,
order, reservation and feeder/attrition evidence unestablished. Synthetic tests
exercise the real adapter with mocked transport and do not claim live availability.

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

## Gerber upload transport and durable upload state (#5145)

`kicad_tools.manufacturers.jlc_upload` provides an offline protocol model and
durable upload ledger for an already-prepared, already-reviewed Gerber bundle.
**Live upload and preview are disabled** until the complete first-party wire
contract is verified. This is partial progress on #5145, not completion of its
live integration. It provides no order, payment, quote, fabrication-parameter,
BOM/CPL or PCBA-submission operation; assembly remains an explicit manual
website handoff through `jlc_upload.PCBA_WEBSITE_HANDOFF`.

Call `upload_gerber` and `fetch_preview` only with an injected offline transport
whose `TransportIdentity.live` is exactly `False`. The ledger and reconciliation
APIs remain usable with mock protocol responses. A transport declaring live
operation is rejected with `UploadGateError`, even if a matching receipt exists.
Direct `RequestsUploadTransport` calls are also blocked, including with an
injected session. No session is created and no live request is sent. There is
no verification boolean or environment override to bypass the missing contract.

`prepare_multipart_request(url, headers=..., fields=..., files=...)` uses
requests' multipart encoder to inspect the provisional wire format without
creating a session or sending anything. Its output is not proof that JLCPCB
accepts the request. Offline tests use dummy credentials and injected responses;
no real factory receipt is created by this workflow.

### The pre-network gate

Immediately before any request is built — never trusting a value computed in
an earlier call or process — `upload_gerber`:

1. re-verifies the published handoff with `verify_submission(plan.directory,
   plan.sha256)` (every published output is re-read and re-hashed, and the
   published file set is re-inventoried);
2. re-verifies the human review with `verify_review(record, plan,
   source_root=...)` — the fail-closed re-hash, not a "a review record
   exists" check;
3. re-reads the Gerber bytes and re-checks them against the plan's own bound
   hash and size;
4. consults the durable ledger for this exact `(file sha256, app identity,
   endpoint)` binding.

A failure at any of those steps raises `UploadGateError` and **nothing is
sent**. The upload filename is the plan's own bound `artifacts["gerber"]
["output_name"]`; nothing is guessed or globbed.

### Protocol evidence and remaining gate

First-party documentation retrieved on 2026-09-11 confirms:

- [Basic rules](https://api.jlcpcb.com/docs/start): multipart uploads.
- [Request signatures](https://api.jlcpcb.com/docs/api-request-signature): sign
  metadata JSON for uploads, with five newline-terminated fields and Base64
  HMAC-SHA256.
- [API keys](https://api.jlcpcb.com/docs/configure-api-key) and
  [applications](https://api.jlcpcb.com/docs/create-an-application): keys belong
  to applications.
- [API list](https://api.jlcpcb.com/docs/api-list): Gerber upload returns a file
  ID and preview takes that ID and a language.

The public pages were read through their documentation CMS reader; these were
published-document retrievals, not operational factory requests. They resolved
the earlier shell-only access limitation but did not establish exact endpoint
paths, multipart part names, metadata fields or hexadecimal MD5 encoding.

The offline fixture still models `POST /overseas/openapi/pcb/uploadGerber`,
`meta` containing `{}`, a `file` part, lowercase-hex `Content-MD5`, and preview
at `/overseas/openapi/pcb/audit/get`. Those details remain the parent issue's
**user-reported SDK observations**, not verified endpoint requirements. The
HTTP library generates the multipart boundary. Live enablement requires the
missing authoritative contract and a separately reviewed implementation;
a caller assertion cannot supply that evidence.

### Durable state machine, not a retry flag

Every attempt is appended to a JSONL `UploadLedger` (written outside the
read-only handoff, and distinct from the plan and review records — this module
reads those rather than duplicating their fields). The intent (bundle hash,
app identity, endpoint, plan/review digests, caller timestamp) is appended and
`fsync`-ed **before** the request is built or sent, so a crash mid-request
always leaves evidence of exactly what was attempted.

```text
intent --success-------------------> succeeded
       --failure-------------------> failed
       --uncertain-----------------> uncertain --reconciliation--> succeeded|failed
       --(no outcome ever written)-> uncertain --reconciliation--> succeeded|failed
```

| state | meaning |
|---|---|
| `not-attempted` | No intent exists for this exact binding. |
| `succeeded` | A file key is bound to these exact bytes, app id and endpoint. |
| `failed` | A definite failure (business error, or a request the transport proved was never sent). A fresh attempt is allowed. |
| `uncertain` | The request may or may not have been received: a timeout, a dropped connection, an unclassified transport fault, malformed/contradictory response, unusable success receipt, unresolved identity, or an intent whose outcome was never written at all. |

An attempt whose intent was persisted but whose outcome never was folds to
`uncertain` **by construction** — there is no boolean "retry me" flag
anywhere. For a binding, an unresolved `uncertain` attempt *dominates*: the
next `upload_gerber` call raises `UploadBlockedError` and makes no network
call. Nothing assumes, in either direction, whether the request reached the
factory.

Resolving it is an explicit, human act:

```python
for intent in pending_reconciliations(ledger):
    print(intent["attempt_id"], intent["file"]["sha256"], intent["requested_at"])

reconcile_upload(
    ledger,
    attempt_id=...,
    resolution="succeeded",  # or "failed"
    file_key="...",  # required iff resolution == "succeeded"
    reconciled_by="alice@example.com",  # never synthesized here
    reconciled_at="2026-01-09T00:00:00Z",
    evidence="Checked the JLCPCB portal by hand; the file is present exactly once.",
)
```

A successful file key is reused **only** for the exact same file hash, under
the same app identity and the same endpoint — never selected by filename,
upload order, or revision. Re-running `upload_gerber` for an unchanged bundle
returns the prior receipt with `reused=True` and performs no request.

### Receipts, evidence, and what is never claimed

A receipt binds the file hash/MD5/size and upload name, the app identity, the
plan and review digests, the request state, and the file key. `evidence` is
one of:

| evidence | `is_factory_receipt` | `states["upload"]` |
|---|---|---|
| `live-factory-response` | `True` | `uploaded` |
| `mock-protocol-only` | `False` | `mock-protocol-only` |
| `human-reconciliation` | `False` | `reconciled` |

Live evidence values above remain readable for existing ledgers; current calls
cannot create live evidence while the protocol gate is closed.

The injected transport must declare a `TransportIdentity(name, live)`. A
success produced by a transport that did not declare `live=True` is recorded
and labeled `mock-protocol-only` — **a mocked protocol success is never
logged or labeled as a real factory receipt** — and a human reconciliation is
recorded as a human attestation, never as a protocol receipt.

### Failure handling

Success requires a JSON integer `code` of 200 and the exact boolean
`success: true`, plus a usable receipt and matching identities. An explicit
rejection requires an integer non-200 code with `success: false`. Strings,
numeric booleans, missing fields, contradictory outcomes and truncated replies
cannot establish a receipt. After an upload send, such outcomes and missing
file keys are recorded as `uncertain`, blocking another send until explicit
reconciliation. Valid business rejections remain `failed` and permit retry.
Failures retain only a fixed
classification word (`auth-failed`, `ip-not-whitelisted`, `permission-denied`,
`quota-exceeded`, `incomplete-response`, `identity-mismatch`,
`transport-unsent`, `transport-uncertain`, `request-failed`), a whitelisted
plain-prose reason, the HTTP status, the business code, and a whitelisted
`J-Trace-ID`. Credential material is redacted, and any reason that redaction
touched — or that contains markup/raw-payload characters — is withheld
entirely rather than surfaced or persisted. The raw response body is never
logged, raised, or written to the ledger.

A present app ID, file MD5 or SHA-256 must be a nonblank string matching the
request binding; missing optional echo fields remain allowed. Malformed or
mismatched echoes raise `UploadIdentityError`, a subclass of
`UploadUncertainError`, and upload attempts are recorded as `uncertain`.
Preview errors never create a preview result or change the upload receipt.

### Preview retrieval is a separate call

`fetch_preview(receipt, ...)` issues `POST /overseas/openapi/pcb/audit/get` as
its **own** request — never issued by `upload_gerber`, and never inferred from
an upload succeeding. It requires a receipt whose file key is actually
recorded as successful in the ledger for the same app identity and file hash,
and it persists its own record binding the attempt, file key, file hash and
payload digest. Interpreting the preview/DFM payload is #5146's scope; nothing
here derives a verdict from it.

## DFM report attachment and binding (#5146)

`kicad_tools.export.factory_dfm` binds an original factory DFM report and a
typed manual/OCR-assisted transcription of its findings to an exact upload
identity. Like `factory_selection.py`, this is a pure, dependency-injectable
binding module: it makes no network call, performs no filesystem I/O, and
does not run JLCPCB's own DFM checker or claim "headless DFM verification."
A human (or another tool) already produced the report bytes and, when the
report is raster/image-only, already produced the transcription; this module
only validates and binds that already-produced evidence.

```python
from kicad_tools.export.factory_dfm import (
    CategoryFinding,
    ModuleCoverage,
    TranscriptionEvidence,
    attach_dfm_report,
    verify_dfm_attachment,
)

transcription = TranscriptionEvidence(
    provenance="manual",          # or "ocr", or "unknown" if even that isn't known
    performed_by="alice@example.com",
    confidence=0.95,               # 0.0-1.0, or None when unknown
    extracted_rows=12,
    ocr_failed=False,
    image_only_source=True,        # a raster-only/scanned report page
    categories=(
        CategoryFinding(
            category="silkscreen-clearance",
            count=3,
            limit=10,
            capped=False,           # True marks a truncated/capped count
            coordinates_available=True,
            threshold="0.1 mm",
        ),
    ),
    modules=(
        ModuleCoverage("pcb_fabrication", True),
        ModuleCoverage("smt_assembly", True),
    ),
)

attachment = attach_dfm_report(
    report_bytes=report_pdf_bytes,       # never edited or re-encoded
    report_revision="v27",
    checker_time="2026-01-03T00:00:00Z",
    gerber_sha256=receipt.file_sha256,   # exact Gerber bundle hash, from #5145
    app_identity=receipt.app_identity,
    endpoint=receipt.endpoint,
    transcription=transcription,
    ledger=ledger,                       # optional; omit for a fully offline attachment
)
verify_dfm_attachment(attachment, report_pdf_bytes)  # raises on any byte drift
```

`report_bytes` is never edited, re-encoded, or "cleaned up" -- only its own
SHA-256 and size are recorded (`attachment.report_sha256` /
`.report_size`), for later re-verification with `verify_dfm_attachment`. A
bare filename or revision string alone is never accepted as identity; there
is no such parameter. The binding is the combination of the exact Gerber
bundle hash, the application identity/endpoint, and the report's own
declared revision and checker/report timestamp.

An optional `report_claimed_gerber_sha256` lets a caller transcribe a hash
the report itself echoes as the file it audited. When supplied and it
disagrees with the current `gerber_sha256` (for example, a stale v25 report
reviewed against the current v27 Gerber), `attach_dfm_report` raises
`DFMError` rather than silently binding a report to a bundle it never
examined.

When `ledger` is supplied, this looks up a receipt for the exact
`(gerber_sha256, app_identity, endpoint)` binding via
`jlc_upload.find_receipt` -- never by filename or upload order. A ledger
that has no matching receipt (for example, a report bound to the wrong
upload) leaves `attachment.upload_binding` at `"unknown"` rather than
raising; when `plan`/`review` are also supplied and a receipt *is* found,
its own bound `plan_sha256`/`review_sha256` must still match them, or the
receipt is stale relative to the current submission state and
`attach_dfm_report` raises. **A DFM attachment can be created entirely
offline** with no ledger at all -- `upload_binding` then stays explicitly
`"unknown"`.

`attachment.dfm_status` (`"pass"`, `"fail"`, or `"unresolved"`) is computed
here from `transcription`, never accepted as a caller claim:

- Zero `extracted_rows`, an explicit `ocr_failed`, or an entirely empty
  `categories` tuple all keep the status at `"unresolved"` -- a raster-only
  report with no successful transcription is never an implicit clean bill of
  health.
- Any category left `"unknown"` -- a missing `count`/`limit`, or a
  `capped=True` truncation flag (the true count may exceed what was
  recorded) -- also keeps the overall status `"unresolved"`.
- A category whose `count` exceeds its `limit` (and isn't `capped`) makes
  the status `"fail"`.
- Only when every category resolves within its limit does the status become
  `"pass"`.

`attachment.modules` always reports both `"pcb_fabrication"` and
`"smt_assembly"`, filling in `covered=None` for any module the report never
mentions -- coverage is recorded exactly as transcribed, never inferred from
`factory_selection.py`'s component-selection comparison (that module has no
concept of DFM analysis-module coverage).

`attachment.readiness_eligible` is `True` only when `dfm_status == "pass"`
**and** `upload_binding == "bound"`. Readiness/Ready-badge integration must
consult this property, not `dfm_status` alone: a passing transcription bound
to an `"unknown"` upload (no verified #5145 receipt yet) must never promote
a board.
