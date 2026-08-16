English | [简体中文](README.zh-CN.md)

# nexus-browser-mcp

**A browser-automation MCP server with event-driven, deterministic snapshots.**

Built on Playwright. Drives a browser for LLMs through the Accessibility Tree — navigate, click, type, read, fill forms, manage tabs. Key differences from alternatives (e.g. Playwright MCP):

1. **Deterministic snapshots**: no fixed-interval `sleep` guessing. A `MutationObserver` records the last DOM mutation and the browser's own `requestAnimationFrame` loop decides when the page has been quiet for `STABLE_WINDOW_MS` (default 800ms) before extracting a snapshot — eliminating "captured mid-animation" races.
2. **Built-in governance gates**: HITL rules (e.g. clicking "pay/confirm" requires human approval), `browser_evaluate` disabled by default with unconditional confirmation, JSONL audit log (sensitive-parameter redaction + per-call in/out character metering, so token cost can be reconciled).
3. **Multi-task isolation**: one MCP connection (session) can host multiple independent `task_id`s, each with its own BrowserContext (no login-state cross-contamination). Idle tasks are reclaimed by TTL; on next use they're rebuilt and the last page is restored automatically.
4. **Death observability + self-healing**: if a tab or the whole browser is closed externally or crashes, the next call rebuilds it automatically (a persistent profile keeps your login state) and prepends a `[state change]` notice telling the agent exactly what was restored and what was lost — no raw Playwright exceptions leak through.
5. **Developer observability**: every page records console messages, uncaught JS exceptions and network request metadata (method/URL/status/failure reason — **never bodies**) into capped ring buffers; `browser_console` / `browser_errors` / `browser_network` read them incrementally via a `since` cursor, so the agent can answer "why did nothing happen" instead of guessing.

## Installation

```bash
pip install nexus-browser-mcp
# or
uvx nexus-browser-mcp
```

Two executable entry points are installed: `nexus-browser-mcp` and `nexus-browser`. A guaranteed fallback: `python -m nexus_browser.server`.

Requires `playwright` and its browser binary:

```bash
pip install playwright && playwright install chromium
```

## Integrate (any MCP client)

**opencode** (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "browser": {
      "type": "local",
      "command": ["uvx", "nexus-browser-mcp"],
      "enabled": true
    }
  }
}
```

**Claude Code** (`.mcp.json`, project root):

```json
{
  "mcpServers": {
    "browser": {
      "type": "stdio",
      "command": "uvx",
      "args": ["nexus-browser-mcp"]
    }
  }
}
```

**Pi Coding Agent**: reads standard MCP configuration — project `.mcp.json` or user-global `~/.config/mcp/mcp.json`; stdio is the default transport:

```json
{
  "mcpServers": {
    "browser": {
      "command": "uvx",
      "args": ["nexus-browser-mcp"]
    }
  }
}
```

See `docs/INTEGRATE.md` for details (Chinese).

## Use your own browser (with login state)

By default, `isolated` mode launches Playwright's bundled Chromium **without your cookies/login state**. To use your own browser, pick one:

**Option A — load your browser profile directly (recommended, simplest)**

Use system Chrome with your everyday user data directory (cookies/login/bookmarks included):

```
BROWSER_CHANNEL=chrome
BROWSER_USER_DATA_DIR="C:\Users\<you>\AppData\Local\Google\Chrome\User Data"
```

> Note: while running against your real User Data, the process owns the browser — launching your own Chrome concurrently will conflict. Prefer a copied profile or a dedicated `--user-data-dir`.

**Recommended: a tool-dedicated profile (no conflict with your daily browser)**

Use `BROWSER_CHANNEL=chrome` plus a dedicated user data dir (e.g. `C:\Users\<you>\.nexus-browser\chrome-profile`):

```
BROWSER_CHANNEL=chrome
BROWSER_USER_DATA_DIR="C:\Users\<you>\.nexus-browser\chrome-profile"
```

On first use, log in to target sites once in the dedicated Chrome window that pops up when the agent calls a browser tool. Cookies persist in that profile forever after — the agent carries login state while staying fully isolated from your daily browser.

**Option B — attach to a running Chrome via CDP**

Start `chrome --remote-debugging-port=9222` first, then set `BROWSER_MODE=cdp`.

> If the CDP connection fails, the server now **fails loudly** (no silent fallback to a fresh browser) and tells you to start the debug-port browser first.

## Configuration (environment variables)

Every option can be overridden via `BROWSER_`-prefixed env vars:

| Variable | Default | Description |
|---|---|---|
| `BROWSER_MODE` | `isolated` | `isolated` (fresh isolated browser) / `cdp` (attach to your Chrome) |
| `BROWSER_CDP_ENDPOINT` | `http://localhost:9222` | CDP endpoint |
| `BROWSER_CHANNEL` | `""` | System browser channel: `chrome`/`msedge` etc. (empty = Playwright bundled Chromium) |
| `BROWSER_USER_DATA_DIR` | `""` | User data dir (carries cookies/login). When set, one shared persistent context across tasks. Empty = fresh profile |
| `BROWSER_HEADLESS` | `false` | Headless mode (isolated only) |
| `BROWSER_DEFAULT_TIMEOUT_MS` | `30000` | Playwright per-operation timeout (navigation etc.) |
| `BROWSER_TOOL_TIMEOUT_MS` | `60000` | Outer timeout guard per tool call (returns ERROR instead of hanging) |
| `BROWSER_STABLE_WINDOW_MS` | `800` | Quiet window: how long without DOM mutations counts as "stable" |
| `BROWSER_STABLE_REQUIRED` | `2` | Consecutive identical snapshots confirming stability (guards non-DOM changes like animations) |
| `BROWSER_STABLE_TIMEOUT_MS` | `3000` | Total stability-wait timeout; degrades gracefully on expiry |
| `BROWSER_SNAPSHOT_MAX_NODES` | `100` | Max nodes per snapshot |
| `BROWSER_CONTEXT_TTL_SEC` | `600` | Idle task auto-reclaim (seconds) |
| `BROWSER_STREAM_CHAR_CAP` | `16000` | Max chars per stream buffer (oldest dropped with a seam marker) |
| `BROWSER_STREAM_PAGE_CAP` | `64000` | Total stream buffer chars per page |
| `BROWSER_EVENT_MAX_ENTRIES` | `500` | Max events (console/exception/request) per page, oldest dropped with a counter |
| `BROWSER_EVENT_TEXT_CAP` | `500` | Per-event text truncation length |
| `BROWSER_EVENT_HANDLE_MAX` | `50` | Recent requests per page keeping a live response handle (for on-demand body reads) |
| `BROWSER_ALLOW_NETWORK_BODY` | `false` | Allow `browser_network_body` (response bodies may carry sensitive data) |
| `BROWSER_NETWORK_BODY_CAP` | `4000` | Max chars returned per response body |
| `BROWSER_TRANSPORT` | `stdio` | `stdio` / `http` (streamable-http for remote/multi-client) |
| `BROWSER_HTTP_HOST` | `127.0.0.1` | HTTP bind address; non-localhost requires `BROWSER_HTTP_TOKEN` (refuses to start otherwise) |
| `BROWSER_HTTP_PORT` | `8817` | HTTP port |
| `BROWSER_HTTP_TOKEN` | `""` | Bearer token for HTTP transport |
| `BROWSER_ALLOW_JS_EXECUTION` | `false` | Allow `browser_evaluate` (unconditional HITL when enabled) |
| `BROWSER_HITL_RULES` | `[]` | JSON array of HITL rules, e.g. `[{"action":"click","name_pattern":"pay|confirm"}]` |
| `BROWSER_AUDIT_PATH` | `~/.nexus-browser/audit.jsonl` | Audit log path |

