import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";

const REPO_LINK = /(?:https:\/\/github\.com\/rjwalters\/kicad-tools|\$\{REPO\})#([A-Za-z0-9_-]+)/g;

/** GitHub-style heading slug: lowercase, drop punctuation, spaces to hyphens. */
export function slugify(heading: string): string {
  return heading
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

/** All heading anchors in a markdown document, ignoring fenced code, with GitHub's -N suffix for duplicates. */
export function headingAnchors(markdown: string): Set<string> {
  const anchors = new Set<string>();
  const seen = new Map<string, number>();
  let fenced = false;
  for (const line of markdown.split("\n")) {
    if (/^\s*```/.test(line)) {
      fenced = !fenced;
      continue;
    }
    const match = !fenced && /^#{1,6}\s+(.*?)\s*#*\s*$/.exec(line);
    if (!match) continue;
    const slug = slugify(match[1]);
    const count = seen.get(slug) ?? 0;
    seen.set(slug, count + 1);
    anchors.add(count === 0 ? slug : `${slug}-${count}`);
  }
  return anchors;
}

export function readmeAnchorLinks(source: string): string[] {
  return [...source.matchAll(REPO_LINK)].map((m) => m[1]);
}

describe("homepage README anchors", () => {
  const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
  const page = readFileSync(new URL("../src/pages/index.astro", import.meta.url), "utf8");

  it("resolves every github.com/rjwalters/kicad-tools#anchor link to a README heading", () => {
    const anchors = headingAnchors(readme);
    const links = readmeAnchorLinks(page);
    expect(links.length).toBeGreaterThan(0);
    const missing = links.filter((a) => !anchors.has(a));
    expect(missing).toEqual([]);
  });

  it("slugifies headings with backticks and parentheses like GitHub", () => {
    const anchors = headingAnchors("## Agent Skills (`kct` namespace)\n## Using it from Claude Code, Codex and opencode");
    expect(anchors.has("agent-skills-kct-namespace")).toBe(true);
    expect(anchors.has("using-it-from-claude-code-codex-and-opencode")).toBe(true);
  });

  it("numbers duplicate headings and ignores headings inside code fences", () => {
    const anchors = headingAnchors("## Usage\n```\n# not-a-heading\n```\n## Usage");
    expect(anchors).toEqual(new Set(["usage", "usage-1"]));
  });

  it("flags a renamed anchor", () => {
    const anchors = headingAnchors("## Python API\n");
    expect(readmeAnchorLinks("https://github.com/rjwalters/kicad-tools#python-api")
      .every((a) => anchors.has(a))).toBe(true);
    expect(readmeAnchorLinks("https://github.com/rjwalters/kicad-tools#python-apis")
      .every((a) => anchors.has(a))).toBe(false);
  });
});

const BLOB_LINK = /\$\{DOCS\}\/([A-Za-z0-9_./-]+)|\$\{REPO\}\/blob\/main\/([A-Za-z0-9_./-]+)|https:\/\/github\.com\/rjwalters\/kicad-tools\/blob\/main\/([A-Za-z0-9_./-]+)/g;

/** Repository paths a page links to, from literal URLs and the `${DOCS}` / `${REPO}/blob/main` templates. */
export function blobLinkPaths(source: string): string[] {
  return [...source.matchAll(BLOB_LINK)].map((m) => (m[1] ? `docs/${m[1]}` : (m[2] ?? m[3])));
}

describe("homepage and header documentation links (issue #8101)", () => {
  const sources = ["../src/pages/index.astro", "../src/components/Header.astro"].map((rel) =>
    readFileSync(new URL(rel, import.meta.url), "utf8"),
  );

  it("points every blob/main link at a file that exists on main", () => {
    const paths = sources.flatMap(blobLinkPaths);
    expect(paths.length).toBeGreaterThan(5);
    const missing = paths.filter((p) => !existsSync(new URL(`../../${p}`, import.meta.url)));
    expect(missing).toEqual([]);
  });

  it("extracts paths from every link form", () => {
    expect(blobLinkPaths("href={`${DOCS}/mcp/README.md`} href={`${REPO}/blob/main/docs/README.md`}"))
      .toEqual(["docs/mcp/README.md", "docs/README.md"]);
    expect(blobLinkPaths("https://github.com/rjwalters/kicad-tools/blob/main/CHANGELOG.md")).toEqual(["CHANGELOG.md"]);
  });
});
