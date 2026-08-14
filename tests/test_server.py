"""server 层行为测试: 工具注册 + HITL 拦截 + session/task 注入。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import nexus_browser.server as server_mod
from nexus_browser.settings import BrowserSettings


@pytest.fixture
def patch_server(monkeypatch, tmp_path):
    """用假 manager/settings 替换 server 内部依赖, 直接测工具函数。"""
    audit_path = tmp_path / "audit.jsonl"
    settings = BrowserSettings(audit_path=str(audit_path))
    manager = FakeManager()
    monkeypatch.setattr(server_mod, "_settings", settings)
    monkeypatch.setattr(server_mod, "_manager", manager)
    monkeypatch.setattr(server_mod, "_audit", server_mod.AuditLogger(str(audit_path)))
    return manager, settings


class FakeTask:
    def __init__(self, task_id):
        self.task_id = task_id
        self.closed = False
        self.navigate_calls = 0
        self.settle_count = 0
        self.settle_event = asyncio.Event()
        self.nav_event = asyncio.Event()
        self.page = AsyncMock()
        self.page.url = "https://example.com"
        # locator 返回可 await 的 aria_snapshot, 支持 get_stable_tree
        loc = AsyncMock()
        loc.aria_snapshot = AsyncMock(return_value='- button "Login" [ref=s1e3]\n')
        loc.inner_text = AsyncMock(return_value="Hello")
        self.page.locator = MagicMock(return_value=loc)
        self.page.get_by_role = MagicMock(return_value=loc)
        self.page.get_by_text = MagicMock(return_value=loc)

    async def navigate(self, *a, **kw):
        self.navigate_calls += 1
        return {"url": "https://example.com", "title": "Example", "readyState": "complete", "timed_out": False}


class FakeManager:
    """记录 task_id 注入, 不真连浏览器。"""

    def __init__(self):
        self.tasks: dict[str, FakeTask] = {}
        from nexus_browser.streams import StreamStore
        self.streams = StreamStore()

    async def navigate(self, session_id, task_id, url, wait_until="load"):
        task = await self.ensure_task(session_id, task_id)
        task.navigate_calls += 1
        return {"url": "https://example.com", "title": "Example", "readyState": "complete", "timed_out": False}

    async def get_page(self, session_id, task_id):
        return FakeTask(task_id)

    async def ensure_task(self, session_id, task_id):
        if task_id not in self.tasks:
            self.tasks[task_id] = FakeTask(task_id)
        return self.tasks[task_id]

    async def close_task(self, session_id, task_id):
        if task_id in self.tasks:
            self.tasks[task_id].closed = True
            del self.tasks[task_id]

    def list_sessions(self):
        return [{"session_id": "s", "task_count": len(self.tasks)}]


async def test_session_id_stable_across_calls(monkeypatch):
    """同一服务器进程 session_id 恒定。"""
    s1 = server_mod._session_id  # module 级, fixture 不换
    assert s1  # 非空


async def test_navigate_auto_creates_task(patch_server):
    mgr, _ = patch_server
    result = await server_mod.browser_navigate("https://example.com", task_id="task-x")
    assert "已导航至" in result or "Example" in result
    assert "task-x" in mgr.tasks


async def test_navigate_default_task_id(patch_server):
    """不传 task_id → 自动生成默认 task。"""
    mgr, _ = patch_server
    result = await server_mod.browser_navigate("https://example.com")
    assert list(mgr.tasks.keys())[0]  # 有默认 task
    assert isinstance(result, str)


async def test_hitl_blocks_matching_click(patch_server):
    """点击匹配 HITL 规则 → 返回 CONFIRMATION_REQUIRED。"""
    mgr, settings = patch_server
    settings.hitl_rules = [{"action": "click", "name_pattern": "支付|确认"}]
    out = await server_mod.browser_click(role="button", name="确认支付", task_id="t")
    assert "CONFIRMATION_REQUIRED" in out


async def test_hitl_non_match_passes(patch_server):
    mgr, settings = patch_server
    settings.hitl_rules = [{"action": "click", "name_pattern": "支付"}]
    # browser_click 需要真实 manager 点击逻辑; FakeManager 无 find_element
    # → 这里只验证 HITL 层未拦截 (返回应是 ERROR, 而非 CONFIRMATION_REQUIRED)
    out = await server_mod.browser_click(role="button", name="登录", task_id="t")
    assert "CONFIRMATION_REQUIRED" not in out


async def test_evaluate_requires_config(patch_server):
    mgr, settings = patch_server
    settings.allow_js_execution = False
    out = await server_mod.browser_evaluate("1+1", task_id="t")
    assert "JS 执行未启用" in out


async def test_evaluate_hitl_when_enabled(patch_server):
    mgr, settings = patch_server
    settings.allow_js_execution = True
    out = await server_mod.browser_evaluate("1+1", task_id="t")
    assert "CONFIRMATION_REQUIRED" in out


async def test_tools_listed(patch_server):
    names = server_mod.tool_names()
    assert "browser_navigate" in names
    assert "browser_snapshot" in names
    assert "browser_tasks" in names
    assert "browser_close_task" in names
    assert "browser_list_sessions" in names
    assert len(names) >= 17


async def test_close_task_lifecycle(patch_server):
    mgr, _ = patch_server
    await server_mod.browser_navigate("https://x.com", task_id="t1")
    out = await server_mod.browser_close_task(task_id="t1")
    assert "t1" not in mgr.tasks
    assert isinstance(out, str)


async def test_audit_written_on_tool_call(patch_server):
    mgr, settings = patch_server
    await server_mod.browser_navigate("https://example.com", task_id="tx")
    lines = settings.resolve_audit_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    assert "browser_navigate" in lines[0]


async def test_tool_timeout_returns_error_not_hang():
    """slow 工具超时后返回 ERROR, 不挂死。"""
    from nexus_browser import server as s

    async def slow_tool(**kw):
        await asyncio.sleep(10)

    out = await s._with_tool_timeout(slow_tool, "browser_slow", timeout_ms=100)
    assert "超时" in out
    assert "browser_slow" in out


async def test_tool_timeout_pass_through_normal():
    """正常快速工具透传结果。"""
    from nexus_browser import server as s

    async def fast_tool(**kw):
        return "ok"

    out = await s._with_tool_timeout(fast_tool, "browser_fast", timeout_ms=5000)
    assert out == "ok"


async def test_wait_navigation_initial_url_match(patch_server):
    """URL 已匹配 → 立即返回, 无等待。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.url = "https://search.bilibili.com/all?keyword=python"
    out = await server_mod.browser_wait_navigation(url_contains="search", task_id="t")
    assert "已导航至" in out


