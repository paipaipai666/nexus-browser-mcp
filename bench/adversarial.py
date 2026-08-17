"""对抗性能力基准: 瞄准 nexus 的缺口 vs playwright-mcp 的优势

与 realworld.py 的区别: 那份测"日常操作的公平赛道"; 这份测**能力覆盖**——
刻意挑选对手有专用工具、我们没有的操作 (hover/select/upload/键盘/对话框/拖拽/批量表单/后退),
加 iframe 深交互验证我方声称的优势。目标: 找出本产品的问题, 不是吹牛。

分三档计分 (每案例):
  native  = 专用工具直接完成
  escape  = 逃生舱完成 (我方: evaluate JS; 对方: run_code_unsafe 未动用——不需要)
  fail    = 完全做不了
fixture 全部本地 hermetic; 双方 evaluate/代码执行能力对称开启。

用法: .venv/Scripts/python.exe -u bench/adversarial.py
产出: docs/bench/adversarial.json + stdout 表格
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
FIX = Path(__file__).parent / "fixture_adv"
OUT = ROOT / "docs" / "bench"
_enc = tiktoken.get_encoding("cl100k_base")
REF_RE = r"ref=((?:f\d+)?e\d+)"

DONE_CHECK = ("(() => { const d = document.getElementById('done');"
              " return !!d && getComputedStyle(d).display !== 'none'; })()")


class A:
    """适配器: 两侧工具名/参数差异 + 计量。"""

    def __init__(self, s: ClientSession, kind: str) -> None:
        self.s, self.kind = s, kind
        self.calls: list[dict] = []

    async def call(self, name: str, args: dict, note: str = "") -> tuple[str, bool]:
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.s.call_tool(name, args), timeout=60)
            text = "".join(getattr(c, "text", "") or "" for c in res.content)
            ok = not res.is_error
        except Exception as e:
            text, ok = f"EXC {type(e).__name__}: {e}", False
        ms = int((time.perf_counter() - t0) * 1000)
        tok = len(_enc.encode(json.dumps(args, ensure_ascii=False) + text))
        self.calls.append({"tool": name, "ok": ok, "ms": ms, "tokens": tok, "note": note})
        print(f"    [{self.kind}] {name:28s} {'ok' if ok else 'FAIL'} {ms:5d}ms {tok:5d}tok {note}",
              file=sys.stderr, flush=True)
        return text, ok

    def nav(self, url: str):
        return self.call("browser_navigate", {"url": url})

    def click_sel(self, sel: str):
        if self.kind == "nexus":
            return self.call("browser_click", {"selector": sel})
        return self.call("browser_click", {"target": sel, "element": sel})

    def click_ref(self, ref: str):
        if self.kind == "nexus":
            return self.call("browser_click", {"ref": ref})
        return self.call("browser_click", {"target": ref, "element": "iframe button"})

    def eval(self, expr: str):
        if self.kind == "nexus":
            return self.call("browser_evaluate", {"expression": expr, "confirmed": True})
        return self.call("browser_evaluate", {"function": f"() => {expr}"})

    async def snap_ref(self, name_pat: str) -> str | None:
        # diff=false: navigate 已播种基线, 默认快照会命中 diff 返回短消息(无 ref 行) —— 取 ref 必须显式全量
        text, _ = await self.call("browser_snapshot",
                                  {"diff": False} if self.kind == "nexus" else {})
        rx = re.compile(name_pat, re.I)
        for line in text.splitlines():
            if rx.search(line):
                m = re.search(REF_RE, line)
                if m:
                    return m.group(1)
        return None

    async def verify(self, expr: str = DONE_CHECK) -> bool:
        text, ok = await self.eval(expr)
        return ok and "true" in text.lower()


# ── 案例: 每个都是对手有专用工具、我方待证的操作 ─────────────────────

async def case_hover(a: A, base: str) -> tuple[str, str]:
    """hover 展开的菜单 (仅 mouseenter): 对手 browser_hover。"""
    await a.nav(f"{base}/hover.html")
    if a.kind == "pw":
        await a.call("browser_hover", {"target": "#menu", "element": "产品菜单"})
        await a.click_sel("#prolink")
        return ("native" if await a.verify() else "fail"), "browser_hover 直达"
    # nexus: 无 hover 工具 → 逃生舱
    await a.eval("document.getElementById('menu').dispatchEvent(new MouseEvent('mouseenter'))")
    await a.click_sel("#prolink")
    return (("escape" if await a.verify() else "fail"),
            "无 hover 工具; evaluate 派发 synthetic mouseenter (isTrusted=false, 检查 isTrusted 的站点会拒)")


async def case_select(a: A, base: str) -> tuple[str, str]:
    """<select> 下拉选择: 对手 browser_select_option。"""
    await a.nav(f"{base}/select.html")
    if a.kind == "pw":
        await a.call("browser_select_option", {"target": "#city", "element": "城市", "values": ["上海"]})
        return ("native" if await a.verify() else "fail"), "select_option 直达"
    ref = await a.snap_ref(r"option|combobox")
    note = "快照未暴露 option 角色 (不在 INTERACTIVE_ROLES)" if not ref else f"找到 ref={ref}, 尝试点击"
    if ref:
        await a.click_ref(ref)
        if await a.verify():
            return "native", note
    await a.eval("const s=document.getElementById('city'); s.value='上海';"
                 " s.dispatchEvent(new Event('change',{bubbles:true})); 'ok'")
    return (("escape" if await a.verify() else "fail"), note + "; 逃生舱直接赋值+change 事件")


async def case_upload(a: A, base: str) -> tuple[str, str]:
    """文件上传: 对手 browser_file_upload。浏览器安全模型下 JS 无法设 files → 我方无路径。"""
    await a.nav(f"{base}/upload.html")
    if a.kind == "pw":
        await a.click_sel("#f")  # 先点开 file chooser (modal 态), file_upload 才有上下文
        f = str(FIX / "frame.html")  # 任意现存文件
        await a.call("browser_file_upload", {"paths": [f]})
        return ("native" if await a.verify() else "fail"), "click 开 chooser → file_upload (Playwright set_input_files)"
    return "fail", "无上传工具; evaluate 受浏览器安全模型限制无法设置 input.files → 完全不可行"


async def case_keyboard(a: A, base: str) -> tuple[str, str]:
    """仅 Escape 可关的模态框: 对手 browser_press_key。"""
    await a.nav(f"{base}/keyboard.html")
    await a.click_sel("#open")
    if a.kind == "pw":
        await a.call("browser_press_key", {"key": "Escape"})
        return ("native" if await a.verify() else "fail"), "press_key 直达 (可信 CDP 键事件)"
    await a.eval("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); 'ok'")
    return (("escape" if await a.verify() else "fail"),
            "无 press_key; synthetic KeyboardEvent (isTrusted=false; 且快捷键/Tab 序等场景覆盖不了)")


async def case_dialog(a: A, base: str) -> tuple[str, str]:
    """必须点"确定"的 confirm: 对手 browser_handle_dialog(accept)。
    我方哲学 dismiss-only + Playwright 默认 auto-dismiss → confirm=false。"""
    await a.nav(f"{base}/dialog.html")
    if a.kind == "pw":
        await a.click_sel("#del")                      # 先点 → 对话框打开 (modal 态)
        await a.call("browser_handle_dialog", {"accept": True})  # 仅 modal 态可调
        return ("native" if await a.verify() else "fail"), "click → handle_dialog(accept)"
    await a.click_sel("#del")
    if await a.verify():
        return "native", "意外: auto-dismiss 被接受了?"
    await a.eval("window.confirm = () => true; 'stubbed'")
    await a.click_sel("#del")
    return (("escape" if await a.verify() else "fail"),
            "confirm 被 auto-dismiss 返回 false → 首次点击无效; 逃生舱 stub window.confirm 后重击")


async def case_drag(a: A, base: str) -> tuple[str, str]:
    """HTML5 拖拽: 对手 browser_drag。"""
    await a.nav(f"{base}/drag.html")
    if a.kind == "pw":
        await a.call("browser_drag", {"startTarget": "#src", "startElement": "拖我",
                                      "endTarget": "#zone", "endElement": "放到这"})
        return ("native" if await a.verify() else "fail"), "browser_drag 直达 (真实输入管线)"
    await a.eval("""(() => { const dt = new DataTransfer();
      const src = document.querySelector('#src'), zone = document.querySelector('#zone');
      src.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
      zone.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true}));
      zone.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true})); return 'ok'; })()""")
    return (("escape" if await a.verify() else "fail"),
            "无 drag 工具; synthetic DragEvent 序列 (依赖监听方不校验事件细节)")


async def case_form(a: A, base: str) -> tuple[str, str]:
    """5 字段批量表单: 对手 fill_form 一次调用 vs 我方 5×type。"""
    await a.nav(f"{base}/form.html")
    if a.kind == "pw":
        fields = [{"name": n, "target": f"#{i}", "type": "textbox", "value": f"v_{i}"}
                  for n, i in zip(("姓名", "邮箱", "电话", "城市", "备注"), "abcde")]
        await a.call("browser_fill_form", {"fields": fields})
    else:
        for i in "abcde":
            await a.call("browser_type", {"selector": f"#{i}", "text": f"v_{i}"})
    await a.click_sel("#sub")
    ok = await a.verify()
    return ("native" if ok else "fail"), "调用数对比: 对手 fill_form=1 次 vs 我方 type=5 次"


async def case_back(a: A, base: str) -> tuple[str, str]:
    """后退导航: 对手 browser_navigate_back。"""
    await a.nav(f"{base}/backa.html")
    await a.click_sel("#to")
    await asyncio.sleep(0.5)
    if a.kind == "pw":
        await a.call("browser_navigate_back", {})
    else:
        await a.eval("history.back(); 'ok'")
    await asyncio.sleep(0.8)
    ok = await a.verify("location.href.endsWith('backa.html')")
    return (("native" if (ok and a.kind == "pw") else "escape" if ok else "fail"),
            "" if a.kind == "pw" else "无后退工具; evaluate history.back()")


async def case_iframe(a: A, base: str) -> tuple[str, str]:
    """iframe 内元素点击: 我方 f-前缀 ref 的声称优势, 实测验证。"""
    await a.nav(f"{base}/iframe.html")
    ref = await a.snap_ref(r"框内按钮")
    if not ref:
        return "fail", "快照未暴露 iframe 内按钮 ref"
    await a.click_ref(ref)
    return (("native" if await a.verify() else "fail"), f"ref={ref}")


CASES = [
    ("hover菜单", case_hover), ("select下拉", case_select), ("文件上传", case_upload),
    ("键盘Esc", case_keyboard), ("confirm对话框", case_dialog), ("HTML5拖拽", case_drag),
    ("批量表单", case_form), ("后退导航", case_back), ("iframe深交互", case_iframe),
]


def _serve() -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


async def run_side(kind: str, params: StdioServerParameters, base: str) -> dict:
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            a = A(s, kind)
            results = {}
            for name, fn in CASES:
                print(f"[{kind}] ▶ {name}", file=sys.stderr, flush=True)
                before = len(a.calls)
                t0 = time.perf_counter()
                try:
                    tier, note = await fn(a, base)
                except Exception as e:
                    tier, note = "fail", f"EXC {type(e).__name__}: {e}"
                calls = a.calls[before:]
                results[name] = {
                    "tier": tier, "note": note,
                    "calls": len(calls), "tokens": sum(c["tokens"] for c in calls),
                    "ms": int((time.perf_counter() - t0) * 1000),
                }
                print(f"[{kind}] {name}: {tier} ({results[name]['calls']} 调用, "
                      f"{results[name]['tokens']} tok)", file=sys.stderr, flush=True)
            return {"kind": kind, "cases": results, "calls": a.calls}


async def main() -> None:
    base, srv = _serve()
    try:
        py = str(ROOT / ".venv" / "Scripts" / "python.exe")
        env_n = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
        ours = await run_side("nexus", StdioServerParameters(
            command=py, args=["-m", "nexus_browser.server"], env=env_n), base)
        theirs = await run_side("pw", StdioServerParameters(
            command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base)
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "adversarial.json").write_text(
        json.dumps({"nexus": ours, "playwright": theirs}, ensure_ascii=False, indent=2), encoding="utf-8")

    TIER_MARK = {"native": "✅原生", "escape": "🟡逃生舱", "fail": "❌"}
    print(f"\n{'案例':14s} │ {'nexus':>10s} {'tok':>5s} {'calls':>5s} │ {'pw-mcp':>10s} {'tok':>5s} {'calls':>5s}")
    for name, _ in CASES:
        a, b = ours["cases"][name], theirs["cases"][name]
        print(f"{name:14s} │ {TIER_MARK[a['tier']]:>10s} {a['tokens']:5d} {a['calls']:5d} │ "
              f"{TIER_MARK[b['tier']]:>10s} {b['tokens']:5d} {b['calls']:5d}")


if __name__ == "__main__":
    asyncio.run(main())
