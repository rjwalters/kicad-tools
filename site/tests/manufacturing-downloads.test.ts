/** Exercise the real staging script and rendered Astro pages, not copied link logic. */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const combinations = [
  { slug: "both", source: true, gerber: true },
  { slug: "source-only", source: true, gerber: false },
  { slug: "gerber-only", source: false, gerber: true },
  { slug: "neither", source: false, gerber: false },
];
const sourceFile = "kicad_project.zip";
const gerberFile = "gerbers/gerbers.zip";
let root: string;
let fixtureSite: string;
let boards: string;

function run(command: string, args: string[]) {
  return execFileSync(command, args, {
    cwd: fixtureSite,
    env: { ...process.env, KCT_BOARDS_DIR: boards, ASTRO_TELEMETRY_DISABLED: "1" },
    encoding: "utf8",
    timeout: 60_000,
    stdio: "pipe",
  });
}
function build() {
  run(process.execPath, ["scripts/copy-renders.mjs"]);
  run(process.execPath, [join(site, "node_modules/astro/bin/astro.mjs"), "build"]);
}
function html(slug: string) {
  return readFileSync(join(fixtureSite, "dist", slug, "index.html"), "utf8");
}
function staged(slug: string, file: string) {
  return join(fixtureSite, "public/boards", slug, "manufacturing", file);
}

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "kct-gallery-downloads-"));
  fixtureSite = join(root, "site");
  boards = join(root, "boards");
  mkdirSync(fixtureSite);
  for (const entry of ["src", "scripts", "astro.config.mjs", "package.json", "tsconfig.json"]) {
    cpSync(join(site, entry), join(fixtureSite, entry), { recursive: true });
  }
  symlinkSync(join(site, "node_modules"), join(fixtureSite, "node_modules"), "dir");
  for (const { slug, source, gerber } of combinations) {
    const output = join(boards, slug, "output");
    mkdirSync(join(output, "manufacturing/gerbers"), { recursive: true });
    // Deliberately stale metadata on absent-source cases proves existence gates links.
    writeFileSync(join(output, "board.json"), JSON.stringify({
      $schema: "https://kicad-tools.org/schemas/board/v1.json",
      schema_version: 1, generated_at: "2026-09-10T00:00:00Z", slug,
      status: "ok", manufacturing_package: "manufacturing/kicad_project.zip",
    }));
    for (const [file, present] of [[sourceFile, source], [gerberFile, gerber]] as const) {
      if (present) writeFileSync(join(output, "manufacturing", file), `distinct bytes: ${slug}/${file}`);
    }
    writeFileSync(join(output, "manufacturing/private-intermediate.txt"), "must not stage");
  }
  build();
}, 120_000);

afterAll(() => { if (root) rmSync(root, { recursive: true, force: true }); });

describe("source and fabrication download presence", () => {
  for (const { slug, source, gerber } of combinations) {
    it(`stages only existing allow-listed files for ${slug}`, () => {
      for (const [file, present] of [[sourceFile, source], [gerberFile, gerber]] as const) {
        expect(existsSync(staged(slug, file))).toBe(present);
        if (present) {
          expect(readFileSync(staged(slug, file))).toEqual(
            readFileSync(join(boards, slug, "output/manufacturing", file)),
          );
        }
      }
      expect(existsSync(staged(slug, "private-intermediate.txt"))).toBe(false);
    });
    it(`renders independent, accurately labeled links for ${slug}`, () => {
      const page = html(slug);
      for (const [file, present, label] of [
        [sourceFile, source, "KiCad project (ZIP)"],
        [gerberFile, gerber, "Gerber fabrication files (ZIP)"],
      ] as const) {
        const href = `/boards/${slug}/manufacturing/${file}`;
        expect(page.includes(`href="${href}"`)).toBe(present);
        if (present) {
          const link = page.match(new RegExp(`<a\\b[^>]*href="${href.replaceAll(".", "\\.")}"[^>]*>([\\s\\S]*?)</a>`));
          expect(link?.[1]).toContain(label);
          expect(existsSync(join(fixtureSite, "dist", href))).toBe(true);
        }
      }
      expect(page).not.toContain("Manufacturing package (ZIP)");
      expect(page.includes('class="download-list"')).toBe(source || gerber);
    });
  }
  it("removes previously staged downloads and links when both source files disappear", () => {
    rmSync(join(boards, "both/output/manufacturing", sourceFile));
    rmSync(join(boards, "both/output/manufacturing", gerberFile));
    build();
    for (const file of [sourceFile, gerberFile]) {
      expect(existsSync(staged("both", file))).toBe(false);
      expect(html("both")).not.toContain(`href="/boards/both/manufacturing/${file}"`);
      expect(existsSync(join(fixtureSite, "dist/boards/both/manufacturing", file))).toBe(false);
    }
  }, 120_000);
});
