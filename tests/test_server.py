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
        self.snap_diff: dict = {}
        self.snap_refs: dict = {}
        self.pending_dialog = None    # 与 TaskState 对齐: 对话框挂起
        self.dialog_waiter = None
        self.pending_new_page = None  # 新标签裁决
        self.pending_download = None  # 下载观测
        self.page = AsyncMock()
        self.page.url = "https://example.com"
        # __nexusLastMutation 返回 100s 前 → wait_dom_settled 走快路径, 不傻等
        import time as _time
        self.page.evaluate = AsyncMock(return_value=_time.time() * 1000 - 100_000)
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
        from nexus_browser.events import EventStore
        from nexus_browser.streams import StreamStore
        self.streams = StreamStore()
        self.events = EventStore()

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
        return [{"session_id": server_mod._session_id, "task_count": len(self.tasks),
                 "tasks": [{"task_id": tid, "pages": 1} for tid in self.tasks]}]

    def known_task(self, session_id, task_id):
        return task_id in self.tasks

    def pin(self, session_id, task_id):
        pass

    def peek_task(self, session_id, task_id):
        return self.tasks.get(task_id or "default")

    def unpin(self, session_id, task_id):
        pass


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
    """审计在 _guarded_call 单点完成: 工具名 + 字符计量 + HITL 检测。"""
    mgr, settings = patch_server
    out = await server_mod._guarded_call(
        "browser_navigate", server_mod.browser_navigate, (),
        {"url": "https://example.com", "task_id": "tx"},
    )
    assert "已导航至" in out or "Example" in out
    lines = settings.resolve_audit_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    entry = json.loads(lines[0])
    assert entry["tool"] == "browser_navigate"
    assert entry["params"]["url"] == "[redacted]"  # 敏感参数脱敏
    assert entry["in_chars"] == len("https://example.com") + len("tx")  # 计量在脱敏前
    assert entry["out_chars"] == len(out)


async def test_audit_detects_hitl_from_result(patch_server):
    """HITL 拦截的调用, 审计 hitl_triggered=True。"""
    mgr, settings = patch_server
    settings.hitl_rules = [{"action": "click", "name_pattern": "支付|确认"}]
    await server_mod._guarded_call(
        "browser_click", server_mod.browser_click, (),
        {"role": "button", "name": "确认支付", "task_id": "t"},
    )
    import json
    line = settings.resolve_audit_path().read_text(encoding="utf-8").strip().splitlines()[0]
    entry = json.loads(line)
    assert entry["hitl_triggered"] is True
    assert entry["risk"] == "medium"


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
    assert "browser_console" in names
    assert "browser_errors" in names
    assert "browser_network" in names
    assert "browser_perf" in names
    assert "browser_network_body" in names
    assert len(names) == 33  # 32 + browser_adopt_page (外部标签接管)


async def test_read_follow_creates_stream_and_returns_delta(patch_server):
    """follow: 首次建立流返回全文, 后续只回增量, full 取缓冲全文。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
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
    await mgr.ensure_task("s", "t")
    await server_mod.browser_read(selector=".msg", follow=True, task_id="t")
    sid = next(iter(mgr.streams._streams))
    out2 = await server_mod.browser_read(follow=True, stream_id=sid, task_id="t")
    assert f"流 {sid}" in out2
    assert "Hello" not in out2 or "无新增" in out2  # 无变化 → 无新增


async def test_read_follow_requires_selector(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
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


# ── 快照 diff + ref 代际链 ────────────────────────────────────────


async def test_snapshot_diff_suppresses_identical(patch_server):
    """逐节点一致 → 第二次只回'无变化', 全文字符全省。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")  # Issue K 后: 读类工具要求 task 已存在
    out1 = await server_mod.browser_snapshot(task_id="t")
    assert "Login" in out1
    out2 = await server_mod.browser_snapshot(task_id="t")
    assert "快照无变化" in out2 and "Login" not in out2
    out3 = await server_mod.browser_snapshot(diff=False, task_id="t")
    assert "Login" in out3  # diff=false 总是全量


