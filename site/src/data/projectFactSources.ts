/**
 * Repository paths (relative to the repo root) that `projectFacts.ts` reads at
 * build time. Kept side-effect free so test fixtures that build the site in a
 * temporary tree can copy exactly these files (issue #8101).
 */
export const PROJECT_FACT_SOURCES = [
  "pyproject.toml",
  "src/kicad_tools/agent_surfaces.py",
  "src/kicad_tools/mcp/tools/registry.py",
  "src/kicad_tools/agent_skills/kct",
] as const;
