"""MCP 服务器: 17 个浏览器工具 + session/task 生命周期 + 治理门。

session 模型: 一个 MCP 连接 = 一个 session_id (服务器启动生成, uuid4)。
同 session 内多个 task_id 各自隔离 (isolated: 独立 BrowserContext; cdp: 独立 Page)。
task_id 不传时自动生成默认 task。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from time import monotonic
from typing import Any

from nexus_browser import fmt
from nexus_browser.core import BrowserManager
from nexus_browser.gates import AuditLogger, hitl_required
from nexus_browser.settings import BrowserSettings
from nexus_browser.snapshot import assemble_snapshot, ensure_watcher, get_stable_tree, wait_dom_settled

logger = logging.getLogger(__name__)

_settings = BrowserSettings()
_session_id = "conn-" + uuid.uuid4().hex[:12]
_manager = BrowserManager(_settings)
_audit = AuditLogger(_settings.resolve_audit_path())


def tool_names() -> list[str]:
    return [
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_read", "browser_screenshot", "browser_evaluate", "browser_wait",
        "browser_wait_stable", "browser_wait_ms",
        "browser_scroll", "browser_scroll_to", "browser_wait_navigation",
        "browser_dismiss_popup", "browser_list_pages", "browser_switch_page",
        "browser_tasks", "browser_close_task",
        "browser_list_sessions", "browser_close_session",
    ]


def _default_task(task_id: str) -> str:
    return task_id or "default"


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
    try:
        notice = _manager.pop_notice(_session_id, task_id)
    except Exception:
        notice = None
    if notice and not result.startswith(notice):
        return notice + "\n" + result
    return result


def _hitl_block(action: str, role, name, task_id) -> str | None:
    if hitl_required(_settings.hitl_rules, action, role, name):
        return (
            fmt.warning(f"操作需要人工确认: {action} role={role} name={name}")
            + "\nCONFIRMATION_REQUIRED"
        )
    return None


def _record(tool: str, task_id: str, params: dict, risk: str, hitl: bool = False, error: str | None = None) -> None:
    try:
        _audit.log(_session_id, task_id, tool, params, risk, hitl_triggered=hitl, error=error)
    except Exception as e:  # 审计失败不影响工具执行
        logger.warning("audit failed: %s", e)


# ---------------------------------------------------------------------------
# 快照辅助: 格式化 get_stable_tree 结果为 LLM 可读文本
# ---------------------------------------------------------------------------


async def _snapshot_text(task_id: str, scope=None, mode="reading", include_offscreen=False,
                         include_generic=False, wait_stable=True) -> str:
    mgr = _manager
    ts = await mgr.ensure_task(_session_id, task_id)
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
    return "\n".join(parts)


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
    result = await _manager.navigate(_session_id, task_id, url, wait_until)
    _record("browser_navigate", task_id, {"url": url}, "low")
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
        skeleton = await _snapshot_text(task_id, mode="interactive", wait_stable=False)
        parts.append(f"\n## 页面快照\n{skeleton}")
    except Exception:
        pass
    return "\n".join(parts)


async def browser_snapshot(scope: str | None = None, mode: str = "reading",
                      include_offscreen: bool = False, wait_stable: bool = True,
                      include_generic: bool = False, *, task_id: str = ""):
    return await _snapshot_text(_default_task(task_id), scope, mode,
                                include_offscreen, include_generic, wait_stable)


async def browser_click(ref=None, role=None, name=None, selector=None, double_click=False,
                   pos=None, wait_stable: bool = False, *, task_id: str = ""):
    tid = _default_task(task_id)
    block = _hitl_block("click", role, name, tid)
    if block:
        return block
    return await _click(tid, ref, role, name, selector, double_click, pos, wait_stable)


async def _click(task_id, ref, role, name, selector, double_click, pos, wait_stable=False) -> str:
    ts = await _manager.ensure_task(_session_id, task_id)
    try:
        locator = await _manager.find_element(_session_id, task_id, ref=ref, role=role,
                                              name=name, selector=selector, pos=pos)
        if isinstance(locator, str):  # pos: 坐标点击
            x, y, w, h = (int(p) for p in locator.split(","))
            cx, cy = x + w // 2, y + h // 2
            await ts.page.mouse.dblclick(cx, cy) if double_click else await ts.page.mouse.click(cx, cy)
            if wait_stable:
                await _settle_after_action(ts)
            return f"已点击坐标 ({cx}, {cy})。"
        if double_click:
            await locator.dblclick(timeout=5000)
        else:
            await locator.click(timeout=5000)
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
                  wait_stable: bool = False, *, task_id: str = ""):
    tid = _default_task(task_id)
    block = _hitl_block("type", role, name, tid)
    if block:
        return block
    return await _type(tid, text, ref, role, name, selector, clear, press_enter, pos, wait_stable)


async def _type(task_id, text, ref, role, name, selector, clear, press_enter, pos, wait_stable=False) -> str:
    ts = await _manager.ensure_task(_session_id, task_id)
    try:
        locator = await _manager.find_element(_session_id, task_id, ref=ref, role=role,
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
        if clear:
            await locator.clear(timeout=5000)
        await locator.fill(text, timeout=5000)
        if press_enter:
            await locator.press("Enter")
        if wait_stable:
            await _settle_after_action(ts)
        return "已输入文本。"
    except ValueError as e:
        return fmt.error(str(e))
    except Exception as e:
        return _op_error("输入", e)


async def browser_read(selector=None, ref=None, max_chars: int = 5000,
                  wait_stable: bool = False, max_wait_ms: int = 10000,
                  follow: bool = False, stream_id: str = "", full: bool = False, *, task_id: str = ""):
    return await _read(_default_task(task_id), selector, ref, max_chars,
                       wait_stable, max_wait_ms, follow, stream_id, full)


def _wait_budget(max_wait_ms: int) -> int:
    """等待预算硬上限: 给外层超时护栏留 5s 余量, 否则 wait 参数形同虚设。"""
    return min(max_wait_ms, max(1000, _settings.tool_timeout_ms - 5000))


async def _read(task_id, selector, ref, max_chars, wait_stable, max_wait_ms, follow, stream_id, full) -> str:
    ts = await _manager.ensure_task(_session_id, task_id)
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
            locator = await _manager.find_element(_session_id, task_id, ref=ref)
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

    ts = await _manager.ensure_task(_session_id, task_id)
    if not path:
        ss_dir = _settings.screenshot_dir or str(Path.home() / ".nexus-browser" / "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        path = os.path.join(ss_dir, f"screenshot_{uuid.uuid4().hex[:8]}.png")
    try:
        await ts.page.screenshot(path=path, full_page=full_page)
        return f"截图已保存: {path}"
    except Exception as e:
        return _op_error("截图", e)


async def browser_evaluate(expression: str, *, task_id: str = ""):
    if not _settings.allow_js_execution:
        return fmt.error("JS 执行未启用", detail="当前配置禁止执行 JavaScript",
                         hint="设置 BROWSER_ALLOW_JS_EXECUTION=true 以启用")
    return "CONFIRMATION_REQUIRED\n" + fmt.warning(
        "browser_evaluate 无条件人工确认", detail=f"expression={expression!r}",)


async def browser_wait(role=None, name=None, ref=None, text=None, timeout: int = 5000, *, task_id: str = ""):
    return await _wait(_default_task(task_id), role, name, ref, text, timeout)


async def _wait(task_id, role, name, ref, text, timeout) -> str:
    if not any([role, name, ref, text]):
        return fmt.error("必须指定 role+name、ref 或 text 中的至少一个参数")
    from time import monotonic

    from nexus_browser.snapshot import get_stable_tree

    deadline = monotonic() + timeout / 1000
    ts = await _manager.ensure_task(_session_id, task_id)
    while monotonic() < deadline:
        nodes = await get_stable_tree(ts.page, None, _settings, task=ts)
        for node in nodes:
            if ref and node.get("ref") == ref:
                return f"元素 ref={ref} 已出现。"
            if role and name and node.get("role") == role and name.lower() in node.get("name", "").lower():
                return f"元素 role={role} name={name} 已出现。"
            if text and text.lower() in node.get("name", "").lower():
                return f"文本 \"{text}\" 已出现。"
        await asyncio.sleep(0.4)
    return fmt.warning(f"等待超时 ({timeout}ms): 未找到匹配元素")


async def browser_wait_stable(timeout_ms: int = 10000, *, task_id: str = ""):
    return await _wait_stable(_default_task(task_id), timeout_ms)


async def _wait_stable(task_id, timeout_ms) -> str:
    """等 DOM 静默窗口: 流式回复/连续动画"停止变化"的判定原语。"""
    ts = await _manager.ensure_task(_session_id, task_id)
    budget = _wait_budget(timeout_ms)
    await ensure_watcher(ts.page, _settings)
    t0 = monotonic()
    await wait_dom_settled(ts.page, _settings, task=ts, timeout_ms=budget)
    waited = (monotonic() - t0) * 1000
    _record("browser_wait_stable", task_id, {"timeout_ms": timeout_ms}, "low")
    if waited >= budget:
        return fmt.warning(
            f"等待稳定超时 ({budget}ms): 页面仍在变化",
            detail="上限 = BROWSER_TOOL_TIMEOUT_MS - 5s; 可调大 timeout_ms 或 BROWSER_TOOL_TIMEOUT_MS",
        )
    return f"页面已稳定 (静默 ≥{_settings.stable_window_ms}ms, 耗时 {int(waited)}ms)。"


async def browser_wait_ms(ms: int, *, task_id: str = ""):
    tid = _default_task(task_id)
    cap = _wait_budget(ms)
    if ms > cap:
        return fmt.error(
            f"等待时长超限: {ms}ms > {cap}ms",
            hint="上限 = BROWSER_TOOL_TIMEOUT_MS - 5s; 更长等待请调大 BROWSER_TOOL_TIMEOUT_MS, 或用 browser_wait_stable",
        )
    await asyncio.sleep(ms / 1000)
    _record("browser_wait_ms", tid, {"ms": ms}, "low")
    return f"已等待 {ms}ms。"


async def _page_of(task_id):

    ts = await _manager.ensure_task(_session_id, task_id)
    return ts.page


async def browser_scroll(direction: str = "down", amount: int = 500, *, task_id: str = ""):
    return await _scroll(_default_task(task_id), direction, amount)


async def _scroll(task_id, direction, amount) -> str:
    if direction not in ("up", "down", "left", "right"):
        return fmt.error(f"不支持的滚动方向: {direction}")
    ts = await _manager.ensure_task(_session_id, task_id)
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
    ts = await _manager.ensure_task(_session_id, task_id)
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
            if not re.fullmatch(r"e\d+", ref):
                return fmt.error(f"ref 格式应为 e+数字 (来自 browser_snapshot 输出), 收到: {ref!r}")
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

    ts = await _manager.ensure_task(_session_id, task_id)
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
    ts = await _manager.ensure_task(_session_id, task_id)
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
            _record("browser_dismiss_popup", task_id, {"clicked": clicked}, "low")
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
    pages = await _manager.list_pages(_session_id, task_id)
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
    try:
        page = await _manager.switch_page(_session_id, task_id, index)
        return f"已切换到标签页 [{index}]: {await page.title()} ({page.url})"
    except ValueError as e:
        return fmt.error(str(e))


# ---------------------------------------------------------------------------
# 生命周期工具
# ---------------------------------------------------------------------------


async def browser_tasks():
    sessions = _manager.list_sessions()
    sid = _session_id
    for s in sessions:
        if s.get("session_id") == sid:
            tasks = s.get("tasks") or []
            lines = [f"- {t['task_id']}: {t['pages']} 个页面" for t in tasks]
            return "\n".join(lines) if lines else "当前 session 无 task。"
    return "当前 session 无 task。"


async def browser_close_task(*, task_id: str = ""):
    return await _close_task(_default_task(task_id))


async def _close_task(task_id) -> str:
    await _manager.close_task(_session_id, task_id)
    return f"已关闭 task: {task_id}"


async def browser_list_sessions():
    sessions = _manager.list_sessions()
    if not sessions:
        return "无活跃 session。"
    return "\n".join(
        f"- {s['session_id']}: {s['task_count']} 个 task" for s in sessions
    )


async def browser_close_session():
    return await _close_session(_session_id)


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
        version="0.1.0",
    )

    def register(fn, name, description, params: dict) -> None:
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            tid = kwargs.get("task_id") or "default"
            _manager.pin(_session_id, tid)  # 操作期间钉住, TTL 不回收
            try:
                result = await _with_tool_timeout(fn, name, _settings.tool_timeout_ms, args, kwargs)
            finally:
                _manager.unpin(_session_id, tid)
            return _attach_notice(result, tid)

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
         "reading/interactive 只看视口内; mode=full 自动包含全页面(视口外标注 offscreen)。",
         {"type": "object", "properties": {
             "scope": {"type": "string", "description": "限定区域: CSS selector 或快照 ref(如 e57)"},
             "mode": {"type": "string", "enum": ["interactive", "reading", "full"], "default": "reading"},
             "include_offscreen": {"type": "boolean", "default": False, "description": "包含视口外节点(full 模式默认开启)"},
             "wait_stable": {"type": "boolean", "default": True},
             "include_generic": {"type": "boolean", "default": False, "description": "抖音/B站等SPA页面非语义div时用"},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_click, "browser_click",
         "点击页面元素。定位优先级: pos坐标 → ref(快照句柄) → selector → role+name → name。wait_stable=true 点击后等 DOM 静默。",
         {"type": "object", "properties": {
             "pos": {"type": "string"}, "ref": {"type": "string", "description": "快照输出的 ref, 如 e12"},
             "role": {"type": "string"}, "name": {"type": "string"},
             "selector": {"type": "string"}, "double_click": {"type": "boolean", "default": False},
             "wait_stable": {"type": "boolean", "default": False},
             "task_id": {"type": "string"},
         }, "required": []}),
        (browser_type, "browser_type",
         "在输入框中键入文本。定位优先级: pos坐标 → ref(快照句柄) → selector → role+name(不中自动回退 placeholder/label/常见输入框CSS)。wait_stable=true 输入后等 DOM 静默。",
         {"type": "object", "properties": {
             "text": {"type": "string"}, "ref": {"type": "string"}, "role": {"type": "string"},
             "name": {"type": "string"}, "selector": {"type": "string"},
             "clear": {"type": "boolean", "default": True}, "press_enter": {"type": "boolean", "default": False},
             "pos": {"type": "string"}, "wait_stable": {"type": "boolean", "default": False},
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
         "执行JavaScript表达式。默认禁用; 开启后无条件人工确认。",
         {"type": "object", "properties": {
             "expression": {"type": "string"}, "task_id": {"type": "string"},
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


def main() -> None:
    """uvx 入口: 启动 stdio MCP 服务器。"""
    import asyncio

    server = build_server()
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()


