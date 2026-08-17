"""新工具真实场景实测: 第一波+第二波的 7 个工具在真实站点/标准测试场上的回归猎杀

与 adversarial.py 的区别: 那份用 hermetic fixture 证"能力存在"; 这份用真实站点证"站得住"。
只测 nexus (目标是找我们的问题, 不是对比)。每案例 3-6 个可程序化判定的子任务。

站点:
- the-internet.herokuapp.com: dropdown/upload/hovers/drag_and_drop/javascript_alerts (标准公共测试场)
- github.com: "/" 键盘快捷键 (真实 SPA)
- zh.wikipedia.org: 正文链接 hovercard (真实 hover 交互)
- duckduckgo.com: 搜索→进结果→后退 组合流
- jqueryui.com sortable: pointer 系拖拽 (drag_to 的硬骨头)

用法: .venv/Scripts/python.exe -u bench/newtools_real.py
产出: docs/bench/newtools-real.json + stdout 汇总
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adversarial import A  # noqa: E402  复用适配器/计量
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
TI = "https://the-internet.herokuapp.com"


class Rec:
    """子任务级记录: (名称, 通过?, 备注)。"""

    def __init__(self, case: str) -> None:
        self.case = case
        self.subs: list[dict] = []

    def check(self, name: str, ok: bool, note: str = "") -> None:
        self.subs.append({"sub": name, "ok": bool(ok), "note": note})
        print(f"      {'ok' if ok else 'FAIL'} {name} {note}", file=sys.stderr, flush=True)

    def done(self) -> dict:
        return {"case": self.case, "passed": sum(1 for s in self.subs if s["ok"]),
                "total": len(self.subs), "subs": self.subs}


# ── 案例 ─────────────────────────────────────────────────────────

async def case_dropdown(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/dropdown")
    await a.call("browser_select_option", {"values": ["Option 2"], "selector": "#dropdown"})
    ok, *_ = [await a.verify("document.getElementById('dropdown').value === '2'")]
    r.check("select_option 真实 <select>", ok)
    r2 = await a.call("browser_select_option", {"values": ["1"], "selector": "#dropdown"})
    r.check("按 value 再选回 Option 1", "已选择" in r2[0])
    ok = await a.verify("document.getElementById('dropdown').value === '1'")
    r.check("value 已回切", ok)


async def case_upload(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/upload")
    # 门控子任务: 未确认必须先被拒
    text, _ = await a.call("browser_upload_file",
                           {"paths": [str(ROOT / "pyproject.toml")], "selector": "#file-upload"})
    r.check("未 confirmed → CONFIRMATION_REQUIRED", "CONFIRMATION_REQUIRED" in text)
    await a.call("browser_upload_file",
                 {"paths": [str(ROOT / "pyproject.toml")], "selector": "#file-upload", "confirmed": True})
    await a.click_sel("#file-submit")
    await asyncio.sleep(1)
    ok = await a.verify("document.querySelector('h3') && document.querySelector('h3').textContent.includes('File Uploaded')")
    r.check("真实上传全流程 (HITL→提交→服务端确认页)", ok)
    ok2 = await a.verify("document.body.textContent.includes('pyproject.toml')")
    r.check("服务端回显文件名", ok2)


async def case_hovers(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/hovers")
    await a.call("browser_hover", {"selector": ".figure:nth-child(3) img"})
    await asyncio.sleep(0.8)
    ok = await a.verify("getComputedStyle(document.querySelector('.figure:nth-child(3) .figcaption')).display === 'block'")
    r.check("hover 触发 figcaption 显示 (computed)", ok)
    snap, _ = await a.call("browser_snapshot", {"diff": False})
    r.check("快照中露出悬停区的可操作链接 (View profile)", "View profile" in snap)


async def case_drag_html5(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/drag_and_drop")
    await a.call("browser_drag", {"from_selector": "#column-a", "to_selector": "#column-b"})
    await asyncio.sleep(0.5)
    ok = await a.verify("document.querySelector('#column-a header').textContent.trim() === 'B'")
    r.check("HTML5 拖拽 A↔B 交换", ok)


async def case_drag_sortable(a: A, r: Rec) -> None:
    """jQuery UI sortable: pointer 系拖拽 (mousedown/mousemove), drag_to 的硬骨头。"""
    await a.nav("https://jqueryui.com/resources/demos/sortable/default.html")
    before, _ = await a.eval("[...document.querySelectorAll('#sortable li')].map(x=>x.textContent).join(',')")
    await a.call("browser_drag", {"from_selector": "#sortable li:nth-child(1)",
                                  "to_selector": "#sortable li:nth-child(3)"})
    await asyncio.sleep(0.6)
    after, _ = await a.eval("[...document.querySelectorAll('#sortable li')].map(x=>x.textContent).join(',')")
    r.check("sortable 顺序变化", before != after, f"before={before[:30]!r} after={after[:30]!r}")


async def case_dialogs(a: A, r: Rec) -> None:
    await a.nav(f"{TI}/javascript_alerts")
    # alert: 直接 auto-dismiss, 但事件必须留痕
    await a.click_sel("button[onclick='jsAlert()']")
    ok = await a.verify("document.getElementById('result').textContent.includes('successfully')")
    r.check("alert 自动 dismiss + 页面继续", ok)
    await asyncio.sleep(0.3)
    errs, _ = await a.call("browser_errors", {"since": 0})
    r.check("dialog 事件入流 (browser_errors 可见)", "dialog" in errs and "I am a JS Alert" in errs)
    # confirm accept: 挂起 → 通知前置 → respond(accept, confirmed) → 页面拿到 true
    await a.click_sel("button[onclick='jsConfirm()']")
    errs2, _ = await a.call("browser_errors", {})  # 任意工具调用都应前置等待决策提醒
    r.check("挂起期间回复前置 [对话框等待决策]", "对话框等待决策" in errs2)
    # 未 confirmed 的 accept 必须被拒
    t, _ = await a.call("browser_dialog_respond", {"accept": True})
    r.check("accept 未 confirmed → CONFIRMATION_REQUIRED", "CONFIRMATION_REQUIRED" in t)
    await a.call("browser_dialog_respond", {"accept": True, "confirmed": True})
    ok = await a.verify("document.getElementById('result').textContent.includes('Ok')")
    r.check("confirm accept → 页面收到 Ok", ok)
    # confirm dismiss
    await a.click_sel("button[onclick='jsConfirm()']")
    await a.call("browser_dialog_respond", {"accept": False})
    ok = await a.verify("document.getElementById('result').textContent.includes('Cancel')")
    r.check("confirm dismiss → 页面收到 Cancel", ok)
    # prompt 填文本
    await a.click_sel("button[onclick='jsPrompt()']")
    await a.call("browser_dialog_respond", {"accept": True, "prompt_text": "nexus-实测", "confirmed": True})
    ok = await a.verify("document.getElementById('result').textContent.includes('nexus-实测')")
    r.check("prompt 填文本", ok)


async def case_github_keyboard(a: A, r: Rec) -> None:
    await a.nav("https://github.com/paipaipai666/nexus-browser-mcp")
    await a.call("browser_press_key", {"key": "/"})
    await asyncio.sleep(0.8)
    ok = await a.verify("document.activeElement && /INPUT|TEXTAREA/.test(document.activeElement.tagName)")
    r.check("'/' 快捷键聚焦搜索框", ok)
    await a.call("browser_type", {"text": "issues", "clear": True})
    await a.call("browser_press_key", {"key": "Escape"})
    ok = await a.verify("document.activeElement === document.body || !/INPUT/.test(document.activeElement.tagName)")
    r.check("Escape 退出聚焦/关闭面板", ok)


async def case_wiki_hovercard(a: A, r: Rec) -> None:
    await a.nav("https://zh.wikipedia.org/wiki/Python")
    find = ("(() => { const a = [...document.querySelectorAll('#mw-content-text p a')]"
            ".find(x => /范罗苏姆|范羅蘇姆|吉多/.test(x.textContent));"
            " if (!a) return 'NONE'; a.scrollIntoView({block:'center'});"
            " const b = a.getBoundingClientRect();"
            " return '' + Math.round(b.x+b.width/2) + ',' + Math.round(b.y+b.height/2); })()")
    txt, _ = await a.eval(find)
    if "NONE" in txt:
        r.check("定位正文条目链接", False, "吉多·范罗苏姆 链接未找到")
        return
    xy = txt.split("结果: ")[1].strip().strip("'").split(",")
    r.check("定位正文条目链接", True, f"pos={xy}")
    await a.call("browser_hover", {"pos": f"{xy[0]},{xy[1]},4,4"})  # 精确坐标悬停
    await asyncio.sleep(1.5)
    ok, _ = await a.eval("(() => { const p = document.querySelector('.mwe-popups');"
                         " return !!p && p.textContent.length > 20; })()")
    r.check("hovercard 弹出且有内容", "true" in ok.lower())


async def case_search_back(a: A, r: Rec) -> None:
    """组合流: 搜索→新标签结果(切回) + 同标签导航(后退)。
    Bing 结果 target=_blank → 走 Issue N 新标签+switch_page 路径;
    HN 评论链同标签 → 走 navigate_back 路径。两种都是日常。"""
    await a.nav("https://www.bing.com")
    ref = await a.snap_ref(r"textbox|searchbox|combobox")
    r.check("定位搜索框", bool(ref))
    await a.call("browser_type", {"ref": ref, "text": "nexus browser mcp", "press_enter": True})
    await asyncio.sleep(3)
    ok = await a.verify("location.href.includes('q=nexus')")
    r.check("搜索跳转 (URL 带 q=)", ok)
    first = await a.snap_ref(r'link.*nexus')
    if not first:
        r.check("新标签打开结果并切回", False, "结果链接 ref 未定位")
    else:
        await a.click_ref(first)
        await asyncio.sleep(2.5)
        pages, _ = await a.call("browser_list_pages", {})
        two = "[1]" in pages
        r.check("结果在新标签打开且可枚举", two, pages[:80].replace("\n", " | "))
        if two:
            await a.call("browser_switch_page", {"index": 0})
            await asyncio.sleep(0.8)
            ok = await a.verify("location.href.includes('q=nexus')")
            r.check("切回搜索结果页", ok)
    # 同标签后退流: 维基正文链接 (HN 当前经此代理出口不可达, 换同结构可靠目标)
    await a.nav("https://zh.wikipedia.org/wiki/Python")
    find = ("(() => { const a = [...document.querySelectorAll('#mw-content-text p a')]"
            ".find(x => /范罗苏姆|范羅蘇姆|吉多/.test(x.textContent)); return a ? 'ok' : 'NONE'; })()")
    txt, _ = await a.eval(find)
    r.check("维基定位正文链接", "ok" in txt)
    if "ok" in txt:
        await a.eval("(() => { const a = [...document.querySelectorAll('#mw-content-text p a')]"
                     ".find(x => /范罗苏姆|范羅蘇姆|吉多/.test(x.textContent));"
                     " a.scrollIntoView({block:'center'}); return 'ok'; })()")
        snap, _ = await a.call("browser_snapshot", {"diff": False})
        import re as _re
        m = None
        for line in snap.splitlines():
            if _re.search(r"范罗苏姆|范羅蘇姆|吉多", line):
                m = _re.search(r"ref=((?:f\d+)?e\d+)", line)
                if m:
                    break
        if m:
            await a.click_ref(m.group(1))
            await asyncio.sleep(2)
            ok = await a.verify("location.href.includes('wiki') && !location.href.includes('Python')")
            r.check("同标签进入条目页", ok)
            await a.call("browser_navigate_back", {})
            await asyncio.sleep(1.2)
            ok = await a.verify("location.href.endsWith('/wiki/Python')")
            r.check("navigate_back 回到 Python 条目", ok)
        else:
            r.check("同标签进入条目页", False, "快照中未找到链接 ref")


CASES = [
    ("select真实下拉", case_dropdown),
    ("真实文件上传", case_upload),
    ("hover真实悬停", case_hovers),
    ("HTML5拖拽", case_drag_html5),
    ("pointer系拖拽(sortable)", case_drag_sortable),
    ("对话框三部曲", case_dialogs),
    ("GitHub键盘快捷键", case_github_keyboard),
    ("维基hovercard", case_wiki_hovercard),
    ("搜索→后退组合流", case_search_back),
]

# 命令行子串过滤: python bench/newtools_real.py github sortable
_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
if _filters:
    CASES = [(n, f) for n, f in CASES if any(x.lower() in n.lower() for x in _filters)]


async def main() -> None:
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    async with stdio_client(StdioServerParameters(
            command=py, args=["-m", "nexus_browser.server"], env=env)) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, "nexus")
            results = []
            for name, fn in CASES:
                print(f"[newtools] ▶ {name}", file=sys.stderr, flush=True)
                r = Rec(name)
                t0 = time.perf_counter()
                try:
                    await fn(a, r)
                except Exception as e:
                    r.check("案例异常中断", False, f"{type(e).__name__}: {e}")
                results.append({**r.done(), "ms": int((time.perf_counter() - t0) * 1000)})
                print(f"[newtools] {name}: {results[-1]['passed']}/{results[-1]['total']}",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(1.5)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "newtools-real.json").write_text(
        json.dumps({"cases": results, "calls": a.calls}, ensure_ascii=False, indent=2), encoding="utf-8")
    tp = sum(c["passed"] for c in results)
    tt = sum(c["total"] for c in results)
    print(f"\n{'case':26s} │ 子任务   {'ms':>7s}")
    for c in results:
        print(f"{c['case']:26s} │ {c['passed']}/{c['total']}    {c['ms']:7d}")
    print("-" * 46)
    print(f"{'TOTAL':26s} │ {tp}/{tt}")


if __name__ == "__main__":
    asyncio.run(main())