async def test_snapshot_diff_keyed_by_mode(patch_server):
    """换 mode 属另一缓存键 → 不被误抑制。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    await server_mod.browser_snapshot(mode="reading", task_id="t")
    out = await server_mod.browser_snapshot(mode="interactive", task_id="t")
    assert "快照无变化" not in out and "Login" in out


async def test_snapshot_diff_hit_chains_refs(patch_server):
    """diff 命中: 同内容新代际 ref → 旧 ref 链到新 ref; 内容变化 → 全量且映射清空。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    await server_mod.browser_snapshot(task_id="t")
    task.page.locator.return_value.aria_snapshot = AsyncMock(
        return_value='- button "Login" [ref=s2e3]\n')
    out = await server_mod.browser_snapshot(task_id="t")
    assert "快照无变化" in out
    entry = task.snap_refs[(id(task.page), "")]
    assert entry["map"].get("s1e3") == "s2e3"
    task.page.locator.return_value.aria_snapshot = AsyncMock(
        return_value='- button "Logout" [ref=s3e3]\n')
    out = await server_mod.browser_snapshot(task_id="t")
    assert "Logout" in out
    assert entry["map"] == {}  # 树变了: 旧映射作废, 防误指别的元素


async def test_navigate_seeds_diff_baseline(patch_server):
    """navigate 附加快照与 browser_snapshot 默认参数同源(reading+diff): 建立 digest/ref 基线,
    导航后首次显式 snapshot 命中 diff (省一次全量输出)。"""
    mgr, _ = patch_server
    out = await server_mod.browser_navigate("https://example.com", task_id="t")
    assert "页面快照" in out
    assert mgr.tasks["t"].snap_diff != {}  # 基线已建
    snap = await server_mod.browser_snapshot(task_id="t")
    assert "快照无变化" in snap  # 同一代际内容 → diff 命中


async def test_wait_preserves_refs_via_tracking(patch_server):
    """browser_wait 轮询重拍(同内容新代际) → 旧 ref 链式续命, 不被 wait 静默杀死。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    await server_mod.browser_snapshot(task_id="t")
    task.page.locator.return_value.aria_snapshot = AsyncMock(
        return_value='- button "Login" [ref=s2e3]\n')
    out = await server_mod.browser_wait(text="Login", task_id="t")
    assert "已出现" in out
    entry = task.snap_refs[(id(task.page), "")]
    assert entry["map"].get("s1e3") == "s2e3"


async def test_wait_tree_change_invalidates_refs(patch_server):
    """wait 期间树真的变了 → 代际作废, 旧 ref 不得误指。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    await server_mod.browser_snapshot(task_id="t")
    task.page.locator.return_value.aria_snapshot = AsyncMock(
        return_value='- button "Login" [ref=s2e3]\n- button "New" [ref=s2e4]\n')
    out = await server_mod.browser_wait(text="Login", task_id="t")
    assert "已出现" in out
    entry = task.snap_refs[(id(task.page), "")]
    assert entry["map"] == {} and entry["refs"] == ["s2e3", "s2e4"]


