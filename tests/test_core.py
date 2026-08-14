"""core.BrowserManager 行为测试: 多 task 隔离 / TTL / 崩溃自愈 / session。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus_browser.core import BrowserManager
from nexus_browser.settings import BrowserSettings


def _Page():
    p = AsyncMock()
    p.url = "about:blank"
    p.title = AsyncMock(return_value="Page")
    p.on = MagicMock(return_value=None)  # Playwright 的 page.on 是同步注册
    p.is_closed = MagicMock(return_value=False)  # 同步活性探测
    return p


@pytest.fixture
def settings():
    return BrowserSettings(mode="isolated", context_ttl_sec=60)


@pytest.fixture
async def mgr(fake_playwright, settings):
    m = BrowserManager(settings)
    yield m
    await m.close_all()


@pytest.fixture
def fake_playwright(monkeypatch):
    """假 playwright.chromium: 每个 new_context 返回独立 context/page。"""
    from nexus_browser import core

    def _make_page():
        p = AsyncMock()
        p.url = "about:blank"
        p.title = AsyncMock(return_value="Page")
        p.on = MagicMock(return_value=None)  # 同步注册事件
        p.is_closed = MagicMock(return_value=False)
        return p

    def _make_context():
        ctx = AsyncMock()
        page = _make_page()
        ctx.new_page.return_value = page
        ctx.pages = [page]
        ctx.on = MagicMock(return_value=None)  # 同步注册事件
        return ctx

    browser = AsyncMock()
    browser.new_context.side_effect = lambda **kw: _make_context()
    browser.contexts = []
    browser.on = MagicMock(return_value=None)  # disconnected 钩子同步注册
    pw = AsyncMock()
    pw.chromium.launch.return_value = browser
    pw.chromium.connect_over_cdp.side_effect = lambda **kw: AsyncMock()
    pw.start.return_value = pw

    class _CM:
        async def start(self):
            return pw

        async def __aenter__(self):
            return pw

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(core, "async_playwright", lambda: _CM())
    browser._pw = pw  # type: ignore[attr-defined]
    return browser


async def test_ensure_browser_singleton(mgr, fake_playwright):
    b1 = await mgr.ensure_browser()
    b2 = await mgr.ensure_browser()
    assert b1 is b2
    fake_playwright.new_context.assert_not_called()  # 只建浏览器, 未建 context


async def test_two_tasks_isolated_contexts(mgr, fake_playwright):
    p1 = await mgr.get_page("sess-1", "task-a")
    p2 = await mgr.get_page("sess-1", "task-b")
    assert p1 is not p2
    assert fake_playwright.new_context.call_count == 2  # 隔离模式: 每 task 独立 context


async def test_same_task_reuses_page(mgr, fake_playwright):
    p1 = await mgr.get_page("sess-1", "task-a")
    p2 = await mgr.get_page("sess-1", "task-a")
    assert p1 is p2
    assert fake_playwright.new_context.call_count == 1


async def test_cdp_shares_context_isolates_page(mgr, fake_playwright):
    # cdp 需要共享 context 已存在; 每个 new_page 返回独立可 await 的 page
    shared = AsyncMock()
    shared.new_page = AsyncMock(side_effect=lambda: _Page())
    shared.on = MagicMock(return_value=None)
    fake_playwright.contexts = [shared]

    # 用独立 cdp manager: 复用已建的浏览器, 不建新 context
    s = BrowserSettings(mode="cdp", context_ttl_sec=60)
    m = BrowserManager(s)
    m._ttl_enabled = False
    m._browser = fake_playwright
    m._browser_ready = True
    try:
        p1 = await m.get_page("sess-1", "task-a")
        p2 = await m.get_page("sess-1", "task-b")
        assert p1 is not p2  # 独立 page
        assert fake_playwright.new_context.call_count == 0  # 复用共享 context
    finally:
        m._browser_ready = False
        m._browser = None


async def test_navigate_returns_meta(mgr, fake_playwright):
    result = await mgr.navigate("sess-1", "task-a", "https://example.com", wait_until="load")
    assert result["url"] == "about:blank"
    assert "title" in result
    assert "readyState" in result


async def test_isolated_launch_passes_channel(monkeypatch):
    """isolated 配 channel(无 userdata)时透传给 launch。"""
    from nexus_browser import core

    browser = AsyncMock()
    browser.new_context.side_effect = lambda **kw: _ctx()
    browser.on = MagicMock(return_value=None)
    pw = AsyncMock()
    pw.chromium.launch.return_value = browser
    pw.start.return_value = pw

    class _CM:
        async def start(self):
            return pw

    monkeypatch.setattr(core, "async_playwright", lambda: _CM())
    s = BrowserSettings(mode="isolated", channel="chrome")
    m = BrowserManager(s)
    m._ttl_enabled = False
    await m.ensure_browser()
    kwargs = pw.chromium.launch.call_args.kwargs
    assert kwargs.get("channel") == "chrome"


async def test_isolated_userdata_uses_persistent_context(monkeypatch):
    """isolated 配 user_data_dir 时用 persistent context 共享登录态。"""
    from nexus_browser import core

    persistent = AsyncMock()
    persistent.new_page.side_effect = lambda: _Page()
    persistent.pages = []
    persistent.on = MagicMock(return_value=None)
    persistent.browser.on = MagicMock(return_value=None)
    pw = AsyncMock()
    pw.chromium.launch_persistent_context.return_value = persistent
    pw.start.return_value = pw

    class _CM:
        async def start(self):
            return pw

    monkeypatch.setattr(core, "async_playwright", lambda: _CM())
    s = BrowserSettings(mode="isolated", user_data_dir=r"C:\Profile\User Data")
    m = BrowserManager(s)
    m._ttl_enabled = False
    await m.ensure_browser()
    kwargs = pw.chromium.launch_persistent_context.call_args.kwargs
    assert kwargs.get("user_data_dir") == r"C:\Profile\User Data"
    # 拿到上下文后建 page 走 new_page (结果忽略)
    await m.get_page("s", "t")
    # 不应走到 chromium.launch
    pw.chromium.launch.assert_not_called()


async def test_cdp_failure_raises_not_fallback(monkeypatch):
    """cdp 连接失败必须抛明确错误, 不许静默启动全新浏览器。"""
    from nexus_browser import core

    connect_fail = AsyncMock(side_effect=Exception("connect refused"))
    pw = AsyncMock()
    pw.chromium.connect_over_cdp = connect_fail
    pw.start.return_value = pw

    class _CM:
        async def start(self):
            return pw

    monkeypatch.setattr(core, "async_playwright", lambda: _CM())
    s = BrowserSettings(mode="cdp", cdp_endpoint="http://localhost:9222")
    m = BrowserManager(s)
    m._ttl_enabled = False
    import pytest

    with pytest.raises(RuntimeError) as ei:
        await m.ensure_browser()
    assert "cdp" in str(ei.value).lower() or "9222" in str(ei.value)
    # 不得调用 launch 回退
    pw.chromium.launch.assert_not_called()


def _ctx():
    ctx = AsyncMock()
    page = _Page()
    ctx.new_page.return_value = page
    ctx.pages = [page]
    ctx.on = MagicMock(return_value=None)
    return ctx


async def test_settle_binding_registered_on_context(mgr, fake_playwright):
    """task context 创建时注册一次 __nexusSettleReport binding。"""
    await mgr.get_page("s-1", "t-a")
    ts = mgr._task("s-1", "t-a")
    ts.context.expose_binding.assert_awaited_once()
    assert ts.context.expose_binding.call_args.args[0] == "__nexusSettleReport"


async def test_settle_handler_increments_count(mgr, fake_playwright):
    """binding 回调: 递增 settle_count 并 set event。"""
    page = await mgr.get_page("s-1", "t-a")
    ts = mgr._task("s-1", "t-a")
    handler = ts.context.expose_binding.call_args.args[1]
    handler({"page": page})
    assert ts.settle_count == 1
    assert ts.settle_event.is_set()


async def test_settle_binding_shared_context_routes_to_page_owner(mgr, fake_playwright):
    """共享 context (cdp/persistent) 下, binding 回调按 page 归属路由到正确 task。"""
    s = BrowserSettings(mode="isolated", user_data_dir=r"C:\tmp\nxbm-prof")
    persistent = AsyncMock()
    p1, p2 = _Page(), _Page()
    persistent.new_page = AsyncMock(side_effect=[p1, p2])
    persistent.on = MagicMock(return_value=None)
    persistent.browser.on = MagicMock(return_value=None)
    persistent.pages = []
    pw = fake_playwright._pw
    pw.chromium.launch_persistent_context.return_value = persistent
    m = BrowserManager(s)
    m._ttl_enabled = False
    try:
        await m.get_page("sess", "ta")
        await m.get_page("sess", "tb")
        handler = persistent.expose_binding.call_args.args[1]
        handler({"page": p2})
        ts_b = m._task("sess", "tb")
        ts_a = m._task("sess", "ta")
        assert ts_b.settle_count == 1
        assert ts_a.settle_count == 0
    finally:
        m._persistent_context = None


async def test_new_page_sets_nav_event(mgr, fake_playwright):
    """新标签事件 → task.nav_event 置位 (事件驱动 wait_navigation)。"""
    await mgr.get_page("s-1", "t-a")
    ts = mgr._task("s-1", "t-a")
    mgr._on_new_page("s-1", "t-a", _Page())
    assert ts.nav_event.is_set()


async def test_close_task_cleans_page_owners(mgr, fake_playwright):
    """close_task 清理 page→task 归属映射, 防脏路由。"""
    page = await mgr.get_page("s-1", "t-a")
    assert id(page) in mgr._page_owners
    await mgr.close_task("s-1", "t-a")
    assert id(page) not in mgr._page_owners


async def test_find_element_role_only(mgr, fake_playwright):
    """仅 role 定位: 取该 role 第一个元素 (如 B站搜索框的 name 是轮播词, 每次变)。"""
    page = await mgr.get_page("s-1", "t-a")
    loc = MagicMock()
    loc.count = AsyncMock(return_value=2)
    loc.first = loc
    page.get_by_role = MagicMock(return_value=loc)
    out = await mgr.find_element("s-1", "t-a", role="textbox")
    assert out is loc


async def test_find_element_role_only_missing(mgr, fake_playwright):
    page = await mgr.get_page("s-1", "t-a")
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.get_by_role = MagicMock(return_value=loc)
    import pytest

    with pytest.raises(ValueError):
        await mgr.find_element("s-1", "t-a", role="textbox")


async def test_find_element_by_role_name(mgr, fake_playwright):
    """按 role+name 定位返回 locator.first。"""
    page = await mgr.get_page("s-1", "t-a")
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    loc.first = loc
    page.get_by_role = MagicMock(return_value=loc)
    out = await mgr.find_element("s-1", "t-a", role="button", name="登录")
    assert out is loc


async def test_find_element_pos_returns_raw(mgr, fake_playwright):
    """pos 坐标直接返回原串, 交给上层坐标点击。"""
    await mgr.get_page("s-1", "t-a")
    out = await mgr.find_element("s-1", "t-a", pos="100,200,50,30")
    assert out == "100,200,50,30"


async def test_find_element_missing_raises(mgr, fake_playwright):

    page = await mgr.get_page("s-1", "t-a")
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.get_by_role = MagicMock(return_value=loc)
    import pytest

    with pytest.raises(ValueError):
        await mgr.find_element("s-1", "t-a", role="button", name="不存在")


async def test_find_element_no_selector_raises(mgr, fake_playwright):
    import pytest

    await mgr.get_page("s-1", "t-a")
    with pytest.raises(ValueError):
        await mgr.find_element("s-1", "t-a")


async def test_close_task_cleans_context(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    ts = mgr._task("sess-1", "task-a")
    ctx_a = ts.context
    await mgr.close_task("sess-1", "task-a")
    ctx_a.close.assert_awaited()
    # 再取同 task → 新 context
    await mgr.get_page("sess-1", "task-a")
    assert fake_playwright.new_context.call_count == 2


async def test_ttl_evicts_idle_task_and_saves_snapshot(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    mgr._ttl_enabled = False  # 手动触发
    page = mgr._task("sess-1", "task-a").pages[0]
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")

    # 老 bug: _touch 把 task 永久加进 _active_tasks → TTL 永不触发。
    # 现在空闲即回收, 无需任何手工摘除; 只有 pin(操作进行中) 才豁免。
    mgr._last_access["sess-1"]["task-a"] = 0  # 很久前
    await mgr._evict_idle_tasks()

    snap = mgr.get_evicted_snapshot("sess-1", "task-a")
    assert snap and snap["url"] == "https://example.com"
    assert snap["title"] == "Example"


async def test_close_session_cleans_all_tasks(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    await mgr.get_page("sess-1", "task-b")
    await mgr.close_session("sess-1")
    assert "sess-1" not in mgr._sessions


async def test_reset_stale_browser_reinitializes(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    old_browser = mgr._browser
    await mgr.reset_stale_browser("sess-1", "task-a")
    assert mgr._browser is None
    old_browser.close.assert_awaited()
    # 再取 → 触发重新初始化 (chromium.launch 再次被调)
    before = fake_playwright._pw.chromium.launch.call_count
    await mgr.get_page("sess-1", "task-b")
    assert fake_playwright._pw.chromium.launch.call_count > before
    assert mgr._browser is not None


async def test_multiple_sessions_isolated(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    await mgr.get_page("sess-2", "task-a")
    assert fake_playwright.new_context.call_count == 2


async def test_close_all(mgr, fake_playwright):
    await mgr.get_page("sess-1", "task-a")
    await mgr.get_page("sess-1", "task-b")
    await mgr.close_all()
    assert mgr._browser is None
    assert not mgr._sessions


# ── G: 死亡观测 / 自愈 / TTL pin ────────────────────────────────────


async def test_get_page_heals_externally_closed_page(mgr, fake_playwright):
    """标签页被外部关闭 → 同 task 自愈重建 + 恢复 URL + 一次性状态通知。"""
    page = await mgr.get_page("sess-1", "task-a")
    ts = mgr._task("sess-1", "task-a")
    ts.last_url = "https://example.com"
    page.is_closed = MagicMock(return_value=True)  # 用户关了标签页
    new_page = _Page()
    ts.context.new_page = AsyncMock(return_value=new_page)

    out = await mgr.get_page("sess-1", "task-a")
    assert out is new_page
    assert mgr._task("sess-1", "task-a").pages == [new_page]
    new_page.goto.assert_awaited_once()  # 恢复了上次 URL
    assert new_page.goto.call_args.args[0] == "https://example.com"
    notice = mgr.pop_notice("sess-1", "task-a")
    assert notice and "外部关闭" in notice and "https://example.com" in notice
    assert mgr.pop_notice("sess-1", "task-a") is None  # 一次性消费


async def test_get_page_heal_skips_restore_for_navigation(mgr, fake_playwright):
    """即将导航时不恢复旧 URL (避免双跳), 通知里明示"未恢复"。"""
    page = await mgr.get_page("sess-1", "task-a")
    ts = mgr._task("sess-1", "task-a")
    ts.last_url = "https://example.com"
    page.is_closed = MagicMock(return_value=True)
    new_page = _Page()
    ts.context.new_page = AsyncMock(return_value=new_page)

    await mgr.get_page("sess-1", "task-a", for_navigation=True)
    new_page.goto.assert_not_awaited()
    notice = mgr.pop_notice("sess-1", "task-a")
    assert "未恢复" in notice


async def test_get_page_rebuilds_after_browser_disconnected(mgr, fake_playwright):
    """浏览器进程被杀 → 整链重建 (launch 再次调用) + 通知说明状态重置。"""
    await mgr.get_page("sess-1", "task-a")
    mgr._on_browser_disconnected()
    launches_before = fake_playwright._pw.chromium.launch.call_count
    page = await mgr.get_page("sess-1", "task-a")
    assert fake_playwright._pw.chromium.launch.call_count > launches_before
    assert page is not None
    assert not mgr._browser_disconnected
    notice = mgr.pop_notice("sess-1", "task-a")
    assert "浏览器进程已退出" in notice


async def test_cdp_disconnect_raises_clear_error(mgr, fake_playwright):
    """cdp 模式浏览器断开不能替用户重启 Chrome → 明确报错指引。"""
    s = BrowserSettings(mode="cdp", context_ttl_sec=60)
    m = BrowserManager(s)
    m._ttl_enabled = False
    shared = AsyncMock()
    shared.new_page = AsyncMock(side_effect=lambda: _Page())
    shared.on = MagicMock(return_value=None)
    fake_playwright.contexts = [shared]
    m._browser = fake_playwright
    m._browser_ready = True
    try:
        await m.get_page("sess-1", "task-a")
        m._on_browser_disconnected()
        with pytest.raises(RuntimeError) as ei:
            await m.get_page("sess-1", "task-a")
        assert "9222" in str(ei.value)
    finally:
        m._browser_ready = False
        m._browser = None


async def test_ttl_skips_pinned_task(mgr, fake_playwright):
    """pin(操作进行中) 豁免回收; unpin 后恢复可回收。"""
    await mgr.get_page("sess-1", "task-a")
    mgr._ttl_enabled = False
    mgr._last_access["sess-1"]["task-a"] = 0
    mgr.pin("sess-1", "task-a")
    await mgr._evict_idle_tasks()
    assert "task-a" in mgr._sessions["sess-1"].tasks
    mgr.unpin("sess-1", "task-a")
    await mgr._evict_idle_tasks()
    session = mgr._sessions.get("sess-1")
    assert session is None or "task-a" not in session.tasks


async def test_evicted_task_recreates_with_url_restore(mgr, fake_playwright):
    """TTL 回收过的 task 重建 → 自动恢复上次 URL + 状态通知。"""
    page = await mgr.get_page("sess-1", "task-a")
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")
    mgr._ttl_enabled = False
    mgr._last_access["sess-1"]["task-a"] = 0
    await mgr._evict_idle_tasks()

    new_page = await mgr.get_page("sess-1", "task-a")
    new_page.goto.assert_awaited_once()
    assert new_page.goto.call_args.args[0] == "https://example.com"
    notice = mgr.pop_notice("sess-1", "task-a")
    assert "空闲超时被自动回收" in notice and "https://example.com" in notice


async def test_shared_context_new_tab_single_attribution(fake_playwright):
    """共享 context: page 监听只挂一次; 新标签只归属最近活跃的 task (不扇出)。"""
    s = BrowserSettings(mode="isolated", user_data_dir=r"C:\tmp\nxbm-prof-fanout")
    persistent = AsyncMock()
    p1, p2 = _Page(), _Page()
    persistent.new_page = AsyncMock(side_effect=[p1, p2])
    persistent.on = MagicMock(return_value=None)
    persistent.browser.on = MagicMock(return_value=None)
    persistent.pages = []
    pw = fake_playwright._pw
    pw.chromium.launch_persistent_context.return_value = persistent
    m = BrowserManager(s)
    m._ttl_enabled = False
    try:
        await m.get_page("sess", "ta")
        await m.get_page("sess", "tb")
        page_hooks = [c for c in persistent.on.call_args_list if c.args[0] == "page"]
        assert len(page_hooks) == 1  # 老 bug: 每 task 挂一次 → 新标签扇出到所有 task
        new_tab = _Page()
        page_hooks[0].args[1](new_tab)
        ts_a, ts_b = m._task("sess", "ta"), m._task("sess", "tb")
        assert new_tab in ts_b.pages      # tb 最近活跃
        assert new_tab not in ts_a.pages
    finally:
        m._persistent_context = None


async def test_page_close_hook_cleans_and_marks(mgr, fake_playwright):
    """page close 事件: 清归属映射 + 标记死因 + 失效该页流(缓冲保留)。"""
    page = await mgr.get_page("s-1", "t-a")
    close_handler = next(c.args[1] for c in page.on.call_args_list if c.args[0] == "close")
    st, _ = mgr.streams.get_or_create(page, ".msg")

    close_handler()
    assert id(page) not in mgr._page_owners
    assert mgr._task("s-1", "t-a").died_reason == "closed"
    assert st.dead == "页面已关闭"
    assert mgr.streams.find(page, ".msg") is None  # 死流不再被复用


# ── 定位链: ref / 显式 selector 优先 / 族内回退 / 自救清单 ──────────


def _Loc(count=1):
    loc = MagicMock()
    loc.count = AsyncMock(return_value=count)
    loc.first = loc
    loc.nth = MagicMock(side_effect=lambda i: loc)
    loc.is_visible = AsyncMock(return_value=True)
    return loc


async def test_find_element_by_ref(mgr, fake_playwright):
    """ref → aria-ref 选择器引擎 (快照句柄闭环)。"""
    page = await mgr.get_page("s-1", "t-a")
    loc = _Loc(1)
    page.locator = MagicMock(return_value=loc)
    out = await mgr.find_element("s-1", "t-a", ref="e12")
    page.locator.assert_called_with("aria-ref=e12")
    assert out is loc


async def test_find_element_ref_bad_format(mgr, fake_playwright):
    await mgr.get_page("s-1", "t-a")
    with pytest.raises(ValueError, match="ref 格式"):
        await mgr.find_element("s-1", "t-a", ref="abc")


async def test_find_element_ref_stale_message(mgr, fake_playwright):
    page = await mgr.get_page("s-1", "t-a")
    page.locator = MagicMock(return_value=_Loc(0))
    with pytest.raises(ValueError, match="已失效"):
        await mgr.find_element("s-1", "t-a", ref="e12")


async def test_find_element_explicit_selector_first(mgr, fake_playwright):
    """显式 selector 优先于 role+name (agent 明确给了就不猜)。"""
    page = await mgr.get_page("s-1", "t-a")
    sel_loc, role_loc = _Loc(1), _Loc(1)
    page.locator = MagicMock(return_value=sel_loc)
    page.get_by_role = MagicMock(return_value=role_loc)
    out = await mgr.find_element("s-1", "t-a", role="button", name="x", selector="#y")
    assert out is sel_loc
    page.get_by_role.assert_not_called()


async def test_find_element_text_entry_fallback_to_css(mgr, fake_playwright):
    """role+name 不中 → placeholder/label 不中 → CSS 候选命中 (豆包 textarea 案例)。"""
    page = await mgr.get_page("s-1", "t-a")
    zero = _Loc(0)
    ta = _Loc(1)
    page.get_by_role = MagicMock(return_value=zero)
    page.get_by_placeholder = MagicMock(return_value=zero)
    page.get_by_label = MagicMock(return_value=zero)
    page.locator = MagicMock(side_effect=lambda s: ta if s == "textarea" else _Loc(0))
    out = await mgr.find_element("s-1", "t-a", role="textbox", name="发消息")
    assert out is ta


async def test_find_element_fallback_prefers_visible(mgr, fake_playwright):
    """CSS 候选多个命中时取第一个可见 (站点常藏重复隐藏输入框)。"""
    page = await mgr.get_page("s-1", "t-a")
    zero = _Loc(0)
    page.get_by_role = MagicMock(return_value=zero)
    page.get_by_placeholder = MagicMock(return_value=zero)
    page.get_by_label = MagicMock(return_value=zero)
    multi = MagicMock()
    multi.count = AsyncMock(return_value=2)
    hidden, shown = MagicMock(), MagicMock()
    hidden.is_visible = AsyncMock(return_value=False)
    shown.is_visible = AsyncMock(return_value=True)
    multi.nth = MagicMock(side_effect=[hidden, shown])
    multi.first = hidden
    page.locator = MagicMock(return_value=multi)
    out = await mgr.find_element("s-1", "t-a", role="textbox", name="x")
    assert out is shown


async def test_find_element_miss_lists_family_candidates(mgr, fake_playwright):
    """彻底不中 → 错误附同族元素清单(含 ref), 把死路变成自救信号。"""
    page = await mgr.get_page("s-1", "t-a")
    page.get_by_role = MagicMock(return_value=_Loc(0))
    page.get_by_placeholder = MagicMock(return_value=_Loc(0))
    page.get_by_label = MagicMock(return_value=_Loc(0))
    body = MagicMock()
    body.aria_snapshot = AsyncMock(
        return_value='- searchbox "输入搜索词" [active] [ref=e6] [box=1,2,3,4]\n'
    )
    page.locator = MagicMock(side_effect=lambda s: body if s == "body" else _Loc(0))
    with pytest.raises(ValueError) as ei:
        await mgr.find_element("s-1", "t-a", role="textbox", name="Search the web")
    msg = str(ei.value)
    assert "输入类" in msg and "searchbox" in msg and "ref=e6" in msg



