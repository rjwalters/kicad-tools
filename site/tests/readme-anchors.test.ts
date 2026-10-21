import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const REPO_LINK = /https:\/\/github\.com\/rjwalters\/kicad-tools#([A-Za-z0-9_-]+)/g;

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