async def test_wait_seeds_snapshot_diff(patch_server):
    """单一渲染管道: wait 末帧记入 digest 基线 → 紧随的 snapshot 命中 diff,
    且消息标注来源 browser_wait (日常模式: 等元素→读页面, 免一次全量)。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    out = await server_mod.browser_wait(text="Login", task_id="t")
    assert "已出现" in out
    snap = await server_mod.browser_snapshot(task_id="t")
    assert "快照无变化" in snap and "browser_wait" in snap


# ── 观测性工具: console / errors / network ─────────────────────────


async def test_console_empty_buffer(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    out = await server_mod.browser_console(task_id="t")
    assert "尚无事件缓冲" in out


async def test_console_incremental_and_level_filter(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    from nexus_browser.events import KIND_CONSOLE
    mgr.events.record(task.page, KIND_CONSOLE, level="log", text="noise")
    mgr.events.record(task.page, KIND_CONSOLE, level="error", text="boom", location="https://x/a.js:1")
    out = await server_mod.browser_console(level="error", task_id="t")
    assert "boom" in out and "noise" not in out
    out2 = await server_mod.browser_console(level="error", task_id="t")
    assert "无匹配" in out2  # 增量游标: 不重复返回
    out3 = await server_mod.browser_console(level="error", since=0, task_id="t")
    assert "boom" in out3  # since=0 全量重读


async def test_errors_merges_pageerror_console_error_failed_request(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    from nexus_browser.events import KIND_CONSOLE, KIND_PAGEERROR, KIND_REQUEST
    mgr.events.record(task.page, KIND_PAGEERROR, level="error", text="Uncaught TypeError")
    mgr.events.record(task.page, KIND_CONSOLE, level="error", text="console-bad")
    mgr.events.record(task.page, KIND_CONSOLE, level="log", text="console-ok")
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/ok", status=200)
    mgr.events.record(task.page, KIND_REQUEST, method="POST", url="https://a/pay", status=502)
    out = await server_mod.browser_errors(since=0, task_id="t")
    assert "Uncaught TypeError" in out and "console-bad" in out and "502" in out
    assert "console-ok" not in out and "https://a/ok" not in out


async def test_network_failed_only_default_and_url_filter(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    from nexus_browser.events import KIND_REQUEST
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/ok", status=200)
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://cdn/x.png",
                      status=None, failure="net::ERR_FAILED", resource_type="image")
    out = await server_mod.browser_network(since=0, task_id="t")
    assert "ERR_FAILED" in out and "https://a/ok" not in out  # 默认只看失败
    out = await server_mod.browser_network(since=0, failed_only=False, url_pattern="a/ok", task_id="t")
    assert "https://a/ok" in out and "cdn/x.png" not in out


async def test_observability_tools_have_no_impl_audit(patch_server):
    """新工具走 _guarded_call 单点审计, 不依赖 impl 内手工 _record。"""
    mgr, settings = patch_server
    await server_mod._guarded_call("browser_console", server_mod.browser_console, (), {"task_id": "t"})
    import json
    entry = json.loads(settings.resolve_audit_path().read_text(encoding="utf-8").strip())
    assert entry["tool"] == "browser_console" and entry["risk"] == "low"


# ── browser_network_body + confirmed 确认原语 ─────────────────────


async def test_network_body_disabled_by_default(patch_server):
    out = await server_mod.browser_network_body(seq=1, task_id="t")
    assert "未启用" in out and "BROWSER_ALLOW_NETWORK_BODY" in out


async def test_network_body_requires_confirmation(patch_server):
    mgr, settings = patch_server
    settings.allow_network_body = True
    task = await mgr.ensure_task("s", "t")
    from nexus_browser.events import KIND_REQUEST
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/api", status=200)
    out = await server_mod.browser_network_body(seq=1, task_id="t")
    assert "CONFIRMATION_REQUIRED" in out and "https://a/api" in out


async def test_network_body_reads_text_with_handle(patch_server):
    mgr, settings = patch_server
    settings.allow_network_body = True
    task = await mgr.ensure_task("s", "t")
    handle = AsyncMock()
    handle.body = AsyncMock(return_value=b'{"ok": true}')
    handle.headers = {"content-type": "application/json"}
    from nexus_browser.events import KIND_REQUEST
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/api",
                      status=200, handle=handle)
    out = await server_mod.browser_network_body(seq=1, confirmed=True, task_id="t")
    assert '{"ok": true}' in out


async def test_network_body_binary_not_decoded(patch_server):
    mgr, settings = patch_server
    settings.allow_network_body = True
    task = await mgr.ensure_task("s", "t")
    handle = AsyncMock()
    handle.body = AsyncMock(return_value=b"\x89PNG" * 100)
    handle.headers = {"content-type": "image/png"}
    from nexus_browser.events import KIND_REQUEST
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/x.png",
                      status=200, handle=handle)
    out = await server_mod.browser_network_body(seq=1, confirmed=True, task_id="t")
    assert "[binary]" in out and "400 字节" in out


async def test_network_body_stale_handle_clear_error(patch_server):
    mgr, settings = patch_server
    settings.allow_network_body = True
    task = await mgr.ensure_task("s", "t")
    from nexus_browser.events import KIND_REQUEST
    mgr.events.record(task.page, KIND_REQUEST, method="GET", url="https://a/old",
                      status=200, handle=None)
    out = await server_mod.browser_network_body(seq=1, confirmed=True, task_id="t")
    assert "句柄已失效" in out


async def test_network_body_bad_seq(patch_server):
    mgr, settings = patch_server
    settings.allow_network_body = True
    await mgr.ensure_task("s", "t")
    out = await server_mod.browser_network_body(seq=999, confirmed=True, task_id="t")
    assert "不是当前页面的有效请求事件" in out


# ── Issue K: 坏 task_id 显式报错 / Issue N: _blank 新标签不误报 ──────


async def test_unknown_task_explicit_error(patch_server):
    """不存在的 task_id → 明确报错 + 列出现有 task, 不再伪装"页面为空"。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "good")
    out = await server_mod.browser_snapshot(task_id="nosuchtask")
    assert "task_id 'nosuchtask' 不存在" in out and "good" in out


