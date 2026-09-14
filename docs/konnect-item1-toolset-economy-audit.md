# Konnect item 1 audit — on-demand MCP toolsets for context economy — 2026-09

**Issue:** #4896 (item 1 of #4880's 13-item Konnect audit)
**Verified at:** `eb8544d3` (2026-09-14)
**Scope:** read-only with respect to `src/`. This is an audit/decision
deliverable, not a code-change PR — see the Verdict section for why no
implementation is bundled.

> **Citation style — symbol anchors, not line numbers (repo policy #4764,
> `tests/test_docs_source_citations.py`).** Every claim below is anchored on a
> **symbol + file path**, not a line number, because `docs/*.md` is a policed
> glob and line citations rot silently on refactor. Verify any claim with:
> ```bash
> rg -n '<symbol>' <path>
> ```

---

## 0. Question this audit answers

Konnect (a 203-tool KiCAD MCP server, AGPL-3.0, ideas-only reference — see
Konnect source note below) pre-loads a small ~2K-token starter kit instead of
its full `tools/list`, and lets the calling LLM discover and pull in more
tools on demand: `list_toolboxes` → `load_toolset(name|[names])` /
`unload_toolset`, `tools/list_changed` push notifications after a
load/unload, and a "tool not loaded" error that names the owning toolset so
recovery is one hop.

**Does kicad-tools' `tools/list` cost enough tokens, today, to justify
building the same on-demand mechanism from scratch?**

**Verdict: DECLINE.** See §4.

---

## 1. Measurement: `tools/list` token cost today

`TOOL_REGISTRY` (`src/kicad_tools/mcp/tools/registry.py`) is the single
source of truth for both transports — `MCPServer.get_tools_list()`
(`src/kicad_tools/mcp/server.py`) serves it over stdio, and
`create_fastmcp_server`'s `RegistryFastMCP.list_tools()` override (same file)
serves the identical payload over HTTP by delegating to the same
`MCPServer` dispatcher instance.

Reproduced with the issue's own measurement script (`register_tool` builds
each `ToolSpec` with `name`/`description`/`parameters`/`category` —
`src/kicad_tools/mcp/tools/registry.py`) against `eb8544d3`:

```
Tool count: 44
Categories: 11
  analysis: 3 tools, 2733 chars
  context: 8 tools, 5274 chars
  export: 4 tools, 3390 chars
  mistakes: 2 tools, 1206 chars
  observability: 1 tools, 883 chars
  patterns: 4 tools, 2325 chars
  placement: 6 tools, 7204 chars
  routing: 3 tools, 2757 chars
  screenshot: 2 tools, 2381 chars
  session: 10 tools, 5354 chars
  workflow: 1 tools, 1699 chars
Total chars: 35206
Approx tokens (chars/4): 8801.5
```

Same result independent of key order: serializing the full payload list as
the wire format (`{"name", "description", "inputSchema"}` per tool, matching
`get_tools_list`) gives **35,294 chars / ~8,824 tokens compact**, and
**46,802 chars / ~11,701 tokens pretty-printed** (`json.dumps(..., indent=2)`
— relevant only if a client logs or re-serializes the payload for a
transcript; the wire format itself is compact).

This matches the curation pass's own re-run to within rounding (35,206 vs
35,294 chars — the ~90-char difference is measuring per-category dict sum
vs. one concatenated list, not a regression) — the number is stable across
this window of development.

**Comparison to Konnect's own motivating number:** Konnect's README cites
~23K tokens for its full 203-tool `tools/list`, the number it built the
starter-kit mechanism to avoid. kicad-tools' 44-tool registry costs **~8.8K
tokens compact — about 38% of Konnect's own cited threshold, using less
than a quarter of the tool count** (44 / 203 ≈ 22%). Per-tool, kicad-tools'
tools average more schema (≈200 tokens/tool vs. Konnect's ≈113 tokens/tool,
consistent with richer per-parameter descriptions), but the aggregate stays
well inside a budget that did not motivate Konnect's own maintainers to
build the mechanism until crossing a much higher tool count.

---

## 2. Technical investigation: the notification-capability gap

Konnect's mechanism is not just a filtered tool list — it depends on
`tools/list_changed` **push** notifications so a client that cached
`tools/list` at session start learns about a newly-loaded toolset without
polling. This repo has two transports; both were checked directly against
the currently pinned/installed SDK rather than assumed.

### 2.1 stdio — structurally cannot push today

