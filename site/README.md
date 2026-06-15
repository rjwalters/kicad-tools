# kicad-tools.org demo gallery (Astro site)

Static site for the kicad-tools demo gallery (Epic #3674, Phase 2). It is a
self-contained Astro project: it has its own `package.json` and does **not**
depend on the repository's Python tooling or root `package.json`.

## Quick start

```bash
cd site
npm install      # installs Astro + TypeScript into site/node_modules
npm run dev      # serves the site at http://localhost:4321/
npm run build    # produces a static site in site/dist/
npm run preview  # serves the built site/dist/ locally
npm run check    # Astro + TypeScript type check
npm test         # runs the loader unit tests (vitest)
```

`npm run build` succeeds even on a fresh checkout with **zero** `board.json`
files present — every board without data is listed as `status: no_artifacts`.

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
  package.json        # Astro + TypeScript dependencies (isolated from repo root)
  astro.config.mjs    # Astro configuration (static output)
  tsconfig.json       # extends astro/tsconfigs/strict
  src/
    data/
      types.ts         # Board / BoardSize / CostEstimate types (schema v1)
      loadBoards.ts    # build-time board data loader
      loadBoards.test.ts
    pages/
      index.astro      # placeholder slug + status listing
```

## Scope

This site currently ships only the scaffold, the board data loader, and a
placeholder index page. The polished gallery index (cards, renders, metrics) is
issue #3680, and the per-board detail page is issue #3681.
