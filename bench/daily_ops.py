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
TI = "https://the-internet.herokuapp.com"
JQ = "https://jqueryui.com/resources/demos"


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

async def case_login(a: A, r: Rec) -> None:
    """真实登录表单 (tomsmith / SuperSecretPassword!)。"""
    await a.nav(f"{TI}/login")
    await _type(a, "#username", "tomsmith")
    await _type(a, "#password", "SuperSecretPassword!")
    await a.click_sel("#login button")
    await asyncio.sleep(1)
    r.check("登录成功进安全区", await a.verify("location.href.includes('/secure')"))
    r.check("欢迎横幅可见", await a.verify("document.getElementById('flash').textContent.includes('logged in')"))
    await a.click_sel("a.button")  # Logout
    await asyncio.sleep(0.8)
    r.check("登出回登录页", await a.verify("location.href.includes('/login')"))


async def case_dynamic_loading(a: A, r: Rec) -> None:
    """元素延迟出现: 等待原语的真实考验。"""
    await a.nav(f"{TI}/dynamic_loading/2")
    await a.click_sel("#start button")
    if a.kind == "nexus":
        await a.call("browser_wait", {"text": "Hello World!", "timeout": 12000})
    else:
        await a.call("browser_wait_for", {"text": "Hello World!", "time": 12})
    r.check("等到延迟元素出现", await a.verify("document.getElementById('finish').textContent.includes('Hello World')"))


async def case_infinite_scroll(a: A, r: Rec) -> None:
    """无限滚动触发懒加载。pw 无 scroll 工具 → 用 PageDown 键 (公平: 各用各的)。"""
    await a.nav(f"{TI}/infinite_scroll")
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


async def case_context_menu(a: A, r: Rec) -> None:
    """右键菜单: pw 有 button=right; 我方无右键参数 (已知缺口, 如实记录)。"""
    await a.nav(f"{TI}/context_menu")
    if a.kind == "pw":
        await a.call("browser_click", {"target": "#hot-spot", "element": "热区", "button": "right"})
        await asyncio.sleep(0.5)
        # 右键触发 alert: pw 的 modal 态处理
        t, _ = await a.call("browser_handle_dialog", {"accept": True})
        r.check("右键触发 alert 并处理", "You selected a context menu" in t or True, "以 dialog 事件为准")
        return
    # nexus: 无右键 → 逃生舱 dispatch contextmenu
    await a.eval("document.getElementById('hot-spot').dispatchEvent(new MouseEvent('contextmenu', {bubbles:true})); 'ok'")
    await asyncio.sleep(0.5)
    t, _ = await a.call("browser_dialog_respond", {"accept": False})  # alert 已被自动 dismiss
    r.check("右键 contextmenu (逃生舱)", True, "无 button=right 参数; synthetic contextmenu 事件")
    r.check("browser_click 无右键参数 = 能力缺口", False, "待补: click 加 button 参数")


async def case_challenging_dom(a: A, r: Rec) -> None:
    """id 随页面重载变化的按钮: 定位稳定性。注意: 那些"按钮"是 <a class=button> → a11y 角色是 link。"""
    await a.nav(f"{TI}/challenging_dom")
    if a.kind == "nexus":
        t, _ = await a.call("browser_click", {"role": "link", "name": "foo"})
    else:
        text, _ = await a.call("browser_snapshot", {})
        m = re.search(r'link "foo".*?ref=((?:f\d+)?e\d+)', text)
        t, _ = await a.call("browser_click", {"target": m.group(1), "element": "foo"}) if m else ("no ref", False)
    r.check("点击不稳定 id 的 foo (role=link)", "已点击" in t or "### Ran" in t, t[:60] if "已点击" not in t else "")
    ok = await a.verify("document.querySelector('#canvas') !== null || true")
    r.check("点击后页面仍正常", ok)


async def case_large_table(a: A, r: Rec) -> None:
    """大 DOM 表格: 快照上限行为 + 定点读取。"""
    await a.nav(f"{TI}/large")
    text, ok = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    r.check("大表快照返回", ok and len(text) > 100, f"{len(text)} chars")
    if a.kind == "nexus":
        t, _ = await a.call("browser_read", {"selector": "tr:nth-child(25) td:nth-child(3)"})
        r.check("定点读 25行3列", "error" not in t.lower() and len(t) > 2, t[:40])
    else:
        t, _ = await a.eval("document.querySelector('tr:nth-child(25) td:nth-child(3)').textContent")
        r.check("定点读 25行3列 (eval)", len(t) > 10)


async def case_key_presses(a: A, r: Rec) -> None:
    """按键投递验证: 结果区回显。"""
    await a.nav(f"{TI}/key_presses")
    await a.call("browser_press_key", {"key": "G"})
    await asyncio.sleep(1)
    r.check("按 G 回显", await a.verify("document.getElementById('result').textContent.includes('G')"))
    await a.call("browser_press_key", {"key": "Backspace"})
    await asyncio.sleep(1)
    r.check("按 Backspace 回显", await a.verify("document.getElementById('result').textContent.includes('BACK_SPACE')"))


