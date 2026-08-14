"""事件驱动确定性快照: MutationObserver 静默窗口 + a11y 提取 + 优先级截断。

策略(相比竞品的固定 settle 硬等):
1. add_init_script 注入 MutationObserver, 记录页面最后一次 DOM 变异的时刻。
2. wait_for_function(polling="raf") 让浏览器自身 rAF 循环判定
   "lastMutation 距今超过 W(默认 800ms)" —— 即静默窗口。
3. 兜底 get_stable_tree: 静默判定后连拍 REQUIRED 次快照, 内容一致才返回,
   防纯动画/canvas 这类不触发 DOM 变异的变化; 超时则优雅降级。
"""

from __future__ import annotations

import asyncio
import re
import time
from time import monotonic
from typing import Any

from nexus_browser.settings import BrowserSettings

# 角色语义(照搬自 AgentNexus agentnexus/tools/browser.py)
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox",
    "checkbox", "radio", "switch", "slider", "spinbutton",
    "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "option", "scrollbar", "tablist",
    "dialog", "alertdialog",
}

READING_ROLES = INTERACTIVE_ROLES | {
    "heading", "paragraph", "status", "alert", "log",
    "marquee", "timer", "note", "definition",
}

LANDMARK_ROLES = {
    "banner", "complementary", "contentinfo", "form",
    "main", "navigation", "region", "search",
}

NON_GENERIC_ROLES = INTERACTIVE_ROLES | READING_ROLES | LANDMARK_ROLES | {
    "img", "list", "listitem", "table", "row", "cell",
    "separator", "toolbar", "tree", "treeitem", "grid", "gridcell",
}

def build_watcher_js(window_ms: int) -> str:
    """注入脚本: MutationObserver + in-page 防抖, 静默时调用 expose_binding 注册的
    __nexusSettleReport Python 回调 (CDP binding, 不受页面 CSP 限制; 非 eval)。
    add_init_script 自动跨导航重建。用 Date.now 而非 performance.now, 跨进程时钟可比较。"""
    return f"""
(() => {{
  try {{
    if (window.__nexusWatcherInstalled) return;
    window.__nexusWatcherInstalled = true;
    let last = Date.now();
    let timer = null;
    const fire = () => {{
      window.__nexusLastSettle = Date.now();
      if (typeof window.__nexusSettleReport === "function") {{
        try {{ window.__nexusSettleReport(Date.now()); }} catch (e) {{}}
      }}
    }};
    const onMut = () => {{
      last = Date.now();
      window.__nexusLastMutation = last;
      if (timer) clearTimeout(timer);
      timer = setTimeout(fire, {window_ms});
    }};
    new MutationObserver(onMut).observe(document.documentElement, {{
      childList: true, subtree: true,
      attributes: true, characterData: true,
    }});
    window.__nexusLastMutation = last;
    timer = setTimeout(fire, {window_ms});  // 初始静默 (静态页)
  }} catch (e) {{ /* 页面脚本环境受限时跳过, 走 REQUIRED 兜底 */ }}
}})();
"""

# 两段式解析: 真实 aria 输出的属性顺序/数量不固定
# ([level=1] 可在 [ref] 前, [cursor=pointer]/[active] 等多组并存, 文本在冒号后)。
# 旧版单一锚定正则对这三类行全部静默丢弃 —— Bing 搜索框/example.com 正文失踪的根因。
_YAML_NODE_RE = re.compile(r"^(\s*)- (\w+)(.*)$")
_YAML_NAME_RE = re.compile(r'^\s*"((?:\\.|[^"\\])*)"')
_YAML_ATTR_RE = re.compile(r"\[([^\]]+)\]")
_YAML_URL_RE = re.compile(r"^(\s*)- /url:\s*(.*)$")


async def ensure_watcher(page: Any, settings: BrowserSettings) -> None:
    """确保 MutationObserver 生效: add_init_script 供后续导航 + evaluate 立即注入当前页。"""
    js = build_watcher_js(settings.stable_window_ms)
    await page.add_init_script(js)
    try:
        await page.evaluate(js + "\ntrue")
    except Exception:
        pass  # 页面环境受限时跳过, 走 REQUIRED 兜底


