// @ts-check
import { defineConfig } from "astro/config";

// kicad-tools.org demo gallery — static site.
// The board data loader (src/data/loadBoards.ts) runs at build time and reads
// board.json files from the sibling ../boards directory. See site/README.md.
export default defineConfig({
  site: "https://kicad-tools.org",
  // Static output: every page is pre-rendered to HTML at build time.
  output: "static",
});
