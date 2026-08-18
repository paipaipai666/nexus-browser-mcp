"""企业级知识工作双侧基准 (业界方法论落地: WebArena/WorkArena/Mind2Web)

方法论来源与落地:
- WebArena Verified 教训: 脆弱字符串匹配虚增成绩 → 本套件断言一律读状态(eval/DOM 真值),
  且"答案必须从工具输出中读到" (bench 解析工具返回文本), 杜绝 eval 自我作弊
- WorkArena 企业六类 → 覆盖: 列表过滤/排序、仪表盘读数、知识库答题、多步下单、跨店比价
- Mind2Web 分层指标 → 子任务 SR + 元素命中 (data-ok 旗标) + 每次成功的步数/token
- 模板化防背题 → 夹具每次加载随机化 (商品/价格/退款天数/订单金额全随机), 真值由 bench 运行时读 DOM

用法: .venv/Scripts/python.exe -u bench/enterprise_ops.py [子串过滤]
产出: docs/bench/enterprise-ops.json
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

sys.path.insert(0, str(Path(__file__).parent))
from adversarial import A  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
FIXE = Path(__file__).parent / "fixture_ent"


def _serve() -> tuple[str, object]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXE))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


async def _eval_num(a: A, expr: str) -> float | None:
    """读 DOM 真值 (bench 侧 ground truth)。"""
    txt, ok = await a.eval(expr)
    if not ok:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", txt)
    return float(m.group().replace(",", "")) if m else None


async def _read_page_text(a: A) -> str:
    """用工具读页面正文 (答案的合法来源): nexus=read, pw/cdt=snapshot。"""
    if a.kind == "nexus":
        t, _ = await a.call("browser_read", {"max_chars": 4000})
        return t
    if a.kind == "cdt":
        t, _ = await a.call("take_snapshot", {})
        return t
    t, _ = await a.call("browser_snapshot", {})
    return t


# ── 案例 ─────────────────────────────────────────────────────────

async def case_list_filter(a: A, r: Rec, base: str) -> None:
    """列表过滤: 随机客户名过滤, 行数与 DOM 真值一致 (真值在过滤前从全量数据读取, 防循环论证)。"""
    await a.nav(f"{base}/orders.html")
    dump, _ = await a.eval("[...document.querySelectorAll('#t tbody tr')].map("
                           "x => [x.children[0].textContent, x.children[1].textContent, x.children[2].textContent]"
                           ".join('|')).join(';')")
    m = re.search(r"结果: '(.+)'", dump, re.S) or re.search(r'### Result\s*\n"?(.+?)"?(?:\n###|$)', dump, re.S)
    body = m.group(1) if m else dump
    all_rows = [row.split("|") for row in body.split(";") if "|" in row]
    name = all_rows[0][1]  # 必存在的客户名
    exp_name = sum(1 for row in all_rows if name in row[1])
    exp_combo = sum(1 for row in all_rows if name in row[1] and row[2] == "已发货")
    await a.type_into("#q", name)
    await asyncio.sleep(0.5)
    visible = await _eval_num(a, "document.querySelectorAll('#t tbody tr').length")
    r.check("名称过滤: 行数=真值", visible == exp_name, f"visible={visible} truth={exp_name}")
    await a.select_native("#st", ["已发货"])
    await asyncio.sleep(0.5)
    visible2 = await _eval_num(a, "document.querySelectorAll('#t tbody tr').length")
    r.check("名称+状态组合过滤", visible2 == exp_combo, f"visible={visible2} truth={exp_combo}")


async def case_list_sort(a: A, r: Rec, base: str) -> None:
    """列表排序: 点金额表头 → 首行金额=当前最大值; 元素命中=点的是表头。"""
    await a.nav(f"{base}/orders.html")
    await a.click_sel("#thAmt")
    await asyncio.sleep(0.5)
    sorted_flag, _ = await a.eval("document.getElementById('thAmt').dataset.sorted || ''")
    r.check("元素命中: 点中排序表头", "desc" in sorted_flag or "asc" in sorted_flag, sorted_flag[:40])
    first_amt = await _eval_num(a, "document.querySelector('#t tbody tr td').textContent")
    max_amt = await _eval_num(a, "Math.max(...[...document.querySelectorAll('#t tbody tr td:first-child')]"
                                 ".map(x => +x.textContent))")
    min_amt = await _eval_num(a, "Math.min(...[...document.querySelectorAll('#t tbody tr td:first-child')]"
                                 ".map(x => +x.textContent))")
    ok = first_amt is not None and first_amt in (max_amt, min_amt)
    r.check("首行金额=极值 (排序生效)", ok, f"first={first_amt} max={max_amt} min={min_amt}")


async def case_dashboard(a: A, r: Rec, base: str) -> None:
    """仪表盘读数: 答案必须从工具输出读到, 与 DOM 真值比对。"""
    await a.nav(f"{base}/dashboard.html")
    page_text = await _read_page_text(a)
    sales_true = await _eval_num(a, "document.getElementById('sales').textContent.replace(/[^\\d.]/g,'')")
    m = re.search(r"¥([\d,]+)", page_text)
    sales_tool = float(m.group(1).replace(",", "")) if m else None
    r.check("销售额: 工具输出值=真值", sales_tool is not None and sales_tool == sales_true,
            f"tool={sales_tool} truth={sales_true}")
    conv_true_txt, _ = await a.eval("document.getElementById('conv').textContent")
    m2 = re.search(r"结果: '(.+?)'", conv_true_txt) or re.search(r'### Result\s*\n"?(.+?)"?\s*(?:\n###|$)',
                                                                 conv_true_txt, re.S)
    conv_true = m2.group(1) if m2 else conv_true_txt.strip()
    r.check("转化率: 工具输出含真值", conv_true in page_text, f"truth={conv_true}")


async def case_kb_answer(a: A, r: Rec, base: str) -> None:
    """知识库答题: 搜索退款政策 → 打开 → 回答"退款期限几天", 答案须来自工具输出。"""
    await a.nav(f"{base}/kb.html")
    await a.type_into("#q", "退款")
    await asyncio.sleep(0.5)
    count = await _eval_num(a, "document.querySelectorAll('#list li').length")
    r.check("搜索过滤命中 1 篇", count == 1, f"count={count}")
    await a.click_sel("#list li a")
    await asyncio.sleep(0.5)
    page_text = await _read_page_text(a)
    days_true = await _eval_num(a, "window.__refundDays")
    m = re.search(r"(\d+)\s*天", page_text)
    days_tool = float(m.group(1)) if m else None
    r.check("退款天数: 工具输出值=真值", days_tool is not None and days_true is not None
            and days_tool == days_true, f"tool={days_tool} truth={days_true}")


async def case_shop_order(a: A, r: Rec, base: str, tag: str = "") -> None:
    """多步下单: 读列表找最低价 → 点开 → 填单 → 提交 → 确认页。元素命中=data-ok 旗标。"""
    await a.nav(f"{base}/shop.html")
    if a.kind == "cdt":
        text, _ = await a.call("take_snapshot", {})
        items = [(m.group(2), m.group(3), m.group(1)) for m in
                 re.finditer(r'uid=(\d+_\d+) link "([^"]+?) ¥(\d+)"', text)]
    else:
        text, _ = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
        items = [(m.group(1), m.group(2), m.group(3)) for m in
                 re.finditer(r'link "([^"]+?) ¥(\d+)"[^\n]*?ref=((?:f\d+)?e\d+)', text)]
    if not items:
        r.check("解析商品列表", False, f"快照中无商品行 (len={len(text)})")
        return
    name, price, ref = min(items, key=lambda x: int(x[1]))
    r.check("解析商品列表", True, f"共{len(items)}个, 最低={name}¥{price}")
    truth_price = await _eval_num(a, "Math.min(...[...document.querySelectorAll('#plist a')]"
                                     ".map(x => +x.textContent.match(/¥(\\d+)/)[1]))")
    r.check("工具输出最低价=真值", truth_price is not None and int(price) == truth_price,
            f"tool={price} truth={truth_price}")
    await a.click_ref(ref)
    await asyncio.sleep(0.5)
    if a.kind == "pw":
        await a.call("browser_fill_form", {"fields": [
            {"name": "数量", "target": "#qty", "type": "textbox", "value": "2"},
            {"name": "收货人", "target": "#rcpt", "type": "textbox", "value": "张三"},
            {"name": "地址", "target": "#addr", "type": "textbox", "value": "上海市浦东新区"}]})
    else:
        await a.type_into("#qty", "2")
        await a.type_into("#rcpt", "张三")
        await a.type_into("#addr", "上海市浦东新区")
    await a.click_sel("#buy")
    await asyncio.sleep(0.6)
    confirm_ok = await a.verify("document.getElementById('confirm').textContent.includes('订单已提交')")
    r.check("订单确认出现", confirm_ok)
    hit, _ = await a.eval("document.getElementById('confirm').dataset.ok === '1'")
    r.check("元素命中: 点中的是最低价商品", "true" in hit.lower())
    oid_txt, _ = await a.eval("document.getElementById('confirm').dataset.oid || ''")
    r.check("订单号格式 NXP+8位", bool(re.search(r"NXP\d{8}", oid_txt)), oid_txt[:40])


async def case_price_compare(a: A, r: Rec, base: str) -> None:
    """跨店比价: 两店随机价, 买到便宜的那家 (多标签/后退各展其长)。"""
    await a.nav(f"{base}/shop_a.html")
    text_a = await _read_page_text(a)
    m = re.search(r"¥(\d+)", text_a)
    price_a = int(m.group(1)) if m else None
    r.check("读到 A 店价格", price_a is not None, f"A={price_a}")
    if a.kind == "nexus":
        # 新标签路径: 点击 target=_blank 链接 → 自动切到 B 店标签
        await a.click_sel("#toB")
        await asyncio.sleep(1)
        text_b = await _read_page_text(a)
    elif a.kind == "cdt":
        # cdt: 同标签导航 (无 navigate_back; 回退用 evaluate history.back)
        await a.nav(f"{base}/shop_b.html")
        text_b = await _read_page_text(a)
    else:
        await a.nav(f"{base}/shop_b.html")
        text_b = await _read_page_text(a)
    m2 = re.search(r"¥(\d+)", text_b)
    price_b = int(m2.group(1)) if m2 else None
    r.check("读到 B 店价格", price_b is not None, f"B={price_b}")
    if price_a is None or price_b is None:
        return
    if price_b <= price_a:
        await a.click_sel("#buy")           # 当前在 B
    else:
        if a.kind == "nexus":
            await a.call("browser_switch_page", {"index": 0})
        elif a.kind == "cdt":
            await a.eval("history.back()")
        else:
            await a.call("browser_navigate_back", {})
        await asyncio.sleep(0.8)
        await a.click_sel("#buy")
    await asyncio.sleep(0.6)
    bought, _ = await a.eval("window.__bought || ''")
    cheaper = "B" if price_b <= price_a else "A"
    r.check("买到便宜店", cheaper in bought, f"bought={bought} cheaper={cheaper} A={price_a} B={price_b}")


CASES = [
    ("列表过滤", case_list_filter),
    ("列表排序", case_list_sort),
    ("仪表盘读数", case_dashboard),
    ("知识库答题", case_kb_answer),
    ("下单流", case_shop_order),
    ("下单流·变体", lambda a, r, b: case_shop_order(a, r, b, tag="v2")),  # 模板随机化第二实例
    ("跨店比价", case_price_compare),
]

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
if _filters:
    CASES = [(n, f) for n, f in CASES if any(x in n for x in _filters)]


async def run_side(kind: str, params: StdioServerParameters, base: str) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            results = []
            mark = 0
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
                                "tokens": sum(c["tokens"] for c in calls),
                                "calls_n": len(calls)})
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
        cdt = await run_side("cdt", StdioServerParameters(
            command="npx", args=["-y", "chrome-devtools-mcp@latest", "--headless", "--isolated"]), base)
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "enterprise-ops.json").write_text(
        json.dumps({"nexus": ours, "playwright": theirs, "chrome_devtools": cdt},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    sides = [ours, theirs, cdt]
    print(f"\n{'case':16s} │ {'nx 子任务':>7s} {'tok':>5s} │ {'pw 子任务':>7s} {'tok':>5s} │ {'cdt 子任务':>7s} {'tok':>5s}")
    for i, name in enumerate([n for n, _ in CASES]):
        row = f"{name:16s} │"
        for sd in sides:
            c = sd["cases"][i]
            row += f" {c['passed']:>3d}/{c['total']:<3d} {c['tokens']:5d} │"
        print(row)
    row = f"{'TOTAL':16s} │"
    for sd in sides:
        p = sum(c['passed'] for c in sd['cases'])
        t_ = sum(c['total'] for c in sd['cases'])
        tk = sum(c['tokens'] for c in sd['cases'])
        row += f" {p:>3d}/{t_:<3d} {tk:5d} │"
    print("-" * 62)
    print(row)


if __name__ == "__main__":
    asyncio.run(main())
