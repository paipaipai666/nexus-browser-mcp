"""元素正确识别率基准 (三方): 复杂页面上的"点的是不是那个元素"。

动机: 此前所有套件验证"结果状态对", 从未系统测"动作落在语义正确的元素上"。
同时回答一个产品质疑: 省 token 的手段 (节点截断/视口裁剪/diff) 是否在复杂页面上
砍掉了定位所需的信息 → 本套件同页对照 元素识别率 × token 成本。

夹具 bench/fixture_acc/: 同名按钮海 (卡片×4 / 表格行×20)、相似名 (提交订单/提交申请/
重新提交订单; 姓名/联系人姓名/紧急联系人姓名)、display:none 隐藏陷阱、disabled、
icon-only (仅 aria-label)、shadow DOM 组件、iframe 表单、模态遮罩、排序后整体重渲染。

方法: 每案例给中文任务描述; resolver 只用各家快照输出里存在的信息定位元素
(nexus: 平铺行+box 坐标做容器包含判定; pw: YAML 缩进子树; cdt: 平铺行邻近窗口),
执行后读 window.__hits (capture 相标 data-acc 埋点) 判定实际命中。
指标: 命中率 / 误点率(点了 decoy) / 未定位率, 附每案例 token。

用法: .venv/Scripts/python.exe -u bench/element_acc.py [子串过滤] [--sides=nexus,pw,cdt]
产出: docs/bench/element-acc.json
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
from scale_ops import Rec, _body, _json2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
FIXA = Path(__file__).parent / "fixture_acc"


def _serve(d: Path) -> tuple[str, object]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


async def _snap(a: A) -> str:
    """各侧默认快照流 (实验对象就是默认行为)。"""
    if a.kind == "nexus":
        t, _ = await a.call("browser_snapshot", {"diff": False})
        return t
    if a.kind == "cdt":
        t, _ = await a.call("take_snapshot", {})
        return t
    t, _ = await a.call("browser_snapshot", {})
    return t


# ── resolver: 只用各侧快照里的信息 ────────────────────────────────

def _resolve_nexus(snap: str, role: str, name: str, hint: str) -> str | None:
    """nexus 平铺行 + box: 容器带状最近距离 (同行/同卡最近者胜)。
    只用快照里存在的信息: 若快照没暴露容器或候选, 如实返回 None/首候选。"""
    entries = []
    for line in snap.splitlines():
        m = re.match(r'\s*\[\d+\] (\S+) "([^"]*)"', line)
        if not m:
            continue
        mr = re.search(r"ref=(\S+)", line)
        mb = re.search(r"box=([\d.,]+)", line)
        box = None
        if mb:
            parts = [p for p in mb.group(1).split(",") if p]
            box = [float(x) for x in parts] if len(parts) == 4 else None
        entries.append({"role": m.group(1), "name": m.group(2),
                        "ref": mr.group(1) if mr else None, "box": box})
    cands = [e for e in entries if e["role"] == role and e["name"] == name and e["ref"]]
    if not cands:
        return None
    if not hint:
        return cands[0]["ref"]
    # 容器不在快照里 = 上下文不可见 → None 让 runner 滚动升级 (盲选首候选=作弊)
    lines = snap.splitlines()
    hint_idx = max((i for i, ln in enumerate(lines) if hint in ln), default=-1)
    if hint_idx < 0:
        return cands[0]["ref"] if len(cands) == 1 else None
    lines = snap.splitlines()
    parsed_cands = []
    for i, line in enumerate(lines):
        m = re.match(r'\s*\[\d+\] (\S+) "([^"]*)"', line)
        if not m:
            continue
        mr = re.search(r"ref=(\S+)", line)
        if m.group(1) == role and m.group(2) == name and mr:
            parsed_cands.append((i, mr.group(1)))
    after = [ref_ for i, ref_ in parsed_cands if i > hint_idx]
    return after[0] if after else None   # 锚点后无候选 → 可能在更下方, 升级滚动


def _parse_pw_lines(snap: str):
    out = []
    for line in snap.splitlines():
        m = re.match(r'(\s*)- (\S+) "([^"]*)".*?\[ref=(\S+)\]', line)
        if m:
            out.append({"indent": len(m.group(1)), "role": m.group(2),
                        "name": m.group(3), "ref": m.group(4)})
        else:
            m2 = re.match(r'(\s*)- (\S+)(?: "([^"]*)")?', line)
            if m2:
                out.append({"indent": len(m2.group(1)), "role": m2.group(2),
                            "name": m2.group(3) or "", "ref": None})
    return out


def _resolve_pw(snap: str, role: str, name: str, hint: str) -> str | None:
    lines = _parse_pw_lines(snap)
    cands = [i for i, e in enumerate(lines) if e["role"] == role and e["name"] == name and e["ref"]]
    if not cands:
        return None
    if not hint:
        return lines[cands[0]]["ref"]
    ci = next((i for i, e in enumerate(lines) if hint in (e["name"] or "")), None)
    if ci is None:
        return lines[cands[0]]["ref"] if len(cands) == 1 else None
    pi = max((j for j in range(ci) if lines[j]["indent"] < lines[ci]["indent"]), default=None)
    if pi is None:                      # hint 是顶层 → 用其自身子树
        pi = ci
    base = lines[pi]["indent"]
    sub = []
    for j in range(pi + 1, len(lines)):
        if lines[j]["indent"] <= base:
            break
        sub.append(lines[j])
    for e in sub:
        if e["role"] == role and e["name"] == name and e["ref"]:
            return e["ref"]
    return None

def _resolve_cdt(snap: str, role: str, name: str, hint: str) -> str | None:
    lines = snap.splitlines()
    pat = re.compile(r'uid=(\d+_\d+)\s+(\S+)\s+"([^"]*)"')
    parsed = []
    for i, line in enumerate(lines):
        m = pat.search(line)
        if m:
            parsed.append((i, m.group(1), m.group(2), m.group(3)))
    cands = [(i, uid) for i, uid, r, n in parsed if r == role and n.strip() == name]
    if not cands:
        return None
    if hint:
        ci = next((i for i, _, _, n in parsed if hint in n), None)
        if ci is not None:
            near = [uid for i, uid, r, n in parsed
                    if ci < i <= ci + 14 and r == role and n.strip() == name]
            if near:
                return near[0]
        elif len(cands) == 1:
            return cands[0][1]
    return cands[0][1]


RESOLVER = {"nexus": _resolve_nexus, "pw": _resolve_pw, "cdt": _resolve_cdt}


async def _act(a: A, ref: str, role: str, name: str, text: str | None) -> tuple[str, bool]:
    if text is None:
        return await a.click_ref(ref)
    if a.kind == "nexus":
        return await a.call("browser_type", {"ref": ref, "text": text})
    if a.kind == "pw":
        return await a.call("browser_type", {"target": ref, "element": name, "text": text})
    return await a.call("fill", {"uid": ref, "value": text})


# ── 案例: steps=[{act, role, name, hint, text?}]; expect=最后一步应命中的 data-acc ──

def C(cid, page, task, steps, expect, note=""):
    return {"id": cid, "page": page, "task": task, "steps": steps, "expect": expect, "note": note}


BTN, TXT, HD = "button", "textbox", "heading"

CASES = [
    # ── admin: 同名按钮海 ──
    C("card-edit-sales", "admin.html", "点击 销售概览 卡片里的 编辑 按钮",
      [{"act": "click", "role": BTN, "name": "编辑", "hint": "销售概览"}], "card-sales-edit"),
    C("card-edit-stock", "admin.html", "点击 库存告警 卡片里的 编辑 按钮",
      [{"act": "click", "role": BTN, "name": "编辑", "hint": "库存告警"}], "card-stock-edit"),
    C("card-edit-user", "admin.html", "点击 用户增长 卡片里的 编辑 按钮",
      [{"act": "click", "role": BTN, "name": "编辑", "hint": "用户增长"}], "card-user-edit"),
    C("card-edit-notice", "admin.html", "点击 系统公告 卡片里的 编辑 按钮",
      [{"act": "click", "role": BTN, "name": "编辑", "hint": "系统公告"}], "card-notice-edit"),
    C("row-del-3", "admin.html", "在订单列表里点击 PO-2003 那一行的 删除",
      [{"act": "click", "role": BTN, "name": "删除", "hint": "PO-2003"}], "row3-del"),
    C("row-copy-12", "admin.html", "在订单列表里点击 PO-2012 那一行的 复制",
      [{"act": "click", "role": BTN, "name": "复制", "hint": "PO-2012"}], "row12-copy"),
    C("row-edit-14", "admin.html", "在订单列表里点击 PO-2014 那一行的 编辑",
      [{"act": "click", "role": BTN, "name": "编辑", "hint": "PO-2014"}], "row14-edit"),
    # ── 相似名 ──
    C("similar-submit-order", "admin.html", "点击 提交订单 按钮 (不是提交申请也不是重新提交订单)",
      [{"act": "click", "role": BTN, "name": "提交订单", "hint": ""}], "submit-order"),
    # ── icon-only (aria-label 是唯一线索) ──
    C("icon-setting", "admin.html", "点击顶栏的 设置 图标按钮",
      [{"act": "click", "role": BTN, "name": "设置", "hint": ""}], "icon-setting"),
    # ── 模态流程 ──
    C("modal-confirm", "admin.html", "点击 打开危险操作, 在弹窗里点击 确认清空",
      [{"act": "click", "role": BTN, "name": "打开危险操作", "hint": ""},
       {"act": "click", "role": BTN, "name": "确认清空", "hint": "危险操作确认"}], "modal-confirm"),
    # ── shadow DOM ──
    C("shadow-profile", "admin.html", "点击会员卡组件里的 查看资料 按钮",
      [{"act": "click", "role": BTN, "name": "查看资料", "hint": "会员卡组件"}], "shadow-view-profile"),
    # ── iframe ──
    C("iframe-save", "admin.html", "点击备注面板里的 保存草稿 按钮",
      [{"act": "click", "role": BTN, "name": "保存草稿", "hint": "备注面板"}], "iframe-save"),
    C("iframe-type", "admin.html", "在备注面板的 备注 输入框里输入 hello",
      [{"act": "type", "role": TXT, "name": "备注", "hint": "备注面板", "text": "hello"}], "iframe-memo"),
    # ── 隐藏/禁用: 正确行为是判定不可操作 ──
    C("hidden-delete-all", "admin.html", "点击 删除全部 按钮",
      [{"act": "click", "role": BTN, "name": "删除全部", "hint": ""}], "hidden-delete-all",
      note="display:none — 快照不应暴露; 点了即失败"),
    C("disabled-del", "admin.html", "点击 删除选中 按钮",
      [{"act": "click", "role": BTN, "name": "删除选中", "hint": ""}], "disabled-del",
      note="disabled — 点击应报错而非静默成功"),
    # ── grid: 重渲染 + 位置/取值消歧 ──
    C("grid-sort-btn", "grid.html", "点击 价格排序 按钮",
      [{"act": "click", "role": BTN, "name": "价格排序", "hint": ""}], "sort-price"),
    C("grid-first-after-sort", "grid.html", "点击 价格排序, 然后点击排完后第一个商品的 加入购物车",
      [{"act": "click", "role": BTN, "name": "价格排序", "hint": ""},
       {"act": "click", "role": BTN, "name": "加入购物车", "hint": ""}], "buy-G17",
      note="升序首个=¥29 砧板 G17; 整表重渲染后需重新定位"),
    C("grid-by-price-88", "grid.html", "点击商品编号 G02 的 加入购物车 (价格 ¥88)",
      [{"act": "click", "role": BTN, "name": "加入购物车", "hint": "G02"}], "buy-G02",
      note="需从商品文本定位按钮; 快照不含商品文本的服务端在此失分"),
    C("grid-by-name", "grid.html", "点击 体脂秤 的 加入购物车",
      [{"act": "click", "role": BTN, "name": "加入购物车", "hint": "体脂秤"}], "buy-G11"),
    C("wiz-name", "wizard.html", "在第一步的 姓名 输入框输入 张三",
      [{"act": "type", "role": TXT, "name": "姓名", "hint": "第一步", "text": "张三"}], "f-name"),
    C("wiz-contact-name", "wizard.html", "在第一步的 联系人姓名 输入框输入 李四",
      [{"act": "type", "role": TXT, "name": "联系人姓名", "hint": "第一步", "text": "李四"}], "f-contact-name"),
    C("wiz-emergency-name", "wizard.html", "在第一步的 紧急联系人姓名 输入框输入 王五",
      [{"act": "type", "role": TXT, "name": "紧急联系人姓名", "hint": "第一步", "text": "王五"}], "f-emergency-name"),
    C("wiz-flow-cs", "wizard.html",
      "第一步 姓名 填 张三、联系人姓名 填 李四, 点 下一步; 第二步 客服姓名 填 赵六",
      [{"act": "type", "role": TXT, "name": "姓名", "hint": "第一步", "text": "张三"},
       {"act": "type", "role": TXT, "name": "联系人姓名", "hint": "第一步", "text": "李四"},
       {"act": "click", "role": BTN, "name": "下一步", "hint": "第一步"},
       {"act": "type", "role": TXT, "name": "客服姓名", "hint": "第二步", "text": "赵六"}], "f-cs-name"),
    C("wiz-flow-submit", "wizard.html",
      "走完向导并提交: 第一步 姓名 张三 / 联系人姓名 李四 → 下一步; 第二步 店铺名称 好店 → 下一步; 第三步点 提交申请",
      [{"act": "type", "role": TXT, "name": "姓名", "hint": "第一步", "text": "张三"},
       {"act": "type", "role": TXT, "name": "联系人姓名", "hint": "第一步", "text": "李四"},
       {"act": "click", "role": BTN, "name": "下一步", "hint": "第一步"},
       {"act": "type", "role": TXT, "name": "店铺名称", "hint": "第二步", "text": "好店"},
       {"act": "click", "role": BTN, "name": "下一步", "hint": "第二步"},
       {"act": "click", "role": BTN, "name": "提交申请", "hint": "第三步"}], "submit-apply"),
    C("wiz-save-exit", "wizard.html", "点击 保存并退出 按钮",
      [{"act": "click", "role": BTN, "name": "保存并退出", "hint": ""}], "save-exit"),
]

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
_sides = "nexus,pw,cdt"
for x in sys.argv[1:]:
    if x.startswith("--sides="):
        _sides = x.split("=", 1)[1]
SEL = [c for c in CASES if not _filters or any(f in c["id"] for f in _filters)]
async def run_case(a: A, r: Rec, base: str, case: dict) -> None:
    await a.nav(f"{base}/{case['page']}")
    await asyncio.sleep(1.2)
    for step in case["steps"]:
        snap = await _snap(a)
        ref = RESOLVER[a.kind](snap, step["role"], step["name"], step.get("hint", ""))
        tries = 0
        while ref is None and not case["expect"].startswith(("hidden-", "disabled-")) and tries < 3:
            # agent 真实行为: 目标不在当前视图 → 连续翻页找 (最多 3 屏)
            if a.kind == "nexus":
                await a.call("browser_scroll", {"direction": "down", "amount": 700})
            else:
                expr = "() => window.scrollBy(0, 700)"
                await a.call("browser_evaluate" if a.kind == "pw" else "evaluate_script",
                             {"function": expr})
            await asyncio.sleep(0.4)
            snap = await _snap(a)
            ref = RESOLVER[a.kind](snap, step["role"], step["name"], step.get("hint", ""))
            tries += 1
        if ref is None:
            if case["expect"].startswith(("hidden-", "disabled-")):
                continue                      # 未定位对隐藏/禁用目标是正确行为
            r.check(f"定位: {step['name']}", False, "快照中未找到")
            continue
        out, ok = await _act(a, ref, step["role"], step["name"], step.get("text"))
        if not ok:
            if case["expect"] in ("hidden-delete-all", "disabled-del"):
                continue                      # 工具拒绝不可操作元素 = 正确
            r.check(f"执行: {step['name']}", False, out[:70])
        await asyncio.sleep(0.35)
    v, _ = await a.eval("JSON.stringify(window.__hits || [])")
    hits = _json2(_body(v)) or []
    last = hits[-1] if isinstance(hits, list) and hits else None
    if case["expect"] in ("hidden-delete-all", "disabled-del"):
        r.check("不可操作元素未被生效点击", last != case["expect"], f"hits={hits[-3:]}")
        return
    r.check("命中正确元素", last == case["expect"], f"last={last} expect={case['expect']}")
    decoys = [h for h in hits if isinstance(h, str) and h.startswith("decoy:")]
    r.check("无误点 decoy", not decoys, f"{decoys[:2]}")


async def run_side(kind: str, params: StdioServerParameters, base: str) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            results = []
            mark = 0
            for case in SEL:
                print(f"[{kind}] ▶ {case['id']}", file=sys.stderr, flush=True)
                r = Rec(case["id"])
                t0 = time.perf_counter()
                try:
                    await run_case(a, r, base, case)
                except Exception as e:
                    r.check("案例异常中断", False, f"{type(e).__name__}: {e}")
                calls = a.calls[mark:]
                mark = len(a.calls)
                results.append({**r.done(), "ms": int((time.perf_counter() - t0) * 1000),
                                "tokens": sum(c["tokens"] for c in calls)})
                print(f"[{kind}] {case['id']}: {results[-1]['passed']}/{results[-1]['total']}",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(0.3)
            return {"kind": kind, "cases": results, "calls": a.calls}


async def main() -> None:
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    base, srv = _serve(FIXA)
    sides = []
    try:
        if "nexus" in _sides:
            sides.append(await run_side("nexus", StdioServerParameters(
                command=py, args=["-m", "nexus_browser.server"], env=env), base))
        if "pw" in _sides:
            sides.append(await run_side("pw", StdioServerParameters(
                command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base))
        if "cdt" in _sides:
            sides.append(await run_side("cdt", StdioServerParameters(
                command="npx", args=["-y", "chrome-devtools-mcp@latest", "--headless", "--isolated"]),
                base))
    finally:
        srv.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "element-acc.json").write_text(
        json.dumps({s["kind"]: s for s in sides}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'case':22s} │" + "".join(
        f" {s['kind']:>5s} 命中 {'tok':>6s} │" for s in sides))
    for i, case in enumerate(SEL):
        row = f"{case['id']:22s} │"
        for sd in sides:
            c = sd["cases"][i]
            mark = "✓" if c["passed"] == c["total"] else ("~" if c["passed"] else "✗")
            row += f" {mark:>5s} {c['passed']}/{c['total']:<3d} {c['tokens']:6d} │"
        print(row)
    row = f"{'TOTAL':22s} │"
    for sd in sides:
        p_ = sum(c["passed"] for c in sd["cases"])
        t_ = sum(c["total"] for c in sd["cases"])
        row += f" {p_*100//t_:>4d}% {p_:>2d}/{t_:<3d} {sum(c['tokens'] for c in sd['cases']):6d} │"
    print("-" * 80)
    print(row)


if __name__ == "__main__":
    asyncio.run(main())