def _parse_aria_yaml(raw: str) -> list[dict]:
    """解析 aria_snapshot YAML 输出为扁平节点列表 {role, name, ref, attrs, box, text, depth}。

    - 属性组与顺序/数量无关: ref= → ref, box= → 解析为四元数, 其余保留进 attrs
      (active/level/checked/expanded/disabled 等状态对 agent 决策有价值)。
    - 冒号后的行内文本 → text (paragraph/listitem 等阅读节点的主要内容)。
    - /url 伪节点按缩进挂到最近的浅层节点(父 link) → url。
    """
    nodes: list[dict] = []
    for line in raw.splitlines():
        um = _YAML_URL_RE.match(line)
        if um:
            depth = len(um.group(1)) // 2
            for n in reversed(nodes):
                if n["depth"] < depth:
                    n["url"] = um.group(2).strip()
                    break
            continue
        m = _YAML_NODE_RE.match(line)
        if not m:
            continue
        indent, role, rest = m.groups()
        depth = len(indent) // 2 if indent else 0
        name = ""
        nm = _YAML_NAME_RE.match(rest)
        if nm:
            name = nm.group(1).replace('\\"', '"')
            rest = rest[nm.end():]
        ref, box, attrs = "", None, []
        for a in _YAML_ATTR_RE.findall(rest):
            if a.startswith("ref="):
                ref = a[4:]
            elif a.startswith("box="):
                try:
                    box = [float(x) for x in a[4:].split(",")]
                except ValueError:
                    box = None
            else:
                attrs.append(a)
        text = ""
        leftover = _YAML_ATTR_RE.sub("", rest)
        if ":" in leftover:
            text = leftover.split(":", 1)[1].strip().strip('"')
        nodes.append({
            "role": role,
            "name": name,
            "ref": ref,
            "attrs": " ".join(attrs),
            "depth": depth,
            "box": box,
            "text": text,
        })
    return nodes