async def test_navigate_still_autocreates_task(patch_server):
    """navigate 不受门禁限制: 自动创建新 task 是设计行为。"""
    mgr, _ = patch_server
    out = await server_mod.browser_navigate("https://example.com", task_id="brand-new")
    assert "已导航至" in out and "brand-new" in mgr.tasks


async def test_default_task_not_gated(patch_server):
    """default 是隐式工作区, 不存在时也放行 (首次使用零仪式)。"""
    out = await server_mod.browser_snapshot()
    assert "不存在" not in out


async def test_click_blank_link_reports_new_tab(patch_server):
    """target=_blank: 点击后新标签打开 → 成功回报 index, 不再误报超时。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    new_page = AsyncMock()
    new_page.url = "https://new.example"
    loc = AsyncMock()
    loc.get_attribute = AsyncMock(return_value="_blank")

    async def _open(*a, **k):
        task.pending_new_page = new_page
        task.pages = [task.page, new_page]

    loc.click = AsyncMock(side_effect=_open)
    task.pages = [task.page]
    task.pending_new_page = None
    mgr.find_element = AsyncMock(return_value=loc)
    mgr.consume_pending_new_page = MagicMock(
        side_effect=lambda s, t: setattr(task, "pending_new_page", None) or new_page)
    out = await server_mod.browser_click(selector="a.ext", task_id="t")
    assert "新标签打开" in out and "index=1" in out


async def test_click_real_timeout_still_error(patch_server):
    """无新标签的真超时 → 原样报错, 不被 _blank 竞速吞掉。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.pages = [task.page]
    task.pending_new_page = None
    loc = AsyncMock()
    loc.click = AsyncMock(side_effect=TimeoutError("waiting for scheduled navigations to finish"))
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_click(selector="a.x", task_id="t")
    assert "点击失败" in out


# ── 第一波补洞工具 (adversarial 基准驱动) ─────────────────────────


