/**
 * Build-time board data loader for the kicad-tools.org demo gallery.
 *
 * Discovers every board directory under the repo's `boards/` tree, reads each
 * board's `boards/<id>/output/board.json` (the schema-v1 contract produced by
 * `kct board-metrics`), and returns a typed, slug-sorted `Board[]`.
 *
 * Design notes (see `docs/board-json-schema.md` and issue #3679):
 *   - Runs at build time only (Node `fs`). Not bundled into client output.
 *   - Resilient to missing files: NO `board.json` files exist on a fresh
 *     checkout — they are generated at runtime via `kct board-metrics --all`.
 *     A board directory with no `board.json` yields a stub record with
 *     `status: "no_artifacts"` rather than crashing the build.
 *   - Forward-compat guard: a `board.json` whose `schema_version` is not the
 *     version we understand is logged and SKIPPED (a stub is emitted instead),
 *     so the build still completes with the remaining boards.
 *   - Board discovery mirrors `kct board-metrics`' `_iter_board_dirs`: immediate
 *     subdirectories of `boards/`, skipping hidden / `_`-prefixed entries, and
 *     descending one level into `external/`.
 */

import { readdirSync, existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { SCHEMA_VERSION } from "./types.ts";
import type { Board, BoardCategory, BoardStatus } from "./types.ts";
import { EXCLUDED_SLUGS } from "./galleryConfig.mjs";

/** True if a discovered board path lives under `boards/external/`. */
function categoryForPath(boardPath: string): BoardCategory {
  return /(^|[\\/])external[\\/]/.test(boardPath) ? "project" : "demo";
}

const VALID_STATUSES: ReadonlySet<string> = new Set<BoardStatus>([
  "ok",
  "partial",
  "no_artifacts",
]);

/** Directory of this module when executed unbundled (Node ESM, vitest). */
const MODULE_DIR = dirname(fileURLToPath(import.meta.url));

/**
 * Absolute path to the repository's `boards/` directory.
 *
 * Resolution order:
 *   1. `KCT_BOARDS_DIR` env override (used by tests and CI).
 *   2. `<cwd>/../boards` — `astro build`/`dev` run with cwd = `site/`, so the
 *      repo root is one level up. This path is stable even after Vite bundles
 *      this module (bundling rewrites `import.meta.url`, so we cannot rely on
 *      the module's own location during a build).
 *   3. `<module>/../../../boards` — fallback for direct unbundled execution
 *      (e.g. `node`/vitest importing `src/data/loadBoards.ts`).
 */
export function boardsDir(): string {
  const override = process.env.KCT_BOARDS_DIR;
  if (override) return resolve(override);

  const fromCwd = resolve(process.cwd(), "..", "boards");
  if (isDir(fromCwd)) return fromCwd;

  return resolve(MODULE_DIR, "..", "..", "..", "boards");
}

/** True if `path` exists and is a directory. */
function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Enumerate board directories under `root`, mirroring the Python producer's
 * discovery rules so the site lists exactly the boards `kct board-metrics`
 * would emit.
 *
 * Returns absolute paths to each board directory, in discovery order.
 */
export function discoverBoardDirs(root: string): string[] {
  if (!isDir(root)) return [];

  const dirs: string[] = [];
  for (const entry of readdirSync(root).sort()) {
    if (entry.startsWith(".") || entry.startsWith("_")) continue;
    if (EXCLUDED_SLUGS.has(entry)) continue;
    const full = join(root, entry);
    if (!isDir(full)) continue;

    if (entry === "external") {
      // Group directory: descend one level.
      for (const sub of readdirSync(full).sort()) {
        if (sub.startsWith(".")) continue;
        if (EXCLUDED_SLUGS.has(sub)) continue;
        const subFull = join(full, sub);
        if (isDir(subFull)) dirs.push(subFull);
      }
      continue;
    }
    dirs.push(full);
  }
  return dirs;
}

/** Construct a `no_artifacts` stub for a board with no parsable `board.json`. */
function makeStub(slug: string, category: BoardCategory): Board {
  return {
    $schema: "https://kicad-tools.org/schemas/board/v1.json",
    schema_version: SCHEMA_VERSION,
    generated_at: new Date(0).toISOString(),
    slug,
    status: "no_artifacts",
    category,
  };
}

/**
 * Validate a parsed JSON value against the required-field shape of schema v1.
 * Returns the value typed as `Board` when valid, or `null` when it is missing
 * required fields / has an unknown `schema_version`.
 *
 * Note: only required-field presence and the `status` enum are enforced.
 * Optional fields are passed through as-is (the producer guarantees their
 * shapes per the contract).
 */
function validateBoard(data: unknown, slug: string): Board | null {
  if (typeof data !== "object" || data === null) {
    console.warn(`[loadBoards] ${slug}: board.json is not an object; using stub`);
    return null;
  }
  const obj = data as Record<string, unknown>;

  if (obj.schema_version !== SCHEMA_VERSION) {
    console.warn(
      `[loadBoards] ${slug}: unknown schema_version ` +
        `${JSON.stringify(obj.schema_version)} (expected ${SCHEMA_VERSION}); skipping`,
    );
    return null;
  }

  const required = ["$schema", "generated_at", "slug", "status"] as const;
  for (const key of required) {
    if (!(key in obj)) {
      console.warn(`[loadBoards] ${slug}: board.json missing required field "${key}"; using stub`);
      return null;
    }
  }

  if (typeof obj.status !== "string" || !VALID_STATUSES.has(obj.status)) {
    console.warn(`[loadBoards] ${slug}: invalid status ${JSON.stringify(obj.status)}; using stub`);
    return null;
  }

  return obj as unknown as Board;
}

/**
 * Read and validate a single board directory's `board.json`.
 * Always returns a `Board`: a parsed record when valid, otherwise a stub.
 */
export function loadBoard(boardPath: string): Board {
  const slug = boardPath.split(/[\\/]/).filter(Boolean).pop() ?? boardPath;
  const category = categoryForPath(boardPath);
  const jsonPath = join(boardPath, "output", "board.json");

  if (!existsSync(jsonPath)) {
    return makeStub(slug, category);
  }

  let raw: string;
  try {
    raw = readFileSync(jsonPath, "utf8");
  } catch (err) {
    console.warn(`[loadBoards] ${slug}: failed to read board.json (${String(err)}); using stub`);
    return makeStub(slug, category);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    console.warn(`[loadBoards] ${slug}: board.json is not valid JSON (${String(err)}); using stub`);
    return makeStub(slug, category);
  }

  const board = validateBoard(parsed, slug);
  if (!board) return makeStub(slug, category);
  // `category` is loader-assigned (not part of board.json); always set it.
  board.category = category;
  return board;
}

/**
 * Load every board, slug-sorted.
 *
 * Never throws for missing/invalid `board.json` — those become stubs so the
 * static build always succeeds, even with zero `board.json` files present.
 */
export function loadBoards(root: string = boardsDir()): Board[] {
  const boards = discoverBoardDirs(root).map(loadBoard);
  boards.sort((a, b) => a.slug.localeCompare(b.slug));
  return boards;
}
