# 分发提交包（货架层文案）

> 官方 MCP Registry 已由 release.yml 自动发布（tag 推送 → PyPI → registry），本文件是其余货架的提交文案。
> 更新文案时同步改这里，避免多处口径漂移。

## Glama build spec（admin/dockerfile 表单实测要点）

- Glama **不用仓库的 Dockerfile**（那文件是给用户的）；它在表单里填自己的 build spec
- 表单字段（2026-08 实测版）：base image `python:3.13-slim-bookworm`；build steps `pip install .`（**别装 chromium**——内省/tools-list 不需要浏览器，装了徒增构建失败面）；command `python`；arguments `-m nexus_browser.server`（arguments 至少一条是硬校验，空报 "At least one command argument is required"）
- 新仓库的 Maintenance 分受"6 个月"结构性限制，B 是天花板，半年后自然升 A（先例：linksee-memory）

## 一句话定位

- EN: A browser-automation MCP server that gives LLMs deterministic, event-driven page snapshots plus governance (HITL gates, audit) and developer observability (console, JS errors, network, Web Vitals) — built for unattended agents, not just coding demos.
- ZH: 给 LLM 用的浏览器 MCP：事件驱动确定性快照（不靠 sleep 猜）、内置 HITL 治理与审计、多任务隔离、开发者级观测（console/JS 异常/网络/Web Vitals）——为无人值守 agent 而生。

## awesome-mcp-servers（punkpeye 格式）

```markdown
- [paipaipai666/nexus-browser-mcp](https://github.com/paipaipai666/nexus-browser-mcp) 🐍 🏠 🪟 🍎 🐧 — Browser automation via accessibility tree with event-driven deterministic snapshots (MutationObserver quiet-window instead of fixed sleeps), HITL governance gates with redacted JSONL audit, multi-task isolation with self-healing rebuilds, and developer-grade observability (console / pageerror / network metadata / Web Vitals) with since-cursor incremental reads.
```

## appcypher/awesome-mcp-servers 格式

```markdown
- **[nexus-browser-mcp](https://github.com/paipaipai666/nexus-browser-mcp)** — Browser automation MCP with deterministic event-driven snapshots, HITL governance + audit, multi-task isolation, and console/network/perf observability (Python, local, stdio/HTTP).
```

## 目录站提交表单文案（mcp.so / Smithery / Glama / PulseMCP / mcp.directory）

**Name**: nexus-browser-mcp
**Category**: Browser Automation / Developer Tools
**Homepage**: https://github.com/paipaipai666/nexus-browser-mcp
**Install**: `uvx nexus-browser-mcp`（PyPI: `pip install nexus-browser-mcp`）
**Description (short)**:
> Deterministic browser control for LLM agents: event-driven snapshots, governance gates (HITL + audit), multi-task isolation, and real developer observability (console/errors/network/perf).

**Description (long)**:
> nexus-browser-mcp drives a browser for LLMs through the accessibility tree — navigate, click, type, read, fill forms, manage tabs.
> Unlike Playwright MCP-style wrappers it replaces fixed-interval sleeps with an event-driven quiet-window (MutationObserver + rAF), so snapshots are never captured mid-animation. It ships governance as a first-class feature: HITL confirmation rules, JS execution off by default, and a redacted JSONL audit with per-call token metering. Multiple isolated tasks share one connection with automatic self-healing rebuilds. For debugging, agents get developer eyes: console messages, uncaught JS exceptions, network request metadata and Web Vitals — all read incrementally via since-cursors to save tokens.

**Tags**: mcp, mcp-server, browser-automation, playwright, llm-agent, ai-agent, accessibility-tree, python

## 提交状态台账

| 货架 | 状态 | 日期 |
|---|---|---|
| PyPI | ✅ 0.2.2 已发布（CI 自动） | 2026-08-16 |
| 官方 MCP Registry | ✅ `io.github.paipaipai666/nexus-browser-mcp` 0.2.2 已收录（tag/dispatch 自动） | 2026-08-16 |
| awesome-mcp-servers (punkpeye, 92k★) | ✅ PR [#12264](https://github.com/punkpeye/awesome-mcp-servers/pull/12264) 已提交, Glama 徽章已补, CI 绿, 待合并 | 2026-08-16 |
| appcypher/awesome-mcp-servers | ❌ 仓库已归档(2026-05), 不提 | — |
| Glama | ✅ 已收录（awesome 榜前置要求已满足） | 2026-08-16 |
| mcp.so | ⬜ 待提交 | — |
| Smithery | ⬜ 待 GitHub 连接 | — |
| PulseMCP | ⬜ 待提交 | — |
| mcp.directory | ⬜ 待提交 | — |