async def test_press_key_real_cdp_event(patch_server):
    """press_key 走 page.keyboard.press (isTrusted=true 的真实键事件)。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.keyboard.press = AsyncMock()
    out = await server_mod.browser_press_key("Escape", task_id="t")
    assert "Escape" in out
    task.page.keyboard.press.assert_awaited_once_with("Escape")


async def test_hover_uses_locator_hover(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    loc = MagicMock()
    loc.hover = AsyncMock()
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_hover(selector="#menu", task_id="t")
    assert "已悬停元素" in out
    loc.hover.assert_awaited_once()


async def test_select_option_passes_values(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    loc = MagicMock()
    loc.select_option = AsyncMock(return_value=["上海"])
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_select_option(values=["上海"], selector="#city", task_id="t")
    assert "上海" in out
    loc.select_option.assert_awaited_once_with(["上海"], timeout=5000)


async def test_upload_file_requires_confirmation(patch_server):
    """文件出站 = 无条件 confirmed 门 (与 evaluate 同级); 文件不存在 → 明确错误。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    out = await server_mod.browser_upload_file(paths=["/tmp/x.txt"], selector="#f", task_id="t")
    assert "CONFIRMATION_REQUIRED" in out and "/tmp/x.txt" in out
    out2 = await server_mod.browser_upload_file(paths=["/不存在的文件.txt"], selector="#f",
                                                confirmed=True, task_id="t")
    assert "文件不存在" in out2


async def test_upload_file_calls_set_input_files(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    loc = MagicMock()
    loc.set_input_files = AsyncMock()
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_upload_file(paths=["bench/fixture_adv/frame.html"],
                                               selector="#f", confirmed=True, task_id="t")
    assert "已上传 1 个文件" in out and "frame.html" in out


async def test_navigate_back_and_empty_history(patch_server):
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.page.go_back = AsyncMock(return_value=object())
    task.page.title = AsyncMock(return_value="页面A")
    task.page.url = "https://a/"
    out = await server_mod.browser_navigate_back(task_id="t")
    assert "已后退" in out and "页面A" in out
    task.page.go_back = AsyncMock(return_value=None)  # 历史起点
    out2 = await server_mod.browser_navigate_back(task_id="t")
    assert "历史起点" in out2


async def test_drag_calls_drag_to(patch_server):
    """分段拖拽: bounding_box 定位两端 → mouse move/down/分段 move/up (替代 drag_to,
    pointer 系库静默无效的实测定案)。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    src, dst = MagicMock(), MagicMock()
    src.bounding_box = AsyncMock(return_value={"x": 0, "y": 0, "width": 50, "height": 20})
    dst.bounding_box = AsyncMock(return_value={"x": 0, "y": 100, "width": 50, "height": 20})
    mgr.find_element = AsyncMock(side_effect=[src, dst])
    out = await server_mod.browser_drag(from_selector="#src", to_selector="#zone", task_id="t")
    assert "已拖拽" in out
    task.page.mouse.down.assert_awaited_once()
    task.page.mouse.up.assert_awaited_once()
    assert task.page.mouse.move.await_count >= 11  # 入位 + 10 步分段


async def test_type_fallback_rich_editor(patch_server):
    """fill 拒收/静默无效 (富文本内嵌节点, 实测捕获) → 升级 contenteditable 容器逐键写入。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    host = MagicMock()
    host.count = AsyncMock(return_value=1)
    host.click = AsyncMock()
    host.press = AsyncMock()
    host.press_sequentially = AsyncMock()
    loc = MagicMock()
    loc.clear = AsyncMock(side_effect=Exception("Element is not an <input>, <textarea>, "
                                                "<select> or [contenteditable]"))
    loc.locator = MagicMock(return_value=host)
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_type(text="hello", ref="f1e2", task_id="t")
    assert "已输入文本" in out
    host.click.assert_awaited_once()
    host.press_sequentially.assert_awaited_once_with("hello", timeout=5000)


async def test_click_button_param_passthrough(patch_server):
    """button=right 直达 locator.click (上下文菜单); 非法值明确报错。"""
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    loc = MagicMock()
    loc.click = AsyncMock()
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_click(selector="#hot", button="right", task_id="t")
    assert "已点击" in out
    loc.click.assert_awaited_once_with(timeout=5000, no_wait_after=True, button="right")
    bad = await server_mod.browser_click(selector="#hot", button="hyper", task_id="t")
    assert "不支持的 button" in bad


async def test_click_reports_download(patch_server):
    """点击触发下载 (pending_download 落位) → 回复带文件名与保存路径。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    task.pages = [task.page]
    task.pending_new_page = None
    loc = AsyncMock()

    async def _click_side_effect(**_kw):
        task.pending_download = {"filename": "report.pdf", "path": "/tmp/x/report.pdf", "url": "https://a/r.pdf"}

    loc.click = AsyncMock(side_effect=_click_side_effect)
    mgr.find_element = AsyncMock(return_value=loc)
    out = await server_mod.browser_click(selector="a.dl", task_id="t")
    assert "开始下载" in out and "report.pdf" in out and "/tmp/x/report.pdf" in out
    assert task.pending_download is None  # 已消费


# ── 对话框治理 (第二波) ───────────────────────────────────────────


def _fake_dialog(dtype="confirm", message="确认删除?"):
    d = MagicMock()
    d.type = dtype
    d.message = message
    d.accept = AsyncMock()
    d.dismiss = AsyncMock()
    return d


async def test_dialog_accept_requires_confirmed(patch_server):
    """accept=替用户点确定 → 无条件 HITL 门; dismiss 不需要。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "t")
    d = _fake_dialog()
    task.pending_dialog = d
    out = await server_mod.browser_dialog_respond(accept=True, task_id="t")
    assert "CONFIRMATION_REQUIRED" in out
    assert task.pending_dialog is not None  # 未被处置, 仍挂起
    out2 = await server_mod.browser_dialog_respond(accept=True, confirmed=True, task_id="t")
    assert "已接受" in out2
    assert task.pending_dialog is None
    d.accept.assert_awaited_once()


