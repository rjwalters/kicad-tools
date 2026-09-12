# kicad-tools.org website

The Astro site presents implemented capabilities, a board gallery and dated
external-board routing evidence. It has its own `package.json` and lockfile;
site development does not require the repository's Python or native tools.

## Develop and verify

From the repository root:

```bash
npm --prefix site ci
npm --prefix site run dev
```

Before requesting review:

```bash
npm --prefix site test
npm --prefix site run check
npm --prefix site run build
npm --prefix site run preview -- --host 127.0.0.1
```

The preview serves the production build. Check the printed URL, then inspect
home, `/benchmarks/`, a board with assets and a development board such as
`/09-usbc-pd-power/`. Test narrow mobile and desktop widths, the first-Tab skip
link, keyboard disclosures, focus visibility and table scrolling. Follow the
capability/documentation links, gallery cards, report/reproduction links and
available viewer/download links. A successful build alone does not verify
these paths or the published website.

`predev` and `prebuild` run `scripts/copy-renders.mjs` automatically. To stage
assets separately, run `npm --prefix site run copy-renders`. `site/dist/` and
`site/public/boards/` are generated and ignored by git.

## Pages and content ownership

- `src/pages/index.astro`: capability introduction, documentation entry points
  and gallery sections. Feature descriptions must match implemented behavior;
  link to representative examples and identify experimental scope.
- `src/components/Header.astro` and `src/styles/global.css`: shared navigation,
  typography, responsive layout and keyboard focus/skip navigation.
- `src/components/BoardCard.astro` and `src/pages/[slug].astro`: board cards,
  detail views, evidence status, renders, KiCanvas and downloads.
- `src/data/galleryConfig.mjs`: shared discovery exclusions. Preserve these
  exclusions when refreshing content; do not inadvertently publish an
  excluded project through asset staging.
- `src/pages/benchmarks.astro` and `src/components/BenchmarkRun.astro`: current
  dated routing attempts above the preserved historical archive.

The primary navigation says **Routing status** and retains `/benchmarks/` for
existing links. The September 12, 2026 collection includes actual partial
router outputs, so it is useful engineering evidence, but it does not support
competitive performance claims. Two other cases measured fallback inputs;
STRF has a negative connectivity delta and its declared tuned run did not
apply the intended rules. Keep these limitations beside the results. The
label describes the page's purpose without promising successful routing.

Project version comes from `pyproject.toml`; no manual version badge update is
needed. The footer separately records the site build time and source commit.
Board report timestamps and benchmark run dates remain dates of their own
evidence. Rebuilding the site does not make that evidence newer.

## Refresh board evidence and assets

The loader discovers board directories under `boards/` and one level below
`boards/external/`, applying the gallery exclusions. It reads optional
`output/board.json` summaries produced by `kct board-metrics` and independently
revalidates `output/readiness.json` against its recorded input hashes. See
[the board data contract](../docs/board-json-schema.md).

Missing, malformed or unsupported summaries do not break the gallery. Valid
readiness can still show development progress without a summary or a
manufacturing package. Missing/stale readiness is unverified; missing checks
are unreported. A development status never creates a download or implies
manufacturing release, assembly availability or hardware validation.

1. Select an identified source revision and inspect the board's own generation,
   checking and release instructions. Refresh its evidence with those producers
   when necessary. Never edit hashes, timestamps or statuses to make stale
   evidence look current. If verification cannot be completed, preserve the
   unverified state and explain the limitation.
2. Reconcile the selected PCB with its renders and any manufacturing package.
   Check manifest hashes, the nested editable-project PCB and the standalone
   viewer PCB. Existing committed outputs may be present; their existence is
   not proof that they match newly generated copper.
3. Generate optional summaries from the repository root:

   ```bash
   uv run kct board-metrics --all
   ```

   This extracts metadata and consumes readiness; it does not perform the
   missing native checks or qualify a manufacturing package.
4. Build and inspect the resulting gallery/detail pages. Confirm status,
   evidence date, render correspondence and which downloads are actually
   available. Keep generated summaries and staged assets out of source commits.

