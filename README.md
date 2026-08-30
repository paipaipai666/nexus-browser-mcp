<!-- mcp-name: io.github.paipaipai666/nexus-browser-mcp -->

English | [简体中文](README.zh-CN.md)

# nexus-browser-mcp

[![PyPI](https://img.shields.io/pypi/v/nexus-browser-mcp)](https://pypi.org/project/nexus-browser-mcp/)
[![CI](https://github.com/paipaipai666/nexus-browser-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/paipaipai666/nexus-browser-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.paipaipai666%2Fnexus--browser--mcp-purple)](https://registry.modelcontextprotocol.io/v0.1/servers?search=nexus-browser-mcp)
[![Glama](https://glama.ai/mcp/servers/paipaipai666/nexus-browser-mcp/badges/score.svg)](https://glama.ai/mcp/servers/paipaipai666/nexus-browser-mcp)

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

In CDP (or persistent-profile) mode the agent can also **take over tabs you already have open**: `browser_list_pages` shows them under "external tabs", and `browser_adopt_page(ext_index)` pulls one into the task (snapshot/click/read all work from then on). Adoption always requires `confirmed=true` — it hands the agent full read/write access to that page, including its login state.

## Configuration (environment variables)

Every option can be overridden via `BROWSER_`-prefixed env vars:

> **评测/全功能使用**：两个高危能力默认关闭（安全优先）。需要 `browser_evaluate` 时设 `BROWSER_ALLOW_JS_EXECUTION=true`，需要 `browser_network_body` 时设 `BROWSER_ALLOW_NETWORK_BODY=true`——冷启动跑基准/评测不开它们，对应子任务会被拒（这是设计，不是故障）。

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
| `BROWSER_STABLE_REQUIRED` | `2` | Fallback only: consecutive identical snapshots confirming stability, used when the MutationObserver watcher is unavailable (primary path verifies "zero mutations during capture" via the mutation timeline directly) |
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
| `BROWSER_DIALOG_TIMEOUT_MS` | `20000` | Parked confirm/prompt auto-dismiss timeout (trail kept in event log) |
| `BROWSER_DOWNLOAD_DIR` | `~/.nexus-browser/downloads` | Where accepted downloads are saved (click reports filename + path) |
| `BROWSER_PROXY` | `""` | `none` = launch browser with `--no-proxy-server` (bypass system proxy) |

## Tools

34 tools: `browser_navigate`, `browser_navigate_back`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_hover`, `browser_press_key`, `browser_select_option`, `browser_upload_file` (HITL-confirmed), `browser_drag`, `browser_dialog_respond` (dialogs are parked for agent/user decision; accept requires `confirmed=true`), `browser_adopt_page` (take over an already-open browser tab in cdp/persistent mode; HITL-confirmed), `browser_read`, `browser_screenshot`, `browser_evaluate`, `browser_wait`, `browser_wait_stable`, `browser_wait_ms`, `browser_scroll`, `browser_scroll_to`, `browser_wait_navigation`, `browser_find` (locate content by text → refs), `browser_dismiss_popup`, `browser_list_pages`, `browser_switch_page`, observability tools `browser_console`, `browser_errors`, `browser_network`, `browser_perf`, `browser_network_body`, plus 4 lifecycle tools: `browser_tasks`, `browser_close_task`, `browser_list_sessions`, `browser_close_session`.

Observability (debugging): console output, uncaught exceptions and request metadata are buffered per page from creation; `browser_errors()` returns a merged "JS exceptions + console.error + failed requests" view in one call. All three support a `since` cursor (omit = continue from last read, `0` = full) and `limit` paging.

Performance: `browser_perf()` returns FCP/LCP/CLS/INP, navigation timings and the 5 slowest resources. Response bodies can be fetched on demand with `browser_network_body(seq)` — off by default (`BROWSER_ALLOW_NETWORK_BODY`), every call gated by `confirmed=true`, hard char cap, and the body never enters the audit log.

## Token cost: measured, not claimed

Same 10-step task, both servers at default config, metered at the JSON-RPC payload layer (cl100k tokens; harness + raw data in [docs/bench/token-comparison.md](docs/bench/token-comparison.md), reproduce with `bench/compare.py`):

| | nexus-browser-mcp | playwright-mcp |
|---|---:|---:|
| 10-step task total | **3,460 tok** | 26,032 tok |
| snapshot right after navigate | **65 tok** | 6,931 tok |

**7.5x fewer tokens overall; 100x on repeated snapshots** — the dominant cost in real agent loops (polling, multi-step forms, state confirmation).

Real-site benchmark (7 scenarios × 5-7 verifiable sub-tasks each: Baidu/Bing/DuckDuckGo search, Wikipedia reading, Hacker News, GitHub browsing — [docs/bench/realworld.md](docs/bench/realworld.md)): **sub-task completion 37/42 vs 31/42**, **18.2k vs 566.4k tokens (31x)**, wall-clock **107s vs 179s** — one Wikipedia article snapshot alone costs pw-mcp ~252k tokens where nexus caps + diffs. Enterprise task suite (filter/sort, dashboard reading, KB answers, multi-step ordering, price comparison — [docs/bench/enterprise-ops.md](docs/bench/enterprise-ops.md)): **21/21 on all three servers (vs playwright-mcp and chrome-devtools-mcp); tokens 3.6k vs 5.1k vs 10.9k**. At scale (**106 cases / 184 sub-tasks, three servers, seeded deterministic fixtures** — [docs/bench/scale-ops.md](docs/bench/scale-ops.md)): **completion 184/184 vs 180/184 vs 178/184; tokens 153.5k vs 180.5k vs 231.1k (1.00 : 1.18 : 1.51)** — the competitor gaps are stable zeros (richtext-iframe writes, download observability, right-click), not noise.

Element-identification accuracy on adversarially complex pages (duplicate-name button grids, shadow DOM, iframes, hidden/disabled traps, full re-render — [docs/bench/element-acc.md](docs/bench/element-acc.md)): **nexus 92% = pw 92% < chrome-devtools-mcp 100%, with cdt also cheapest** — its verbose per-node dump carries product texts that lean snapshots drop, which value-based targeting needs. We publish this as a known tradeoff of text clipping: total-token savings mean nothing unless per-point-of-accuracy cost holds, and this suite is the canary (fix direction benchmarked: attach sibling-text summaries only when same-name candidates conflict).

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