`MCPServer.run()` (`src/kicad_tools/mcp/server.py`) is a synchronous
`for line in sys.stdin` loop: read one JSON-RPC request, `handle_request`,
write one response, repeat. There is no channel to emit an unsolicited
notification mid-loop without a structural rewrite to an async
read/write-stream architecture. `MCPServer.handle_request`'s `initialize`
response already declares this honestly: `"tools": {"listChanged": False}`
(same symbol, same file) — the codebase does not currently claim a
capability it can't back.

### 2.2 HTTP — the SDK exists, but nothing wires it automatically

`create_fastmcp_server` (`src/kicad_tools/mcp/server.py`) imports
`from mcp.server.fastmcp import FastMCP` — the **official MCP Python SDK's**
`FastMCP` (package `mcp`, installed version 1.28.1), not the third-party
`fastmcp` package pinned in `pyproject.toml` (`"fastmcp>=2.0,<4"`) for the
`mcp` extra. That pin is unused by this import path today — worth noting as
a latent naming trap for the next person who greps `pyproject.toml` expecting
it to explain `server.py`'s behavior.

Checked directly in the installed `mcp` 1.28.1 tree:

- `mcp.server.fastmcp.server.FastMCP.add_tool` / `.remove_tool` mutate the
  tool manager's storage only — neither calls send a
  `notifications/tools/list_changed` notification. A caller must do that
  manually via `mcp.server.session.ServerSession.send_tool_list_changed()`,
  reachable from a running request through `FastMCP.get_context()`.
- `mcp.server.lowlevel.server.Server.create_initialization_options` sets the
  advertised `ToolsCapability(listChanged=...)` from a `NotificationOptions`
  the embedding server supplies — so declaring the capability and actually
  emitting the notification are two separate, both-required steps; neither
  happens automatically from `add_tool`/`remove_tool`.

So the "genuine push" building block (`send_tool_list_changed`) does exist
in the pinned SDK and is reachable from a tool handler — but nothing in
`RegistryFastMCP` (`src/kicad_tools/mcp/server.py`) calls it, and
`RegistryFastMCP` doesn't even route through `FastMCP`'s own tool manager:
its `list_tools`/`call_tool` overrides (same class) delegate straight to the
shared `MCPServer` dispatcher, bypassing `add_tool`/`remove_tool` entirely.
Wiring dynamic load/unload here means building a second, HTTP-only tool
manager layered on top of the registry-driven one that already exists —
real but nontrivial net-new plumbing, not a small addition.

### 2.3 Why "partial: static, no notifications" is not actually cheaper to ship safely

