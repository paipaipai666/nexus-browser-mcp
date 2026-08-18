"""规模化三方基准: 106 案例 / ~200 子任务 (nexus vs playwright-mcp vs chrome-devtools-mcp)

为什么存在: 小样本 (20 例) 完成率三家持平, 看不出差异 → 扩到 100+ 让
token/完成率/能力缺口在统计上显影。方法论沿用 enterprise_ops (WebArena/WorkArena/Mind2Web):
- 读数类答案必须从工具输出解析 (bench 正则提取), eval 只作 bench 侧真值
- 动作类用 eval/DOM 旗标验证 (data-sorted / data-ok / disabled)
- 夹具 ?seed=N 确定性随机 (mulberry32), 三方同种子同数据, 真值内嵌 window.__truth
- 差异化族 (富文本/下载/拖拽/右键) 记录能力缺口, 不算环境 flake

用法: .venv/Scripts/python.exe -u bench/scale_ops.py [子串过滤] [--sides nexus,pw,cdt]
产出: docs/bench/scale-ops.json
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
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adversarial import A  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
FIXS = Path(__file__).parent / "fixture_scale"
FIXA = Path(__file__).parent / "fixture_adv"
FIXD = Path(__file__).parent / "fixture_daily"

ROW_RE = re.compile(r"(SO-1\d{3}).{0,32}?(\d{1,3}).{0,32}?([一-龥]{2}).{0,32}?"
                    r"(待处理|已发货|已完成|已取消)")


def _body(txt: str) -> str:
    """剥掉三家 eval 响应包装: pw='### Result\\n..' / nexus='结果: repr' / cdt=裸值。"""
    m = re.search(r"### Result\s*\n(.*?)(?:\n###|$)", txt, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"结果: (.*)", txt, re.S)
    if m:
        return m.group(1).strip()
    return txt.strip()


def _s(txt: str) -> str:
    """标量归一: 剥包装 + 剥引号 (repr 单引号 / json 双引号)。"""
    return _body(txt).strip('"').strip("'")


def _json2(body: str):
    """多重编码 JSON 解析 (pw/cdt 会把 JSON 字符串再 JSON 编码一层)。"""
    import ast
    for _ in range(3):
        if not isinstance(body, str):
            return body
        try:
            body = json.loads(body)
        except Exception:
            try:
                body = ast.literal_eval(body)
            except Exception:
                return None
    return body if not isinstance(body, str) else None


def _serve(d: Path) -> tuple[str, object]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
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


async def _truth(a: A, expr: str = "JSON.stringify(window.__truth)"):
    txt, _ = await a.eval(expr)
    v = _json2(_body(txt))
    return v if isinstance(v, list) else None


async def _page_text(a: A) -> str:
    if a.kind == "nexus":
        t, _ = await a.call("browser_read", {"max_chars": 6000})
        return t
    if a.kind == "cdt":
        t, _ = await a.call("take_snapshot", {})
        return t
    t, _ = await a.call("browser_snapshot", {})
    return t


def _parse_rows(text: str) -> list[tuple[str, int, str, str]]:
    flat = re.sub(r"\s+", " ", text)
    return [(m.group(1), int(m.group(2)), m.group(3), m.group(4)) for m in ROW_RE.finditer(flat)]


async def _click_text(a: A, text: str) -> tuple[str, bool]:
    """按可访问名点击 (分页按钮/商品链接): nexus/pw 走快照 ref, cdt 走 uid。"""
    if a.kind == "cdt":
        snap, _ = await a.call("take_snapshot", {})
        m = re.search(r'uid=(\d+_\d+)\s+\w+\s+"[^"]*' + re.escape(text), snap)
        return (await a.call("click", {"uid": m.group(1)})) if m else ("uid 未定位", False)
    ref = await a.snap_ref(re.escape(text))
    return (await a.click_ref(ref)) if ref else ("ref 未定位", False)


# ── 订单族 (fixture_scale/orders.html, 60 行 10/页) ─────────────

async def h_filter_status(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    exp = sum(1 for x in rows if x["st"] == p["st"])
    await a.select_native("#st", [p["st"]])
    await asyncio.sleep(0.4)
    text = re.sub(r"\s+", " ", await _page_text(a))
    got = _parse_rows(text)
    r.check("可见行数=min(10,真值)", len(got) == min(10, exp), f"parsed={len(got)} truth={exp}")
    m = re.findall(r"第(\d+)页", text)
    pages = max((int(x) for x in m), default=0)
    import math
    r.check("分页数=ceil(真值/10)", pages == math.ceil(exp / 10), f"pages={pages} exp={math.ceil(exp / 10)}")


async def h_filter_keyword(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    name = rows[p["idx"] % len(rows)]["name"]
    exp = sum(1 for x in rows if name in x["name"])
    await a.type_into("#q", name)
    await asyncio.sleep(0.4)
    got = _parse_rows(await _page_text(a))
    r.check("过滤行数=真值", len(got) == min(10, exp) and all(name in g[2] for g in got),
            f"parsed={len(got)} truth={exp}")


async def h_filter_combo(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    pick = rows[p["idx"] % len(rows)]
    exp = sum(1 for x in rows if pick["name"] in x["name"] and x["st"] == pick["st"])
    await a.type_into("#q", pick["name"])
    await a.select_native("#st", [pick["st"]])
    await asyncio.sleep(0.4)
    got = _parse_rows(await _page_text(a))
    ok = (len(got) == min(10, exp)
          and all(pick["name"] in g[2] and g[3] == pick["st"] for g in got))
    r.check("组合过滤行数+内容=真值", ok, f"parsed={len(got)} truth={exp}")


async def h_sort(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    th = "#thAmt" if p["key"] == "amt" else "#thName"
    clicks = 1 if p["dir"] == "desc" else 2
    for _ in range(clicks):
        await a.click_sel(th)
        await asyncio.sleep(0.3)
    flag, _ = await a.eval(f"document.querySelector('{th}').dataset.sorted || ''")
    r.check("元素命中: 表头旗标", p["dir"] in _s(flag), _s(flag)[:30])
    col = 1 if p["key"] == "amt" else 2
    first, _ = await a.eval(f"document.querySelector('#t tbody tr').children[{col}].textContent")
    zh = "'zh'"
    if p["key"] == "amt":
        cmp = "(y.amt - x.amt)" if p["dir"] == "desc" else "(x.amt - y.amt)"
        ret = "amt"
    else:
        cmp = f"(y.name.localeCompare(x.name, {zh}))" if p["dir"] == "desc" \
            else f"(x.name.localeCompare(y.name, {zh}))"
        ret = "name"
    exp, _ = await a.eval(
        "(() => { const v = [...window.__truth]; v.sort((x, y) => " + cmp + ");"
        " return String(v[0]." + ret + "); })()")
    r.check("首行=真值极值", _s(first) == _s(exp), f"first={_s(first)[:20]} exp={_s(exp)[:20]}")


async def h_paginate(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    n = p["page"]
    out, ok = await _click_text(a, f"第{n}页")
    if not ok:
        r.check("页码按钮可点", False, out[:60])
        return
    await asyncio.sleep(0.4)
    dis, _ = await a.eval(f"[...document.querySelectorAll('#pager button')]"
                          f".find(b => b.textContent === '第{n}页').disabled")
    r.check("元素命中: 当前页按钮禁用", "true" in _s(dis).lower(), _s(dis)[:20])
    first, _ = await a.eval("document.querySelector('#t tbody tr td').textContent")
    exp = rows[(n - 1) * 10]["id"]
    r.check("首行 id=真值", _s(first) == exp, f"first={_s(first)[:12]} exp={exp}")


async def h_aggregate(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    cnt = Counter(x["name"] for x in rows)
    name = next(n for n, c in cnt.items() if 2 <= c <= 8)
    exp = sum(x["amt"] for x in rows if x["name"] == name)
    await a.type_into("#q", name)
    await asyncio.sleep(0.4)
    got = _parse_rows(await _page_text(a))
    r.check("页内金额合计=真值 (从工具输出逐行读)",
            len(got) == cnt[name] and sum(g[1] for g in got) == exp,
            f"parsed={len(got)} rows sum={sum(g[1] for g in got)} truth={cnt[name]}/{exp}")


async def h_lookup(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/orders.html?seed={p['seed']}")
    rows = await _truth(a)
    cnt = Counter(x["name"] for x in rows)
    pick = next(x for i in range(len(rows))
                if cnt[(x := rows[(p["idx"] + i) % len(rows)])["name"]] <= 8)
    await a.type_into("#q", pick["name"])
    await asyncio.sleep(0.4)
    got = _parse_rows(await _page_text(a))
    hit = [g for g in got if g[0] == pick["id"]]
    r.check("按 id 找到行且金额=真值 (工具输出)", len(hit) == 1 and hit[0][1] == pick["amt"],
            f"hit={len(hit)} amt={hit[0][1] if hit else '-'} truth={pick['amt']}")


# ── 商城族 (fixture_scale/shop.html, 12 商品) ────────────────────

async def h_order(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/shop.html?seed={p['seed']}")
    prods = await _truth(a)
    if p["target"] == "cheapest":
        prod = min(prods, key=lambda x: x["p"])
        want = "cheap"
    elif p["target"] == "priciest":
        prod = max(prods, key=lambda x: x["p"])
        want = "pricey"
    else:
        prod = prods[p["idx"] % len(prods)]
        want = "mid" if prod not in (min(prods, key=lambda x: x["p"]),
                                     max(prods, key=lambda x: x["p"])) else "?"
    out, ok = await _click_text(a, f"{prod['n']} ¥{prod['p']}")
    if not ok:
        r.check("商品链接可点", False, out[:60])
        return
    await asyncio.sleep(0.4)
    for sel, val in (("#qty", str(p["qty"])), ("#rcpt", "测试员"), ("#addr", "北京")):
        await a.type_into(sel, val)
    await a.click_sel("#buy")
    await asyncio.sleep(0.5)
    vis, _ = await a.eval("document.getElementById('confirm').style.display === 'block'")
    r.check("订单确认出现", "true" in _s(vis).lower(), _s(vis)[:20])
    dok, _ = await a.eval("document.getElementById('confirm').dataset.ok || ''")
    r.check("元素命中: 点中目标商品", _s(dok) == want, f"got={_s(dok)} want={want}")
    txt, _ = await a.eval("document.getElementById('confirm').textContent")
    r.check("订单号格式 NXP+8位", bool(re.search(r"NXP\d{8}", _s(txt))), _s(txt)[:40])
    r.check("数量回显", f"x{p['qty']}" in _s(txt), _s(txt)[:40])


async def h_price(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/shop.html?seed={p['seed']}")
    prods = await _truth(a)
    text = await _page_text(a)
    pairs = re.findall(r"([一-龥A-Za-z0-9]{2,12})\s*¥(\d+)", re.sub(r"\s+", " ", text))
    got = {n: int(v) for n, v in pairs if n in {x["n"] for x in prods}}
    exp_max = max(x["p"] for x in prods)
    r.check("读全 12 商品 (工具输出)", len(got) == len(prods), f"parsed={len(got)}")
    r.check("最高价=真值", max(got.values(), default=0) == exp_max, f"got={max(got.values(), default=0)} truth={exp_max}")


# ── 知识库/仪表盘族 ─────────────────────────────────────────────

async def h_kb(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/kb.html?seed={p['seed']}")
    arts = await _truth(a)
    art = arts[p["idx"] % len(arts)]
    text = re.sub(r"\s+", " ", await _page_text(a))
    r.check("负责人可读出", art["owner"] in text, f"truth={art['owner']}")
    field = art["date"] if p["field"] == "date" else str(art["views"])
    r.check(f"{p['field']}可读出", field in text, f"truth={field}")


async def h_dash(a: A, r: Rec, base: str, p: dict) -> None:
    await a.nav(f"{base}/dash.html?seed={p['seed']}")
    mets = await _truth(a)
    met = mets[p["idx"] % len(mets)]
    text = re.sub(r"\s+", " ", await _page_text(a))
    m = re.search(re.escape(met["m"]) + r".{0,40}?" + str(met["v"]), text)
    r.check("指标值=真值 (标签邻近)", bool(m), f"truth={met['m']}={met['v']}")
    r.check("环比可读出", f"{met['d']}%" in text, f"truth={met['d']}%")


# ── 差异化族 (能力缺口显影) ──────────────────────────────────────

async def h_richtext(a: A, r: Rec, adv: str, p: dict) -> None:
    """iframe 内 contenteditable 写入 + 读回 (pw 历史静默失败点)。"""
    await a.nav(f"{adv}/richtext.html")
    snap, _ = await a.call("browser_snapshot", {"diff": False} if a.kind == "nexus" else {})
    if a.kind == "cdt":
        m = re.search(r'uid=(\d+_\d+)[^\n]*在此输入', snap) or re.search(r'在此输入[^\n]*uid=(\d+_\d+)', snap)
        if not m:
            r.check("iframe 编辑器可定位", False, "快照无 iframe 内容")
            return
        await a.call("type_text", {"uid": m.group(1), "text": p["text"]})
    else:
        m = re.search(r"paragraph[^\n]*在此输入[^\n]*?ref=((?:f\d+)?e\d+)", snap) or \
            re.search(r"paragraph[^\n]*?ref=(f\d+e\d+)", snap)
        if not m:
            r.check("iframe 编辑器可定位", False, "快照无 paragraph ref")
            return
        args = {"ref": m.group(1), "text": p["text"]} if a.kind == "nexus" else \
            {"target": m.group(1), "element": "编辑器", "text": p["text"]}
        await a.call("browser_type", args)
    v, _ = await a.eval("(() => { const f = document.querySelector('iframe');"
                        " return f.contentDocument.body.textContent; })()")
    r.check("写入读回一致", p["text"] in _s(v), _s(v)[:40])


async def h_download(a: A, r: Rec, daily: str, p: dict) -> None:
    """下载可观测性: 工具输出必须报告文件名 (pw/cdt 无此能力 = 记缺口)。"""
    await a.nav(f"{daily}/download.html")
    out, ok = await _click_text(a, "lodash-4.17.21.tgz")
    if not ok:
        r.check("下载链接可点", False, out[:60])
        return
    await asyncio.sleep(2)
    # 只查快照段之前的响应头 (页面里有"下载测试" h1, 整串匹配会误判)
    head = out.split("### Snapshot")[0].split("### Page")[0]
    if a.kind == "nexus":
        if "开始下载" in head or "下载" in head:
            r.check("点击即回报下载 (文件名+路径)", True, out[:60])
            return
        errs = ""
        for _ in range(5):                       # CDN 抖动: 轮询事件流兜底
            errs, _ = await a.call("browser_errors", {"since": 0})
            if "下载" in errs:
                break
            await asyncio.sleep(2)
        r.check("下载事件入流", "下载" in errs, errs[:60])
    else:
        r.check("工具输出报告下载文件", "download" in head.lower() or "下载" in head,
                "无下载观测 = 能力缺口")


async def h_drag(a: A, r: Rec, adv: str, p: dict) -> None:
    await a.nav(f"{adv}/drag.html")
    if a.kind == "nexus":
        await a.call("browser_drag", {"from_selector": "#src", "to_selector": "#zone"})
    elif a.kind == "pw":
        await a.call("browser_drag", {"startTarget": "#src", "startElement": "拖我",
                                      "endTarget": "#zone", "endElement": "放到这"})
    else:
        snap, _ = await a.call("take_snapshot", {})
        m1 = re.search(r'uid=(\d+_\d+)[^\n]*拖我', snap)
        m2 = re.search(r'uid=(\d+_\d+)[^\n]*放到这', snap)
        if not (m1 and m2):
            r.check("拖拽端点可定位", False, "uid 未找到")
            return
        await a.call("drag", {"from_uid": m1.group(1), "to_uid": m2.group(1)})
    ok = await a.verify()
    r.check("HTML5 拖拽落区", ok, "")


async def h_dialog(a: A, r: Rec, adv: str, p: dict) -> None:
    await a.nav(f"{adv}/dialog.html")
    await a.click_sel("#del")
    await asyncio.sleep(0.4)
    if a.kind == "nexus":
        await a.call("browser_dialog_respond", {"accept": True, "confirmed": True})
    elif a.kind == "pw":
        await a.call("browser_handle_dialog", {"accept": True})
    else:
        await a.call("handle_dialog", {"action": "accept"})
    r.check("confirm 处置 → done", await a.verify(), "")


async def h_hover(a: A, r: Rec, adv: str, p: dict) -> None:
    await a.nav(f"{adv}/hover.html")
    if a.kind == "nexus":
        await a.call("browser_hover", {"selector": "#menu"})
    elif a.kind == "pw":
        await a.call("browser_hover", {"target": "#menu", "element": "产品菜单"})
    else:
        uid = await a._cdt_uid_for("#menu", allow_static=True)
        if not uid:
            r.check("hover 目标可定位", False, "uid 未找到")
            return
        await a.call("hover", {"uid": uid})
    await a.click_sel("#prolink")
    r.check("hover 展开 → 点击直达", await a.verify(), "")


async def h_rightclick(a: A, r: Rec, daily: str, p: dict) -> None:
    """右键触发 alert: 仅 nexus 有 button=right 原生 (差异化族)。"""
    await a.nav(f"{daily}/contextmenu.html")
    await a.eval("(window.__al='', window.alert=(m => { window.__al = m; }), 'ok')")
    if a.kind == "nexus":
        await a.call("browser_click", {"selector": "#hot-spot", "button": "right"})
    elif a.kind == "pw":
        await a.call("browser_click", {"target": "#hot-spot", "element": "热区", "button": "right"})
    else:
        uid = await a._cdt_uid_for("#hot-spot", allow_static=True)
        if uid:
            await a.call("click", {"uid": uid, "button": "right"})
    await asyncio.sleep(0.5)
    if a.kind == "pw":
        await a.call("browser_handle_dialog", {"accept": False})   # 解除 modal 封锁再读回
    v, _ = await a.eval("window.__al || ''")
    r.check("右键触发 contextmenu→alert", "context menu" in _s(v), _s(v)[:40])


async def h_keypress(a: A, r: Rec, daily: str, p: dict) -> None:
    await a.nav(f"{daily}/key_presses.html")
    if a.kind == "cdt":
        await a.call("press_key", {"key": p["key"]})
    else:
        await a.call("browser_press_key", {"key": p["key"]})
    await asyncio.sleep(0.4)
    v, _ = await a.eval("document.getElementById('result').textContent")
    r.check("按键入结果区", p["key"].upper() in _s(v), _s(v)[:40])


# ── 用例生成 (106 案例) ─────────────────────────────────────────

def gen_cases() -> list[tuple[str, str, object, dict]]:
    """→ (family, label, handler, params)"""
    C: list[tuple[str, str, object, dict]] = []
    for st, seeds in (("待处理", (11, 12, 13)), ("已发货", (11, 12, 13)),
                      ("已完成", (11, 12)), ("已取消", (11, 12))):
        for s in seeds:
            C.append(("过滤状态", f"过滤状态·{st}#s{s}", h_filter_status, {"st": st, "seed": s}))
    for i, s in enumerate((21, 22, 23, 24, 25, 26, 27, 28)):
        C.append(("过滤关键词", f"过滤关键词#{i}", h_filter_keyword, {"seed": s, "idx": i * 7}))
    for i, s in enumerate((31, 32, 33, 34, 35, 36)):
        C.append(("组合过滤", f"组合过滤#{i}", h_filter_combo, {"seed": s, "idx": i * 11}))
    for i, (key, d) in enumerate((("amt", "desc"), ("amt", "asc"), ("name", "desc"),
                                  ("name", "asc"), ("amt", "desc"), ("name", "asc"))):
        C.append(("排序", f"排序·{key}/{d}#{i}", h_sort, {"seed": 41 + i, "key": key, "dir": d}))
    for i, n in enumerate((2, 3, 4, 5, 6, 2, 3, 4, 5, 6)):
        C.append(("分页", f"分页·第{n}页#{i}", h_paginate, {"seed": 51 + i, "page": n}))
    for i, s in enumerate((61, 62, 63, 64, 65, 66)):
        C.append(("页内聚合", f"页内聚合#{i}", h_aggregate, {"seed": s}))
    for i, s in enumerate(range(71, 81)):
        C.append(("按id查找", f"按id查找#{i}", h_lookup, {"seed": s, "idx": i * 5}))
    for i in range(8):
        tgt = ("cheapest", "priciest", "idx")[i % 3]
        C.append(("下单流", f"下单流·{tgt}#{i}", h_order,
                  {"seed": 81 + i, "target": tgt, "idx": i, "qty": 1 + i % 3}))
    for i, s in enumerate((91, 92, 93, 94, 95, 96)):
        C.append(("商品价格", f"商品价格#{i}", h_price, {"seed": s}))
    for i in range(12):
        C.append(("知识库", f"知识库·KB-{101 + i % 10}#{i}", h_kb,
                  {"seed": 101 + i, "idx": i, "field": "views" if i >= 10 else "date"}))
    for i in range(10):
        C.append(("仪表盘", f"仪表盘·指标{i}", h_dash, {"seed": 111 + i, "idx": i}))
    for i in range(2):
        C.append(("富文本iframe", f"富文本iframe#{i}", h_richtext, {"text": f"规模化写入测试{i}"}))
        C.append(("下载观测", f"下载观测#{i}", h_download, {}))
        C.append(("拖拽", f"HTML5拖拽#{i}", h_drag, {}))
        C.append(("对话框", f"confirm对话框#{i}", h_dialog, {}))
        C.append(("悬停", f"hover菜单#{i}", h_hover, {}))
        C.append(("右键", f"右键菜单#{i}", h_rightclick, {}))
        C.append(("键盘", f"键盘按键#{i}", h_keypress, {"key": ("A", "B")[i]}))
    return C


CASES_ALL = gen_cases()
_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
_sides = "nexus,pw,cdt"
for x in sys.argv[1:]:
    if x.startswith("--sides="):
        _sides = x.split("=", 1)[1]
SEL = [c for c in CASES_ALL if not _filters or any(f in c[1] for f in _filters)]


async def run_side(kind: str, params: StdioServerParameters, base: str, adv: str, daily: str) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            results = []
            mark = 0
            for fam, name, fn, p in SEL:
                print(f"[{kind}] ▶ {name}", file=sys.stderr, flush=True)
                r = Rec(name)
                t0 = time.perf_counter()
                b = adv if fn in (h_richtext, h_drag, h_dialog, h_hover) else \
                    daily if fn in (h_download, h_rightclick, h_keypress) else base
                try:
                    await fn(a, r, b, p)
                except Exception as e:
                    r.check("案例异常中断", False, f"{type(e).__name__}: {e}")
                calls = a.calls[mark:]
                mark = len(a.calls)
                results.append({**r.done(), "family": fam,
                                "ms": int((time.perf_counter() - t0) * 1000),
                                "tokens": sum(c["tokens"] for c in calls),
                                "calls_n": len(calls)})
                print(f"[{kind}] {name}: {results[-1]['passed']}/{results[-1]['total']}",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(0.3)
            return {"kind": kind, "cases": results, "calls": a.calls}


async def main() -> None:
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    base, srv = _serve(FIXS)
    adv, srv2 = _serve(FIXA)
    daily, srv3 = _serve(FIXD)
    sides: list[dict] = []
    try:
        if "nexus" in _sides:
            sides.append(await run_side("nexus", StdioServerParameters(
                command=py, args=["-m", "nexus_browser.server"], env=env), base, adv, daily))
        if "pw" in _sides:
            sides.append(await run_side("pw", StdioServerParameters(
                command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base, adv, daily))
        if "cdt" in _sides:
            sides.append(await run_side("cdt", StdioServerParameters(
                command="npx", args=["-y", "chrome-devtools-mcp@latest", "--headless", "--isolated"]),
                base, adv, daily))
    finally:
        srv.shutdown()
        srv2.shutdown()
        srv3.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scale-ops.json").write_text(
        json.dumps({s["kind"]: s for s in sides}, ensure_ascii=False, indent=2), encoding="utf-8")

    fams = []
    for fam, *_ in SEL:
        if fam not in fams:
            fams.append(fam)
    hdr = f"{'family':12s} │"
    for sd in sides:
        hdr += f" {sd['kind']:>4s} 子任务 {'tok':>6s} │"
    print(f"\n{hdr}")
    for fam in fams:
        row = f"{fam:12s} │"
        for sd in sides:
            cs = [c for c in sd["cases"] if c["family"] == fam]
            p_, t_ = sum(c["passed"] for c in cs), sum(c["total"] for c in cs)
            row += f" {p_:>4d}/{t_:<3d} {sum(c['tokens'] for c in cs):6d} │"
        print(row)
    row = f"{'TOTAL':12s} │"
    for sd in sides:
        p_ = sum(c["passed"] for c in sd["cases"])
        t_ = sum(c["total"] for c in sd["cases"])
        row += f" {p_:>4d}/{t_:<3d} {sum(c['tokens'] for c in sd['cases']):6d} │"
    print("-" * len(hdr))
    print(row)
    print(f"案例数/侧: {len(SEL)}")


if __name__ == "__main__":
    asyncio.run(main())
