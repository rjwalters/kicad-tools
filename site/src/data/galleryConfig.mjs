/**
 * Shared gallery configuration.
 *
 * Imported by BOTH the TypeScript loader (`loadBoards.ts`) and the plain-ESM
 * staging script (`scripts/copy-renders.mjs`). Kept as `.mjs` (plain JS) so
 * `node scripts/copy-renders.mjs` can import it directly without TypeScript
 * transpilation, while Astro/Vitest resolve it from the `.ts` loader too.
 */

/**
 * Board slugs that must never be published to the gallery.
 *
 * `chorus-test-revA` is a private design symlinked into `boards/external/`;
 * it must produce no card, route, render, or staged PCB. Honored by both the
 * loader's discovery and the render-staging script so it is dropped uniformly.
 */
export const EXCLUDED_SLUGS = new Set(["chorus-test-revA"]);