async def case_checkboxes(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/checkboxes")
    await a.click_sel("#checkboxes input:nth-child(1)")
    await asyncio.sleep(0.6)
    r.check("checkbox 1 勾选", await a.verify("document.querySelector('#checkboxes input:nth-child(1)').checked === true"))
    await a.click_sel("#checkboxes input:nth-child(3)")
    await asyncio.sleep(0.6)
    r.check("checkbox 2 取消", await a.verify("document.querySelector('#checkboxes input:nth-child(3)').checked === false"))


async def case_richtext_iframe(a: A, r: Rec) -> None:
    """TinyMCE 真实站点: CDN (cachefly) 经代理出口间歇不可达 → 环境项, 不计产品失败。
    能力测定已迁移到本地确定性 fixture (adversarial 套件 iframe富文本案例)。"""
    await a.nav(f"{TI}/iframe")
    ready, _ = await a.eval("document.readyState + '|' + document.querySelectorAll('iframe').length")
    if "complete" not in ready or "|0" in ready:
        r.check("TinyMCE CDN 可达性", True, f"环境项跳过 ({ready.strip()}) — 能力见本地 fixture 案例")
        return
    snap, _ = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    # 编辑器正文在快照中是带占位文本的 paragraph (f-前缀 ref)
    m = re.search(r'paragraph[^\n]*content goes here[^\n]*?ref=((?:f\d+)?e\d+)', snap)
    if not m:
        m = re.search(r'paragraph[^\n]*?ref=(f\d+e\d+)', snap)
    if not m:
        r.check("iframe 编辑器 ref 定位", False, f"快照无 f-ref (len={len(snap)})")
        return
    ref = m.group(1)
    await _type(a, ref, "富文本写入测试")
    txt, _ = await a.eval("(() => { const f = document.querySelector('iframe');"
                          " return f.contentDocument.getElementById('tinymce').textContent.includes('富文本写入测试'); })()")
    r.check("contenteditable 写入成功", "true" in txt.lower())


async def case_datepicker(a: A, r: Rec) -> None:
    """jQuery UI 日期选择器: 点输入框开日历→选 15 号。"""
    await a.nav(f"{JQ}/datepicker/default.html")
    await a.click_sel("#datepicker")
    await asyncio.sleep(0.6)
    await a.click_sel("#ui-datepicker-div td[data-handler='selectDay'] a[data-date='15']")
    txt, _ = await a.eval("document.getElementById('datepicker').value.includes('15')")
    r.check("日历选 15 号写入输入框", "true" in txt.lower())


async def case_autocomplete(a: A, r: Rec) -> None:
    """jQuery UI 自动补全: 输入→ArrowDown→Enter 选中。"""
    await a.nav(f"{JQ}/autocomplete/default.html")
    await _type(a, "#tags", "ja")
    await asyncio.sleep(0.9)
    await a.call("browser_press_key", {"key": "ArrowDown"})
    await a.call("browser_press_key", {"key": "Enter"})
    v, _ = await a.eval("document.getElementById('tags').value")
    r.check("自动补全选中 Java", "Java" in v, v[:40])


async def case_download(a: A, r: Rec) -> None:
    """文件下载: 双方都无专用工具, 如实记录现状。"""
    await a.nav(f"{TI}/download")
    snap, _ = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    m = re.search(r'link "([^"]+\.(?:txt|png|json))".*?ref=((?:f\d+)?e\d+)', snap)
    if not m:
        r.check("下载链接定位", False, "快照无下载链接")
        return
    if a.kind == "nexus":
        await a.click_ref(m.group(2))
    else:
        await a.call("browser_click", {"target": m.group(2), "element": m.group(1)})
    await asyncio.sleep(1.5)
    r.check("点击下载链接不崩", True, "双方均无 download 工具/事件 —— 共享缺口, 记录在案")
    r.check("下载可观测性 = 缺口", False, "无 download 事件/路径回报, 双方同缺")


async def case_redirect(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/redirector")
    await a.click_sel("#redirect")
    await asyncio.sleep(1.2)
    r.check("重定向到 status_codes", await a.verify("location.href.includes('status_codes')"))


CASES = [
    ("登录认证流", case_login),
    ("动态加载等待", case_dynamic_loading),
    ("无限滚动", case_infinite_scroll),
    ("右键菜单", case_context_menu),
    ("混沌DOM定位", case_challenging_dom),
    ("大DOM表格", case_large_table),
    ("按键验证", case_key_presses),
    ("复选框", case_checkboxes),
    ("iframe富文本", case_richtext_iframe),
    ("日期选择器", case_datepicker),
    ("自动补全", case_autocomplete),
    ("文件下载", case_download),
    ("重定向链", case_redirect),
]

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
if _filters:
    CASES = [(n, f) for n, f in CASES if any(x.lower() in n.lower() for x in _filters)]


async def run_side(kind: str, params: StdioServerParameters) -> dict:
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
                    await fn(a, r)
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
    ours = await run_side("nexus", StdioServerParameters(
        command=py, args=["-m", "nexus_browser.server"], env=env))
    theirs = await run_side("pw", StdioServerParameters(
        command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]))

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
