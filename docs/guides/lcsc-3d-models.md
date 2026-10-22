# LCSC/EasyEDA fetch-on-demand 3D models

`kct pcb add-3d-models` resolves `(model ...)` 3D bodies for footprints in four
tiers (first hit wins):

1. **Exact** installed KiCad `library:name` footprint.
2. **Same-library variant** — a suffixed name in the same library.
3. **Cross-library substitution** — a curated `lib_id -> lib_id` table for
   generic/synthetic lib ids with a body-compatible equivalent elsewhere.
4. **LCSC/EasyEDA fetch-on-demand** — for footprints whose only usable identity
   is an **LCSC C-number** (the JLCPCB-assembly fleet already carries these in
   its BOMs). Nearly every LCSC part has a STEP body in the EasyEDA parts
   database; this tier fetches it on demand into a local cache and emits a
   portable model ref.

## Policy: per-user, opt-in, never committed or shipped

Operator ruling Q44 (issue #7686). EasyEDA/LCSC STEP bodies are not
redistributable, and the [EasyEDA Terms](https://easyeda.com/legal) forbid
automated access except as authorized by EasyEDA in writing. So:

- Fetching runs **only on your own machine, when you explicitly opt in**
  (`--fetch-lcsc` or `KCT_LCSC_FETCH=1`). It is off by default and in CI.
- Fetched bodies live in your local cache (`${KCT_LCSC_3D_DIR}`, default
  `~/.cache/kicad-tools/lcsc-3d/`). They are **never committed** to a repo,
  shipped in a wheel or package, or embedded in a board, manufacturing bundle
  or site render. Demo boards and published packages use only KiCad stock
  models and self-generated ones.
- The first fetch into a cache prints a terms notice. A fetch is capped per
  run (`KCT_LCSC_MAX_FETCHES`, default 25) and rate-limited
  (`KCT_LCSC_FETCH_INTERVAL` seconds between requests, default 1).
- The cache refuses to be created inside a git work tree.
- Every cache entry has a `C<n>.meta.json` with `"source": "lcsc"` so export
  and embedding can exclude it. `tests/test_lcsc_no_commit_guard.py` fails if a
  `*.step`/`*.stp` whose sha256 matches an LCSC cache entry (or that carries
  `source=lcsc` metadata) is tracked under `boards/` or `src/`.

Written permission from LCSC/EasyEDA for automated access and redistribution
has been requested by the operator; if it is granted, this ruling will be
revisited in a separate issue.

## Using the LCSC tier

C-numbers are resolved from the **board's own data**, per footprint reference,
first hit wins: an `LCSC`/`JLCPCB Part` footprint property, then an LCSC column
in the BOM CSV, then the BOM row's MPN looked up in `procurement-review.json`
(`parts[<MPN>].lcsc`). The BOM and procurement review are discovered next to
the PCB or in its parent directory (`--bom` / `--procurement` override).

```bash
# Cache-only (no network): C-numbers come from the BOM; only cached bodies resolve.
kct pcb add-3d-models boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb

# Opt in to fetching missing STEP bodies from EasyEDA on a cache miss.
kct pcb add-3d-models board.kicad_pcb --fetch-lcsc
```

A `lib_id` whose references carry *different* C-numbers (a shared `R_0603`
footprint) is reported as a conflict and not mapped: a model keyed by `lib_id`
cannot be right for all of them. Pass `--no-lcsc-bom` to use only a sidecar.

### Sidecar = override only

An optional per-board **sidecar** (`--lcsc-models lcsc_models.json`) overrides
the BOM-derived map for a `lib_id`; it is no longer required. Stale entries
(a `lib_id` not on the board, or a C-number that disagrees with the board
data) are reported as `lcsc_stale_sidecar`.

```json
{
  "Connector_PCIE:PCIE_Mini_Edge": {"lcsc": "C444929", "sha256": "<64 hex digits>"}
}
```

The sidecar is **lib_id-keyed** (not reference-designator-keyed).

### Content pinning

The cache stores `C<n>.step` plus `C<n>.meta.json` (sha256, fetch date,
EasyEDA model uuid, EasyEDA footprint pads, `source`). A cached body whose
sha256 no longer matches the digest recorded at fetch time -- or the sidecar's
`"sha256"` pin, which wins -- is reported as **`lcsc_content_changed`** (in
`kct pcb add-3d-models` output and as a `kct model-audit` class). It is never
silently used or re-pointed.

### Auto-registered transforms

When no sidecar or packaged transform exists for a part, `add-3d-models` fits
the EasyEDA footprint pads (stored in the cache metadata at fetch time) onto
the KiCad footprint's pads, matched by pad number, with a 2D rigid fit
(rotation + translation). The fit's `rotate`/`offset` are written when the
residual is low (rms <= 0.05 mm, max <= 0.10 mm, at least half the pads
matched), reported as `lcsc_registration` with the residual. A high residual is
reported as `lcsc_needs_calibration` and the identity transform stays: calibrate
that part by hand (below). Precedence: sidecar > packaged table > derived >
identity. The idea comes from kicad-jlcpcb-tools' CPL rotation work (MIT);
easyeda2kicad is AGPL and none of its code is used. The EasyEDA response layout
that the pad parser reads has not been verified against the live API.

## Per-part `rotate` / `offset` transforms

The three installed-library tiers *derive* a body's transform geometrically, by
comparing the target footprint's pad field against the source `.kicad_mod`. The
LCSC tier has **no source footprint at all** — it synthesizes the `(model ...)`
node from nothing but a C-number — so there is nothing to derive from, and
EasyEDA bodies are authored in whatever orientation the part vendor chose. The
identity rotation the tier emits by default is therefore a *guess*, and for many
parts it is wrong by a quarter turn.

Authored corrections come from these places, in this order (first non-`None`
wins, **merged per field** — with one guard, below):

| Precedence | Source | Scope |
|---|---|---|
| 1 | Per-board sidecar entry in **object form** | One board — a local escape hatch |
| 2 | Packaged table `src/kicad_tools/pcb/lcsc_model_transforms.py` | The whole fleet + every downstream consumer of the wheel |
| 3 | Derived by EasyEDA pad registration (see above) | Parts fetched with pad data and a low fit residual |
| 4 | Identity (`rotate (xyz 0 0 0)`, `offset (xyz 0 0 0)`) | Every part with no entry anywhere |

Merging is **per field**: a sidecar entry that overrides only `offset` still
inherits the packaged `rotate`, and vice versa.

#### Guard: a packaged `offset` belongs to its sibling `rotate`

`offset` is applied *after* `rotate` (see [Frame semantics](#frame-semantics)),
so a packaged `offset` is only meaningful in the frame of the `rotate` it was
measured under. The per-field merge therefore stops short of one case: a sidecar
that overrides `rotate` to a **different** value while silently inheriting a
packaged `offset` is a **build error** (`ValueError` naming the lib id, the
C-number, both triples and the packaged entry's provenance) — not a
plausible-but-wrong body.

The invalidated components are `X`/`Y` when the two rotations differ only about
Z (a Z-axis spin leaves height above the board untouched), and `X`/`Y`/`Z` when
they differ about X or Y as well (an off-Z rotation tilts the body). The error
only fires when at least one invalidated component is non-zero — a packaged
`offset` of `[0, 0, 1.25]` (the "STEP origin is off the board plane" knob) is
still inherited freely under a Z-only rotation change.

Two ways to resolve it, both of which force a decision rather than inheriting
one:

- **Restate `offset` in the sidecar** — including an explicit `[0, 0, 0]`, which
  suppresses the packaged value. Re-render first; that is the point.
- **Drop the sidecar `rotate`**, keeping the calibrated `(rotate, offset)` pair.

The symmetric case is *not* an error: a sidecar overriding only `offset` while
inheriting the packaged `rotate` is fine, because its author necessarily
measured with the packaged rotation active.

### Sidecar object form

```json
{
  "Module:Joystick_Analog": "C50950",
  "Connector_PCIE:PCIE_Mini_Edge": {
    "lcsc": "C444929",
    "rotate": [0, 0, -90],
    "offset": [0, 0, 0]
  }
}
```

The bare-string form stays valid and unchanged. In the object form `lcsc` is
required; `rotate` and `offset` are independently optional and must each be
exactly 3 finite numbers. A malformed sidecar is a **build error** (`ValueError`
naming the file and the offending key), and **unknown keys are rejected rather
than ignored** — a typo'd `"rotation"` fails loudly instead of degrading to a
silent, green no-op.

### The packaged table

`lcsc_model_transforms.LCSC_MODEL_TRANSFORMS` is keyed by **C-number**, not by
lib id. An LCSC C-number is a stable, global, vendor-issued identifier and a
body's native orientation is a property of *the part*; the fleet's lib ids are
unique per board (see above), so a lib_id-keyed table would have a reuse rate of
~zero. Keyed by C-number, one calibration is correct for every board, and it
ships in the wheel to downstream consumers.

Prefer the packaged table. Reach for the sidecar object form only to correct a
wrong or missing packaged value without waiting for a release.

### Frame semantics

KiCad applies a model's transforms in the order **scale → rotate → offset**, in
the *model frame*: X matches the footprint 2D X, **Y is negated** relative to
the footprint 2D frame, and Z is up from the board.

- `rotate` is written **verbatim** into the emitted node. The LCSC tier's
  derived `theta` is always `0` (there is no source footprint), so nothing
  composes with it and it can never double-apply.
- `offset` is **added to** the pad-centroid delta `(dx, -dy)` that the shared
  offset machinery already injects. Its **Z passes through untouched** — that is
  the knob for a STEP whose origin is not on the board plane.
- Because `offset` is applied *after* `rotate`, it is a post-rotation
  translation. Practical consequence: **set `rotate` first, render, and only
  then measure `offset`.** The corollary: an authored `offset` is calibrated
  *for* the `rotate` beside it, and changing one without re-measuring the other
  invalidates the pair — which is why overriding a packaged `rotate` from a
  sidecar without restating its `offset` is rejected (see
  [the guard above](#guard-a-packaged-offset-belongs-to-its-sibling-rotate)).
- A footprint's own `(at x y angle)` rotates the whole footprint *including* its
  model, so an authored per-part transform is **placement-angle independent**.
  Calibrate once per part; never per instance. This is the property that makes a
  part-keyed table sound.

### Calibrating a per-part transform

**Only a render can tell you whether a number is right.** The automated tests
around this mechanism verify *plumbing and process* — that the composition,
signs, model-frame Y negation and Z passthrough are wired correctly, and that
every packaged entry carries provenance. They can **never** verify that the
authored value for a given part is correct. CI has no KiCad and cannot render.

So: run this loop, look at the picture, and only then commit a number.

```bash
# 1. Scratch copy with the stale model node stripped.  (add-3d-models is
#    insert-only until #4586 lands, so it will not rewrite an existing ref.)
cp boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb /tmp/cal.kicad_pcb
#    ...strip the existing (model ...) block for the part under test...

# 2. Re-insert with the candidate transform in the packaged table (or in a
#    scratch sidecar, which is faster to iterate on).
uv run kct pcb add-3d-models /tmp/cal.kicad_pcb \
    --lcsc-models /tmp/sidecar.json

# 3. Render and LOOK.  (KCT_LCSC_3D_DIR is setdefault-ed by the render runner.)
uv run kct render /tmp/cal.kicad_pcb          # isometric 3d-front.png / 3d-back.png

# 4. For an unambiguous read, add an orthographic top view zoomed on the part.
kicad-cli pcb render /tmp/cal.kicad_pcb --side top --zoom 5 \
    --pivot <X>,<Y>,0 -o /tmp/top.png
```

**Compare `+90` and `-90` visually — do not reason your way to a sign.** Most
connector bodies are not symmetric under a 180° flip, so the mating face lands on
the wrong side for the wrong sign even when the long axis looks right. Useful
objective tiebreakers, both readable off an orthographic top view: the body's
long axis must be parallel to the footprint's pad field, and the body must lie
inside the `Edge.Cuts` outline rather than hanging across a board edge.

Then record what you saw. **Every packaged entry must carry a
`TransformProvenance`** (board, refdes, ISO date, the literal command, and notes
on what the render showed); `tests/test_lcsc_models.py` walks the table and
**fails** on any entry whose provenance is missing or malformed. That is the
structural gate: an uncalibrated guess cannot land green, so the only way into
the table is a human who actually rendered.

## 3D model coverage audit

`kct pcb model-audit <pcb>` (issue #7682) classifies **every footprint**, not
just every `(model ...)` ref, so a footprint with no model can no longer hide
behind "0 models checked". One classifier
(`kicad_tools/pcb/model_audit.py`) backs every surface below.

| Class | Meaning | Covered? |
|---|---|---|
| `resolved` | an enabled model ref resolves to a file | yes |
| `embedded` | `kicad-embed://` ref whose name exists in `embedded_files` with data | yes |
| `dangling` | the ref names a missing file (or an embedded name with no data) | no |
| `no_ref` | the footprint has no `(model ...)` at all | no |
| `unresolved_var` | a `${VAR}` in the path is not defined in the environment | no |
| `hidden_only` | every model on the footprint is hidden | no |
| `lcsc_cache_miss` | the ref points into the LCSC cache (`KCT_LCSC_3D_DIR`, `C<digits>.step`) but the file is not cached | no |
| `lcsc_content_changed` | the cached LCSC body's sha256 no longer matches the digest pinned when it was fetched | no |
| `placeholder` | resolves, but the file name is a stand-in (`placeholder`, `dummy`, `generic`, `proxy`) | no |
| `exempt` | no 3D body expected; excluded from the denominator | n/a |

**Exempt list** (data in `model_audit.py`: `EXEMPT_ATTRS`, `EXEMPT_LIB_PATTERNS`):
footprints with a `board_only` or `exclude_from_bom` attribute, and the
`MountingHole`, `NetTie`, `TestPoint` and `Fiducial` library families.

**Resolution environments.** `--env host` (default) is what `kct render` sees on
this machine, so the totals depend on the KiCad model library installed there
(the audited reference numbers were measured against KiCad 10.0.x).
`--env pinned` is the pinned toolchain image plus the checksum-verified subset of
KiCad's stock 3D library that `kct models sync` fetches (issue #7683, see
below). Until that is synced it prints `pinned model set not synced`, every
library ref reads `dangling` and only `${KIPRJMOD}`-relative and embedded models
resolve. `--model-dir DIR` maps every
`KICADn_3DMODEL_DIR` variable to `DIR`.

**Surfaces.**

- `kct pcb model-audit <pcb> [--env host|pinned] [--model-dir DIR] [--format json]`
- `kct models sync --sparse` / `kct models status`: fetch / check the pinned
  model subset (below).
- `kct models generate`: parametric STEP bodies (or `--placeholder` boxes) for
  footprints whose stock model does not exist; see
  [parametric-3d-models.md](parametric-3d-models.md) (issue #7687).
- `kct check --3d-models` (or `--only model_coverage`): one *warning* per
  non-exempt uncovered footprint (message carries reference, `lib_id`, class
  and path), or a single *info* finding ("3D coverage not evaluated") when the
  host has no model library. The category is **opt-in**: plain `kct check`,
  `--strict`, board gates, `kct readiness` and `run_drc` do not run it. The
  pinned subset landed with #7683, but generated boards still carry
  host-independent `no_ref` gaps that would fail zero-warning board gates, so
  it becomes default-on with #7685 (every board at 100% on the pinned set).
- `kct render`: boards with uncovered footprints report `partial` with a reason
  per footprint (`model_check.uncovered_models`) instead of `passed`; exit code
  is unchanged.
- `kct fleet status`: a `3D` column (`covered/total`, host environment).
  Informational; never a ship-ready blocker.
- CI: the non-blocking `3D Model Coverage Report` job uploads
  `model-coverage.json` (host and pinned summaries, gaps and suspects per board).

**Pinned model subset (`kct models sync`).** The pinned toolchain image ships no
3D models, so the fleet fetches only the files its boards reference, from
`kicad-packages3D` at tag `10.0.6` (equal to the pinned image's KiCad version; a
test fails on drift), and verifies each against the sha256 in
`src/kicad_tools/data/kicad-3dmodels/manifest.json`. The cache is
`~/.cache/kicad-tools/kicad-3dmodels/<tag>/` (override with
`KCT_KICAD_3DMODEL_CACHE`); a second `sync` makes no network calls, and a digest
mismatch deletes the file and exits non-zero. `--env pinned` and the Docker
`kicad-cli` shim (`KCT_KICAD_DOCKER=1`) use the cache only when every manifest
file verifies. Regenerate the manifest with
`uv run python scripts/update_3dmodel_manifest.py` when a board references a new
stock model. The models are CC-BY-SA-4.0 with the KiCad design exception; the
manifest ships with `ATTRIBUTION.md`, which `sync` copies next to the cache.
The subset is KiCad stock models only and is kept apart from the LCSC cache
(Q44): the manifest loader rejects any `C<n>.step` or `lcsc/`/`easyeda/` entry,
the two caches live in separate directories, and `--env pinned` never resolves
`${KCT_LCSC_3D_DIR}` refs from the synced KiCad subset.

**Wrong-body heuristic.** When the model file name implies a pin grid
(`..._1x04_P2.54mm`) whose pin count differs from the footprint's pad count, the
entry is reported as a `suspect` (still counted as covered), e.g. a `PinHeader_1x05`
footprint carrying a 1x04 model.

## Cache and environment variables

- **`KCT_LCSC_3D_DIR`** — the on-disk STEP cache directory. Default:
  `~/.cache/kicad-tools/lcsc-3d/`, one file per C-number (`C50950.step`). It
  doubles as the **path variable** in emitted refs
  (`(model "${KCT_LCSC_3D_DIR}/C50950.step" ...)`), so committed `.kicad_pcb`
  files stay machine-portable — each machine resolves the ref from its own
  cache at render time. `kct render` `setdefault`s this variable into the
  `kicad-cli` subprocess environment (mirroring the `${KICADn_3DMODEL_DIR}`
  precedent).
- **`KCT_LCSC_FETCH`** — set to `1`/`true`/`yes`/`on` to enable fetch-on-demand
  without the `--fetch-lcsc` flag. Default: cache-only.

## Safety and behavior notes

- **Offline / CI safe.** The tier activates only on a cache hit, or when
  fetching is explicitly enabled. CI never needs network.
- **Skip-on-miss.** On a cache miss with fetching disabled (or a fetch
  failure), the footprint is reported *unresolved* and **no** `(model ...)` ref
  is emitted — the tool never ships a `.kicad_pcb` with a ref to a
  known-absent file. Fetch failures degrade to a warning; the patch and render
  never fail for want of a body.
- **Pure metadata insertion.** As with the other tiers, only `(model ...)`
  lines are inserted — copper, placement, zones, and nets are untouched, so DRC
  results are identical before and after.
- **Origin-authored placement (approximation).** An EasyEDA STEP is a bare
  `.step` with no `.kicad_mod`, so there is no source pad centroid. The body is
  treated as origin-centered (`source_anchor=(0, 0)`), and the shared offset
  machinery lands its origin on the target footprint's pad centroid. Scale is
  always left at the KiCad default; rotation and any extra offset come from the
  authored per-part transform described above (identity when no entry exists, so
  an uncorrected body whose native orientation differs from the footprint
  silkscreen may still sit rotated). These bodies are cosmetic render aids.
- **Licensing.** EasyEDA/LCSC models are design-use-oriented and **not**
  explicitly redistributable — they are cached locally and **never committed**
  to the repo. Only the portable `${KCT_LCSC_3D_DIR}` path-variable ref is
  committed.

## No new dependency

The EasyEDA client is a ~50-line in-repo, stdlib-only (`urllib` + `json`)
two-call fetch (`.../api/products/{lcsc_id}/components` → extract the 3D-model
uuid → `https://modules.easyeda.com/.../{uuid}` → raw STEP bytes). No
`easyeda2kicad` dependency is added.

## See also

- [3D model coverage research note](../research/3d-model-coverage.md): measured
  per-board 3D body coverage on the host and pinned CI toolchains, model
  licensing findings, and how coverage interacts with the render checks.