async def test_wait_navigation_event_wakeup(patch_server):
    """事件驱动: nav_event 置位 + URL 变化 → 唤醒返回, 非定时轮询。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.url = "about:blank"

    async def later():
        await asyncio.sleep(0.05)
        task.page.url = "https://search.bilibili.com/all?keyword=python"
        task.nav_event.set()

    asyncio.create_task(later())
    import time

    t0 = time.time()
    out = await server_mod.browser_wait_navigation(url_contains="search", timeout=3000, task_id="t")
    assert "已导航至" in out
    assert time.time() - t0 < 1.0  # 事件唤醒, 非轮询到超时


# ── H-1/H-2: 流式读取 + 等待参数 ────────────────────────────────────


async def test_new_tools_listed(patch_server):
    names = server_mod.tool_names()
    assert "browser_wait_stable" in names
    assert "browser_wait_ms" in names
    assert len(names) == 20


async def test_read_follow_creates_stream_and_returns_delta(patch_server):
    """follow: 首次建立流返回全文, 后续只回增量, full 取缓冲全文。"""
    mgr, _ = patch_server
    out1 = await server_mod.browser_read(selector=".msg", follow=True, task_id="t")
    assert "流 " in out1 and "+5 字符" in out1 and "Hello" in out1

    out2 = await server_mod.browser_read(selector=".msg", follow=True, task_id="t")
    assert "无新增内容" in out2

    task = mgr.tasks["t"]
    task.page.locator.return_value.inner_text = AsyncMock(return_value="Hello world")
    out3 = await server_mod.browser_read(selector=".msg", follow=True, task_id="t")
    assert "+6 字符" in out3 and " world" in out3 and "Hello world" not in out3

    out4 = await server_mod.browser_read(selector=".msg", follow=True, full=True, task_id="t")
    assert "Hello world" in out4


async def test_read_follow_resumes_by_stream_id(patch_server):
    mgr, _ = patch_server
    await server_mod.browser_read(selector=".msg", follow=True, task_id="t")
    sid = next(iter(mgr.streams._streams))
    out2 = await server_mod.browser_read(follow=True, stream_id=sid, task_id="t")
    assert f"流 {sid}" in out2
    assert "Hello" not in out2 or "无新增" in out2  # 无变化 → 无新增


async def test_read_follow_requires_selector(patch_server):
    out = await server_mod.browser_read(follow=True, task_id="t")
    assert "必须提供 selector" in out


async def test_read_follow_invalid_stream_id(patch_server):
    out = await server_mod.browser_read(follow=True, stream_id="nope", task_id="t")
    assert "不存在" in out


async def test_wait_ms_caps_at_tool_timeout(patch_server):
    out = await server_mod.browser_wait_ms(999999, task_id="t")
    assert "超限" in out


async def test_wait_ms_sleeps(patch_server):
    out = await server_mod.browser_wait_ms(10, task_id="t")
    assert "已等待 10ms" in out


async def test_wait_stable_fast_path_when_quiet(patch_server):
    """页面早已静默 → 立即返回"已稳定", 不傻等。"""
    import time as _t

    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.evaluate = AsyncMock(return_value=_t.time() * 1000 - 100_000)  # 最后变异在 100s 前
    out = await server_mod.browser_wait_stable(2000, task_id="t")
    assert "已稳定" in out


async def test_attach_notice_prepends_state_change(patch_server, monkeypatch):
    """自愈发生 → 状态变更前置到工具返回, agent 感知世界变了。"""
    monkeypatch.setattr(server_mod._manager, "pop_notice",
                        lambda s, t: "[状态变更] 页面已被外部关闭, 已自动重建。", raising=False)
    out = server_mod._attach_notice("正文", "default")
    assert out.startswith("[状态变更]")
    assert "正文" in out


async def test_attach_notice_noop_without_notice(patch_server):
    """无状态变更 → 原文返回 (FakeManager 无 pop_notice 也不炸)。"""
    assert server_mod._attach_notice("正文", "default") == "正文"


async def test_op_error_classifies_death():
    """底层 closed/crashed 异常 → 人话 + 重试指引, 不泄漏 Playwright 原文。"""
    out = server_mod._op_error("点击", Exception("Target page, context or browser has been closed"))
    assert "外部关闭" in out and "重试" in out
    out2 = server_mod._op_error("点击", Exception("timeout 5000ms exceeded"))
    assert "timeout" in out2 and "外部关闭" not in out2


# ── 弹窗重做 (Issue I) / scroll_to ref / snapshot scope ─────────────


def _popup_page(*, dialog_button="关闭", icon_only=False, consent_only=False):
    """构造弹窗假页面。state['open'] 模拟弹窗随点击消失。"""
    state = {"open": True}
    btn = MagicMock()
    btn.is_visible = AsyncMock(return_value=True)

    async def _click(timeout=0):
        state["open"] = False

    btn.click = _click

    def btn_query(role, name=None, exact=False):
        loc = MagicMock()
        hit = False
        if role == "button":
            if consent_only:
                hit = name == "同意"
            elif dialog_button and not icon_only:
                hit = name == dialog_button
        loc.count = AsyncMock(return_value=1 if hit else 0)
        loc.first = btn
        return loc

    def css_query(sel):
        loc = MagicMock()
        hit = icon_only and sel == ".close"
        loc.count = AsyncMock(return_value=1 if hit else 0)
        loc.first = btn
        return loc

    dialog_el = MagicMock()
    dialog_el.is_visible = AsyncMock(side_effect=lambda: state["open"])
    dialog_el.get_by_role = btn_query
    dialog_el.locator = css_query

    dialogs = MagicMock()
    dialogs.count = AsyncMock(side_effect=lambda: 1 if state["open"] else 0)
    dialogs.nth = MagicMock(side_effect=lambda i: dialog_el)

    def page_gbr(role, name=None, exact=False):
        if role in ("dialog", "alertdialog"):
            loc = MagicMock()
            loc.or_ = MagicMock(return_value=dialogs)
            return loc
        return btn_query(role, name)

    page = MagicMock()
    page.get_by_role = page_gbr
    page.locator = css_query
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    return page, btn


async def test_dismiss_popup_clicks_dialog_button(patch_server):
    """可见 dialog 内的"关闭"按钮 → 点击并报告。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page, btn = _popup_page(dialog_button="关闭")
    out = await server_mod.browser_dismiss_popup(task_id="t")
    assert "已关闭弹窗" in out and "关闭" in out
    task.page.keyboard.press.assert_not_called()


