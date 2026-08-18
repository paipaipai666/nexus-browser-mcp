"""MCP 服务器: 25 个浏览器工具 + session/task 生命周期 + 治理门。

session 模型: 一个 MCP 连接 = 一个 session_id (服务器启动生成, uuid4)。
同 session 内多个 task_id 各自隔离 (isolated: 独立 BrowserContext; cdp: 独立 Page)。
task_id 不传时自动生成默认 task。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from contextvars import ContextVar
from time import monotonic
from typing import Any

from mcp.server.mcpserver.context import Context as _MCPContext

from nexus_browser import fmt
from nexus_browser.core import BrowserManager
from nexus_browser.events import KIND_CONSOLE, KIND_DIALOG, KIND_DOWNLOAD, KIND_NAV, KIND_PAGEERROR, KIND_REQUEST
from nexus_browser.gates import AuditLogger, hitl_required
from nexus_browser.settings import BrowserSettings
from nexus_browser.snapshot import (
    REF_RE,
    assemble_snapshot,
    ensure_vitals,
    ensure_watcher,
    format_vitals,
    get_stable_tree,
    nodes_digest,
    wait_dom_settled,
)

logger = logging.getLogger(__name__)

_settings = BrowserSettings()
_session_id = "conn-" + uuid.uuid4().hex[:12]
_manager = BrowserManager(_settings)
_audit = AuditLogger(_settings.resolve_audit_path())

# HTTP 传输: 每请求的 MCP session id 置位于此; stdio 下恒为 None → 回落全局单例。
_session_var: ContextVar[str | None] = ContextVar("nexus_mcp_session", default=None)


def _sid() -> str:
    """当前请求的 session id: HTTP 多客户端按请求解析, stdio 回落进程级单例。"""
    return _session_var.get() or _session_id


def _session_from_ctx(ctx) -> str | None:
    """从注入的 MCP Context 取 HTTP session id (mcp-session-id 头); stdio → None。"""
    if ctx is None:
        return None
    try:
        req_ctx = ctx.request_context  # 无请求上下文时抛异常 (SDK 属性)
    except Exception:
        return None
    headers = getattr(getattr(req_ctx, "request", None), "headers", None)
    if not headers:
        return None
    return headers.get("mcp-session-id")


def tool_names() -> list[str]:
    return [
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_read", "browser_screenshot", "browser_evaluate", "browser_wait",
        "browser_wait_stable", "browser_wait_ms",
        "browser_scroll", "browser_scroll_to", "browser_wait_navigation",
        "browser_dismiss_popup", "browser_list_pages", "browser_switch_page",
        "browser_console", "browser_errors", "browser_network", "browser_perf",
        "browser_network_body",
        "browser_press_key", "browser_hover", "browser_select_option",
        "browser_upload_file", "browser_navigate_back", "browser_drag",
        "browser_dialog_respond",
        "browser_tasks", "browser_close_task",
        "browser_list_sessions", "browser_close_session",
    ]


def _default_task(task_id: str) -> str:
    return task_id or "default"


async def _require_task(task_id: str) -> str | None:
    """读/操作类工具的 task 门卫: 未知 task_id 显式报错, 不静默建空 task 再谎报"页面为空"。

    例外放行: default(隐式工作区, task_id='' 也在此归一化)、TTL 回收过的 task(get_page 自愈重建, Issue G 契约)。
    browser_navigate 不设此门卫 —— 它本就负责创建新 task。
    """
    task_id = _default_task(task_id)  # '' 与 'default' 同义; 否则 evaluate 等直传裸参数会误拒默认 task
    if task_id == "default" or _manager.known_task(_sid(), task_id):
        return None
    sessions = _manager.list_sessions()
    known = [t["task_id"] for s in sessions if s.get("session_id") == _sid()
             for t in (s.get("tasks") or [])]
    return fmt.error(
        f"task_id '{task_id}' 不存在",
        detail=f"当前 session 现有 task: {', '.join(known) or '(无)'}",
        hint="检查 task_id 拼写; 或先用 browser_navigate 创建该 task",
    )


# 页面/浏览器死亡类错误的底层特征 (Playwright 原始异常信息)
_DEATH_RE = re.compile(r"has been closed|target closed|page crashed|disconnected", re.I)


def _op_error(op: str, e: Exception, hint: str = "") -> str:
    """操作异常 → agent 可读错误。死亡类错误明确告知"会自动重建", 不泄漏底层异常。"""
    msg = str(e)
    if _DEATH_RE.search(msg):
        return fmt.error(
            f"{op}失败: 页面或浏览器已被外部关闭/崩溃",
            hint="直接重试即可(将自动重建并尽量恢复上次页面); 或 browser_close_task 后重开",
        )
    return fmt.error(f"{op}失败: {e}", hint=hint)


def _attach_notice(result: str, task_id: str) -> str:
    """自愈/回收恢复发生过 → 状态变更前置到工具返回, agent 必须感知世界变了。"""
    prefix = ""
    try:
        notice = _manager.pop_notice(_sid(), task_id)
        if notice and not result.startswith(notice):
            prefix = notice + "\n"
    except Exception:
        pass
    # 对话框挂起中: 每次回复都前置提醒, 直到 respond/超时清理 — 页面 JS 正被它冻结
    try:
        ts = _manager.peek_task(_sid(), task_id)
        d = ts.pending_dialog if ts else None
        if d is not None:
            prefix += (f"[对话框等待决策] {d.type}: \"{(d.message or '')[:120]}\" → "
                       "browser_dialog_respond(accept=true/false); accept 需 confirmed=true\n")
    except Exception:
        pass
    return prefix + result if prefix else result


def _hitl_block(action: str, role, name, task_id, confirmed: bool = False) -> str | None:
    if confirmed:
        return None  # 用户已在对话中同意, agent 带 confirmed=true 重调
    if hitl_required(_settings.hitl_rules, action, role, name):
        return (
            fmt.warning(f"操作需要人工确认: {action} role={role} name={name}")
            + "\n向用户说明该操作, 用户同意后以 confirmed=true 重调。\nCONFIRMATION_REQUIRED"
        )
    return None


# 工具风险分级: 默认 low(只读/导航); 改变页面状态 medium; JS 执行/文件出站 high。
_RISK = {
    "browser_click": "medium", "browser_type": "medium",
    "browser_dismiss_popup": "medium", "browser_evaluate": "high",
    "browser_network_body": "high", "browser_upload_file": "high",
    "browser_press_key": "medium", "browser_select_option": "medium",
    "browser_drag": "medium", "browser_dialog_respond": "high",
}


async def _guarded_call(name: str, fn, args=(), kwargs=None) -> str:
    """单次工具调用的统一护栏: pin → 外层超时 → 状态变更前置 → 审计(含字符计量)。

    审计在此单点完成(全覆盖): 入参字符数(脱敏前计量) + 返回字符数 + 耗时 +
    HITL 命中(CONFIRMATION_REQUIRED 出现在返回里)。token 成本可由此直接对账。
    """
    kwargs = dict(kwargs or {})
    if "task_id" in kwargs:
        # 分发层归一化 (单点): '' → 'default'。下游 18+ 调用点不再依赖各包装层
        # 记得 _default_task — 约定固化在边界, 新工具想错都难 (幻影 task 的根治)。
        kwargs["task_id"] = _default_task(kwargs["task_id"])
    tid = kwargs.get("task_id") or "default"
    _manager.pin(_sid(), tid)  # 操作期间钉住, TTL 不回收
    t0 = monotonic()
    try:
        result = await _with_tool_timeout(fn, name, _settings.tool_timeout_ms, args, kwargs)
    finally:
        _manager.unpin(_sid(), tid)
    out = _attach_notice(result, tid)
    try:
        _audit.log(
            _sid(), tid, name, dict(kwargs),
            risk=_RISK.get(name, "low"),
            hitl_triggered="CONFIRMATION_REQUIRED" in out,
            duration_ms=(monotonic() - t0) * 1000,
            in_chars=sum(len(str(v)) for v in kwargs.values()),
            out_chars=len(out),
        )
    except Exception as e:  # 审计失败不影响工具执行
        logger.warning("audit failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# 快照辅助: 格式化 get_stable_tree 结果为 LLM 可读文本
# ---------------------------------------------------------------------------


def _record_view(ts, scope, mode, include_offscreen, include_generic, nodes,
                 chars: int, source: str) -> None:
    """页面视图生产者的统一记录点 (单管道副作用): digest 基线 + ref 代际。

    _snapshot_text 与 _wait 共用: wait 结束时拍到的末帧就是 agent 下一眼看到的
    页面, 记入基线后紧随的 browser_snapshot 直接命中 diff (日常模式: 等元素→读页面)。
    """
    key = (id(ts.page), scope or "", mode, include_offscreen, include_generic)
    ts.snap_diff[key] = (nodes_digest(nodes), chars, len(nodes), source)
    if len(ts.snap_diff) > 8:  # 有界: FIFO 丢最旧
        ts.snap_diff.pop(next(iter(ts.snap_diff)))
    _track_ref_gen(ts, scope, nodes)


async def _snapshot_text(task_id: str, scope=None, mode="reading", include_offscreen=False,
                         include_generic=False, wait_stable=True, diff=True) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    mgr = _manager
    ts = await mgr.ensure_task(_sid(), task_id)
    if wait_stable:
        await ensure_watcher(ts.page, _settings)
    try:
        nodes = await get_stable_tree(ts.page, scope, _settings, task=ts)
    except ValueError as e:  # scope 未匹配等
        return fmt.error(str(e))
    from nexus_browser.snapshot import _format_a11y_tree

    viewport = await _viewport(ts.page)
    # E 决策: reading/interactive 保持视口内; full 默认开全局(标注 offscreen, 顶帽 max_nodes)
    offscreen = include_offscreen or mode == "full"
    assembled = assemble_snapshot(
        nodes, viewport,
        mode=mode, include_offscreen=offscreen,
        include_generic=include_generic, max_nodes=_settings.snapshot_max_nodes,
    )
    parts = []
    skeleton = assembled["skeleton"]
    detail = assembled["detail"]
    if skeleton:
        parts.append("## 页面结构")
        parts.append(_format_a11y_tree(skeleton))
        parts.append("")
    if detail:
        label = f"mode={mode}" + (",generic" if include_generic else "")
        parts.append(f"## 可交互元素 ({label})")
        parts.append(_format_a11y_tree(detail, show_url=(mode == "full")))
    else:
        parts.append("当前页面无可交互元素。")
    scopes = assembled["suggested_scopes"]
    if scopes and len(detail) < 10:
        parts.append("\n## 内容较少, 建议精细快照:")
        for s in scopes:
            parts.append(f"  scope={s}")
        parts.append("使用 browser_snapshot(scope='...', mode='full', include_generic=true) 获取更多内容。")
    popup = assembled["popup_hint"]
    if popup:
        parts.append(f"\n[弹窗检测] 存在弹窗: \"{popup}\", 建议 browser_dismiss_popup()。")
    text = "\n".join(parts)

    key = (id(ts.page), scope or "", mode, include_offscreen, include_generic)
    digest = nodes_digest(nodes)
    hit = ts.snap_diff.get(key)
    if diff and hit and hit[0] == digest:
        # 逐节点一致: 顺位链式映射旧代 ref → 新代, agent 手里的旧 ref 继续可用
        _track_ref_gen(ts, scope, nodes)
        chars_note = f", 上次全文 {hit[1]} 字符已省略" if hit[1] else ""
        return (f"[快照无变化] 与上次 {hit[3]} 的页面视图完全一致 ({hit[2]} 个节点{chars_note})。\n"
                "上次快照的 ref/pos 仍然有效, 可直接操作; 重看全文: browser_snapshot(diff=false)。")
    _record_view(ts, scope, mode, include_offscreen, include_generic, nodes,
                 len(text), "browser_snapshot" if diff else "browser_snapshot(diff=false)")
    return text


# ── 快照 ref 代际管理 ─────────────────────────────────────────────
# Playwright 每次 aria_snapshot 重写 DOM 里的 aria-ref 属性且按代际编号
# (s1e3 的 s1 = 快照代), 旧 ref 在重拍后失效。diff 命中(逐节点一致)时顺位
# 链式映射旧 ref → 新 ref, agent 不必因 ref 失效被迫重拍全文。


def _snap_ref_entry(ts, page, scope) -> dict:
    key = (id(page), scope or "")
    entry = ts.snap_refs.get(key)
    if entry is None:
        entry = {"refs": [], "map": {}}
        ts.snap_refs[key] = entry
        if len(ts.snap_refs) > 4:  # 有界: FIFO
            ts.snap_refs.pop(next(iter(ts.snap_refs)))
    return entry


def _record_ref_gen(ts, scope, nodes) -> None:
    """新代际(树可能已变): 清空旧映射防误指, 记录当前代 ref 列表。"""
    entry = _snap_ref_entry(ts, ts.page, scope)
    entry["map"].clear()
    entry["refs"] = [n["ref"] for n in nodes if n.get("ref")]


def _chain_refs(ts, scope, nodes) -> None:
    """diff 命中: 逐节点一致 → 上代 ref 顺位对到本代; 历史映射一并前链。"""
    entry = _snap_ref_entry(ts, ts.page, scope)
    new_refs = [n["ref"] for n in nodes if n.get("ref")]
    new_map = dict(zip(entry["refs"], new_refs))
    for k, v in list(entry["map"].items()):
        if v in new_map:
            entry["map"][k] = new_map[v]
    entry["map"].update(new_map)
    if len(entry["map"]) > 1000:  # 有界
        entry["map"] = dict(list(entry["map"].items())[-1000:])
    entry["refs"] = new_refs
    entry["digest"] = nodes_digest(nodes)


def _track_ref_gen(ts, scope, nodes) -> None:
    """任何内部 aria_snapshot 调用后必须过这里: 按内容指纹决定链式映射还是代际作废。

    browser_wait 等轮询路径同样重拍快照(重写 DOM 的 aria-ref 属性)——不过这道
    的话, agent 走 snapshot → wait → click(ref) 标准路径时 ref 被 wait 静默杀死。
    """
    entry = _snap_ref_entry(ts, ts.page, scope)
    new_refs = [n["ref"] for n in nodes if n.get("ref")]
    if entry["refs"] == new_refs:
        return  # 同代际重复跟踪 (wait 末帧再记录等): 恒等映射无信息量, 且会冲掉"代际作废"语义
    if entry["refs"] and entry.get("digest") == nodes_digest(nodes):
        _chain_refs(ts, scope, nodes)
    else:
        _record_ref_gen(ts, scope, nodes)
        entry["digest"] = nodes_digest(nodes)


async def _viewport(page) -> tuple[int, int] | None:
    try:
        vp = page.viewport_size
        return (vp["width"], vp["height"]) if vp else (1280, 720)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 工具实现 (task_id 显式参数, 懒创建)
# ---------------------------------------------------------------------------


async def browser_navigate(url: str, wait_until: str = "load", *, task_id: str = ""):
    return await _navigate(_default_task(task_id), url, wait_until)


async def _navigate(task_id: str, url: str, wait_until: str) -> str:
    if wait_until not in ("load", "domcontentloaded", "networkidle"):
        wait_until = "load"
    result = await _manager.navigate(_sid(), task_id, url, wait_until)
    if "error" in result:
        if _DEATH_RE.search(result["error"]):
            return fmt.error("导航失败: 页面或浏览器已被外部关闭/崩溃", detail=f"URL: {url}",
                             hint="直接重试即可(将自动重建并尽量恢复上次页面)")
        return fmt.error(f"导航失败: {result['error']}", detail=f"URL: {url}",
                         hint="检查 URL 或尝试 wait_until='domcontentloaded'")
    parts = [f"已导航至: {result['url']}", f"标题: {result['title']}", f"readyState: {result['readyState']}"]
    if result.get("timed_out"):
        parts.insert(0, fmt.warning("networkidle 超时, 已 fallback 继续", detail="可能为 WebSocket 或长轮询"))
    try:
        # reading 模式 + diff=True: 与 browser_snapshot 默认参数同源, 记录 digest/ref 基线。
        # 效果: 导航后首次显式 snapshot 命中 diff (~77 tok 而非全量), 且 reading 树
        # 同时包含内容与可交互元素, 是 navigate 更合理的默认视图; 重复导航同页也命中 diff。
        skeleton = await _snapshot_text(task_id, mode="reading", wait_stable=False, diff=True)
        parts.append(f"\n## 页面快照\n{skeleton}")
    except Exception:
        pass
    return "\n".join(parts)


async def browser_snapshot(scope: str | None = None, mode: str = "reading",
                      include_offscreen: bool = False, wait_stable: bool = True,
                      include_generic: bool = False, diff: bool = True, *, task_id: str = ""):
    return await _snapshot_text(_default_task(task_id), scope, mode,
                                include_offscreen, include_generic, wait_stable, diff)


async def browser_click(ref=None, role=None, name=None, selector=None, double_click=False,
                   pos=None, wait_stable: bool = False, button: str = "left",
                   confirmed: bool = False, *, task_id: str = ""):
    tid = _default_task(task_id)
    block = _hitl_block("click", role, name, tid, confirmed)
    if block:
        return block
    return await _click(tid, ref, role, name, selector, double_click, pos, wait_stable, button)


async def _click(task_id, ref, role, name, selector, double_click, pos, wait_stable=False,
                 button: str = "left") -> str:
    if button not in ("left", "right", "middle"):
        return fmt.error(f"不支持的 button: {button}", hint="left / right(上下文菜单) / middle(新标签后台打开)")
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        locator = await _manager.find_element(_sid(), task_id, ref=ref, role=role,
                                              name=name, selector=selector, pos=pos)
        if isinstance(locator, str):  # pos: 坐标点击
            x, y, w, h = (int(p) for p in locator.split(","))
            cx, cy = x + w // 2, y + h // 2
            if double_click:
                await ts.page.mouse.dblclick(cx, cy)
            else:
                await ts.page.mouse.click(cx, cy, button=button)
            if wait_stable:
                await _settle_after_action(ts)
            return f"已点击坐标 ({cx}, {cy})。"
        # Issue N 根治: Playwright 默认点击后等"scheduled navigations" —— target=_blank /
        # window.open 的新标签导航把本等待拖死成 5s 误报。no_wait_after 跳过该等待:
        # 点击动作在返回前已执行完毕; 导航等待交给 browser_wait_navigation / nav_event。
        ts.nav_event.clear()
        if double_click:
            await locator.dblclick(timeout=5000, no_wait_after=True)
        else:
            await locator.click(timeout=5000, no_wait_after=True, button=button)
        # 新标签/下载裁决: context "page" 事件与 download 事件异步到达, 短窗口内出现 → 明确告知
        t0 = monotonic()
        while (monotonic() - t0 < 0.8
               and ts.pending_new_page is None and ts.pending_download is None):
            await asyncio.sleep(0.05)
        if ts.pending_new_page is not None:
            _manager.consume_pending_new_page(_sid(), task_id)
            if wait_stable:
                await _settle_after_action(ts)
            idx = len(ts.pages) - 1
            return f"已点击元素; 链接在新标签打开 (index={idx}), 已自动切换为当前页。"
        if ts.pending_download is not None:
            dl = ts.pending_download
            ts.pending_download = None
            if wait_stable:
                await _settle_after_action(ts)
            return f"已点击元素; 开始下载: {dl['filename']} → 已保存 {dl['path']}"
        if wait_stable:
            await _settle_after_action(ts)
        return "已点击元素。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("点击", e, hint="尝试用 pos 坐标: browser_click(pos='x,y,w,h')")


async def _settle_after_action(ts) -> None:
    """操作后等待 DOM 静默 (SPA 局部更新)。失败不阻断操作结果。"""
    try:
        await ensure_watcher(ts.page, _settings)
        await wait_dom_settled(ts.page, _settings, task=ts)
    except Exception:
        pass


async def browser_type(text: str, ref=None, role=None, name=None, selector=None,
                  clear: bool = True, press_enter: bool = False, pos=None,
                  wait_stable: bool = False, confirmed: bool = False, *, task_id: str = ""):
    tid = _default_task(task_id)
    block = _hitl_block("type", role, name, tid, confirmed)
    if block:
        return block
    return await _type(tid, text, ref, role, name, selector, clear, press_enter, pos, wait_stable)


async def _type(task_id, text, ref, role, name, selector, clear, press_enter, pos, wait_stable=False) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        locator = await _manager.find_element(_sid(), task_id, ref=ref, role=role,
                                              name=name, selector=selector, pos=pos)
        if isinstance(locator, str):
            x, y, w, h = (int(p) for p in locator.split(","))
            cx, cy = x + w // 2, y + h // 2
            await ts.page.mouse.click(cx, cy)
            if clear:
                await ts.page.keyboard.press("Control+a")
            await ts.page.keyboard.type(text)
            if press_enter:
                await ts.page.keyboard.press("Enter")
            if wait_stable:
                await _settle_after_action(ts)
            return "已输入文本。"
        try:
            if clear:
                await locator.clear(timeout=5000)
            await locator.fill(text, timeout=5000)
            # fill 对"内嵌在 contenteditable 容器里的子元素"静默无效 (Playwright 层, 实测捕获)
            # → 读回校验: 目标值/文本不含所填内容即视为未写入
            try:
                current = await locator.evaluate("e => e.value ?? e.textContent ?? ''")
                landed = text in (current or "")
            except Exception:
                landed = True  # 读回失败不动 fill 结果 (正常 input 路径)
        except Exception:
            landed = False
        if not landed:
            # 升级到最近 contenteditable 容器 (无则退回目标本身), 聚焦逐键写入
            host = locator.locator("xpath=ancestor-or-self::*[@contenteditable='true'][1]")
            try:
                if await host.count() == 0:
                    host = locator
            except Exception:
                host = locator
            await host.click(timeout=3000, no_wait_after=True)
            if clear:
                await host.press("Control+a")
                await host.press("Delete")
            await host.press_sequentially(text, timeout=5000)
        if press_enter:
            await locator.press("Enter")
        if wait_stable:
            await _settle_after_action(ts)
        return "已输入文本。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("输入", e)


async def browser_press_key(key: str, wait_stable: bool = False, *, task_id: str = ""):
    return await _press_key(_default_task(task_id), key, wait_stable)


async def _press_key(task_id, key, wait_stable=False) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        await ts.page.keyboard.press(key)  # 真实 CDP 键事件 (isTrusted=true), 非 synthetic dispatch
        if wait_stable:
            await _settle_after_action(ts)
        return f"已按键: {key}。"
    except Exception as e:
        return _op_error("按键", e, hint="key 用 Playwright 键名: Escape/Tab/Enter/ArrowDown/Control+a ...")


async def browser_hover(ref=None, role=None, name=None, selector=None, pos=None,
                        wait_stable: bool = False, *, task_id: str = ""):
    return await _hover(_default_task(task_id), ref, role, name, selector, pos, wait_stable)


async def _hover(task_id, ref, role, name, selector, pos, wait_stable=False) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        locator = await _manager.find_element(_sid(), task_id, ref=ref, role=role,
                                              name=name, selector=selector, pos=pos)
        if isinstance(locator, str):  # pos 坐标: 真实鼠标移动
            x, y, w, h = (int(p) for p in locator.split(","))
            await ts.page.mouse.move(x + w // 2, y + h // 2)
            if wait_stable:
                await _settle_after_action(ts)
            return f"已悬停坐标 ({x + w // 2}, {y + h // 2})。"
        await locator.hover(timeout=5000)  # 真实鼠标移动: CSS :hover 与 JS mouseenter 均触发
        if wait_stable:
            await _settle_after_action(ts)
        return "已悬停元素。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("悬停", e)


async def browser_select_option(values: list[str], ref=None, selector=None,
                                wait_stable: bool = False, *, task_id: str = ""):
    return await _select_option(_default_task(task_id), values, ref, selector, wait_stable)


async def _select_option(task_id, values, ref, selector, wait_stable=False) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        locator = await _manager.find_element(_sid(), task_id, ref=ref, selector=selector)
        if isinstance(locator, str):
            return fmt.error("select_option 不支持 pos 坐标定位", hint="用 ref 或 selector 指向 <select> 元素")
        selected = await locator.select_option(values, timeout=5000)
        if wait_stable:
            await _settle_after_action(ts)
        return f"已选择: {', '.join(selected) or values[0]}。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("选择", e, hint="values 传 option 的 value/label; 目标必须是 <select>")


async def browser_upload_file(paths: list[str], ref=None, selector=None, confirmed: bool = False,
                              *, task_id: str = ""):
    """文件上传 = 本地文件出站, 与 evaluate 同级: 每次需 confirmed=true (用户在对话中同意后重调)。"""
    if not confirmed:
        return "CONFIRMATION_REQUIRED\n" + fmt.warning(
            "browser_upload_file 需人工确认",
            detail=f"将上传本地文件: {paths}。向用户展示路径清单, 同意后以 confirmed=true 重调。")
    return await _upload_file(_default_task(task_id), paths, ref, selector)


async def _upload_file(task_id, paths, ref, selector) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return fmt.error("文件不存在", detail=", ".join(missing))
    await _manager.ensure_task(_sid(), task_id)
    try:
        locator = await _manager.find_element(_sid(), task_id, ref=ref, selector=selector)
        if isinstance(locator, str):
            return fmt.error("上传不支持 pos 坐标定位", hint="用 ref 或 selector 指向 <input type=file>")
        await locator.set_input_files(paths, timeout=5000)
        return f"已上传 {len(paths)} 个文件: {', '.join(os.path.basename(p) for p in paths)}。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("上传", e, hint="目标必须是 <input type='file'>")


async def browser_navigate_back(wait_stable: bool = False, *, task_id: str = ""):
    return await _navigate_back(_default_task(task_id), wait_stable)


async def browser_dialog_respond(accept: bool, prompt_text: str = "", confirmed: bool = False,
                                 *, task_id: str = ""):
    """处置挂起的对话框。dismiss 自由; accept=替用户点"确定" → 每次需 confirmed=true (HITL)。"""
    if accept and not confirmed:
        return "CONFIRMATION_REQUIRED\n" + fmt.warning(
            "接受对话框需人工确认",
            detail="accept=true 将替用户点'确定'。向用户展示对话框内容, 同意后以 confirmed=true 重调。")
    return await _dialog_respond(_default_task(task_id), accept, prompt_text)


async def _dialog_respond(task_id, accept, prompt_text) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    d = ts.pending_dialog
    if d is None:
        return fmt.error("当前无待处理对话框",
                         hint="可能已超时自动 dismiss; browser_errors 可查看对话框事件记录")
    ts.pending_dialog = None
    if ts.dialog_waiter is not None:
        ts.dialog_waiter.cancel()
        ts.dialog_waiter = None
    try:
        if accept:
            await d.accept(prompt_text or None)  # prompt 框可填文本; confirm/alert 忽略
            action = "已接受(accept)"
        else:
            await d.dismiss()
            action = "已拒绝(dismiss)"
        try:
            _manager.events.record(ts.page, KIND_DIALOG, level=d.type, text=f"→ {action}")
        except Exception:
            pass
        await _settle_after_action(ts)  # 解除后页面 JS 继续执行
        return f"对话框{action}。"
    except Exception as e:
        return _op_error("对话框处理", e)


async def _navigate_back(task_id, wait_stable=False) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        resp = await ts.page.go_back(wait_until="load", timeout=_settings.default_timeout_ms)
        if resp is None:
            return "已在历史起点, 无可后退页面。"
        if wait_stable:
            await _settle_after_action(ts)
        title = await ts.page.title()
        return f"已后退至: {ts.page.url}\n标题: {title}"
    except Exception as e:
        return _op_error("后退", e)


async def browser_drag(from_ref=None, from_selector=None, to_ref=None, to_selector=None,
                       wait_stable: bool = False, *, task_id: str = ""):
    return await _drag(_default_task(task_id), from_ref, from_selector, to_ref, to_selector, wait_stable)


async def _drag(task_id, from_ref, from_selector, to_ref, to_selector, wait_stable=False) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        src = await _manager.find_element(_sid(), task_id, ref=from_ref, selector=from_selector)
        dst = await _manager.find_element(_sid(), task_id, ref=to_ref, selector=to_selector)
        if isinstance(src, str) or isinstance(dst, str):
            return fmt.error("拖拽不支持 pos 坐标定位", hint="用 ref 或 selector 指向源与目标元素")
        # 不用 locator.drag_to: 它对 pointer 系库 (jQuery UI sortable 等) 经常静默无效
        # (实测定案)。分段路径 = hover 激活 → down → 10 步过中间点 → 落点偏目标下半部 → up,
        # HTML5 原生 dnd 与 pointer 系库通吃 (同一真实输入管线, 事件更多更拟人)。
        sb = await src.bounding_box(timeout=5000)
        db = await dst.bounding_box(timeout=5000)
        if not sb or not db:
            return fmt.error("拖拽元素不可见或无几何信息", hint="先滚动到可见位置 (browser_scroll_to)")
        x0, y0 = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
        x1, y1 = db["x"] + db["width"] / 2, db["y"] + db["height"] * 0.75
        mouse = ts.page.mouse
        await mouse.move(x0, y0)
        await asyncio.sleep(0.15)
        await mouse.down()
        await asyncio.sleep(0.1)
        for i in range(1, 11):
            await mouse.move(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10)
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.15)
        await mouse.up()
        if wait_stable:
            await _settle_after_action(ts)
        return "已拖拽元素。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("拖拽", e)


async def browser_read(selector=None, ref=None, max_chars: int = 5000,
                  wait_stable: bool = False, max_wait_ms: int = 10000,
                  follow: bool = False, stream_id: str = "", full: bool = False, *, task_id: str = ""):
    return await _read(_default_task(task_id), selector, ref, max_chars,
                       wait_stable, max_wait_ms, follow, stream_id, full)


def _wait_budget(max_wait_ms: int) -> int:
    """等待预算硬上限: 给外层超时护栏留 5s 余量, 否则 wait 参数形同虚设。"""
    return min(max_wait_ms, max(1000, _settings.tool_timeout_ms - 5000))


async def _read(task_id, selector, ref, max_chars, wait_stable, max_wait_ms, follow, stream_id, full) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    page = ts.page

    if wait_stable:
        # 流式回复场景: 等 DOM 静默窗口(默认 800ms 无变异)再读, 免轮询
        await ensure_watcher(page, _settings)
        budget = _wait_budget(max_wait_ms)
        t0 = monotonic()
        await wait_dom_settled(page, _settings, task=ts, timeout_ms=budget)
        timed_out = (monotonic() - t0) * 1000 >= budget
    else:
        timed_out = False

    if follow:
        return await _follow_read(ts, selector, stream_id, full, max_chars, timed_out)

    try:
        if ref:
            locator = await _manager.find_element(_sid(), task_id, ref=ref)
        elif selector:
            locator = page.locator(selector)
        else:
            locator = page.locator("body")
        text = await locator.inner_text(timeout=5000)
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("读取内容", e)
    if wait_stable and timed_out:
        prefix = fmt.warning(f"等待稳定超时 ({_wait_budget(max_wait_ms)}ms)", detail="内容可能仍在生成") + "\n"
    else:
        prefix = ""
    if len(text) > max_chars:
        total = len(text)
        text = text[:max_chars]
        return f"{prefix}{text}\n\n... (内容已截断, 共 {total} 字符, 已显示前 {max_chars} 字符)"
    return prefix + text


async def _follow_read(ts, selector, stream_id, full, max_chars, wait_timed_out) -> str:
    """流式增量读: (page, selector) 一条流, 返回增量; full=True 返回缓冲全文。

    增量语义: 纯追加只回后缀; 内容被整体替换时在缓冲里打 [内容被替换] 标记;
    容量溢出丢最旧部分, 缝合处保留 [...已丢弃 N 字符...]。
    """
    store = _manager.streams
    created = False
    if stream_id:
        st = store.get(stream_id)
        if not st:
            return fmt.error(f"流 {stream_id} 不存在", hint="用 browser_read(selector=..., follow=true) 新建流")
    else:
        if not selector:
            return fmt.error("follow 模式必须提供 selector (watch 目标)",
                             hint="例: browser_read(selector='.message-list', follow=true)")
        st, created = store.get_or_create(ts.page, selector)

    status = "已失效" if st.dead else ("仍在生成" if _stream_active(st) else "已稳定")
    if not st.dead:
        try:
            text = await ts.page.locator(st.selector).inner_text(timeout=5000)
        except Exception as e:
            return _op_error("流式读取", e)
        delta = store.record(st, text)
    else:
        delta = ""

    if wait_timed_out:
        status = "等待超时,可能仍在生成"
    total = st.char_count
    header_parts = [f"流 {st.stream_id}", f"本次 +{len(delta)} 字符", f"累计 {total} 字符", status]
    if created:
        header_parts.append("已建立监听")
    if st.dropped_chars:
        header_parts.append(f"已丢弃 {st.dropped_chars} 字符")
    header = "[" + " | ".join(header_parts) + "]"

    body = store.full_text(st) if full else delta
    if not body:
        return f"{header}\n(无新增内容; full=true 可取缓冲全文)"
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n... (本次输出已截断, 共 {total} 字符, full=true/max_chars 调整)"
    return f"{header}\n{body}"


def _stream_active(st) -> bool:
    """静默窗口内有新增 → 视为仍在生成。"""
    return bool(st.last_chunk_ts) and (monotonic() - st.last_chunk_ts) < _settings.stable_window_ms / 1000


async def browser_screenshot(path: str | None = None, full_page: bool = False, *, task_id: str = ""):
    return await _screenshot(_default_task(task_id), path, full_page)


async def _screenshot(task_id, path, full_page) -> str:
    import os
    from pathlib import Path

    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    if not path:
        ss_dir = _settings.screenshot_dir or str(Path.home() / ".nexus-browser" / "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        path = os.path.join(ss_dir, f"screenshot_{uuid.uuid4().hex[:8]}.png")
    try:
        await ts.page.screenshot(path=path, full_page=full_page)
        return f"截图已保存: {path}"
    except Exception as e:
        return _op_error("截图", e)


async def browser_evaluate(expression: str, confirmed: bool = False, *, task_id: str = ""):
    task_id = _default_task(task_id)  # 唯一漏归一化的入口: bench 实测 evaluate('') 曾建出幻影 task '' 对着 about:blank 求值
    if not _settings.allow_js_execution:
        return fmt.error("JS 执行未启用 (管理员级开关)",
                         detail="BROWSER_ALLOW_JS_EXECUTION=false: 能力被部署方禁用。"
                                "confirmed=true 是启用后的逐次用户确认, 不能越过此开关",
                         hint="在服务器环境设置 BROWSER_ALLOW_JS_EXECUTION=true 并重启后, 再以 confirmed=true 调用")
    if not confirmed:
        return "CONFIRMATION_REQUIRED\n" + fmt.warning(
            "browser_evaluate 需人工确认",
            detail=f"expression={expression!r}。向用户展示该表达式, 同意后以 confirmed=true 重调。")
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        result = await ts.page.evaluate(expression)
        return f"结果: {result!r}"
    except Exception as e:
        return _op_error("JS 执行", e)


async def browser_wait(role=None, name=None, ref=None, text=None, timeout: int = 5000, *, task_id: str = ""):
    return await _wait(_default_task(task_id), role, name, ref, text, timeout)


async def _wait(task_id, role, name, ref, text, timeout) -> str:
    if not any([role, name, ref, text]):
        return fmt.error("必须指定 role+name、ref 或 text 中的至少一个参数")
    from time import monotonic

    from nexus_browser.snapshot import get_stable_tree

    deadline = monotonic() + timeout / 1000
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    nodes: list = []
    result = fmt.warning(f"等待超时 ({timeout}ms): 未找到匹配元素")
    while monotonic() < deadline:
        nodes = await get_stable_tree(ts.page, None, _settings, task=ts)
        _track_ref_gen(ts, None, nodes)  # 轮询重拍会重写 DOM ref; 过代际跟踪防静默杀 ref
        for node in nodes:
            if ref and node.get("ref") == ref:
                result = f"元素 ref={ref} 已出现。"
                break
            if role and name and node.get("role") == role and name.lower() in node.get("name", "").lower():
                result = f"元素 role={role} name={name} 已出现。"
                break
            if text and text.lower() in node.get("name", "").lower():
                result = f"文本 \"{text}\" 已出现。"
                break
        else:
            await asyncio.sleep(0.4)
            continue
        break
    if nodes:
        # 末帧即 agent 下一眼: 记入 diff 基线, 紧随的 browser_snapshot 直接命中
        _record_view(ts, None, "reading", False, False, nodes, 0, "browser_wait")
    return result


async def browser_wait_stable(timeout_ms: int = 10000, *, task_id: str = ""):
    return await _wait_stable(_default_task(task_id), timeout_ms)


async def _wait_stable(task_id, timeout_ms) -> str:
    """等 DOM 静默窗口: 流式回复/连续动画"停止变化"的判定原语。"""
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    budget = _wait_budget(timeout_ms)
    await ensure_watcher(ts.page, _settings)
    t0 = monotonic()
    await wait_dom_settled(ts.page, _settings, task=ts, timeout_ms=budget)
    waited = (monotonic() - t0) * 1000
    if waited >= budget:
        return fmt.warning(
            f"等待稳定超时 ({budget}ms): 页面仍在变化",
            detail="上限 = BROWSER_TOOL_TIMEOUT_MS - 5s; 可调大 timeout_ms 或 BROWSER_TOOL_TIMEOUT_MS",
        )
    return f"页面已稳定 (静默 ≥{_settings.stable_window_ms}ms, 耗时 {int(waited)}ms)。"


async def browser_wait_ms(ms: int, *, task_id: str = ""):
    cap = _wait_budget(ms)
    if ms > cap:
        return fmt.error(
            f"等待时长超限: {ms}ms > {cap}ms",
            hint="上限 = BROWSER_TOOL_TIMEOUT_MS - 5s; 更长等待请调大 BROWSER_TOOL_TIMEOUT_MS, 或用 browser_wait_stable",
        )
    await asyncio.sleep(ms / 1000)
    return f"已等待 {ms}ms。"


async def _page_of(task_id):

    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    return ts.page


async def browser_scroll(direction: str = "down", amount: int = 500, *, task_id: str = ""):
    return await _scroll(_default_task(task_id), direction, amount)


async def _scroll(task_id, direction, amount) -> str:
    if direction not in ("up", "down", "left", "right"):
        return fmt.error(f"不支持的滚动方向: {direction}")
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    dx, dy = 0, 0
    if direction == "down":
        dy = amount
    elif direction == "up":
        dy = -amount
    elif direction == "right":
        dx = amount
    elif direction == "left":
        dx = -amount
    try:
        await ts.page.mouse.wheel(dx, dy)
        await _settle_after_action(ts)  # 滚动触发懒加载, 等静默避免快照拍在中间态
        return f"已向{direction}滚动 {amount}px。"
    except Exception as e:
        return _op_error("滚动", e)


async def browser_scroll_to(landmark=None, ref=None, selector=None, *, task_id: str = ""):
    return await _scroll_to(_default_task(task_id), landmark, ref, selector)


async def _scroll_to(task_id, landmark, ref, selector) -> str:
    if not any([landmark, ref, selector]):
        return fmt.error("必须指定 landmark、ref 或 selector 中的至少一个参数")
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        if landmark:
            loc = ts.page.get_by_role("region", name=landmark)
            if await loc.count() == 0:
                loc = ts.page.locator(f"#{landmark}")
            if await loc.count() == 0:
                loc = ts.page.locator(f"[class*='{landmark}']")
            if await loc.count() == 0:
                return fmt.error(f"找不到 landmark={landmark}")
            await loc.first.scroll_into_view_if_needed(timeout=5000)
            await _settle_after_action(ts)
            return f"已滚动到区域 {landmark}。"
        if ref:
            if not REF_RE.fullmatch(ref):
                return fmt.error(f"ref 格式应为 e59 或 f2e191(iframe 内) (来自 browser_snapshot 输出), 收到: {ref!r}")
            loc = ts.page.locator(f"aria-ref={ref}")
            if await loc.count() == 0:
                return fmt.error(f"ref={ref} 已失效, 请重新 browser_snapshot 获取")
            await loc.first.scroll_into_view_if_needed(timeout=5000)
            await _settle_after_action(ts)
            return f"已滚动到 ref={ref}。"
        if selector:
            loc = ts.page.locator(selector)
            if await loc.count() == 0:
                return fmt.error(f"找不到选择器 {selector}")
            await loc.first.scroll_into_view_if_needed(timeout=5000)
            await _settle_after_action(ts)
            return f"已滚动到 {selector}。"
        return fmt.error("必须指定 landmark、ref 或 selector 中的至少一个参数")
    except Exception as e:
        return _op_error("滚动", e)


async def browser_wait_navigation(url_contains=None, timeout: int = 10000, *, task_id: str = ""):
    return await _wait_navigation(_default_task(task_id), url_contains, timeout)


async def _wait_navigation(task_id, url_contains, timeout) -> str:
    """事件驱动等待导航: nav_event 由 framenavigated/load/新标签事件置位。

    不用 wait_for_url: 它只等"未来"导航事件; B站等按回车开新标签时, 导航完成
    可能先于本次调用 → wait_for_url 永不触发。事件钩子由 core._hook_page 注册。
    """
    from time import monotonic

    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    deadline = monotonic() + timeout / 1000
    while True:
        ts.nav_event.clear()
        page = ts.page  # 每轮重取, 捕捉 _on_new_page 的新标签切换
        if url_contains:
            if url_contains in page.url:
                return f"页面已导航至包含 \"{url_contains}\" 的 URL: {page.url}"
        else:
            try:
                if await page.evaluate("document.readyState") == "complete":
                    return f"页面导航完成: {page.url}"
            except Exception:
                pass
        remaining = deadline - monotonic()
        if remaining <= 0:
            return fmt.warning(f"等待导航超时 ({timeout}ms)", detail=f"当前 URL: {ts.page.url}")
        try:
            await asyncio.wait_for(ts.nav_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return fmt.warning(f"等待导航超时 ({timeout}ms)", detail=f"当前 URL: {ts.page.url}")


async def browser_dismiss_popup(*, task_id: str = ""):
    return await _dismiss_popup(_default_task(task_id))


# 纯 dismissive 词表。授权类(接受/同意/accept)绝不自动点 —— 那是用户的决定, 该走 HITL。
_POPUP_DISMISS_TEXTS = ["×", "✕", "✖", "关闭", "close", "以后再说", "暂不", "稍后", "不用了", "不需要",
                        "later", "not now", "跳过", "skip", "我知道了", "知道了", "got it"]
_POPUP_ICON_CSS = ("[aria-label*='关闭' i]", "[aria-label*='close' i]", ".close")


async def _find_dismiss_control(scope) -> tuple[Any, str] | tuple[None, None]:
    """在给定域内找关闭控件: 按钮词表 → aria-label 关闭/close → .close 图标(SPA 自绘弹窗)。"""
    for pattern in _POPUP_DISMISS_TEXTS:
        loc = scope.get_by_role("button", name=pattern, exact=False)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first, f'按钮 "{pattern}"'
        except Exception:
            continue
    for css in _POPUP_ICON_CSS:
        try:
            loc = scope.locator(css)
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first, f"图标 {css}"
        except Exception:
            continue
    return None, None


async def _visible_dialogs(page) -> list:
    """当前可见的 dialog/alertdialog (DOM 里常驻隐藏弹窗, 必须过 is_visible)。"""
    dialogs = page.get_by_role("dialog").or_(page.get_by_role("alertdialog"))
    out = []
    try:
        for i in range(min(await dialogs.count(), 3)):
            d = dialogs.nth(i)
            if await d.is_visible():
                out.append(d)
    except Exception:
        pass
    return out


async def _dismiss_popup(task_id) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    page = ts.page
    clicked: list[str] = []
    last_sig = None
    try:
        for _ in range(3):  # 叠层弹窗: 每点掉一层重查一次
            vis = await _visible_dialogs(page)
            if not vis and clicked:
                break  # 弹窗已消失
            scope, where = (vis[0], "dialog 内") if vis else (page, "页面范围")
            control, label = await _find_dismiss_control(scope)
            if control is None and vis:
                # dialog 里没找到 → 页面级兜底 (SPA 弹窗不一定标 dialog role)
                control, label = await _find_dismiss_control(page)
                where = "页面范围"
            if control is None:
                break
            sig = (len(vis), label, where)
            if sig == last_sig:
                break  # 上一轮点击没让弹窗消失, 再点同一个控件无意义
            last_sig = sig
            await control.click(timeout=3000)
            clicked.append(f"{label}({where})")
            await page.wait_for_timeout(300)
        if clicked:
            if await _visible_dialogs(page):
                return ("已点击关闭控件: " + "、".join(clicked) +
                        "。但弹窗可能仍在(站点自绘关闭逻辑), 请 browser_snapshot 确认。")
            return "已关闭弹窗: 点击了 " + "、".join(clicked) + "。"
        await page.keyboard.press("Escape")
        return "未发现可点击的关闭控件, 已按下 Escape 键。"
    except Exception as e:
        return _op_error("关闭弹窗", e)


async def browser_list_pages(*, task_id: str = ""):
    return await _list_pages(_default_task(task_id))


async def _list_pages(task_id) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    pages = await _manager.list_pages(_sid(), task_id)
    if not pages:
        return "当前 task 没有打开的页面。"
    parts = ["## 打开的标签页"]
    for p in pages:
        marker = " ← 当前" if p["active"] else ""
        dead = " (已关闭,下次操作将自动重建)" if not p.get("alive", True) else ""
        parts.append(f"  [{p['index']}] \"{p['title']}\" ({p['url']}){marker}{dead}")
    return "\n".join(parts)


async def browser_switch_page(index: int, *, task_id: str = ""):
    return await _switch_page(_default_task(task_id), index)


async def _switch_page(task_id, index) -> str:
    err = await _require_task(task_id)
    if err:
        return err
    try:
        page = await _manager.switch_page(_sid(), task_id, index)
        return f"已切换到标签页 [{index}]: {await page.title()} ({page.url})"
    except ValueError as e:
        return fmt.error(str(e))


# ---------------------------------------------------------------------------
# 观测性工具: console / pageerror / network 元数据 (事件缓冲, since-cursor 增量)
# ---------------------------------------------------------------------------


def _fmt_event(e) -> str:
    if e.kind == KIND_NAV:
        return f"  #{e.seq} ── 导航: {e.text}"
    if e.kind == KIND_CONSOLE:
        loc = f"  at {e.location}" if e.location else ""
        return f"  #{e.seq} [{e.level}] {e.text}{loc}"
    if e.kind == KIND_PAGEERROR:
        return f"  #{e.seq} [pageerror] {e.text}"
    if e.kind == KIND_DIALOG:
        return f"  #{e.seq} [dialog:{e.level}] {e.text}"
    if e.kind == KIND_DOWNLOAD:
        url = f"  ← {e.url}" if e.url else ""
        return f"  #{e.seq} ⤓ 下载: {e.text}{url}"
    # request
    status = str(e.status) if e.status is not None else f"FAILED: {e.failure}"
    rt = f" ({e.resource_type})" if e.resource_type else ""
    return f"  #{e.seq} {e.method} {e.url} → {status}{rt}"


def _render_events(label: str, evs, buf, more: int, empty: str) -> str:
    if buf is None:
        return f"[{label}] 尚无事件缓冲 (页面建立后的事件才会被记录)。"
    head = [label]
    if evs:
        head.append(f"seq {evs[0].seq}-{evs[-1].seq}")
        head.append(f"本次 {len(evs)} 条")
    else:
        head.append("无新增")
    if more:
        head.append(f"另有 {more} 条, 再调一次继续")
    if buf.dropped:
        head.append(f"溢出已丢 {buf.dropped} 条")
    lines = ["[" + " | ".join(head) + "]"]
    if buf.dead:
        lines.append(f"[事件缓冲已失效: {buf.dead}]")
    if evs:
        lines.extend(_fmt_event(e) for e in evs)
    else:
        lines.append(empty)
    return "\n".join(lines)


def _clamp_limit(limit: int) -> int:
    try:
        return max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        return 50


async def browser_console(level=None, since=None, pattern=None, limit: int = 50, *, task_id: str = ""):
    return await _console(_default_task(task_id), level, since, pattern, limit)


async def _console(task_id, level, since, pattern, limit) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return fmt.error(f"pattern 非法正则: {pattern!r}")

    def match(e) -> bool:
        if e.kind == KIND_NAV:
            return True  # 分界事件不受过滤, 保留时间线参照
        if level and e.level != level:
            return False
        if rx and not (rx.search(e.text) or rx.search(e.location)):
            return False
        return True

    evs, buf, more = _manager.events.read(
        ts.page, f"console|{level}|{pattern}", since=since,
        kinds={KIND_CONSOLE, KIND_NAV}, match=match, limit=_clamp_limit(limit),
    )
    return _render_events("console", evs, buf, more, "无匹配 console 输出。")


async def browser_errors(since=None, limit: int = 50, *, task_id: str = ""):
    return await _errors(_default_task(task_id), since, limit)


async def _errors(task_id, since, limit) -> str:
    """一站式排障: 未捕获异常 + console.error + 失败请求, 按时间序。"""
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)

    def match(e) -> bool:
        if e.kind in (KIND_NAV, KIND_PAGEERROR, KIND_DIALOG, KIND_DOWNLOAD):
            return True
        if e.kind == KIND_CONSOLE:
            return e.level == "error"
        return e.failed  # request

    evs, buf, more = _manager.events.read(
        ts.page, "errors", since=since,
        kinds={KIND_PAGEERROR, KIND_CONSOLE, KIND_REQUEST, KIND_NAV, KIND_DIALOG, KIND_DOWNLOAD},
        match=match, limit=_clamp_limit(limit),
    )
    return _render_events("errors", evs, buf, more, "未发现异常或失败请求。")


async def browser_network(url_pattern=None, failed_only: bool = True, since=None, limit: int = 50, *, task_id: str = ""):
    return await _network(_default_task(task_id), url_pattern, failed_only, since, limit)


async def _network(task_id, url_pattern, failed_only, since, limit) -> str:
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    needle = url_pattern.lower() if url_pattern else ""

    def match(e) -> bool:
        if e.kind == KIND_NAV:
            return True
        if failed_only and not e.failed:
            return False
        if needle and needle not in e.url.lower():
            return False
        return True

    evs, buf, more = _manager.events.read(
        ts.page, f"network|{failed_only}|{url_pattern}", since=since,
        kinds={KIND_REQUEST, KIND_NAV}, match=match, limit=_clamp_limit(limit),
    )
    return _render_events("network", evs, buf, more, "无匹配请求记录。")


async def browser_network_body(seq: int, confirmed: bool = False, *, task_id: str = ""):
    return await _network_body(_default_task(task_id), seq, confirmed)


async def _network_body(task_id, seq, confirmed) -> str:
    """按需单取响应体: 总开关 + 逐次 confirmed 确认 + 硬 cap。body 绝不进审计。

    句柄时效: Response 句柄只在页面存活且未被淘汰时有效 (浏览器侧也可能驱逐)。
    """
    if not _settings.allow_network_body:
        return fmt.error("网络 body 读取未启用",
                         detail="默认关闭: 响应体可能携带敏感数据/注入内容",
                         hint="设置 BROWSER_ALLOW_NETWORK_BODY=true 以启用")
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    ev = _manager.events.find(ts.page, seq)
    if ev is None or ev.kind != KIND_REQUEST:
        return fmt.error(f"seq={seq} 不是当前页面的有效请求事件",
                         hint="先 browser_network(failed_only=false, since=0) 查有效 seq")
    if not confirmed:
        return ("CONFIRMATION_REQUIRED\n" + fmt.warning(
            f"将读取响应体: {ev.method} {ev.url} (HTTP {ev.status})",
            detail="响应体是页面方控制的内容, 可能含敏感数据或注入文本。"
                   "向用户展示该 URL, 用户同意后以 confirmed=true 重调。"))
    if ev.handle is None:
        return fmt.error(f"seq={seq} 的响应句柄已失效 (页面导航或句柄淘汰)",
                         hint="重新触发该请求后立即读取")
    try:
        body = await ev.handle.body()
    except Exception as e:
        return fmt.error(f"响应体读取失败: {e}",
                         hint="浏览器侧可能已驱逐该响应; 重新触发请求后立即读取")
    try:
        ctype = (ev.handle.headers or {}).get("content-type", "")
    except Exception:
        ctype = ""
    textual = any(t in ctype for t in ("json", "text", "html", "xml", "javascript", "css", "urlencoded"))
    if not textual:
        return f"[binary] {ev.method} {ev.url} → {len(body)} 字节 (content-type: {ctype or 'unknown'}, 不解码)"
    text = body.decode("utf-8", errors="replace")
    cap = _settings.network_body_cap
    if len(text) > cap:
        return f"{text[:cap]}\n\n... (已截断, 共 {len(text)} 字符, 上限 BROWSER_NETWORK_BODY_CAP={cap})"
    return text


async def browser_perf(*, task_id: str = ""):
    return await _perf(_default_task(task_id))

async def _perf(task_id) -> str:
    """Web Vitals + 最慢资源: 惰性注入采集器, 读取 window.__nexusVitals。"""
    err = await _require_task(task_id)  # Issue K: 未知 task 显式报错, 不静默建空 task
    if err:
        return err
    ts = await _manager.ensure_task(_sid(), task_id)
    try:
        await ensure_vitals(ts.page)
        data = await ts.page.evaluate("window.__nexusVitals")
    except Exception as e:
        return _op_error("读取性能数据", e)
    return format_vitals(data, ts.page.url)


# ---------------------------------------------------------------------------
# 生命周期工具
# ---------------------------------------------------------------------------


async def browser_tasks():
    sessions = _manager.list_sessions()
    sid = _sid()
    for s in sessions:
        if s.get("session_id") == sid:
            tasks = s.get("tasks") or []
            lines = [f"- {t['task_id']}: {t['pages']} 个页面" for t in tasks]
            return "\n".join(lines) if lines else "当前 session 无 task。"
    return "当前 session 无 task。"


async def browser_close_task(*, task_id: str = ""):
    return await _close_task(_default_task(task_id))


async def _close_task(task_id) -> str:
    await _manager.close_task(_sid(), task_id)
    return f"已关闭 task: {task_id}"


async def browser_list_sessions():
    sessions = _manager.list_sessions()
    if not sessions:
        return "无活跃 session。"
    return "\n".join(
        f"- {s['session_id']}: {s['task_count']} 个 task" for s in sessions
    )


async def browser_close_session():
    return await _close_session(_sid())


async def _close_session(sid) -> str:
    await _manager.close_session(sid)
    return f"已关闭 session: {sid}"


# ---------------------------------------------------------------------------
# MCP 注册
# ---------------------------------------------------------------------------


def build_server() -> tuple:
    """构造 MCPServer + 工具。返回 (server, tool_fns)。"""
    import functools

    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "nexus-browser",
        description="浏览器操控: 事件驱动确定性快照 + HITL/审计治理",
        version="0.3.0",
    )

    def register(fn, name, description, params: dict) -> None:
        @functools.wraps(fn)
        async def guarded(*args, _mcp_ctx: _MCPContext | None = None, **kwargs):
            # HTTP 多客户端: 从注入的 MCP Context 解析 session, 置位供 _sid() 读取
            tok = _session_var.set(_session_from_ctx(_mcp_ctx) or _session_id)
            try:
                return await _guarded_call(name, fn, args, kwargs)
            finally:
                _session_var.reset(tok)

        # functools.wraps 只复制 fn 的 __annotations__; SDK 的 find_context_parameter
        # 靠类型注解找 Context 注入位 → 必须显式重建注解表 (不污染 fn 自身)。
        guarded.__annotations__ = {**getattr(fn, "__annotations__", {}),
                                   "_mcp_ctx": _MCPContext | None}
        server.add_tool(guarded, name=name, description=description)
        _TOOLS[name] = (fn, params)

    # 工具描画 (从 browser_provider 迁移的精简版)
    _register_all(register)
    return server


async def _with_tool_timeout(fn, name: str, timeout_ms: int, args=(), kwargs=None) -> str:
    """单次工具调用的外层超时护栏: 超时返回 ERROR, 不挂死。"""
    kwargs = kwargs or {}
    try:
        return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError:
        return fmt.error(
            f"工具 {name} 执行超时 (>{timeout_ms}ms)",
            hint="页面可能无限加载或网络挂起。可重试, 或调大 BROWSER_TOOL_TIMEOUT_MS。",
        )


_TOOLS: dict[str, tuple] = {}


def _register_all(register) -> None:
    # 每个工具: (fn, name, description, param_schema)
    tools = [
        (browser_navigate, "browser_navigate",
         "导航浏览器到指定URL。返回页面标题、URL、readyState和页面结构概览。支持多task隔离。",
         {"type": "object", "properties": {
             "url": {"type": "string"}, "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "default": "load"},
             "task_id": {"type": "string", "description": "任务ID(可选,自动创建)"},
         }, "required": ["url"]}),
        (browser_snapshot, "browser_snapshot",
         "获取页面可访问性快照(Accessibility Tree), 返回结构化元素列表(含ref/box)。基于事件驱动确定性快照, 等待DOM静默窗口。"
         "reading/interactive 只看视口内; mode=full 自动包含全页面(视口外标注 offscreen)。"
         "reading 模式 = 交互元素 + 标题/段落等阅读节点 + 带文本的 listitem/cell (纯壳容器剔除)。",
         {"type": "object", "properties": {
             "scope": {"type": "string", "description": "限定区域: CSS selector 或快照 ref(如 e57)"},
             "mode": {"type": "string", "enum": ["interactive", "reading", "full"], "default": "reading"},
             "include_offscreen": {"type": "boolean", "default": False, "description": "包含视口外节点(full 模式默认开启)"},
             "wait_stable": {"type": "boolean", "default": True},
             "include_generic": {"type": "boolean", "default": False, "description": "抖音/B站等SPA页面非语义div时用"},
             "diff": {"type": "boolean", "default": True, "description": "与上次快照逐节点一致时只回'无变化'(旧ref仍有效); false=总是全量"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_press_key, "browser_press_key",
         "按下键盘键 (真实 CDP 事件, isTrusted=true)。Escape/Tab/Enter/方向键/组合键(如 Control+a)。用于无按钮可点的键盘流: 关闭模态框、快捷键、Tab 序导航。",
         {"type": "object", "properties": {
             "key": {"type": "string", "description": "Playwright 键名, 如 Escape / Tab / Control+a"},
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": ["key"]}),
        (browser_hover, "browser_hover",
         "悬停元素 (真实鼠标移动): 触发 CSS :hover 与 JS mouseenter。用于 hover 才展开的菜单/工具提示。",
         {"type": "object", "properties": {
             "ref": {"type": "string"}, "role": {"type": "string"}, "name": {"type": "string"},
             "selector": {"type": "string"}, "pos": {"type": "string"},
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_select_option, "browser_select_option",
         "在 <select> 下拉中选择 option (按 value 或 label)。",
         {"type": "object", "properties": {
             "values": {"type": "array", "items": {"type": "string"}, "description": "option 的 value 或 label 列表"},
             "ref": {"type": "string"}, "selector": {"type": "string"},
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": ["values"]}),
        (browser_upload_file, "browser_upload_file",
         "上传本地文件到 <input type=file>。文件出站操作, 每次需人工确认: 首次返回 CONFIRMATION_REQUIRED, 用户同意后以 confirmed=true 重调。",
         {"type": "object", "properties": {
             "paths": {"type": "array", "items": {"type": "string"}, "description": "本地文件绝对路径列表"},
             "ref": {"type": "string"}, "selector": {"type": "string"},
             "confirmed": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": ["paths"]}),
        (browser_navigate_back, "browser_navigate_back",
         "浏览器历史后退一页, 返回标题/URL。",
         {"type": "object", "properties": {
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_dialog_respond, "browser_dialog_respond",
         "处置挂起的 confirm/prompt 对话框 (页面弹出时任何工具回复会前置[对话框等待决策])。"
         "accept=true 替用户点'确定', 每次需 confirmed=true; accept=false 为 dismiss 可直接调。",
         {"type": "object", "properties": {
             "accept": {"type": "boolean"},
             "prompt_text": {"type": "string", "description": "prompt 对话框要填的文本"},
             "confirmed": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": ["accept"]}),
        (browser_drag, "browser_drag",
         "拖拽元素到目标 (真实输入管线, HTML5 dnd 及多数 pointer 系库可用)。",
         {"type": "object", "properties": {
             "from_ref": {"type": "string"}, "from_selector": {"type": "string"},
             "to_ref": {"type": "string"}, "to_selector": {"type": "string"},
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_click, "browser_click",
         "点击页面元素。定位优先级: pos坐标 → ref(快照句柄) → selector → role+name → name。wait_stable=true 点击后等 DOM 静默。"
         "button=right 右键(上下文菜单), middle 中键。点击触发下载时自动回报文件名与保存路径。",
         {"type": "object", "properties": {
             "pos": {"type": "string"}, "ref": {"type": "string", "description": "快照输出的 ref, 如 e12"},
             "role": {"type": "string"}, "name": {"type": "string"},
             "selector": {"type": "string"}, "double_click": {"type": "boolean", "default": False},
             "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
             "wait_stable": {"type": "boolean", "default": False},
             "confirmed": {"type": "boolean", "default": False, "description": "命中 HITL 且用户已同意时置 true 重调"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_type, "browser_type",
         "在输入框中键入文本。定位优先级: pos坐标 → ref(快照句柄) → selector → role+name(不中自动回退 placeholder/label/常见输入框CSS)。wait_stable=true 输入后等 DOM 静默。",
         {"type": "object", "properties": {
             "text": {"type": "string"}, "ref": {"type": "string"}, "role": {"type": "string"},
             "name": {"type": "string"}, "selector": {"type": "string"},
             "clear": {"type": "boolean", "default": True}, "press_enter": {"type": "boolean", "default": False},
             "pos": {"type": "string"}, "wait_stable": {"type": "boolean", "default": False},
             "confirmed": {"type": "boolean", "default": False, "description": "命中 HITL 且用户已同意时置 true 重调"},
             "task_id": {"type": "string"},
         }, "required": ["text"]}),
        (browser_read, "browser_read",
         "阅读页面元素的文本内容。不指定则读取整个页面body。"
         "流式回复: wait_stable=true 等 DOM 静默后一次读全(免轮询); "
         "follow=true+selector 增量跟踪(每次只返回新增部分, 缓冲溢出丢最旧并保留丢弃标记)。",
         {"type": "object", "properties": {
             "selector": {"type": "string"}, "ref": {"type": "string"},
             "max_chars": {"type": "integer", "default": 5000},
             "wait_stable": {"type": "boolean", "default": False, "description": "读前等 DOM 静默(流式回复停止生成)"},
             "max_wait_ms": {"type": "integer", "default": 10000, "description": "wait_stable 的等待预算, 上限=工具超时-5s"},
             "follow": {"type": "boolean", "default": False, "description": "增量流式跟踪, 需配合 selector"},
             "stream_id": {"type": "string", "description": "继续已有流(可选, 默认按 page+selector 复用)"},
             "full": {"type": "boolean", "default": False, "description": "follow 模式下返回缓冲全文而非增量"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_screenshot, "browser_screenshot",
         "截取页面截图, 保存文件返回路径。",
         {"type": "object", "properties": {
             "path": {"type": "string"}, "full_page": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_evaluate, "browser_evaluate",
         "执行JavaScript表达式。默认禁用(BROWSER_ALLOW_JS_EXECUTION); 开启后每次需人工确认(confirmed=true)。",
         {"type": "object", "properties": {
             "expression": {"type": "string"},
             "confirmed": {"type": "boolean", "default": False, "description": "用户已审阅表达式并同意时置 true"},
             "task_id": {"type": "string"},
         }, "required": ["expression"]}),
        (browser_wait, "browser_wait",
         "等待元素或文本出现。超时返回WARNING。",
         {"type": "object", "properties": {
             "role": {"type": "string"}, "name": {"type": "string"}, "ref": {"type": "string"},
             "text": {"type": "string"}, "timeout": {"type": "integer", "default": 5000},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_wait_stable, "browser_wait_stable",
         "等待页面 DOM 停止变化(静默窗口, 默认 800ms 无变异)。用于流式回复生成完毕、动画结束的判定。",
         {"type": "object", "properties": {
             "timeout_ms": {"type": "integer", "default": 10000, "description": "等待预算, 上限=工具超时-5s"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_wait_ms, "browser_wait_ms",
         "纯等待指定毫秒数(逃生舱: 动画/限流/延迟渲染)。上限=工具超时-5s。",
         {"type": "object", "properties": {
             "ms": {"type": "integer"}, "task_id": {"type": "string"},
         }, "required": ["ms"]}),
        (browser_scroll, "browser_scroll",
         "滚动页面。direction: up/down/left/right, amount: 像素(默认500)。",
         {"type": "object", "properties": {
             "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
             "amount": {"type": "integer", "default": 500}, "task_id": {"type": "string"},
         }, "required": []}),
        (browser_scroll_to, "browser_scroll_to",
         "滚动到指定元素。landmark(语义区域)→ref→selector。",
         {"type": "object", "properties": {
             "landmark": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_wait_navigation, "browser_wait_navigation",
         "等待click/submit触发的导航完成。",
         {"type": "object", "properties": {
             "url_contains": {"type": "string"}, "timeout": {"type": "integer", "default": 10000},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_dismiss_popup, "browser_dismiss_popup",
         "自动检测并关闭弹窗(登录/cookie同意/广告): 关闭按钮→取消→Escape。",
         {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []}),
        (browser_list_pages, "browser_list_pages",
         "列出当前task所有打开的标签页。",
         {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []}),
        (browser_switch_page, "browser_switch_page",
         "切换到指定索引的标签页。",
         {"type": "object", "properties": {"index": {"type": "integer"}, "task_id": {"type": "string"}}, "required": ["index"]}),
        (browser_console, "browser_console",
         "读取页面 console 输出(级别/文本/位置)。增量游标: 不传 since=接着上次读, since=0=全量; "
         "level 过滤(error/warning/log/...), pattern 正则过滤文本, limit 封顶(默认50, 超了再调一次继续)。",
         {"type": "object", "properties": {
             "level": {"type": "string", "description": "级别过滤, 如 error/warning"},
             "since": {"type": "integer", "description": "增量游标: 省略=上次读到位置, 0=全量"},
             "pattern": {"type": "string", "description": "正则过滤文本/位置"},
             "limit": {"type": "integer", "default": 50},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_errors, "browser_errors",
         "一站式排障: JS 未捕获异常(pageerror) + console.error + 失败请求(网络层失败或HTTP≥400)合并视图, 按时间排序。"
         "点了没反应/页面白屏时先调它。增量游标同 browser_console。",
         {"type": "object", "properties": {
             "since": {"type": "integer", "description": "增量游标: 省略=上次读到位置, 0=全量"},
             "limit": {"type": "integer", "default": 50},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_network, "browser_network",
         "读取网络请求元数据(method/url/status/资源类型; 不含 body)。"
         "failed_only 默认 true 只看失败; url_pattern 子串过滤; 增量游标同上。",
         {"type": "object", "properties": {
             "url_pattern": {"type": "string", "description": "URL 子串过滤"},
             "failed_only": {"type": "boolean", "default": True},
             "since": {"type": "integer", "description": "增量游标: 省略=上次读到位置, 0=全量"},
             "limit": {"type": "integer", "default": 50},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_perf, "browser_perf",
         "读取页面性能指标: FCP/LCP/CLS/INP + TTFB/DOMContentLoaded/load + 最慢 5 条资源。"
         "页面慢/加载异常时用它定位是后端慢(TTFB)还是资源重。SPA 导航不重置计时, 注意输出来源 URL。",
         {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []}),
        (browser_network_body, "browser_network_body",
         "按 seq 读取单个请求的响应体(需 BROWSER_ALLOW_NETWORK_BODY=true)。文本类解码, 二进制只报字节数; "
         "单条上限 BROWSER_NETWORK_BODY_CAP。需 confirmed=true 二次确认 —— 响应体是页面方控制内容, 先把 URL 给用户看。",
         {"type": "object", "properties": {
             "seq": {"type": "integer", "description": "browser_network 输出中的 #N"},
             "confirmed": {"type": "boolean", "default": False, "description": "用户已同意读取该 URL 的响应体时置 true"},
             "task_id": {"type": "string"},
         }, "required": ["seq"]}),
        (browser_tasks, "browser_tasks",
         "列出当前session的所有task及其页面数。",
         {"type": "object", "properties": {}, "required": []}),
        (browser_close_task, "browser_close_task",
         "关闭指定task的浏览器资源(page/context)。",
         {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": []}),
        (browser_list_sessions, "browser_list_sessions",
         "列出所有活跃session。",
         {"type": "object", "properties": {}, "required": []}),
        (browser_close_session, "browser_close_session",
         "关闭当前session的全部task资源。",
         {"type": "object", "properties": {}, "required": []}),
    ]
    for fn, name, desc, schema in tools:
        register(fn, name, desc, schema)


def _token_guard(app, token: str):
    """Bearer token 校验的 ASGI 包装: 无/错 token → 401。lifespan 等非 http scope 透传。"""
    from starlette.responses import PlainTextResponse

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)

    return wrapped


def main() -> None:
    """uvx 入口: 按 BROWSER_TRANSPORT 启动 stdio / streamable-http MCP 服务器。"""
    import asyncio

    server = build_server()
    s = _settings
    if s.transport == "stdio":
        asyncio.run(server.run_stdio_async())
        return
    host = s.http_host
    if host not in ("127.0.0.1", "localhost", "::1") and not s.http_token:
        raise SystemExit(
            "拒绝启动: HTTP 绑定非 localhost 且未设 BROWSER_HTTP_TOKEN。"
            "浏览器控制端口裸奔等于把本机浏览器交给网络。"
        )
    app = server.streamable_http_app(host=host, json_response=True)
    if s.http_token:
        app = _token_guard(app, s.http_token)
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("HTTP 传输需要 uvicorn: pip install uvicorn")
    asyncio.run(uvicorn.Server(
        uvicorn.Config(app, host=host, port=s.http_port, log_level="warning")
    ).serve())


if __name__ == "__main__":
    main()