async def test_dialog_dismiss_free_and_notice_attached(patch_server):
    """dismiss 免确认; 挂起期间其他工具回复前置[对话框等待决策]。"""
    mgr, _ = patch_server
    task = await mgr.ensure_task("s", "default")  # 挂起在 default: 通知按 task 归属
    d = _fake_dialog()
    task.pending_dialog = d
    # 挂起中: 经分发层 (_guarded_call) 的任意工具回复应前置提醒
    out = await server_mod._guarded_call("browser_tasks", server_mod.browser_tasks)
    assert "对话框等待决策" in out and "确认删除" in out
    out2 = await server_mod.browser_dialog_respond(accept=False, task_id="default")
    assert "已拒绝" in out2
    d.dismiss.assert_awaited_once()
    assert task.pending_dialog is None
    out3 = await server_mod._guarded_call("browser_tasks", server_mod.browser_tasks)  # 清理后不再提醒
    assert "对话框等待决策" not in out3


async def test_dialog_respond_without_pending_errors(patch_server):
    mgr, _ = patch_server
    await mgr.ensure_task("s", "t")
    out = await server_mod.browser_dialog_respond(accept=False, task_id="t")
    assert "无待处理对话框" in out


async def test_dialog_timeout_auto_dismiss(patch_server):
    """挂起超时 → 自动 dismiss + 事件留痕 + 状态通知 (防页面永久冻结)。"""
    mgr, settings = patch_server
    settings.dialog_timeout_ms = 50  # 测试加速
    # 走真实 _hook_page 挂接路径太重; 直接驱动 core 的 _on_dialog 等价逻辑:
    # 这里验证 respond 与超时兜底的竞争契约: 先 respond 后超时不得二次处置
    task = await mgr.ensure_task("s", "t")
    d = _fake_dialog()
    task.pending_dialog = d

    async def _auto():
        await asyncio.sleep(settings.dialog_timeout_ms / 1000)
        if task.pending_dialog is d:
            task.pending_dialog = None
            await d.dismiss()

    task.dialog_waiter = asyncio.create_task(_auto())
    out = await server_mod.browser_dialog_respond(accept=False, task_id="t")
    assert "已拒绝" in out
    await asyncio.sleep(0.1)  # 兜底任务已取消, 不得二次 dismiss
    d.dismiss.assert_awaited_once()


