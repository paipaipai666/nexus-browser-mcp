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
from scale_ops import Rec, _body, _json2, _s  # noqa: E402

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
    ok = "已发货" in _s(vis) and (exp and exp[0]["id"] in final)
    return ok, f"筛选值={_s(vis)} 首行={exp[0]['id'] if exp else '?'} in答案={exp[0]['id'] in final if exp else '-'}"


async def v_buy_cheapest(a: A, final: str):
    vis, _ = await a.eval("document.getElementById('confirm').style.display === 'block'")
    dok, _ = await a.eval("document.getElementById('confirm').dataset.ok || ''")
    return "true" in _s(vis).lower() and _s(dok) == "cheap", \
        f"confirm={_s(vis)} data-ok={_s(dok)}"


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
    return str(pages) in final and last_id in final and f"第{pages}/{pages}页" in _s(cur), \
        f"pages={pages} last={last_id} title={_s(cur)[:30]}"


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


# ── Gitea 自托管真实应用 (Phase 3): API 状态验证器 (WebArena 式, 不查 DOM 查后端) ──

GITEA_BASE = os.environ.get("GITEA_BASE", "http://127.0.0.1:3000")
_GITEA_AUTH = os.environ.get("GITEA_AUTH", "benchadmin:BenchPass123")


def _gitea(path: str):
    req = urllib.request.Request(f"{GITEA_BASE}/api/v1{path}")
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(_GITEA_AUTH.encode()).decode())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


async def v_gitea_create_issue(a: A, final: str):
    issues = _gitea("/repos/benchadmin/nexus-demo/issues?state=all&type=issues")
    hit = [i for i in issues if i["title"] == "修复登录页样式"]
    return bool(hit), f"issues={len(issues)} 命中={bool(hit)}"


async def v_gitea_label(a: A, final: str):
    issue = _gitea("/repos/benchadmin/nexus-demo/issues/1")
    labels = [lb["name"] for lb in issue.get("labels", [])]
    return "bug" in labels, f"labels={labels}"


async def v_gitea_readme(a: A, final: str):
    return "nexus-demo" in final, "README 标题应含 nexus-demo"


async def v_gitea_create_label_assign(a: A, final: str):
    issues = _gitea("/repos/benchadmin/nexus-demo/issues?state=all&type=issues")
    hit = [i for i in issues if i["title"] == "v1.0 发布检查单"]
    if not hit:
        return False, "issue 未创建"
    issue = hit[0]
    labels = [lb["name"] for lb in issue.get("labels", [])]
    assignees = [u["login"] for u in issue.get("assignees") or []]
    ok = "enhancement" in labels and "benchadmin" in assignees
    return ok, f"labels={labels} assignees={assignees}"


async def v_gitea_search(a: A, final: str):
    return "基准种子仓库" in final or "工具库" in final, "探索页搜索应找到种子仓库"


async def v_gitea_star(a: A, final: str):
    repo = _gitea("/repos/benchadmin/nexus-lib")
    n = repo.get("stars_count", 0)
    return n >= 1, f"stars={n}"


async def v_gitea_comment(a: A, final: str):
    issues = _gitea("/repos/benchadmin/nexus-demo/issues?state=all&type=issues")
    hit = [i for i in issues if i["title"] == "添加导出 CSV 功能"]
    if not hit:
        return False, "issue 不存在"
    comments = _gitea(f"/repos/benchadmin/nexus-demo/issues/{hit[0]['number']}/comments")
    ok = any("下个迭代" in c.get("body", "") for c in comments)
    return ok, f"comments={len(comments)}"


async def v_gitea_close(a: A, final: str):
    issues = _gitea("/repos/benchadmin/nexus-demo/issues?state=all&type=issues")
    hit = [i for i in issues if i["title"] == "更新 README 安装段"]
    if not hit:
        return False, "issue 不存在"
    return hit[0]["state"] == "closed", f"state={hit[0]['state']}"


async def v_gitea_branch(a: A, final: str):
    branches = _gitea("/repos/benchadmin/nexus-demo/branches")
    names = [b["name"] for b in branches]
    return "fix/login-style" in names, f"branches={names}"


async def v_gitea_milestone(a: A, final: str):
    ms = _gitea("/repos/benchadmin/nexus-demo/milestones?state=all")
    titles = [m["title"] for m in ms]
    return "v1.0" in titles, f"milestones={titles}"


async def v_gitea_pr(a: A, final: str):
    prs = _gitea("/repos/benchadmin/nexus-demo/pulls?state=all")
    titles = [p["title"] for p in prs]
    return "导出功能" in titles, f"pulls={titles}"


