"""E2E LLM 基准: 真模型开工具循环驱动三方 MCP (补此前八套全是脚本 resolver 的缺口)

方法论对齐业界 (OpenBrowser MCP benchmark / Online-Mind2Web):
- agent = 真 LLM (默认 SiliconFlow 免费档 Qwen2.5-7B-Instruct), 自主决定调什么工具
- 同一任务提示逐字喂三方; 难度按参考步数分层 (easy ≤5 / medium 6-10 / hard ≥11)
- 硬验证器为主 (DOM 状态/闭式答案字符串匹配), 不依赖 LLM 裁判
- 指标: 成功率 / 步数 / 模型 API token (usage 字段 input+output) / MCP 响应字符 / 时长
- 反捷径: 任务必须操作目标站点才能完成, 搜索引擎帮不上

用法:
  set SILICONFLOW_API_KEY=sk-...   (siliconflow.cn 免费注册)
  .venv/Scripts/python.exe -u bench/e2e_llm.py [子串过滤] [--sides=nexus,pw,cdt] [--model=Qwen/Qwen2.5-7B-Instruct]
产出: docs/bench/e2e-llm.json
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adversarial import A  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from scale_ops import Rec, _body, _json2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
FIXS = Path(__file__).parent / "fixture_scale"
FIXA = Path(__file__).parent / "fixture_acc"

API_BASE = os.environ.get("BENCH_API_BASE", "https://api.siliconflow.cn/v1")
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
MODEL = os.environ.get("BENCH_MODEL", "Qwen/Qwen2.5-7B-Instruct")
MAX_STEPS = 20

SYSTEM = (
    "你是一个浏览器自动化 agent, 通过 MCP 工具操作浏览器完成任务。"
    "规则: 1) 先用快照类工具看页面再操作; 2) 动作后要确认效果再继续; "
    "3) 不许用搜索引擎抄近路, 必须在目标页面内完成; "
    "4) 如果工具返回 CONFIRMATION_REQUIRED, 直接以 confirmed=true 重调同一工具 (用户已预先授权); "
    "5) 任务完成后, 最后一条消息用中文一句话直接给出答案或结论, 不要调用任何工具。"
)


def call_model(messages: list[dict], tools: list[dict]) -> dict:
    """OpenAI 兼容 chat.completions (同步, urllib; 代理走系统设置)。"""
    body = json.dumps({
        "model": MODEL, "messages": messages, "tools": tools or None,
        "temperature": 0, "max_tokens": 1024,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


# ── 任务: 硬验证器 (verify(a, final_text) -> (ok, note)) ─────────

async def _hits(a: A) -> list:
    v, _ = await a.eval("JSON.stringify(window.__hits || [])")
    return _json2(_body(v)) or []


async def _truth(a: A):
    v, _ = await a.eval("JSON.stringify(window.__truth || [])")
    return _json2(_body(v)) or []


def T(tid, tier, page, seed, prompt, verify):
    return {"id": tid, "tier": tier, "page": page, "seed": seed,
            "prompt": prompt, "verify": verify}


async def v_filter_first_row(a: A, final: str):
    rows = await _truth(a)
    exp = [x for x in rows if x["st"] == "已发货"]
    vis, _ = await a.eval("document.querySelector('#st').value")
    ok = "已发货" in _body(vis) and (exp and exp[0]["id"] in final)
    return ok, f"筛选值={_body(vis)} 首行={exp[0]['id'] if exp else '?'} in答案={exp[0]['id'] in final if exp else '-'}"


async def v_buy_cheapest(a: A, final: str):
    vis, _ = await a.eval("document.getElementById('confirm').style.display === 'block'")
    dok, _ = await a.eval("document.getElementById('confirm').dataset.ok || ''")
    return "true" in _body(vis).lower() and _body(dok) == "cheap", \
        f"confirm={_body(vis)} data-ok={_body(dok)}"


async def v_dash_value(a: A, final: str):
    mets = await _truth(a)
    m0 = mets[0]
    return str(m0["v"]) in final, f"真值={m0['v']}"


async def v_grid_sort_buy(a: A, final: str):
    hits = await _hits(a)
    return bool(hits) and hits[-1] == "buy-G17", f"hits={hits[-2:]}"


async def v_max_amt_of_name(a: A, final: str):
    rows = await _truth(a)
    exp = max((x["amt"] for x in rows if x["name"] == "张伟"), default=None)
    return exp is not None and str(exp) in final, f"真值={exp}"


async def v_wizard_flow(a: A, final: str):
    hits = await _hits(a)
    seq = ["f-name", "f-contact-name", "s1-next", "s2-next", "submit-apply"]
    pos = -1
    for want in seq:
        try:
            pos = hits.index(want, pos + 1)
        except ValueError:
            return False, f"缺步骤 {want} hits={hits}"
    return True, ""


async def v_row_action(a: A, final: str):
    hits = await _hits(a)
    return bool(hits) and hits[-1] == "row3-del", f"hits={hits[-2:]}"


async def v_kb_owner(a: A, final: str):
    arts = await _truth(a)
    art = next(x for x in arts if x["id"] == "KB-107")
    return art["owner"] in final, f"真值={art['owner']}"


async def v_last_page_last_order(a: A, final: str):
    rows = await _truth(a)
    done = [x for x in rows if x["st"] == "已完成"]
    import math
    pages = math.ceil(len(done) / 10)
    last_id = done[pages * 10 - 1]["id"] if len(done) >= pages * 10 else done[-1]["id"]
    cur, _ = await a.eval("document.title")
    return str(pages) in final and last_id in final and f"第{pages}/{pages}页" in _body(cur), \
        f"pages={pages} last={last_id} title={_body(cur)[:30]}"


async def v_price_compare(a: A, final: str):
    prods = await _truth(a)
    a_p = next(x["p"] for x in prods if x["n"] == "升降桌")
    b_p = next(x["p"] for x in prods if x["n"] == "人体工学椅")
    diff = abs(a_p - b_p)
    winner = "升降桌" if a_p > b_p else "人体工学椅"
    return winner in final and str(diff) in final, f"{winner} 差{diff}"


async def v_modal_flow(a: A, final: str):
    hits = await _hits(a)
    ok = "open-modal" in hits and (hits and hits[-1] == "modal-confirm")
    return ok, f"hits={hits[-3:]}"


async def v_multi_filter_read(a: A, final: str):
    rows = await _truth(a)
    cnt = sum(1 for x in rows if "李娜" in x["name"])
    tot = sum(x["amt"] for x in rows if "李娜" in x["name"])
    return str(cnt) in final and str(tot) in final, f"真值={cnt}笔/¥{tot}"


TASKS = [
    # easy (参考步数 ≤5)
    T("filter-first-row", "easy", "orders.html", 3,
      "打开订单页面,把状态筛选改成「已发货」,然后告诉我筛选后第一行的订单编号。",
      v_filter_first_row),
    T("buy-cheapest", "easy", "shop.html", 5,
      "打开商城页面,找到最便宜的商品,买 2 件,收货人填 张三、地址填 北京,提交订单。",
      v_buy_cheapest),
    T("dash-read", "easy", "dash.html", 7,
      "打开运营仪表盘,告诉我「日活用户」的当前数值是多少。", v_dash_value),
    T("grid-sort-buy", "easy", "grid.html", 0,
      "打开商品网格页,先点「价格排序」,然后把排完序后第一个商品加入购物车。",
      v_grid_sort_buy),
    # medium (6-10 步)
    T("max-amt-of-name", "medium", "orders.html", 9,
      "打开订单页面,找出客户「张伟」名下金额最高的一笔订单,告诉我金额是多少。",
      v_max_amt_of_name),
    T("wizard-flow", "medium", "wizard.html", 0,
      "打开入驻向导并完成它:第一步姓名填 张三、联系人姓名填 李四,点下一步;"
      "第二步店铺名称填 好店、客服姓名填 赵六,点下一步;第三步点「提交申请」。",
      v_wizard_flow),
    T("row-action", "medium", "admin.html", 0,
      "打开运营后台,在订单列表里找到 PO-2003 那一行,点它那一行的「删除」按钮。",
      v_row_action),
    T("kb-owner", "medium", "kb.html", 11,
      "打开知识库,查一下 KB-107 这篇文章的负责人是谁,告诉我。", v_kb_owner),
    # hard (≥11 步)
    T("last-page-order", "hard", "orders.html", 13,
      "打开订单页面,筛选「已完成」状态,数一数筛选结果一共分几页;翻到最后一页,"
      "告诉我最后一页最后一行的订单编号,以及一共几页。", v_last_page_last_order),
    T("price-compare", "hard", "shop.html", 15,
      "打开商城页面,比较「升降桌」和「人体工学椅」的价格,告诉我哪个贵、贵多少钱。",
      v_price_compare),
    T("modal-flow", "hard", "admin.html", 0,
      "打开运营后台,往下找到「打开危险操作」按钮点开,在弹出的对话框里点「确认清空」。",
      v_modal_flow),
    T("multi-filter-read", "hard", "orders.html", 17,
      "打开订单页面,按客户名过滤「李娜」,告诉我她名下有几笔订单、这些订单金额加起来一共多少。",
      v_multi_filter_read),
]

PAGE_BASE = {"orders.html": "scale", "shop.html": "scale", "dash.html": "scale",
             "kb.html": "scale", "grid.html": "acc", "wizard.html": "acc", "admin.html": "acc"}

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
_sides = "nexus,pw,cdt"
for x in sys.argv[1:]:
    if x.startswith("--sides="):
        _sides = x.split("=", 1)[1]
    elif x.startswith("--model="):
        MODEL = x.split("=", 1)[1]
SEL = [t for t in TASKS if not _filters or any(f in t["id"] for f in _filters)]


def _serve(d: Path) -> tuple[str, object]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _mcp_to_openai(tools: list) -> list[dict]:
    out = []
    for t in tools:
        out.append({"type": "function", "function": {
            "name": t.name, "description": (t.description or "")[:500],
            "parameters": t.input_schema or {"type": "object", "properties": {}}}})
    return out


async def run_task(sess: ClientSession, a: A, task: dict, base_map: dict, r: Rec) -> dict:
    """一个任务的完整 agent 循环。"""
    oai_tools = _mcp_to_openai(a.mcp_tools)
    url = f"{base_map[PAGE_BASE[task['page']]]}/{task['page']}"
    if task["seed"]:
        url += f"?seed={task['seed']}"
    prompt = f"起始页面: {url}\n\n任务: {task['prompt']}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
    usage = {"prompt": 0, "completion": 0}
    mcp_chars = 0
    steps = 0
    final_text = ""
    t0 = time.perf_counter()
    while steps < MAX_STEPS:
        try:
            resp = await asyncio.to_thread(call_model, messages, oai_tools)
        except Exception as e:
            return {"ok": False, "note": f"模型调用失败: {e}", "steps": steps,
                    "usage": usage, "mcp_chars": mcp_chars,
                    "ms": int((time.perf_counter() - t0) * 1000)}
        u = resp.get("usage") or {}
        usage["prompt"] += u.get("prompt_tokens", 0)
        usage["completion"] += u.get("completion_tokens", 0)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            final_text = msg.get("content") or ""
            break
        messages.append({"role": "assistant", "content": msg.get("content"),
                         "tool_calls": calls})
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            text, _ = await a.call(name, args)
            mcp_chars += len(text)
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": text[:6000]})
            steps += 1
        if steps >= MAX_STEPS:
            final_text = msg.get("content") or ""
    ok, note = await task["verify"](a, final_text)
    r.check("任务成功 (硬验证)", ok, note)
    if steps >= MAX_STEPS:
        r.check("步数未爆", False, f"达到 {MAX_STEPS} 步上限")
    return {"ok": ok, "note": note, "steps": steps, "usage": usage,
            "mcp_chars": mcp_chars, "ms": int((time.perf_counter() - t0) * 1000),
            "final": final_text[:200]}


async def run_side(kind: str, params: StdioServerParameters, base_map: dict) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            a.mcp_tools = (await s.list_tools()).tools
            results = []
            mark = 0
            for task in SEL:
                print(f"[{kind}] ▶ {task['id']} ({task['tier']})", file=sys.stderr, flush=True)
                r = Rec(task["id"])
                try:
                    res = await run_task(s, a, task, base_map, r)
                except Exception as e:
                    r.check("任务异常中断", False, f"{type(e).__name__}: {e}")
                    res = {"ok": False, "note": str(e), "steps": 0,
                           "usage": {"prompt": 0, "completion": 0}, "mcp_chars": 0, "ms": 0}
                calls = a.calls[mark:]
                mark = len(a.calls)
                res.update({"id": task["id"], "tier": task["tier"],
                            "passed": r.subs and all(x["ok"] for x in r.subs),
                            "mcp_tokens": sum(c["tokens"] for c in calls),
                            "calls_n": len(calls)})
                results.append(res)
                print(f"[{kind}] {task['id']}: {'OK' if res['ok'] else 'FAIL'} "
                      f"{res['steps']}步 api={res['usage']['prompt'] + res['usage']['completion']}tok "
                      f"{res['ms'] // 1000}s", file=sys.stderr, flush=True)
                await asyncio.sleep(0.5)
            return {"kind": kind, "model": MODEL, "results": results, "calls": a.calls}


async def main() -> None:
    if not API_KEY:
        raise SystemExit("缺 SILICONFLOW_API_KEY (siliconflow.cn 免费注册获取)")
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    base_map = {}
    srvs = []
    try:
        base_map["scale"], srv = _serve(FIXS)
        base_map["acc"], srv2 = _serve(FIXA)
        srvs = [srv, srv2]
        sides = []
        if "nexus" in _sides:
            sides.append(await run_side("nexus", StdioServerParameters(
                command=py, args=["-m", "nexus_browser.server"], env=env), base_map))
        if "pw" in _sides:
            sides.append(await run_side("pw", StdioServerParameters(
                command="npx", args=["-y", "@playwright/mcp@latest", "--headless"]), base_map))
        if "cdt" in _sides:
            sides.append(await run_side("cdt", StdioServerParameters(
                command="npx", args=["-y", "chrome-devtools-mcp@latest", "--headless", "--isolated"]),
                base_map))
    finally:
        for s_ in srvs:
            s_.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e2e-llm.json").write_text(
        json.dumps({s["kind"]: s for s in sides}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n模型: {MODEL}")
    print(f"{'task':18s} {'tier':7s} │" + "".join(
        f" {s['kind']:>5s} 步 api_tok {'ms':>6s} │" for s in sides))
    for i, task in enumerate(SEL):
        row = f"{task['id']:18s} {task['tier']:7s} │"
        for sd in sides:
            x = sd["results"][i]
            api = x["usage"]["prompt"] + x["usage"]["completion"]
            row += f" {'✓' if x['ok'] else '✗':>4s} {x['steps']:>3d} {api:>7d} {x['ms']:>6d} │"
        print(row)
    row = f"{'TOTAL SR':18s} {'':7s} │"
    for sd in sides:
        ok_n = sum(1 for x in sd["results"] if x["ok"])
        api = sum(x["usage"]["prompt"] + x["usage"]["completion"] for x in sd["results"])
        st = sum(x["steps"] for x in sd["results"])
        row += f" {ok_n}/{len(SEL):<3d} {st:>3d} {api:>7d} {'':>6s} │"
    print("-" * 90)
    print(row)


if __name__ == "__main__":
    asyncio.run(main())
