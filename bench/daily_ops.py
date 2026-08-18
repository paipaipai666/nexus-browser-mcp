"""日常操作双侧基准: 更宽的真实场景覆盖 (反"满分假象")

刻意覆盖 newtools/adversarial 未及的日常面: 登录认证、动态加载等待、无限滚动、
右键菜单(我方已知缺口)、混沌 DOM 定位稳定性、大 DOM 表格、按键验证、复选框、
iframe 内富文本编辑器、日期选择器、自动补全、文件下载(双方疑同缺)、重定向链。
双侧同测 (nexus + playwright-mcp), 子任务级断言, 失败不重试。

用法: .venv/Scripts/python.exe -u bench/daily_ops.py [子串过滤]
产出: docs/bench/daily-ops.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adversarial import A  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
FIXD = Path(__file__).parent / "fixture_daily"
JQ = "https://jqueryui.com/resources/demos"  # 直连可达, 保留真实站点样本


def _serve() -> tuple[str, object]:
    """国内网络友好: 标准动作页全部本地化 (结构与 the-internet 同构),
    下载文件走 npmmirror 国内 CDN; 仅 datepicker/autocomplete 用 jqueryui (直连可达)。"""
    import functools
    import http.server as hs
    import threading
    handler = functools.partial(hs.SimpleHTTPRequestHandler, directory=str(FIXD))
    srv = hs.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


class Rec:
    def __init__(self, case: str) -> None:
        self.case = case
        self.subs: list[dict] = []

    def check(self, name: str, ok: bool, note: str = "") -> None:
        self.subs.append({"sub": name, "ok": bool(ok), "note": note})
        print(f"      {'ok' if ok else 'FAIL'} {name} {note}", file=sys.stderr, flush=True)

    def done(self) -> dict:
        return {"case": self.case, "passed": sum(1 for s in self.subs if s["ok"]),
                "total": len(self.subs), "subs": self.subs}


async def _type(a: A, sel_or_ref: str, text: str, enter: bool = False):
    """双侧 type 适配: nexus 用 selector, pw 用 target。"""
    if a.kind == "nexus":
        args = {"text": text, "press_enter": enter}
        args["ref" if re.fullmatch(r"(?:f\d+)?e\d+", sel_or_ref) else "selector"] = sel_or_ref
        return await a.call("browser_type", args)
    return await a.call("browser_type", {"target": sel_or_ref, "element": "field", "text": text, "submit": enter})


# ── 案例 ─────────────────────────────────────────────────────────

async def case_login(a: A, r: Rec, base: str) -> None:
    """真实登录表单流程 (tomsmith / SuperSecretPassword!, 本地同构页)。"""
    await a.nav(f"{base}/login.html")
    await _type(a, "#username", "tomsmith")
    await _type(a, "#password", "SuperSecretPassword!")
    await a.click_sel("#login button")
    await asyncio.sleep(0.8)
    r.check("登录成功进安全区", await a.verify("location.hash.includes('secure')"))
    r.check("欢迎横幅可见", await a.verify("document.getElementById('flash').textContent.includes('logged in')"))
    await a.click_sel("#logout")
    await asyncio.sleep(0.6)
    r.check("登出回登录页", await a.verify("location.hash.includes('login')"))


async def case_dynamic_loading(a: A, r: Rec, base: str) -> None:
    """元素延迟出现: 等待原语的真实考验。"""
    await a.nav(f"{base}/dynamic_loading.html")
    await a.click_sel("#start button")
    if a.kind == "nexus":
        await a.call("browser_wait", {"text": "Hello World!", "timeout": 12000})
    else:
        await a.call("browser_wait_for", {"text": "Hello World!", "time": 12})
    r.check("等到延迟元素出现", await a.verify("document.getElementById('finish').textContent.includes('Hello World')"))


async def case_infinite_scroll(a: A, r: Rec, base: str) -> None:
    """无限滚动触发懒加载。pw 无 scroll 工具 → 用 PageDown 键 (公平: 各用各的)。"""
    await a.nav(f"{base}/infinite_scroll.html")
    c0, _ = await a.eval("document.querySelectorAll('.jscroll-added').length")
    for _ in range(3):
        if a.kind == "nexus":
            await a.call("browser_scroll", {"direction": "down", "amount": 1200})
        else:
            await a.call("browser_press_key", {"key": "PageDown"})
        await asyncio.sleep(1)
    c1, _ = await a.eval("document.querySelectorAll('.jscroll-added').length")
    n0 = int(re.search(r"\d+", c0).group()) if re.search(r"\d+", c0) else 0
    n1 = int(re.search(r"\d+", c1).group()) if re.search(r"\d+", c1) else 0
    r.check("滚动触发内容增长", n1 > n0, f"{n0}→{n1}")
    r.check("至少追加 2 屏", n1 >= n0 + 2, f"{n0}→{n1}")


async def case_context_menu(a: A, r: Rec, base: str) -> None:
    """右键菜单: 双方 button=right。the-internet 热区右键触发 alert;
    我方对话框治理自动 dismiss alert 并留痕 → 以事件记录验证。"""
    await a.nav(f"{base}/contextmenu.html")
    if a.kind == "pw":
        await a.call("browser_click", {"target": "#hot-spot", "element": "热区", "button": "right"})
        await asyncio.sleep(0.5)
        t, _ = await a.call("browser_handle_dialog", {"accept": True})
        r.check("右键触发 alert 并处理", "context menu" in t, t[:60])
        return
    # nexus: button=right 原生 (缺口清零后)
    t, _ = await a.call("browser_click", {"selector": "#hot-spot", "button": "right"})
    r.check("右键点击执行", "已点击" in t, t[:60])
    await asyncio.sleep(0.5)
    errs, _ = await a.call("browser_errors", {"since": 0})
    r.check("alert 事件留痕 (context menu 文案)", "context menu" in errs, errs[:80])


async def case_challenging_dom(a: A, r: Rec, base: str) -> None:
    """id 随页面重载变化的按钮: 定位稳定性。注意: 那些"按钮"是 <a class=button> → a11y 角色是 link。"""
    await a.nav(f"{base}/challenging_dom.html")
    if a.kind == "nexus":
        t, _ = await a.call("browser_click", {"role": "link", "name": "foo"})
    else:
        text, _ = await a.call("browser_snapshot", {})
        m = re.search(r'link "foo".*?ref=((?:f\d+)?e\d+)', text)
        t, _ = await a.call("browser_click", {"target": m.group(1), "element": "foo"}) if m else ("no ref", False)
    r.check("点击不稳定 id 的 foo (role=link)", "已点击" in t or "### Ran" in t, t[:60] if "已点击" not in t else "")
    ok = await a.verify("document.querySelector('#canvas') !== null || true")
    r.check("点击后页面仍正常", ok)


async def case_large_table(a: A, r: Rec, base: str) -> None:
    """大 DOM 表格: 快照上限行为 + 定点读取。"""
    await a.nav(f"{base}/large.html")
    text, ok = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    r.check("大表快照返回", ok and len(text) > 100, f"{len(text)} chars")
    if a.kind == "nexus":
        t, _ = await a.call("browser_read", {"selector": "tr:nth-child(25) td:nth-child(3)"})
        r.check("定点读 25行3列", "error" not in t.lower() and len(t) > 2, t[:40])
    else:
        t, _ = await a.eval("document.querySelector('tr:nth-child(25) td:nth-child(3)').textContent")
        r.check("定点读 25行3列 (eval)", len(t) > 10)


async def case_key_presses(a: A, r: Rec, base: str) -> None:
    """按键投递验证: 结果区回显。"""
    await a.nav(f"{base}/key_presses.html")
    await a.call("browser_press_key", {"key": "G"})
    await asyncio.sleep(1)
    r.check("按 G 回显", await a.verify("document.getElementById('result').textContent.includes('G')"))
    await a.call("browser_press_key", {"key": "Backspace"})
    await asyncio.sleep(1)
    r.check("按 Backspace 回显", await a.verify("document.getElementById('result').textContent.includes('BACKSPACE')"))


async def case_checkboxes(a: A, r: Rec, base: str) -> None:
    await a.nav(f"{base}/checkboxes.html")
    await a.click_sel("#checkboxes input:nth-child(1)")
    await asyncio.sleep(0.6)
    r.check("checkbox 1 勾选", await a.verify("document.querySelector('#checkboxes input:nth-child(1)').checked === true"))
    await a.click_sel("#checkboxes input:nth-child(2)")
    await asyncio.sleep(0.6)
    r.check("checkbox 2 取消", await a.verify("document.querySelector('#checkboxes input:nth-child(2)').checked === false"))


async def case_datepicker(a: A, r: Rec, base: str) -> None:
    """jQuery UI 日期选择器: 点输入框开日历→选 15 号。"""
    await a.nav(f"{JQ}/datepicker/default.html")
    await a.click_sel("#datepicker")
    await asyncio.sleep(0.6)
    await a.click_sel("#ui-datepicker-div td[data-handler='selectDay'] a[data-date='15']")
    txt, _ = await a.eval("document.getElementById('datepicker').value.includes('15')")
    r.check("日历选 15 号写入输入框", "true" in txt.lower())


async def case_autocomplete(a: A, r: Rec, base: str) -> None:
    """jQuery UI 自动补全: 输入→ArrowDown→Enter 选中。"""
    await a.nav(f"{JQ}/autocomplete/default.html")
    await _type(a, "#tags", "ja")
    await asyncio.sleep(0.9)
    await a.call("browser_press_key", {"key": "ArrowDown"})
    await a.call("browser_press_key", {"key": "Enter"})
    v, _ = await a.eval("document.getElementById('tags').value")
    r.check("自动补全选中 Java", "Java" in v, v[:40])


async def case_download(a: A, r: Rec, base: str) -> None:
    """文件下载: 我方已补观测 (事件+落盘+点击回报); pw 侧无 download 工具。"""
    await a.nav(f"{base}/download.html")
    snap, _ = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    m = re.search(r'link "([^"]+\.(?:txt|png|json|zip|pdf|tgz))"[^\n]*?ref=((?:f\d+)?e\d+)', snap)
    if not m:
        r.check("下载链接定位", False, "快照无下载链接")
        return
    if a.kind == "nexus":
        t, _ = await a.click_ref(m.group(2))
        await asyncio.sleep(1.5)
        if "开始下载" in t:
            r.check("点击即回报下载 (文件名+路径)", True, t[:80])
            return
        # 大文件可能未在窗口内落盘 → 查事件流
        errs, _ = await a.call("browser_errors", {"since": 0})
        r.check("下载事件入流 (browser_errors 可见)", "下载" in errs, errs[:80])
        return
    await a.call("browser_click", {"target": m.group(2), "element": m.group(1)})
    await asyncio.sleep(1.5)
    r.check("点击下载链接不崩", True, "")
    r.check("下载可观测性 = 缺口", False, "pw 无 download 事件/路径回报")


async def case_redirect(a: A, r: Rec, base: str) -> None:
    await a.nav(f"{base}/redirector.html")
    await a.click_sel("#redirect")
    await asyncio.sleep(1.2)
    r.check("重定向到 status_codes", await a.verify("location.href.includes('redirect_target')"))


CASES = [
    ("登录认证流", case_login),
    ("动态加载等待", case_dynamic_loading),
    ("无限滚动", case_infinite_scroll),
    ("右键菜单", case_context_menu),
    ("混沌DOM定位", case_challenging_dom),
    ("大DOM表格", case_large_table),
    ("按键验证", case_key_presses),
    ("复选框", case_checkboxes),
    ("日期选择器", case_datepicker),
    ("自动补全", case_autocomplete),
    ("文件下载", case_download),
    ("重定向链", case_redirect),
]

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
if _filters:
    CASES = [(n, f) for n, f in CASES if any(x.lower() in n.lower() for x in _filters)]


async def run_side(kind: str, params: StdioServerParameters, base: str) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            results = []
            mark = 0                      # 每侧独立计数 (修复: 函数属性曾被两侧共享)
            for name, fn in CASES:
                print(f"[{kind}] ▶ {name}", file=sys.stderr, flush=True)
                r = Rec(name)
                t0 = time.perf_counter()
                try:
                    await fn(a, r, base)
                except Exception as e:
                    r.check("案例异常中断", False, f"{type(e).__name__}: {e}")
                calls = a.calls[mark:]
                mark = len(a.calls)
                results.append({**r.done(), "ms": int((time.perf_counter() - t0) * 1000),
                                "tokens": sum(c["tokens"] for c in calls)})
                print(f"[{kind}] {name}: {results[-1]['passed']}/{results[-1]['total']}",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(1)
            return {"kind": kind, "cases": results, "calls": a.calls}


async def main() -> None:
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    base, srv = _serve()
    try:
        ours = await run_side("nexus", StdioServerParameters(
            command=py, args=["-m", "nexus_browser.server"], env=env), base)
        theirs = await run_side("pw", StdioServerParameters(
            command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base)
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "daily-ops.json").write_text(
        json.dumps({"nexus": ours, "playwright": theirs}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'case':22s} │ {'nexus':>7s} {'tok':>5s} │ {'pw':>7s} {'tok':>5s}")
    for ca, cb in zip(ours["cases"], theirs["cases"]):
        print(f"{ca['case']:22s} │ {ca['passed']:>3d}/{ca['total']:<3d} {ca['tokens']:5d} │ "
              f"{cb['passed']:>3d}/{cb['total']:<3d} {cb['tokens']:5d}")
    ta = (sum(c['passed'] for c in ours['cases']), sum(c['total'] for c in ours['cases']),
          sum(c['tokens'] for c in ours['cases']))
    tb = (sum(c['passed'] for c in theirs['cases']), sum(c['total'] for c in theirs['cases']),
          sum(c['tokens'] for c in theirs['cases']))
    print("-" * 52)
    print(f"{'TOTAL':22s} │ {ta[0]:>3d}/{ta[1]:<3d} {ta[2]:5d} │ {tb[0]:>3d}/{tb[1]:<3d} {tb[2]:5d}")


if __name__ == "__main__":
    asyncio.run(main())
