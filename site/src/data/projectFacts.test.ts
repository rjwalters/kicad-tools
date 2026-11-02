import { describe, expect, it } from "vitest";
import { readdirSync } from "node:fs";
import {
  extras,
  harnesses,
  mcpTools,
  parseExtras,
  parseHarnesses,
  parseMcpTools,
  parsePythonMinimum,
  parseSkill,
  pythonMinimum,
  QUICK_START_EXTRAS,
  skills,
} from "./projectFacts.ts";

describe("project facts read from the repository", () => {
  it("states the package's Python floor and the extras the quick start names", () => {
    expect(pythonMinimum).toMatch(/^3\.\d+$/);
    for (const extra of QUICK_START_EXTRAS) expect(extras).toContain(extra);
  });

  it("lists every packaged skill, and only skills", () => {
    const dir = new URL("../../../src/kicad_tools/agent_skills/kct/", import.meta.url);
    const files = readdirSync(dir).filter((f) => f.endsWith(".md") && f !== "README.md");
    expect(skills.map((s) => s.name).sort()).toEqual(files.map((f) => f.replace(/\.md$/, "")).sort());
    for (const skill of skills) {
      expect(skill.invocation).toBe(`/kct:${skill.name}`);
      expect(skill.summary.length).toBeGreaterThan(10);
      expect(skill.summary.length).toBeLessThan(400);
    }
  });

  it("reads the harnesses and MCP tools the package registers", () => {
    expect(harnesses).toContain("claude-code");
    expect(mcpTools.length).toBeGreaterThan(10);
    expect(mcpTools).toContain("board_summary");
  });
});

describe("project fact parsers", () => {
  it("reads requires-python from [project] only", () => {
    expect(parsePythonMinimum('[tool.x]\nrequires-python=">=2.7"\n[project]\nrequires-python = ">=3.11"\n')).toBe("3.11");
    expect(() => parsePythonMinimum('[project]\nname="x"\n')).toThrow();
  });

  it("reads optional-dependency keys and fails when there are none", () => {
    expect(parseExtras('[project.optional-dependencies]\nmcp = ["a"]\nall = [\n  "b",\n]\n[tool.x]\nz = 1\n')).toEqual(["mcp", "all"]);
    expect(() => parseExtras("[project]\n")).toThrow();
  });

  it("reads the HARNESSES tuple", () => {
    expect(parseHarnesses('HARNESSES: tuple[str, ...] = ("claude-code", "codex")\n')).toEqual(["claude-code", "codex"]);
    expect(() => parseHarnesses("OTHER = ()\n")).toThrow();
  });

  it("reads module-level register_tool names and rejects duplicates", () => {
    const src = 'def register_tool(\n    name: str,\n):\n    pass\n\nregister_tool(\n    name="a_tool",\n)\nregister_tool(\n    name="b_tool",\n)\n';
    expect(parseMcpTools(src)).toEqual(["a_tool", "b_tool"]);
    expect(() => parseMcpTools(src + 'register_tool(\n    name="a_tool",\n)\n')).toThrow();
    expect(() => parseMcpTools("x = 1\n")).toThrow();
  });

  it("summarises a skill by its description's first sentence", () => {
    const skill = parseSkill("---\nname: demo\ninvocation: /kct:demo\ndescription: Do a fab-ready thing — or refuse. Then more.\n---\n# Demo\n");
    expect(skill).toEqual({ name: "demo", invocation: "/kct:demo", summary: "Do a fab-ready thing — or refuse." });
    expect(() => parseSkill("# no front matter\n")).toThrow();
  });
});
