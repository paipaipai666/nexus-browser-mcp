"""同任务双跑 token 对比: nexus-browser vs playwright-mcp

方法: 双方以 stdio 起服务, 用脚本化 MCP 客户端执行完全相同的 10 步动作序列,
在 JSON-RPC 载荷层记录每次 tools/call 的请求/响应字节与 cl100k token 数。
公平性控制: 双方默认配置; 本地 fixture 页 (hermetic); 工具语义一一映射。

用法: .venv/Scripts/python.exe bench/compare.py
产出: docs/bench/token-comparison.json (原始数据), 摘要打印到 stdout
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import re
import sys
import threading
from pathlib import Path

import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixture"
OUT = ROOT / "docs" / "bench"


def _serve() -> tuple[str, http.server.ThreadingHTTPServer]:
    """本地 HTTP 托管 fixture —— playwright-mcp 默认屏蔽 file:// 协议。"""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv

_enc = tiktoken.get_encoding("cl100k_base")


def _tok(s: str) -> int:
    return len(_enc.encode(s))


class Meter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def rec(self, step: str, args: dict, result) -> str:
        req = json.dumps(args, ensure_ascii=False)
        resp = "".join(getattr(c, "text", "") or "" for c in result.content)
        self.calls.append({
            "step": step, "req_chars": len(req), "resp_chars": len(resp),
            "req_tokens": _tok(req), "resp_tokens": _tok(resp),
        })
        return resp


async def _run(label: str, params: StdioServerParameters, steps) -> dict:
    meter = Meter()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            print(f"[{label}] tools={len(tools)}", file=sys.stderr)
            await steps(s, meter)
    tot = {k: sum(c[k] for c in meter.calls) for k in ("req_chars", "resp_chars", "req_tokens", "resp_tokens")}
    return {"label": label, "calls": meter.calls, "total": tot}


def _find_ref(snapshot: str, pattern: str, ref_re: str) -> str | None:
    for line in snapshot.splitlines():
        if re.search(pattern, line, re.I):
            m = re.search(ref_re, line)
            if m:
                return m.group(1)
    return None


async def _nexus_steps(s: ClientSession, m: Meter) -> None:
    async def call(name, args, step):
        return m.rec(step, args, await s.call_tool(name, args))

    nav1 = await call("browser_navigate", {"url": P1}, "navigate_p1")
    snap1 = await call("browser_snapshot", {}, "snapshot_1")
    await call("browser_snapshot", {}, "snapshot_2_repeat")
    kw = _find_ref(nav1, r"textbox|输入|关键词", r"ref=((?:f\d+)?e\d+)")
    add = _find_ref(nav1, r"加载更多", r"ref=((?:f\d+)?e\d+)")
    assert kw and add, f"ref not found: kw={kw} add={add}\n{nav1[:800]}"
    await call("browser_type", {"ref": kw, "text": "机械键盘"}, "type")
    await call("browser_click", {"ref": add}, "click")
    await call("browser_snapshot", {}, "snapshot_3_after_mutation")
    await call("browser_console", {}, "console_read")
    await call("browser_evaluate", {"expression": "document.title", "confirmed": True}, "evaluate")
    await call("browser_navigate", {"url": P2}, "navigate_p2")
    await call("browser_snapshot", {}, "snapshot_4_p2")


async def _pw_steps(s: ClientSession, m: Meter) -> None:
    async def call(name, args, step):
        return m.rec(step, args, await s.call_tool(name, args))

    await call("browser_navigate", {"url": P1}, "navigate_p1")
    snap1 = await call("browser_snapshot", {}, "snapshot_1")
    await call("browser_snapshot", {}, "snapshot_2_repeat")
    kw = _find_ref(snap1, r"textbox|关键词|关键词", r"ref=(e\d+)")
    add = _find_ref(snap1, r"加载更多", r"ref=(e\d+)")
    assert kw and add, f"ref not found: kw={kw} add={add}\n{snap1[:800]}"
    await call("browser_type", {"element": "关键词输入框", "ref": kw, "text": "机械键盘"}, "type")
    await call("browser_click", {"element": "加载更多按钮", "ref": add}, "click")
    await call("browser_snapshot", {}, "snapshot_3_after_mutation")
    await call("browser_console_messages", {}, "console_read")
    await call("browser_evaluate", {"function": "() => document.title"}, "evaluate")
    await call("browser_navigate", {"url": P2}, "navigate_p2")
    await call("browser_snapshot", {}, "snapshot_4_p2")


async def main() -> None:
    base, srv = _serve()
    global P1, P2
    P1, P2 = f"{base}/page1.html", f"{base}/page2.html"
    try:
        env_n = {**os.environ, "BROWSER_HEADLESS": "true"}
        py = str(ROOT / ".venv" / "Scripts" / "python.exe")
        res_a = await _run("nexus-browser", StdioServerParameters(
            command=py, args=["-m", "nexus_browser.server"], env=env_n), _nexus_steps)
        res_b = await _run("playwright-mcp", StdioServerParameters(
            command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), _pw_steps)
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "token-comparison.json").write_text(
        json.dumps({"nexus": res_a, "playwright": res_b}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'step':26s} {'nexus tok':>10s} {'pw-mcp tok':>11s}")
    for ca, cb in zip(res_a["calls"], res_b["calls"]):
        assert ca["step"] == cb["step"]
        ta, tb = ca["req_tokens"] + ca["resp_tokens"], cb["req_tokens"] + cb["resp_tokens"]
        print(f"{ca['step']:26s} {ta:10d} {tb:11d}")
    ta, tb = res_a["total"], res_b["total"]
    print("-" * 50)
    print(f"{'TOTAL tokens':26s} {ta['req_tokens']+ta['resp_tokens']:10d} {tb['req_tokens']+tb['resp_tokens']:11d}")
    print(f"{'TOTAL chars':26s} {ta['req_chars']+ta['resp_chars']:10d} {tb['req_chars']+tb['resp_chars']:11d}")


if __name__ == "__main__":
    asyncio.run(main())
