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
  machinery lands its origin on the target footprint's pad centroid. Scale and
  rotation are left at KiCad defaults; a fetched body whose native orientation
  differs from the footprint silkscreen may sit rotated. These bodies are
  cosmetic render aids.
- **Licensing.** EasyEDA/LCSC models are design-use-oriented and **not**
  explicitly redistributable — they are cached locally and **never committed**
  to the repo. Only the portable `${KCT_LCSC_3D_DIR}` path-variable ref is
  committed.

## No new dependency

The EasyEDA client is a ~50-line in-repo, stdlib-only (`urllib` + `json`)
two-call fetch (`.../api/products/{lcsc_id}/components` → extract the 3D-model
uuid → `https://modules.easyeda.com/.../{uuid}` → raw STEP bytes). No
`easyeda2kicad` dependency is added.
