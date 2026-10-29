import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  featuredExtras,
  loadProjectFacts,
  parseExtras,
  parseMcpTools,
  parseRequiresPython,
  parseSkills,
  projectFacts,
} from "./projectFacts.ts";

const root = new URL("../../../", import.meta.url);
const read = (p: string) => readFileSync(new URL(p, root), "utf8");

describe("project facts", () => {
  it("reads the Python requirement and extras from pyproject.toml", () => {
    expect(projectFacts.requiresPython).toBe(parseRequiresPython(read("pyproject.toml")));
    expect(projectFacts.requiresPython).toMatch(/^>=3\.\d+/);
    expect(projectFacts.extras).toContain("mcp");
    expect(parseExtras(read("pyproject.toml"))).toEqual(expect.arrayContaining(projectFacts.extras));
  });
  it("lists skills from agent_skills/kct, excluding README and help", () => {
    expect(projectFacts.skills).toContain("tapeout");
    expect(projectFacts.skills).not.toContain("help");
    expect(projectFacts.skills).not.toContain("README");
  });
  it("lists MCP tool names from the registry, including parts_*", () => {
    expect(projectFacts.mcpTools).toEqual(expect.arrayContaining(["export_gerbers", "parts_search"]));
    expect(projectFacts.partsTools.every((t) => t.startsWith("parts_"))).toBe(true);
    expect(new Set(projectFacts.mcpTools).size).toBe(projectFacts.mcpTools.length);
  });
  it("loads the same facts from an explicit root", () => {
    expect(loadProjectFacts(new URL(root).pathname)).toEqual(projectFacts);
  });
  it("fails loudly when sources are missing or reformatted", () => {
    expect(() => parseRequiresPython("[project]\nname='x'")).toThrow();
    expect(() => parseExtras("[project]\nname='x'")).toThrow();
    expect(() => featuredExtras(["geometry"])).toThrow(/mcp/);
    expect(() => parseSkills({ "README.md": "x" })).toThrow();
    expect(() => parseSkills({ "a.md": "no frontmatter" })).toThrow();
    expect(() => parseMcpTools("nothing here")).toThrow();
  });
  it("parses skill frontmatter names", () => {
    expect(parseSkills({ "a.md": "---\nname: alpha\ndescription: d\n---\n", "help.md": "" })).toEqual(["alpha"]);
  });
});