## Tools

25 tools: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_read`, `browser_screenshot`, `browser_evaluate`, `browser_wait`, `browser_wait_stable`, `browser_wait_ms`, `browser_scroll`, `browser_scroll_to`, `browser_wait_navigation`, `browser_dismiss_popup`, `browser_list_pages`, `browser_switch_page`, observability tools `browser_console`, `browser_errors`, `browser_network`, `browser_perf`, `browser_network_body`, plus 4 lifecycle tools: `browser_tasks`, `browser_close_task`, `browser_list_sessions`, `browser_close_session`.

Observability (debugging): console output, uncaught exceptions and request metadata are buffered per page from creation; `browser_errors()` returns a merged "JS exceptions + console.error + failed requests" view in one call. All three support a `since` cursor (omit = continue from last read, `0` = full) and `limit` paging.

Performance: `browser_perf()` returns FCP/LCP/CLS/INP, navigation timings and the 5 slowest resources. Response bodies can be fetched on demand with `browser_network_body(seq)` — off by default (`BROWSER_ALLOW_NETWORK_BODY`), every call gated by `confirmed=true`, hard char cap, and the body never enters the audit log.

HITL confirmation closes a loop: any gated call returns `CONFIRMATION_REQUIRED` once; after the user approves in chat, the agent re-calls with `confirmed=true` (applies to HITL rules, `browser_evaluate`, `browser_network_body`).

## HTTP transport (remote / multi-client)

Default is stdio (single client). For remote or multi-client use, run a streamable-HTTP server:

```bash
BROWSER_TRANSPORT=http BROWSER_HTTP_PORT=8817 nexus-browser-mcp
```

Each MCP session gets an isolated `session_id` (isolated contexts per task, as usual). Safety rule: binding a non-localhost address without `BROWSER_HTTP_TOKEN` **refuses to start** — an unauthenticated browser-control port is a footgun; with a token set, requests must send `Authorization: Bearer <token>`.

Streaming content (AI replies etc.): `browser_read(wait_stable=true)` waits for DOM quiet and reads the full text in one call; `browser_read(selector=..., follow=true)` tracks incrementally and returns only new content per call (`full=true` returns the whole buffer). `browser_wait_stable` / `browser_wait_ms` provide event-driven and fixed-duration waiting primitives.

Snapshot diff: a repeated `browser_snapshot` whose tree is node-for-node identical to the last one (refs excluded — Playwright renumbers them per generation) returns a ~120-char `[no change]` notice instead of the full tree, and previously issued refs remain valid via generation chaining; `diff=false` forces a full snapshot. Any real change (content, box, attributes) yields the full snapshot — no partial merges, no stale views.

Most tools accept an optional `task_id` (defaults to a shared `default` task). See usage guides in `docs/` (Chinese).

## Development

```bash
uv venv
uv pip install -e ".[dev]"
python -m pytest tests -q
ruff check src tests
python -m smokes.test_e2e           # real-browser smoke
python -m smokes.test_e2e_interact  # forms + multi-task smoke
python -m smokes.test_e2e_observability  # console/exception/network observability smoke
```

## License

MIT
