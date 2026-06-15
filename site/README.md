# kicad-tools.org demo gallery (Astro site)

Static site for the kicad-tools demo gallery (Epic #3674, Phase 2). It is a
self-contained Astro project: it has its own `package.json` and does **not**
depend on the repository's Python tooling or root `package.json`.

## Quick start

```bash
cd site
npm install        # installs Astro + TypeScript into site/node_modules
npm run dev        # serves the site at http://localhost:4321/
npm run build      # produces a static site in site/dist/
npm run preview    # serves the built site/dist/ locally
npm run check      # Astro + TypeScript type check
npm test           # runs the loader unit tests (vitest)
npm run copy-renders  # stage board renders into public/ (run automatically)
```

`npm run build` succeeds even on a fresh checkout with **zero** `board.json`
files present — every board without data is listed as `status: no_artifacts`.

## Gallery index page

The home page (`src/pages/index.astro`) renders one card per discovered board
using `src/components/BoardCard.astro`. Each card shows a thumbnail, the board
name + description, a status chip, and metric badges (nets routed, DRC, layers,
parts, cost). Badges follow the data contract's omit-when-absent rule — a badge
appears only when its backing field is present. Cards link to `/<slug>`, the
per-board detail route (issue #3681; that route 404s until it lands).

## Board detail pages

Each board also has a per-board detail page at the static route `/<slug>`
(`src/pages/[slug].astro`), generated via Astro's `getStaticPaths` driven by the
same loader — one route per discovered board, including `no_artifacts` stubs so
the gallery cards never 404. A detail page shows:

- a four-render 2×2 gallery (`src/components/RenderGallery.astro`) — 2D
  front/back and 3D front/back, each cell showing the real PNG or a labelled
  placeholder so the grid never collapses;
- a metrics table mapping every present `board.json` field (status, nets routed,
  DRC, layers, board size, parts, cost breakdown, data-generated timestamp),
  following the omit-when-absent rule — absent fields produce no row;
- a downloads section linking the manufacturing package
  (`kicad_project.zip`) plus any optional `report.pdf` / `bom_jlcpcb.csv` /
  `cpl_jlcpcb.csv` that exist for the board (detected at build time);
- a clearly-commented placeholder for the Phase 4 interactive PCB viewer (not
  implemented here);
- back-links to the gallery index.

For `no_artifacts` boards the page renders a "not yet built" notice with four
placeholder render cells, a status-only metrics table, and a "No manufacturing
package available" notice — never a broken page.

### Render images and downloads (`copy-renders` prebuild step)

Astro's static output cannot import assets from outside `site/`, so board
artifacts are staged into `site/public/` before the build. The
`copy-renders` script (`scripts/copy-renders.mjs`) copies:

- `boards/<id>/output/renders/*.png` →
  `site/public/boards/<slug>/renders/<file>` (served at
  `/boards/<slug>/renders/<file>`); and
- the allow-listed manufacturing downloads
  (`kicad_project.zip`, `report.pdf`, `bom_jlcpcb.csv`, `cpl_jlcpcb.csv`) from
  `boards/<id>/output/manufacturing/` →
  `site/public/boards/<slug>/manufacturing/<file>` (served at
  `/boards/<slug>/manufacturing/<file>`). Bulky intermediates (gerbers, images)
  are intentionally **not** staged.

It runs automatically via the `predev` / `prebuild` npm hooks, so plain
`npm run dev` and `npm run build` stage renders and downloads for you. It can
also be run on its own with `npm run copy-renders`.

The thumbnail fallback chain is: `renders["3d_front"]` → `renders["pcb_front"]`
→ the static placeholder at `public/placeholder-board.svg` (used for
`no_artifacts` boards or any board missing both render keys).

The staged `site/public/boards/` tree is **git-ignored** and regenerated on
every build — it is never committed, just like the source renders and
`board.json` files (which are themselves generated artifacts).

## Board data

The build-time loader (`src/data/loadBoards.ts`) discovers board directories
under the repository's `boards/` tree and reads each board's
`boards/<id>/output/board.json` — the schema-v1 data contract documented in
[`../docs/board-json-schema.md`](../docs/board-json-schema.md) and produced by
`kct board-metrics`.

The loader is resilient to missing data:

- **No `board.json`** → a stub record with `status: "no_artifacts"`.
- **Unknown `schema_version`** → skipped with a warning (a stub is emitted), so
  the build still completes with the remaining boards.
- **Valid `board.json`** → parsed into a typed `Board` (see `src/data/types.ts`).

Boards are discovered with the same rules as the Python producer: immediate
subdirectories of `boards/`, skipping hidden / `_`-prefixed entries, and
descending one level into `boards/external/`.

### Generating real board data (optional)

`board.json` files are **not** committed to the repository — they are generated
at runtime. To populate them for local development, run the Phase 1 command from
the repository root:

```bash
# From the repo root (not site/)
uv run kct board-metrics --all
```

Then build or serve the site:

```bash
cd site
npm install
npm run dev
```

With data present, the placeholder index lists every board's real `status`
(`ok` / `partial` / `no_artifacts`).

## Layout

```
site/
  package.json          # Astro + TypeScript dependencies (isolated from repo root)
  astro.config.mjs      # Astro configuration (static output)
  tsconfig.json         # extends astro/tsconfigs/strict
  scripts/
    copy-renders.mjs    # prebuild: stage renders + manufacturing files into public/
  public/
    placeholder-board.svg  # thumbnail/render fallback for boards with no renders
  src/
    components/
      BoardCard.astro    # gallery card (thumbnail + badges + status + link)
      RenderGallery.astro # detail-page 2×2 render grid
    data/
      types.ts           # Board / BoardSize / CostEstimate types (schema v1)
      loadBoards.ts      # build-time board data loader
      loadBoards.test.ts
    pages/
      index.astro        # gallery index — one card per board
      [slug].astro       # per-board detail page (renders, metrics, downloads)
```

## Scope

This site ships the scaffold, the board data loader, the gallery index page
(cards, renders, metric badges), and the per-board detail page (render gallery,
metrics table, downloads). The interactive PCB viewer is Phase 4 — the detail
page leaves a commented placeholder where it will embed.
