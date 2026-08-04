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

## Using the LCSC tier

Provide a per-board **sidecar** mapping `lib_id -> C-number`:

```json
{
  "Module:Joystick_Analog": "C50950",
  "Connector_PCIE:PCIE_Mini_Edge": "C123456"
}
```

Then run:

```bash
# Cache-only (no network): resolves only C-numbers already cached.
kct pcb add-3d-models board.kicad_pcb --lcsc-models lcsc_models.json

# Opt in to fetching missing STEP bodies from EasyEDA on a cache miss.
kct pcb add-3d-models board.kicad_pcb --lcsc-models lcsc_models.json --fetch-lcsc
```

The sidecar is **lib_id-keyed** (not reference-designator-keyed): each synthetic
lib id in the fleet is unique per board, so one C-number per lib id suffices.

## Per-part `rotate` / `offset` transforms

The three installed-library tiers *derive* a body's transform geometrically, by
comparing the target footprint's pad field against the source `.kicad_mod`. The
LCSC tier has **no source footprint at all** — it synthesizes the `(model ...)`
node from nothing but a C-number — so there is nothing to derive from, and
EasyEDA bodies are authored in whatever orientation the part vendor chose. The
identity rotation the tier emits by default is therefore a *guess*, and for many
parts it is wrong by a quarter turn.

Authored corrections come from two places, in this order (first non-`None`
wins, **merged per field**):

| Precedence | Source | Scope |
|---|---|---|
| 1 | Per-board sidecar entry in **object form** | One board — a local escape hatch |
| 2 | Packaged table `src/kicad_tools/pcb/lcsc_model_transforms.py` | The whole fleet + every downstream consumer of the wheel |
| 3 | Identity (`rotate (xyz 0 0 0)`, `offset (xyz 0 0 0)`) | Every part with no entry anywhere |

Merging is **per field**: a sidecar entry that overrides only `rotate` still
inherits the packaged `offset`, and vice versa.

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
  then measure `offset`.**
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

## Cache and environment variables

- **`KCT_LCSC_3D_DIR`** — the on-disk STEP cache directory. Default:
  `~/.cache/kicad-tools/lcsc-3d/`, one file per C-number (`C50950.step`). It
  doubles as the **path variable** in emitted refs
  (`(model "${KCT_LCSC_3D_DIR}/C50950.step" ...)`), so committed `.kicad_pcb`
  files stay machine-portable — each machine resolves the ref from its own
  cache at render time. `kct pcb render` `setdefault`s this variable into the
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