def _format_a11y_tree(nodes: list[dict], start_idx: int = 1, show_url: bool = False) -> str:
    lines: list[str] = []
    for i, n in enumerate(nodes, start=start_idx):
        parts = [f"[{i}]", n["role"]]
        if n.get("name"):
            parts.append(f'"{n["name"]}"')
        if n.get("ref"):
            parts.append(f"ref={n['ref']}")
        if n.get("attrs"):
            parts.append(f"[{n['attrs']}]")
        box = n.get("box")
        if box:
            parts.append(f"[box={','.join(str(int(b)) for b in box)}]")
        if n.get("viewport_status"):
            parts.append(f"[{n['viewport_status']}]")
        text = n.get("text")
        if text:
            parts.append(f": {text[:80]}")  # 单行摘要上限, 长文走 browser_read
        if show_url and n.get("url"):
            parts.append(f"→ {n['url']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _in_viewport(box: list[float] | None, vp_w: int, vp_h: int) -> bool:
    if not box or len(box) != 4:
        return True
    x, y, w, h = box
    return x < vp_w and y < vp_h and x + w > 0 and y + h > 0


def _truncate_by_priority(nodes: list[dict], max_nodes: int) -> list[dict]:
    """按优先级截断: 视口内交互 > 视口内阅读 > 视口外。"""
    if len(nodes) <= max_nodes:
        return nodes
    bucket_vp_interactive, bucket_vp_reading, bucket_offscreen = [], [], []
    for n in nodes:
        in_vp = n.get("viewport_status") != "offscreen"
        if in_vp and n.get("role") in INTERACTIVE_ROLES:
            bucket_vp_interactive.append(n)
        elif in_vp and n.get("role") in READING_ROLES:
            bucket_vp_reading.append(n)
        else:
            bucket_offscreen.append(n)
    result, remaining = [], max_nodes
    for bucket in (bucket_vp_interactive, bucket_vp_reading, bucket_offscreen):
        if remaining <= 0:
            break
        take = bucket[:remaining]
        result.extend(take)
        remaining -= len(take)
    return result


async def wait_dom_settled(page: Any, settings: BrowserSettings, task: Any | None = None,
                           timeout_ms: int | None = None) -> None:
    """事件驱动判定 DOM 静默窗口, 轮询兜底; 超时优雅降级。

    timeout_ms: 自定义总预算(默认 settings.stable_timeout_ms)。流式回复等长场景
    由调用方传入更大预算(上限受 tool_timeout_ms 护栏约束)。

    顺序:
    1. 单次状态检查 (非轮询): 已静默则立即返回 —— 覆盖静态页/老页面。
    2. 事件驱动: task.settle_event 由 expose_binding 回调置位 (页面内防抖计时器
       触发, 非 eval, 不受 CSP 限制) → asyncio.wait_for 唤醒。
    3. 兜底: 无 task (未注册 binding) 时退化为 evaluate 轮询。
    """
    budget_ms = timeout_ms if timeout_ms is not None else settings.stable_timeout_ms
    deadline = time.time() + budget_ms / 1000
    try:
        last = await page.evaluate("window.__nexusLastMutation ?? 0")
    except Exception:
        return  # 页面上下文不可用 → 优雅降级
    if last and time.time() * 1000 - last > settings.stable_window_ms:
        return

    settle_event = getattr(task, "settle_event", None) if task is not None else None
    if settle_event is not None:
        c0 = task.settle_count
        while task.settle_count == c0:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(settle_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            settle_event.clear()
        return

    # 兜底: 无 binding 时轮询
    while time.time() < deadline:
        try:
            last = await page.evaluate("window.__nexusLastMutation ?? 0")
            if last and time.time() * 1000 - last > settings.stable_window_ms:
                return
        except Exception:
            pass
        await asyncio.sleep(settings.stable_poll_ms / 1000)


_SCOPE_REF_RE = re.compile(r"e\d+")


async def _snapshot_raw(page: Any, scope: str | None) -> str:
    if scope:
        # scope 支持 CSS selector 或快照 ref (e57 → aria-ref 选择器引擎)
        sel = f"aria-ref={scope}" if _SCOPE_REF_RE.fullmatch(scope) else scope
        try:
            return await page.locator(sel).aria_snapshot(mode="ai", boxes=True, timeout=5000)
        except Exception as e:
            raise ValueError(
                f"scope 未匹配任何元素: {scope} (支持 CSS selector 或快照 ref, 如 e57)"
            ) from e
    return await page.locator("body").aria_snapshot(mode="ai", boxes=True)


async def get_stable_tree(
    page: Any,
    scope: str | None,
    settings: BrowserSettings,
    task: Any | None = None,
) -> list[dict]:
    """静默判定后连拍 REQUIRED 次一致才解析返回; 超时返回最后一次。"""
    deadline = monotonic() + settings.stable_timeout_ms / 1000
    raw: str | None = None
    while monotonic() < deadline:
        await wait_dom_settled(page, settings, task=task)
        raw = await _snapshot_raw(page, scope)
        ok = True
        for _ in range(settings.stable_required - 1):
            await asyncio.sleep(settings.stable_confirm_gap_ms / 1000)
            again = await _snapshot_raw(page, scope)
            if again != raw:
                ok = False
                break
        if ok:
            break
        raw = None
    if raw is None:
        raw = await _snapshot_raw(page, scope)
    return _parse_aria_yaml(raw)


def assemble_snapshot(
    all_nodes: list[dict],
    page_viewport: tuple[int, int] | None,
    *,
    mode: str = "reading",
    include_offscreen: bool = False,
    include_generic: bool = False,
    max_nodes: int = 100,
) -> dict:
    """从解析后的节点组装 LLM 消费的快照: 骨架 + 详情 + 建议 scope + 弹窗提示。

    返回 {"skeleton", "detail", "suggested_scopes", "popup_hint"}。
    """
    skeleton = [n for n in all_nodes if n.get("role") in LANDMARK_ROLES or n.get("role") == "heading"]

    if mode == "interactive":
        detail = [n for n in all_nodes if n.get("role") in INTERACTIVE_ROLES]
    elif mode == "reading":
        detail = [n for n in all_nodes if n.get("role") in READING_ROLES]
    elif mode == "full":
        detail = [n for n in all_nodes if n.get("role") in NON_GENERIC_ROLES]
    elif include_generic:
        detail = [n for n in all_nodes if n.get("role") not in {"none", "presentation"}]
    else:
        detail = [n for n in all_nodes if n.get("role") in NON_GENERIC_ROLES]

    vp_w, vp_h = page_viewport or (1280, 720)
    for n in detail:
        box = n.get("box")
        n["viewport_status"] = "visible" if _in_viewport(box, vp_w, vp_h) else "offscreen"
    if not include_offscreen:
        detail = [n for n in detail if n.get("viewport_status") != "offscreen"]
    detail = _truncate_by_priority(detail, max_nodes)

    scopes = []
    for n in skeleton:
        if n.get("role") in ("main", "region", "search", "form") and n.get("ref"):
            # scope 接受 aria-ref 形式 (与快照 ref 同源, 不会拼出无效 CSS)
            scopes.append(f"aria-ref={n['ref']}")

    popup_hint = None
    popup_nodes = [n for n in detail if n.get("role") in ("dialog", "alertdialog")]
    if popup_nodes:
        names = [n.get("name") for n in popup_nodes if n.get("name")]
        popup_hint = names[0] if names else "unknown popup"

    return {
        "skeleton": skeleton,
        "detail": detail,
        "suggested_scopes": scopes,
        "popup_hint": popup_hint,
    }