async def v_gitea_issue_filter(a: A, final: str):
    issues = _gitea("/repos/benchadmin/nexus-demo/issues?state=all&type=issues")
    n = sum(1 for i in issues if any(lb["name"] == "bug" for lb in i.get("labels", [])))
    return str(n) in final, f"bug 标签 issue 数={n}"


TASKS = [    # easy (参考步数 ≤5)
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
    T("max-amt-of-name", "medium", "orders.html", 19,
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
    # ── Gitea 自托管真实应用 (登录 + 后端状态验证) ──
    T("gitea-readme", "easy", "gitea:/benchadmin/nexus-demo", 0,
      "打开这个 Gitea 仓库主页,告诉我仓库根目录 README.md 的第一行标题是什么(不含 # 号)。",
      v_gitea_readme),
    T("gitea-create-issue", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "然后在 benchadmin/nexus-demo 仓库创建一个 issue: 标题「修复登录页样式」, 内容「按钮圆角丢失」。",
      v_gitea_create_issue),
    T("gitea-label-issue", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "然后进入 benchadmin/nexus-demo 仓库的 issue 列表, 给标题为「首页在移动端错位」的 issue 打上 bug 标签。",
      v_gitea_label),
    T("gitea-create-label-assign", "hard", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "在 benchadmin/nexus-demo 仓库创建 issue 标题「v1.0 发布检查单」, 给它打上 enhancement 标签,"
      "并把负责人设置为 benchadmin。", v_gitea_create_label_assign),
    T("gitea-search", "easy", "gitea:/explore/repos", 0,
      "打开这个 Gitea 实例的仓库探索页, 搜索 nexus, 告诉我搜到的其中一个仓库的描述是什么。",
      v_gitea_search),
    T("gitea-star", "easy", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "然后打开 benchadmin/nexus-lib 仓库主页并给它加星 (star)。", v_gitea_star),
    T("gitea-issue-comment", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "进入 benchadmin/nexus-demo 仓库, 在标题为「添加导出 CSV 功能」的 issue 下评论「排期到下个迭代」。",
      v_gitea_comment),
    T("gitea-close-issue", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "进入 benchadmin/nexus-demo 仓库, 把标题为「更新 README 安装段」的 issue 关闭。",
      v_gitea_close),
    T("gitea-create-branch", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "在 benchadmin/nexus-demo 仓库从 main 创建一个名为 fix/login-style 的新分支。",
      v_gitea_branch),
    T("gitea-milestone", "medium", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "在 benchadmin/nexus-demo 仓库创建一个标题为 v1.0 的里程碑 (milestone)。",
      v_gitea_milestone),
    T("gitea-open-pr", "hard", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "在 benchadmin/nexus-demo 仓库, 给已有分支 feature/add-export 向 main 发一个 Pull Request,"
      "标题填「导出功能」。", v_gitea_pr),
    T("gitea-issue-filter", "hard", "gitea:/user/login", 0,
      "这是本地 Gitea 测试实例, 先登录(用户名 benchadmin 密码 BenchPass123),"
      "进入 benchadmin/nexus-demo 仓库的 issue 列表, 按 bug 标签筛选, 告诉我筛出来有几个 issue。",
      v_gitea_issue_filter),
]

PAGE_BASE = {"orders.html": "scale", "shop.html": "scale", "dash.html": "scale",
             "kb.html": "scale", "grid.html": "acc", "wizard.html": "acc", "admin.html": "acc"}

_filters = [x for x in sys.argv[1:] if not x.startswith("-")]
_sides = "nexus,pw,cdt"
_runs = 1
for x in sys.argv[1:]:
    if x.startswith("--sides="):
        _sides = x.split("=", 1)[1]
    elif x.startswith("--model="):
        MODEL = x.split("=", 1)[1]
    elif x.startswith("--runs="):
        _runs = int(x.split("=", 1)[1])
SEL = [t for t in TASKS if not _filters or any(f in t["id"] for f in _filters)]
TRACES = OUT / "e2e-traces"


def _serve(d: Path) -> tuple[str, object]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _flush_trace(path: Path | None, trace: list[dict]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in trace:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _mcp_to_openai(tools: list) -> list[dict]:
    out = []
    for t in tools:
        out.append({"type": "function", "function": {
            "name": t.name, "description": (t.description or "")[:500],
            "parameters": t.input_schema or {"type": "object", "properties": {}}}})
    return out


async def run_task(sess: ClientSession, a: A, task: dict, base_map: dict, r: Rec,
                   trace_path: Path | None = None) -> dict:
    """一个任务的完整 agent 循环。trace_path 给则录制轨迹 (JSONL, 供裁判/人评/复盘)。"""
    trace: list[dict] = []

    def rec(ev: str, data: dict) -> None:
        if trace_path is not None:
            trace.append({"t": round(time.perf_counter() - t0, 2), "ev": ev, **data})

    oai_tools = _mcp_to_openai(a.mcp_tools)
    if task["page"].startswith("gitea:"):
        url = GITEA_BASE + task["page"][6:]
    else:
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
        resp = None
        for attempt, wait in enumerate((0, 8, 20)):      # 并发/大上下文下 API 偶发慢, 退避重试
            if wait:
                await asyncio.sleep(wait)
            try:
                rec("model_req", {"n_msgs": len(messages), "steps": steps, "retry": attempt})
                resp = await asyncio.to_thread(call_model, messages, oai_tools)
                break
            except Exception as e:
                rec("model_retry", {"attempt": attempt, "err": str(e)[:150]})
        if resp is None:
            rec("model_error", {"err": "重试耗尽"})
            _flush_trace(trace_path, trace)
            return {"ok": False, "note": "模型调用失败: 重试耗尽", "steps": steps,
                    "usage": usage, "mcp_chars": mcp_chars,
                    "ms": int((time.perf_counter() - t0) * 1000)}
        u = resp.get("usage") or {}
        usage["prompt"] += u.get("prompt_tokens", 0)
        usage["completion"] += u.get("completion_tokens", 0)
        msg = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        rec("model_resp", {"content": (msg.get("content") or "")[:300],
                           "tool_calls": [c["function"]["name"] for c in calls]})
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
            rec("tool", {"name": name, "args": {k: str(v)[:120] for k, v in args.items()},
                         "resp": text[:2000]})
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": text[:6000]})
            steps += 1
        if steps >= MAX_STEPS:
            final_text = msg.get("content") or ""
    ok, note = await task["verify"](a, final_text)
    rec("verdict", {"ok": ok, "note": note, "final": final_text[:300]})
    _flush_trace(trace_path, trace)
    r.check("任务成功 (硬验证)", ok, note)
    if steps >= MAX_STEPS:
        r.check("步数未爆", False, f"达到 {MAX_STEPS} 步上限")
    return {"ok": ok, "note": note, "steps": steps, "usage": usage,
            "mcp_chars": mcp_chars, "ms": int((time.perf_counter() - t0) * 1000),
            "final": final_text[:200]}


def _classify_failure(res: dict) -> str:
    """失败分类法 v1 (自动归因, 供批量复盘)。"""
    note = res.get("note", "")
    if "模型调用失败" in note:
        return "model-error"
    if res.get("steps", 0) >= MAX_STEPS:
        return "cap-hit"             # 撞步数上限 (迷路/打转)
    if not res.get("final"):
        return "no-final-answer"     # 没给出终答就停了
    return "verifier-fail"           # 动作跑完但验证器不认 (真失败 or 验证器 bug)


async def run_side(kind: str, params: StdioServerParameters, base_map: dict,
                   round_idx: int) -> dict:
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            a = A(s, kind)
            a.mcp_tools = (await s.list_tools()).tools
            results = []
            mark = 0
            for task in SEL:
                print(f"[{kind}#r{round_idx}] ▶ {task['id']} ({task['tier']})",
                      file=sys.stderr, flush=True)
                r = Rec(task["id"])
                tpath = TRACES / f"{kind}-{task['id']}-r{round_idx}.jsonl"
                try:
                    res = await run_task(s, a, task, base_map, r, trace_path=tpath)
                except Exception as e:
                    r.check("任务异常中断", False, f"{type(e).__name__}: {e}")
                    res = {"ok": False, "note": str(e), "steps": 0,
                           "usage": {"prompt": 0, "completion": 0}, "mcp_chars": 0, "ms": 0}
                calls = a.calls[mark:]
                mark = len(a.calls)
                res.update({"id": task["id"], "tier": task["tier"], "round": round_idx,
                            "passed": r.subs and all(x["ok"] for x in r.subs),
                            "mcp_tokens": sum(c["tokens"] for c in calls),
                            "calls_n": len(calls)})
                if not res["ok"]:
                    res["failure_class"] = _classify_failure(res)
                results.append(res)
                print(f"[{kind}#r{round_idx}] {task['id']}: {'OK' if res['ok'] else 'FAIL'}"
                      f"{'(' + res['failure_class'] + ')' if not res['ok'] else ''} "
                      f"{res['steps']}步 api={res['usage']['prompt'] + res['usage']['completion']}tok "
                      f"{res['ms'] // 1000}s", file=sys.stderr, flush=True)
                await asyncio.sleep(0.5)
            return {"kind": kind, "model": MODEL, "round": round_idx,
                    "results": results, "calls": a.calls}


def _wilson(ok: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 区间 (比例型指标的诚实 CI, 小样本不崩)。"""
    if n == 0:
        return 0.0, 1.0
    p = ok / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, center - spread), min(1.0, center + spread)


async def main() -> None:
    if not API_KEY:
        raise SystemExit("缺 SILICONFLOW_API_KEY (siliconflow.cn 免费注册获取)")
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    env = {**os.environ, "BROWSER_HEADLESS": "true", "BROWSER_ALLOW_JS_EXECUTION": "true"}
    base_map = {}
    srvs = []
    all_rounds: dict[str, list[dict]] = {}
    try:
        base_map["scale"], srv = _serve(FIXS)
        base_map["acc"], srv2 = _serve(FIXA)
        srvs = [srv, srv2]
        side_params = {}
        if "nexus" in _sides:
            side_params["nexus"] = StdioServerParameters(
                command=py, args=["-m", "nexus_browser.server"], env=env)
        if "pw" in _sides:
            side_params["pw"] = StdioServerParameters(
                command="npx", args=["-y", "@playwright/mcp@latest", "--headless"])
        if "cdt" in _sides:
            side_params["cdt"] = StdioServerParameters(
                command="npx", args=["-y", "chrome-devtools-mcp@latest", "--headless", "--isolated"])
        for rnd in range(_runs):
            for kind, params in side_params.items():
                all_rounds.setdefault(kind, []).append(await run_side(kind, params, base_map, rnd))
    finally:
        for s_ in srvs:
            s_.shutdown()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e2e-llm.json").write_text(
        json.dumps({k: v for k, v in all_rounds.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 趋势看板: 每次全量追加一行 (Phase 5 回归追踪)
    import datetime
    hist = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "model": MODEL,
            "runs": _runs, "sides": {}}
    for k, rounds in all_rounds.items():
        allr = [x for rd in rounds for x in rd["results"]]
        hist["sides"][k] = {
            "ok": sum(1 for x in allr if x["ok"]), "n": len(allr),
            "api_tokens": sum(x["usage"]["prompt"] + x["usage"]["completion"] for x in allr),
            "steps": sum(x["steps"] for x in allr)}
    with open(OUT / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(hist, ensure_ascii=False) + "\n")

    print(f"\n模型: {MODEL} · 每任务轮数 N={_runs}")
    kinds = list(all_rounds)
    print(f"{'task':18s} {'tier':7s} │" + "".join(f" {k:>5s} {'':>3s} {'均步':>4s} {'均tok':>7s} │" for k in kinds))
    for i, task in enumerate(SEL):
        row = f"{task['id']:18s} {task['tier']:7s} │"
        for k in kinds:
            rs = [rd["results"][i] for rd in all_rounds[k]]
            oks = "".join("✓" if x["ok"] else "✗" for x in rs)
            mstep = sum(x["steps"] for x in rs) / len(rs)
            mtok = sum(x["usage"]["prompt"] + x["usage"]["completion"] for x in rs) // len(rs)
            row += f"  {oks:>4s} {mstep:>4.1f} {mtok:>7d} │"
        print(row)
    print("-" * 90)
    row = f"{'SR (Wilson95)':18s} {'':7s} │"
    for k in kinds:
        allr = [x for rd in all_rounds[k] for x in rd["results"]]
        ok_n = sum(1 for x in allr if x["ok"])
        lo, hi = _wilson(ok_n, len(allr))
        row += f"  {ok_n}/{len(allr)} [{lo:.0%},{hi:.0%}]"[:22].ljust(22) + "│"
    print(row)
    row = f"{'失败分类':18s} {'':7s} │"
    for k in kinds:
        from collections import Counter
        cls = Counter(x.get("failure_class") for rd in all_rounds[k] for x in rd["results"] if not x["ok"])
        row += f"  {dict(cls)!s:>19s} │"
    print(row)
    row = f"{'成功任务均tok':18s} {'':7s} │"
    for k in kinds:
        succ = [x["usage"]["prompt"] + x["usage"]["completion"]
                for rd in all_rounds[k] for x in rd["results"] if x["ok"]]
        tps = sum(succ) // len(succ) if succ else 0
        sr = len(succ) / max(1, sum(len(rd["results"]) for rd in all_rounds[k]))
        all_tok = sum(x["usage"]["prompt"] + x["usage"]["completion"]
                      for rd in all_rounds[k] for x in rd["results"])
        eff = sr / (all_tok / 1000) * 1000 if all_tok else 0   # 每 1k tok 换回的成功率百分点×100
        row += f"  {tps // 1000}k tok/成功 {eff:>5.2f}SR/Mtok │"
    print(row)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows GBK 控制台防崩
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