async def test_hitl_confirmed_bypasses_block(patch_server):
    """confirmed=true = 用户已同意 → 不再拦截 (完成 HITL 闭环)。"""
    mgr, settings = patch_server
    settings.hitl_rules = [{"action": "click", "name_pattern": "支付|确认"}]
    out = await server_mod.browser_click(role="button", name="确认支付", confirmed=True, task_id="t")
    assert "CONFIRMATION_REQUIRED" not in out  # FakeManager 无 find_element → 落到 ERROR, 但不被 HITL 拦


async def test_evaluate_confirmed_executes(patch_server):
    mgr, settings = patch_server
    settings.allow_js_execution = True
    await mgr.ensure_task("s", "t")  # Issue K 后: evaluate 也过 task 门禁
    out = await server_mod.browser_evaluate("1+1", confirmed=True, task_id="t")
    assert "CONFIRMATION_REQUIRED" not in out and "结果:" in out


async def test_evaluate_default_task_empty_id(patch_server):
    """task_id='' 与 'default' 同义: bench 实测捕获 — 裸传 '' 曾建出幻影 task '' 对着 about:blank 求值。"""
    mgr, settings = patch_server
    settings.allow_js_execution = True
    await mgr.ensure_task("s", "default")
    out = await server_mod.browser_evaluate("1+1", confirmed=True, task_id="")
    assert "不存在" not in out and "结果:" in out
    assert "" not in mgr.tasks  # 不得创建幻影 task


async def test_dispatch_normalizes_task_id(patch_server):
    """分发层单点归一化: 经 _guarded_call 的 task_id='' 到达工具函数时已是 'default'。"""
    seen = {}

    async def probe(*, task_id: str = ""):
        seen["tid"] = task_id
        return "ok"

    out = await server_mod._guarded_call("browser_evaluate", probe, kwargs={"task_id": ""})
    assert out == "ok" and seen["tid"] == "default"


# ── HTTP transport: session 解析 + token 门 ───────────────────────


def test_sid_fallback_to_global():
    """stdio / 非请求上下文 → 进程级单例 session id。"""
    assert server_mod._sid() == server_mod._session_id
    tok = server_mod._session_var.set("conn-http-x")
    try:
        assert server_mod._sid() == "conn-http-x"
    finally:
        server_mod._session_var.reset(tok)
    assert server_mod._sid() == server_mod._session_id


def test_session_from_ctx():
    class _Req:
        headers = {"mcp-session-id": "sess-abc"}

    class _ReqCtx:
        request = _Req()

    class _Ctx:
        @property
        def request_context(self):
            return _ReqCtx()

    assert server_mod._session_from_ctx(_Ctx()) == "sess-abc"
    assert server_mod._session_from_ctx(None) is None

    class _NoReq:
        @property
        def request_context(self):
            raise ValueError("outside request")

    assert server_mod._session_from_ctx(_NoReq()) is None


def test_token_guard_401_and_pass():
    import anyio

    from nexus_browser.server import _token_guard

    async def app(scope, receive, send):
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    guarded = _token_guard(app, "secret")

    async def call(headers):
        status = {}
        scope = {"type": "http", "headers": headers}

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]

        await guarded(scope, receive, send)
        return status["code"]

    assert anyio.run(call, []) == 401
    assert anyio.run(call, [(b"authorization", b"Bearer secret")]) == 200


def test_transport_validator():
    from nexus_browser.settings import BrowserSettings
    assert BrowserSettings(transport="HTTP").transport == "http"
    with pytest.raises(Exception):
        BrowserSettings(transport="websocket")


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



