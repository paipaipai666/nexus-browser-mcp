# 分发提交包（货架层文案）

> 官方 MCP Registry 已由 release.yml 自动发布（tag 推送 → PyPI → registry），本文件是其余货架的提交文案。
> 更新文案时同步改这里，避免多处口径漂移。

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
| PyPI | ✅ 0.2.1 已发布（CI 自动） | 2026-08-16 |
| 官方 MCP Registry | ⏳ 随 v0.2.2 tag 自动发布 | — |
| awesome-mcp-servers (punkpeye) | ⬜ 待提 PR | — |
| appcypher/awesome-mcp-servers | ⬜ 待提 PR | — |
| mcp.so | ⬜ 待提交 | — |
| Smithery | ⬜ 待 GitHub 连接 | — |
| Glama | ⬜ 待提交 | — |
| PulseMCP | ⬜ 待提交 | — |
| mcp.directory | ⬜ 待提交 | — |