The staging script copies SVG/PNG renders, the explicit manufacturing-file
allowlist, an available full `manufacturing.zip`, and a selected output PCB.
It prefers an output filename containing `_routed` and publishes the viewer
file as `/boards/<slug>/board.kicad_pcb`. Inspect the selected source when
multiple candidates exist. Missing assets produce placeholders or omitted
links, not fabricated evidence. Adding a new download requires matching page
and staging behavior; consult `scripts/copy-renders.mjs` and the detail page
instead of copying an entire output directory into public assets.

## Refresh external routing evidence

Follow the pinned input/protocol procedure in
[the external benchmark guide](../benchmarks/external/README.md) and
[the results guide](../benchmarks/external/results/README.md). The
[September collection](../benchmarks/external/results/2026-09-12/README.md)
records a complete four-case example, including failures and limitations.

- Preserve old collections. Commit new reports under a new dated directory;
  retain raw JSON, human-readable reports and reproducibility records together.
  Record the tested source revision, environment/backend, inputs, protocol,
  runtime budget and actual outcome. Fetch third-party boards at runtime under
  their licensing terms; do not vendor them just to provide a website link.
- Distinguish input connectivity from newly routed connections and measured
  final connectivity. Keep signed regressions, failed-attempt timing,
  fallback-input provenance and absent checks explicit. A protocol name alone
  does not prove that tuning was applied. Keep vendor references separate.
- Review ingestion through `src/data/loadBenchmarks.ts` and the report contract
  in [benchmark-external-report-schema.md](../docs/benchmark-external-report-schema.md).
  The site reads committed data; building it does not launch routing runs.
- Add reviewed interpretation in `src/data/benchmarkContext.ts` against the
  exact report SHA-256. Never transfer old interpretation solely by board name
  or silently rebind it after changing report bytes. Check collection/runtime
  notes, per-run limitations and raw/reproduction links in the built page.
- Run the site tests/check/build and inspect both the new cards and preserved
  archive. A malformed or undiscovered report can be skipped by the loader;
  confirm that every intended case is visibly represented.

## Publish and verify public bytes

Publication is manual through `../scripts/deploy-site.sh`; there is no scheduled
or CI auto-deploy. Use one publication owner for a coordinated release. In
particular, #5318 demonstrated why updating only Board05's viewer would leave
its stale PCB in the downloadable project. Verify viewer, renders and packages
as one set.

The deployment workflow requires `uv`, Node/npm and authenticated `wrangler`
(or its `npx` fallback). `kicad-cli` is optional for the script, but required
for fresh renders. If it is absent, inspect and report the provenance of
retained renders; do not describe them as newly rendered. The script's
Cloudflare account identity guard must pass for the intended existing project.
Do not bypass the guard to resolve an authentication or wrong-account failure.

From the repository root, first prepare and inspect the build:

```bash
./scripts/deploy-site.sh --no-deploy --no-3d
```

This generates available renders/metrics and builds the site without uploading.
Review its warnings, asset hashes and local pages. `--no-3d` omits fresh 3D
rendering; use the full render path when new copper requires new images.
After the content PR is reviewed and merged, select that clean integrated
revision and publish:

```bash
./scripts/deploy-site.sh
```

The script rebuilds before upload, so inspect the final staged bytes as well.
`--preview` targets a preview branch rather than production; inspect the
returned URL and branch before treating it as a production release. See
`./scripts/deploy-site.sh --help` for available options.

For completion, retain the selected commit, deployed URL, build time and
verification results in the issue/PR:

1. Visit the public homepage, routing-status page and representative board
   pages, including the changed board and a development board. Verify the
   footer source/build identity, evidence dates and outcome/readiness labels.
2. Repeat the mobile/keyboard and navigation checks against the deployed site.
   Fetch report/doc links and all affected viewer/render/download assets.
3. Compare public asset SHA-256 hashes to the final staged build. Inspect
   nested package PCBs and manifest entries as well as the viewer. When copper
   was repaired, repeat the relevant geometry census and visually inspect the
   affected layer. Do not equate HTTP200 or a successful upload with freshness.
4. Record unresolved checks explicitly. Close publication work only after the
   public artifact checks pass; a local passing build is preparation evidence.

This site presents project evidence. Publication does not itself establish
DRC cleanliness, manufacturing readiness, component availability or physical
hardware performance.
