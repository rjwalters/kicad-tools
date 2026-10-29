/** Build the real site and assert the not-found page is emitted (issue #7667). */
import { afterAll, beforeAll, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "..");
let root: string;
let fixtureSite: string;

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "kct-gallery-404-"));
  fixtureSite = join(root, "site");
  const boards = join(root, "boards");
  mkdirSync(fixtureSite);
  mkdirSync(boards);
  cpSync(join(site, "..", "pyproject.toml"), join(root, "pyproject.toml"));
  // projectFacts.ts reads these from the repo root at build time.
  for (const f of ["src/kicad_tools/agent_skills/kct", "src/kicad_tools/mcp/tools/registry.py"]) {
    mkdirSync(dirname(join(root, f)), { recursive: true });
    cpSync(join(site, "..", f), join(root, f), { recursive: true });
  }
  for (const entry of ["src", "scripts", "astro.config.mjs", "package.json", "tsconfig.json"]) {
    cpSync(join(site, entry), join(fixtureSite, entry), { recursive: true });
  }
  symlinkSync(join(site, "node_modules"), join(fixtureSite, "node_modules"), "dir");
  const env = { ...process.env, KCT_BOARDS_DIR: boards, ASTRO_TELEMETRY_DISABLED: "1" };
  const opts = { cwd: fixtureSite, env, encoding: "utf8" as const, timeout: 60_000, stdio: "pipe" as const };
  execFileSync(process.execPath, ["scripts/copy-renders.mjs"], opts);
  execFileSync(process.execPath, [join(site, "node_modules/astro/bin/astro.mjs"), "build"], opts);
}, 120_000);

afterAll(() => { if (root) rmSync(root, { recursive: true, force: true }); });

it("emits dist/404.html with shared chrome and recovery links", () => {
  const file = join(fixtureSite, "dist", "404.html");
  expect(existsSync(file)).toBe(true);
  const html = readFileSync(file, "utf8");
  expect(html).toContain("Page not found");
  expect(html).toContain('href="/benchmarks"');
  expect(html).toContain('class="site-nav"');
  // On the 404 path no nav item should be marked as the current page.
  expect(html).not.toContain('aria-current="page"');
});