The MCP spec's `notifications/tools/list_changed` exists specifically so a
client does not have to re-poll `tools/list` speculatively — clients that
conform to that expectation cache `tools/list` at session start and refresh
only on the push. A `load_toolset` tool that mutates the server's exposed
set **without** ever sending that notification is invisible to exactly
those clients until their next reconnect — the mechanism would silently do
nothing for a spec-conforming client on the one transport (stdio) most
kicad-tools MCP clients use. Shipping the "tool not loaded" error (per the
issue's acceptance criteria, for whichever flavor is adopted) partially
compensates — a client that tries an unlisted tool gets pointed at
`load_toolset` — but the LLM still has to already know to try calling a
tool it cannot see in its own schema list, which only works if
`list_toolboxes` primes it with tool names up front, at which point the
token savings from hiding those schemas is partially spent back on the
toolbox directory itself.

---

## 3. Net token-economy math

Assume the "partial, static" shape: an always-visible `list_toolboxes` tool
listing the 11 categories, plus `load_toolset`/`unload_toolset`. A
`list_toolboxes` payload naming 11 categories with a one-line summary each
is small (rough estimate: 11 × ~50 tokens ≈ 550 tokens, well under the
`observability` category's single-tool cost of 883 chars / ~220 tokens
today) — plus the schemas for `list_toolboxes`, `load_toolset`, and
`unload_toolset` themselves, three more tools added to whatever starter set
ships by default. Against a 44-tool / ~8.8K-token baseline that is not
close to Konnect's own ~23K motivating number, the mechanism's net savings
— even before subtracting its own overhead and the "tool not loaded" retry
round-trip cost when the LLM guesses wrong — are small in absolute terms
and asymmetric with its implementation/maintenance cost (a second HTTP tool
manager per §2.2, and a documented but real stdio gap per §2.1).

---

## 4. Verdict: DECLINE

1. **Measured token cost does not motivate the mechanism.** ~8.8K tokens
   (compact) for 44 tools is ~38% of Konnect's own cited ~23K threshold for
   203 tools — the repository would need roughly 2.5x its current tool
   count before approaching the budget pressure that motivated Konnect's
   maintainers to build this.
2. **The notification-capability gap is real, not cosmetic, on the primary
   transport.** stdio (`MCPServer.run`, `src/kicad_tools/mcp/server.py`) is
   a synchronous single-request-at-a-time loop that already declares
   `listChanged: False` honestly; adding load/unload tools without push
   support ships a mechanism invisible to spec-conforming stdio clients
   until reconnect (§2.3) — worse than not building it, because it looks
   functional in a manual test (the LLM just called `tools/list` again)
   but is not reliable under normal client caching behavior.
3. **HTTP is technically feasible but not free.** The building block
   (`ServerSession.send_tool_list_changed`) exists in the pinned SDK, but
   `RegistryFastMCP` (`src/kicad_tools/mcp/server.py`) bypasses the SDK's
   own tool manager entirely, so wiring dynamic load/unload means building
   a second, HTTP-only mutable-tool-manager layer — real implementation
   and maintenance cost for a token saving that is not currently needed.
4. **No adoption pressure signal exists elsewhere in the tree.** `git grep`
   for prior context-budget complaints against `tools/list`
   (`rg -n "tools/list" -g '!*.md'` outside `mcp/`) turns up no caller-side
   workaround, truncation, or complaint — the 44-tool payload has not been
   reported as a problem.

Per the issue's own framing, a "decline" or "adopt (partial, static)"
outcome was explicitly plausible pending investigation — this audit's
conclusion is decline, on the strength of (1) the token measurement being
well under Konnect's own threshold and (2) the "partial, static, no
notifications" flavor's practical unreliability under normal MCP client
caching semantics (§2.3), which removes it as a genuinely cheaper
middle path rather than just a smaller version of the same investment.

### 4.1 Reconsideration triggers

Revisit this decision if either becomes true:

- `TOOL_REGISTRY` (`src/kicad_tools/mcp/tools/registry.py`) grows to
  roughly 100+ tools (approaching half of Konnect's own 203-tool,
  ~23K-token threshold at kicad-tools' current ~200 tokens/tool average).
- stdio (`MCPServer`, `src/kicad_tools/mcp/server.py`) is restructured for
  another reason (e.g. concurrent request handling) in a way that makes an
  async push loop a side effect rather than new dedicated work — at that
  point the notification-capability gap in §2.1 closes for free and the
  cost side of the §3 math changes.

---

## 5. No AGPL-derived code or text

Konnect's `list_toolboxes` / `load_toolset` / `unload_toolset` naming and
its starter-kit-plus-on-demand-load shape are referenced for their
*mechanism* only, from public descriptions of Konnect's behavior. No
Konnect source, prompts, or schema text is reproduced or adapted in this
document, and no code changes are proposed or bundled with this decision
(see §4 — decline requires none, per the issue's own acceptance criteria).
kicad-tools is MIT-licensed; Konnect is AGPL-3.0.

---

## 6. Verification commands

```bash
uv run python3 - <<'EOF'
import importlib.util, json, sys
from collections import defaultdict

path = "src/kicad_tools/mcp/tools/registry.py"
spec = importlib.util.spec_from_file_location("registry", path)
mod = importlib.util.module_from_spec(spec)
sys.modules["registry"] = mod  # required: dataclass() needs sys.modules[cls.__module__]
spec.loader.exec_module(mod)

cat_chars = defaultdict(int)
for name, spec_ in mod.TOOL_REGISTRY.items():
    payload = {"name": spec_.name, "description": spec_.description, "inputSchema": spec_.parameters}
    cat_chars[spec_.category] += len(json.dumps(payload))
print(len(mod.TOOL_REGISTRY), cat_chars, sum(cat_chars.values()))
EOF
# expect: 44 tools, ~35,200 chars, ~8,800 tokens

rg -n '"listChanged": False' src/kicad_tools/mcp/server.py
# expect: one hit inside MCPServer.handle_request's initialize branch

python3 -c "from mcp.server.fastmcp import FastMCP; import inspect; print(inspect.getsourcefile(FastMCP))"
# expect: .../site-packages/mcp/server/fastmcp/server.py -- confirms
# create_fastmcp_server imports the official mcp SDK's FastMCP, not the
# third-party `fastmcp` package pinned in pyproject.toml's mcp extra

rg -n "def add_tool|def remove_tool" \
  "$(python3 -c 'import mcp.server.fastmcp.server as m; print(m.__file__)')"
# expect: neither method sends a notification -- confirms §2.2
```

Related documents: `docs/konnect-item8-design-rules-audit.md` (the audit-doc
precedent this file follows), `docs/mcp/tools.md` (current tool reference —
unchanged by this decision).
