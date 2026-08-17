"""真实站点多场景基准: nexus-browser vs playwright-mcp

场景 = 日常操作流（搜索/阅读/列表/仓库）, 每个场景拆成 5-6 个可程序化判定的子任务。
指标: 子任务完成率 / 每步耗时(ms) / 每步 token(cl100k, 请求+响应 JSON-RPC 载荷)。
公平性: 双方默认配置; 相同子任务序列; 失败不重试; ref 按快照文本正则实时定位。

用法:
  .venv/Scripts/python.exe bench/realworld.py            # 双跑全量
  .venv/Scripts/python.exe bench/realworld.py --probe    # 仅 nexus, 打印各站快照片段(定匹配器用)
产出: docs/bench/realworld.json + 摘要表 (stdout)
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
import time
from pathlib import Path

import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixture"
OUT = ROOT / "docs" / "bench"
_enc = tiktoken.get_encoding("cl100k_base")

REF_NEXUS = r"ref=((?:f\d+)?e\d+)"
REF_PW = r"ref=((?:f\d+)?e\d+)"   # pw 同样有 iframe 前缀 (f1e105)

# ---------------------------------------------------------------- cases
# 子任务类型: nav / snap(可带断言) / type(matcher) / click(matcher) / wait / eval(断言)
# matcher 为快照行正则; assert_ 为响应文本应包含的子串(不区分大小写)。
CASES = [
    {
        "name": "fixture",
        "steps": [
            ("nav", "{FIX}/page1.html", "已导航至|Page URL"),
            ("snap", "商品 1", None),
            ("snap", None, None),                      # 复读 → diff 杠杆
            ("type", r"textbox|关键词", "机械键盘"),
            ("click", r"加载更多", None),
            ("wait", 500, None),
            ("eval", "document.querySelectorAll('#items li').length", "121"),
        ],
    },
    {
        "name": "baidu-search",
        "steps": [
            ("nav", "https://www.baidu.com", "百度"),
            ("snap", None, None),
            ("type", r"textbox.*(搜索|百度|输入)", "nexus-browser-mcp"),
            ("click", r"button.*(百度一下|搜索)", None),
            ("wait", 2000, None),
            ("snap", "nexus", None),
            ("eval", "document.title", "nexus"),
        ],
    },
    {
        "name": "bing-search",
        "steps": [
            ("nav", "https://www.bing.com", "Bing|必应"),
            ("snap", None, None),
            ("type_enter", r"textbox|combobox", "nexus browser mcp github"),
            ("wait", 2500, None),
            ("snap", "nexus", None),
            ("eval", "document.title", "nexus"),
        ],
    },
    {
        "name": "duckduckgo-search",
        "steps": [
            ("nav", "https://duckduckgo.com", "DuckDuckGo"),
            ("snap", None, None),
            ("type_enter", r"textbox|combobox|searchbox", "nexus browser mcp"),
            ("wait", 2500, None),
            ("snap", "nexus", None),
            ("eval", "location.href", "nexus"),
        ],
    },
    {
        "name": "wikipedia-read",
        "steps": [
            ("nav", "https://zh.wikipedia.org/wiki/Python", "Python"),
            ("snap", "Python", None),
            ("snap", None, None),                      # 复读 → diff
            ("type_enter", r"textbox|combobox|searchbox", "nexus browser mcp"),
            ("wait", 2000, None),
            ("eval", "location.href", "search="),
        ],
    },
    {
        "name": "hackernews-browse",
        "steps": [
            ("nav", "https://news.ycombinator.com", "Hacker News"),
            ("snap", "Hacker News", None),
            ("click", r'link "\d+ comments', None),   # 第一条新闻的评论数链接, 排除导航栏 "comments"
            ("wait", 1500, None),
            ("eval", "location.href", "item"),
        ],
    },
    {
        "name": "github-repo",
        "steps": [
            ("nav", "https://github.com/paipaipai666/nexus-browser-mcp", "nexus-browser-mcp"),
            ("snap", "nexus-browser-mcp", None),
            ("click", r"link.*Issues", None),
            ("wait", 2000, None),
            ("eval", "location.pathname", "issues"),
        ],
    },
]


def _serve() -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


class Runner:
    """单服务器执行器: 语义步骤 → 该服务器的工具调用映射 + 计量。"""

    def __init__(self, session: ClientSession, kind: str) -> None:
        self.s = session
        self.kind = kind                      # "nexus" | "pw"
        self.ref_re = REF_NEXUS if kind == "nexus" else REF_PW
        self.calls: list[dict] = []
        self.last_full = ""                   # 最近一次"全量"文本(nav附带快照或全量snapshot)

    @staticmethod
    def _is_diff_hit(text: str) -> bool:
        return text.startswith("[快照无变化]")

    async def call(self, step: str, name: str, args: dict) -> tuple[str, bool, int, int]:
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.s.call_tool(name, args), timeout=90)  # 硬护栏: 单点卡死不拖垮全跑
            text = "".join(getattr(c, "text", "") or "" for c in res.content)
            ok = not res.is_error
        except Exception as e:                # 网络/协议/超时异常 → 子任务失败, 不中断
            text, ok = f"EXC {type(e).__name__}: {e}", False
        ms = int((time.perf_counter() - t0) * 1000)
        payload = json.dumps(args, ensure_ascii=False) + text
        tok = len(_enc.encode(payload))
        self.calls.append({"step": step, "tool": name, "ok": ok, "ms": ms,
                           "chars": len(payload), "tokens": tok, "resp": text[:300]})
        print(f"    [{self.kind}] {step:28s} {'ok' if ok else 'FAIL'} {ms:6d}ms {tok:6d}tok",
              file=sys.stderr, flush=True)
        return text, ok, ms, tok

    def find_ref(self, pattern: str) -> str | None:
        rx = re.compile(pattern, re.I)
        for line in self.last_full.splitlines():
            if rx.search(line):
                m = re.search(self.ref_re, line)
                if m:
                    return m.group(1)
        return None

    async def run_case(self, case: dict, base: str) -> dict:
        self.last_full = ""                   # 场景间隔离: 断言不得吃到上一场景的残留内容
        subs = []
        for spec in case["steps"]:
            kind = spec[0]
            if kind == "nav":
                url = spec[1].replace("{FIX}", base)
                text, ok, ms, tok = await self.call(f"{case['name']}:nav", "browser_navigate", {"url": url})
                ok = ok and bool(re.search(spec[2], text, re.I))
                if "ref=" in text:            # nexus: nav 附带全量快照
                    self.last_full = text
            elif kind == "snap":
                text, ok, ms, tok = await self.call(f"{case['name']}:snap", "browser_snapshot", {})
                if not self._is_diff_hit(text):
                    self.last_full = text     # diff 命中时 last_full 保持 (内容一致)
                if spec[1]:
                    ok = ok and (spec[1].lower() in text.lower()
                                 or spec[1].lower() in self.last_full.lower())
            elif kind in ("type", "type_enter"):
                ref = self.find_ref(spec[1])
                if not ref:
                    subs.append({"type": kind, "ok": False, "ms": 0, "tokens": 0, "why": f"ref未定位: {spec[1]}"})
                    continue
                if self.kind == "pw":         # 新版 pw-mcp: target = 快照 ref/选择器, element = 人类描述(可选)
                    args = {"target": ref, "text": spec[2], "element": "bench target"}
                    if kind == "type_enter":
                        args["submit"] = True
                else:
                    args = {"ref": ref, "text": spec[2]}
                    if kind == "type_enter":  # 真实日常: 输入后回车提交 (两侧均支持)
                        args["press_enter"] = True
                text, ok, ms, tok = await self.call(f"{case['name']}:{kind}", "browser_type", args)
            elif kind == "click":
                ref = self.find_ref(spec[1])
                if not ref:
                    subs.append({"type": "click", "ok": False, "ms": 0, "tokens": 0, "why": f"ref未定位: {spec[1]}"})
                    continue
                args = {"target": ref, "element": "bench target"} if self.kind == "pw" else {"ref": ref}
                text, ok, ms, tok = await self.call(f"{case['name']}:click", "browser_click", args)
            elif kind == "wait":
                t0 = time.perf_counter()
                await asyncio.sleep(spec[1] / 1000)
                ms = int((time.perf_counter() - t0) * 1000)
                text, ok, tok = "(client-side wait)", True, 0
            elif kind == "eval":
                if self.kind == "nexus":
                    args = {"expression": spec[1], "confirmed": True}
                else:
                    args = {"function": f"() => {spec[1]}"}
                text, ok, ms, tok = await self.call(f"{case['name']}:eval", "browser_evaluate", args)
                ok = ok and spec[2].lower() in text.lower()
            subs.append({"type": kind, "ok": ok, "ms": ms, "tokens": tok})
            if kind in ("click", "type", "type_enter") and ok:
                await asyncio.sleep(0.3)      # 让 DOM 反应
        done = sum(1 for x in subs if x["ok"])
        return {"case": case["name"], "subtasks": subs, "passed": done, "total": len(subs),
                "tokens": sum(x["tokens"] for x in subs), "ms": sum(x["ms"] for x in subs)}


async def run_server(label: str, params: StdioServerParameters, base: str, probe: bool) -> dict:
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            print(f"[{label}] tools={len(tools)}", file=sys.stderr)
            rn = Runner(s, "nexus" if label == "nexus" else "pw")
            results = []
            for case in CASES:
                print(f"[{label}] ▶ {case['name']} ({len(case['steps'])} 子任务)", file=sys.stderr, flush=True)
                res = await rn.run_case(case, base)
                results.append(res)
                print(f"[{label}] {res['case']}: {res['passed']}/{res['total']} "
                      f"tok={res['tokens']} ms={res['ms']}", file=sys.stderr, flush=True)
                if probe:
                    for x in res["subtasks"]:
                        print(f"    {x}", file=sys.stderr)
                    print(f"--- last_full of {case['name']} ---\n{rn.last_full[:1200]}\n", file=sys.stderr)
                await asyncio.sleep(2)        # 场景间隔, 降低站点侧串扰
            return {"label": label, "cases": results, "calls": rn.calls}


async def main() -> None:
    probe = "--probe" in sys.argv
    base, srv = _serve()
    try:
        py = str(ROOT / ".venv" / "Scripts" / "python.exe")
        env_n = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
        res_a = await run_server("nexus", StdioServerParameters(
            command=py, args=["-m", "nexus_browser.server"], env=env_n), base, probe)
        if probe:
            return
        res_b = await run_server("pw", StdioServerParameters(
            command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base, probe)
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "realworld.json").write_text(
        json.dumps({"nexus": res_a, "playwright": res_b}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'case':18s} │ {'nexus 子任务':>12s} {'tok':>6s} {'ms':>7s} │ {'pw 子任务':>12s} {'tok':>6s} {'ms':>7s}")
    for ca, cb in zip(res_a["cases"], res_b["cases"]):
        print(f"{ca['case']:18s} │ {ca['passed']:>5d}/{ca['total']:<6d} {ca['tokens']:6d} {ca['ms']:7d} │ "
              f"{cb['passed']:>5d}/{cb['total']:<6d} {cb['tokens']:6d} {cb['ms']:7d}")
    for nm, res in (("TOTAL", (res_a, res_b)),):
        ta = {k: sum(c[k] for c in res[0]["cases"]) for k in ("passed", "total", "tokens", "ms")}
        tb = {k: sum(c[k] for c in res[1]["cases"]) for k in ("passed", "total", "tokens", "ms")}
        print("-" * 80)
        print(f"{nm:18s} │ {ta['passed']:>5d}/{ta['total']:<6d} {ta['tokens']:6d} {ta['ms']:7d} │ "
              f"{tb['passed']:>5d}/{tb['total']:<6d} {tb['tokens']:6d} {tb['ms']:7d}")


if __name__ == "__main__":
    asyncio.run(main())