async def test_dismiss_popup_icon_close_no_button_role(patch_server):
    """SPA 自绘弹窗: 关闭钮是无 button role 的图标 → aria-label/.close 选择器命中。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page, btn = _popup_page(dialog_button=None, icon_only=True)
    out = await server_mod.browser_dismiss_popup(task_id="t")
    assert "已关闭弹窗" in out and ".close" in out


async def test_dismiss_popup_never_clicks_consent(patch_server):
    """治理: "同意"按钮绝不自动点 —— 授权是用户的决定。找不到 dismissive 控件 → Escape。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page, _btn = _popup_page(consent_only=True)
    out = await server_mod.browser_dismiss_popup(task_id="t")
    assert "Escape" in out
    assert "已关闭弹窗" not in out  # 同意按钮未被点击


async def test_dismiss_popup_escape_when_nothing(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page, _ = _popup_page(dialog_button=None)
    out = await server_mod.browser_dismiss_popup(task_id="t")
    assert "Escape" in out
    task.page.keyboard.press.assert_awaited_with("Escape")


async def test_scroll_to_ref_uses_aria_ref_engine(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    loc.first = MagicMock()
    loc.first.scroll_into_view_if_needed = AsyncMock()
    task.page.locator = MagicMock(return_value=loc)
    out = await server_mod.browser_scroll_to(ref="e9", task_id="t")
    task.page.locator.assert_called_with("aria-ref=e9")
    assert "已滚动到 ref=e9" in out


async def test_snapshot_scope_ref_accepted(patch_server):
    """scope 传快照 ref (e57) 不再报语法错误 (Issue F)。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.evaluate = AsyncMock(return_value=1)  # 早已静默
    out = await server_mod.browser_snapshot(scope="e57", task_id="t")
    assert "ERROR" not in out
    task.page.locator.assert_called_with("aria-ref=e57")


async def test_snapshot_scope_miss_clear_error(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.evaluate = AsyncMock(return_value=1)
    task.page.locator.return_value.aria_snapshot = AsyncMock(
        side_effect=Exception("Timeout 5000ms exceeded"))
    out = await server_mod.browser_snapshot(scope="e99", task_id="t")
    assert "scope 未匹配" in out



