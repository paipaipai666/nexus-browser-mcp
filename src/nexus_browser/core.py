"""BrowserManager: session → 多 task 隔离 + TTL 回收 + 崩溃自愈。

从 AgentNexus agentnexus/tools/browser.py 迁移, 核心变化:
- get_settings() → 构造注入 BrowserSettings
- 单一 task_id 注册表 → session_id → {task_id: TaskState} 双层
- 删除 _run_async/_bg_loop (MCP 常驻 asyncio, 不需要跨线程桥)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]

from nexus_browser.settings import BrowserSettings
from nexus_browser.snapshot import INTERACTIVE_ROLES, _parse_aria_yaml
from nexus_browser.streams import StreamStore

logger = logging.getLogger(__name__)


@dataclass
class TaskState:
    """一个 task 的浏览器资源: 独立 context (isolated) 或独立 page (cdp)。"""

    context: Any | None = None      # isolated 模式: BrowserContext
    pages: list[Any] = field(default_factory=list)
    active_page_idx: int = 0
    pending_new_page: Any | None = None
    # 事件驱动状态 (binding 回调 / 页面事件置位)
    settle_count: int = 0
    settle_event: asyncio.Event = field(default_factory=asyncio.Event)
    nav_event: asyncio.Event = field(default_factory=asyncio.Event)
    # 死亡观测与自愈 (G): 页面最后被外部关闭/崩溃时由事件钩子置位
    last_url: str = ""
    last_title: str = ""
    died_reason: str | None = None      # "closed" | "crashed" | "disconnected"
    pending_notice: str | None = None   # 自愈后待上报的状态变更, 一次性消费

    @property
    def page(self) -> Any:
        """当前激活页面。"""
        return self.pages[self.active_page_idx] if self.pages else None


@dataclass
class SessionState:
    """一个 MCP session 的浏览器资源, 管理多个 task。"""

    shared_context: Any | None = None   # cdp 模式: 共享 BrowserContext
    tasks: dict[str, TaskState] = field(default_factory=dict)


class BrowserManager:
    """单例浏览器 + session→task 页面隔离。"""

    def __init__(self, settings: BrowserSettings) -> None:
        self._settings = settings
        self._browser: Any = None
        self._persistent_context: Any = None  # user_data_dir 模式: 共享登录态 context
        self._playwright: Any = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, SessionState] = {}
        self._last_access: dict[str, dict[str, float]] = {}  # session -> task -> ts
        self._active_tasks: dict[str, set[str]] = {}  # session -> set(task)
        self._evicted_snapshots: dict[str, dict[str, dict]] = {}  # session -> task -> {url,title}
        self._browser_ready = False
        self._browser_disconnected = False   # disconnected 事件置位 → 下次 get_page 整链重建
        self._ttl_task: Any = None
        self._ttl_enabled = True
        # 事件驱动: context 级 binding 注册记录 + page→task 归属
        self._watched_contexts: set[int] = set()
        self._page_hooked_contexts: set[int] = set()  # 每 context 只挂一次 page 监听 (防共享 context 串扰)
        self._page_owners: dict[int, tuple[str, str]] = {}  # id(page) -> (session, task)
        # H-1: 流式内容环形缓冲 (page 关闭/导航时按 page 失效)
        self.streams = StreamStore(settings.stream_char_cap, settings.stream_page_cap)

    # ── 浏览器生命周期 ──────────────────────────────────────────────

    async def ensure_browser(self) -> Any:
        if self._browser_ready and self._browser is not None:
            return self._browser
        async with self._lock:
            return await self._ensure_browser_inner()

    async def _ensure_browser_inner(self) -> Any:
        """调用方必须已持有 self._lock。"""
        if self._browser_ready and self._browser is not None:
            return self._browser
        if async_playwright is None:
            raise RuntimeError("playwright 未安装。请执行: pip install playwright && playwright install chromium")
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        s = self._settings
        if s.mode == "cdp":
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(s.cdp_endpoint)
            except Exception as e:
                raise RuntimeError(
                    f"CDP 连接失败: {s.cdp_endpoint} ({e})。"
                    "请先启动带调试端口的浏览器, 例如: chrome --remote-debugging-port=9222。"
                ) from e
        else:
            launch_kwargs: dict = {"headless": s.headless}
            if s.channel:
                launch_kwargs["channel"] = s.channel
            if s.user_data_dir:
                # 共享登录态: 整个浏览器一个 persistent context。
                # 重建时旧进程可能未退净(profile 锁) → 失败重试一次再报明确错误。
                try:
                    self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=s.user_data_dir, **launch_kwargs,
                    )
                except Exception as e:
                    logger.warning("persistent context 启动失败(%s), 1s 后重试", e)
                    await asyncio.sleep(1)
                    try:
                        self._persistent_context = await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=s.user_data_dir, **launch_kwargs,
                        )
                    except Exception as e2:
                        raise RuntimeError(
                            f"persistent profile 启动失败(目录可能被占用): {s.user_data_dir} ({e2})。"
                            "请确认没有残留的浏览器进程占用该目录。"
                        ) from e2
                self._browser = self._persistent_context.browser
            else:
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        # 浏览器进程断连观测: 用户手动关窗/进程被杀 → 标记, 下次 get_page 整链重建
        try:
            self._browser.on("disconnected", lambda *_: self._on_browser_disconnected())
        except Exception:
            pass
        self._browser_disconnected = False
        self._browser_ready = True
        if self._ttl_enabled and self._ttl_task is None:
            self._ttl_task = asyncio.create_task(self._ttl_cleanup_loop())
        return self._browser

    # ── session / task 生命周期 ─────────────────────────────────────

    def _task(self, session_id: str, task_id: str) -> TaskState:
        return self._sessions[session_id].tasks[task_id]

    def _on_browser_disconnected(self) -> None:
        self._browser_disconnected = True
        logger.warning("浏览器进程已断连(外部关闭?), 下次 get_page 将整链重建")

    async def get_page(self, session_id: str, task_id: str, *, for_navigation: bool = False) -> Any:
        """取或建一个 task 的页面。

        已有 task 先做活性探测: page 被外部关闭/崩溃或浏览器断连 → 自愈重建,
        并尽量恢复上次 URL (for_navigation=True 时跳过恢复, 调用方马上要导航)。
        """
        session = self._sessions.get(session_id)
        ts = session.tasks.get(task_id) if session else None
        if ts is not None:
            page = ts.page
            if page is not None and not page.is_closed() and not self._browser_disconnected:
                self._touch(session_id, task_id)
                if page.url and page.url != "about:blank":
                    ts.last_url = page.url
                return page

        async with self._lock:
            session = self._sessions.setdefault(session_id, SessionState())
            ts = session.tasks.get(task_id)
            if ts is None:
                return await self._create_task_locked(session_id, task_id, for_navigation=for_navigation)
            page = ts.page
            if page is not None and not page.is_closed() and not self._browser_disconnected:
                self._touch(session_id, task_id)
                return page
            return await self._heal_task_locked(session_id, task_id, for_navigation=for_navigation)

    async def _create_task_locked(self, session_id: str, task_id: str, *, for_navigation: bool = False) -> Any:
        """调用方必须已持有 self._lock。全新创建 task 的 context/page。"""
        session = self._sessions[session_id]
        browser = await self._ensure_browser_inner()
        s = self._settings

        if s.mode == "cdp" or self._persistent_context is not None:
            if session.shared_context is None:
                if self._persistent_context is not None:
                    session.shared_context = self._persistent_context
                elif browser.contexts:
                    session.shared_context = browser.contexts[0]
                else:
                    session.shared_context = await browser.new_context()
            ctx = session.shared_context
            task_ctx = None
        else:
            task_ctx = await browser.new_context(
                viewport={"width": s.viewport_width, "height": s.viewport_height},
            )
            ctx = task_ctx

        self._hook_context_page(session_id, ctx, owner_task_id=task_id if task_ctx is not None else None)
        await self._register_settle_binding(ctx)
        page = await ctx.new_page()
        ts = TaskState(context=task_ctx, pages=[page])
        session.tasks[task_id] = ts
        self._hook_page(session_id, task_id, page)
        self._touch(session_id, task_id)

        # TTL 回收过的 task 重建 → 恢复上次页面 (回收前存了 url/title 快照)
        snap = self.get_evicted_snapshot(session_id, task_id)
        if snap and snap.get("url") and snap["url"] not in ("about:blank", "unknown"):
            ts.last_url = snap["url"]
            ts.last_title = snap.get("title", "")
            restored = False if for_navigation else await self._restore_url(ts, page)
            ts.pending_notice = self._make_notice("evicted", ts.last_url, restored)
        return page

    async def _heal_task_locked(self, session_id: str, task_id: str, *, for_navigation: bool = False) -> Any:
        """调用方必须已持有 self._lock。task 的 page/context/browser 已死 → 最小单元重建。"""
        session = self._sessions[session_id]
        ts = session.tasks[task_id]
        reason = ts.died_reason or ("disconnected" if self._browser_disconnected else "closed")

        if self._browser_disconnected:
            if self._settings.mode == "cdp":
                # 用户的 Chrome, 不能替他重启 → 明确报错, 不静默换新浏览器
                raise RuntimeError(
                    "CDP 浏览器已断开(你的 Chrome 可能已退出)。"
                    "请重启 chrome --remote-debugging-port=9222 后重试。"
                )
            # 整链重建: 浏览器进程已死, 旧 context/page 引用全部作废
            self._browser = None
            self._persistent_context = None
            self._browser_ready = False
            self._watched_contexts.clear()
            self._page_hooked_contexts.clear()
            for old in ts.pages:
                self._page_owners.pop(id(old), None)
            browser = await self._ensure_browser_inner()
            if self._persistent_context is not None:
                session.shared_context = self._persistent_context
                ctx, task_ctx = session.shared_context, None
            else:
                task_ctx = await browser.new_context(
                    viewport={"width": self._settings.viewport_width, "height": self._settings.viewport_height},
                )
                ctx = task_ctx
            self._hook_context_page(session_id, ctx, owner_task_id=task_id if task_ctx is not None else None)
            await self._register_settle_binding(ctx)
            page = await ctx.new_page()
            ts.context = task_ctx
            ts.pages = [page]
            ts.active_page_idx = 0
            self._hook_page(session_id, task_id, page)
        else:
            # 仅 page 死 (标签页被关/崩溃): context 活着, 登录态/DOM 之外的会话不丢
            ctx = session.shared_context if session.shared_context is not None else ts.context
            if ctx is None:
                session.tasks.pop(task_id, None)
                return await self._create_task_locked(session_id, task_id, for_navigation=for_navigation)
            try:
                page = await ctx.new_page()
            except Exception:
                # context 实际也死了(探测盲区) → 升级为整链重建
                if self._settings.mode == "cdp":
                    raise RuntimeError(
                        "CDP 浏览器上下文已失效。请确认 chrome --remote-debugging-port=9222 在线后重试。"
                    )
                self._browser_disconnected = True
                return await self._heal_task_locked(session_id, task_id, for_navigation=for_navigation)
            for old in ts.pages:
                self._page_owners.pop(id(old), None)
            ts.pages = [page]
            ts.active_page_idx = 0
            self._hook_page(session_id, task_id, page)

        ts.died_reason = None
        self._touch(session_id, task_id)
        url = ts.last_url
        restored = False
        if url and url != "about:blank" and not for_navigation:
            restored = await self._restore_url(ts, page)
        ts.pending_notice = self._make_notice(reason, url, restored)
        return page

    async def _restore_url(self, ts: TaskState, page: Any) -> bool:
        """尽力恢复上次 URL: 失败(站点挂/网络错)不阻断, 只记日志。"""
        try:
            await page.goto(ts.last_url, wait_until="domcontentloaded", timeout=self._settings.default_timeout_ms)
            return True
        except Exception as e:
            logger.info("恢复上次页面失败 (url=%s): %s", ts.last_url, e)
            return False

    def _make_notice(self, reason: str, url: str, restored: bool) -> str:
        cause = {
            "closed": "页面已被外部关闭",
            "crashed": "页面已崩溃",
            "disconnected": "浏览器进程已退出",
            "evicted": "task 空闲超时被自动回收",
        }.get(reason, "页面资源已失效")
        if url in ("", "about:blank", "unknown"):
            url = ""
        head = f"[状态变更] {cause}, 已自动重建"
        if restored and url:
            head += f"并恢复上次页面: {url}。"
        elif url:
            head += f"。上次页面: {url} (未恢复)。"
        else:
            head += "。"
        tail = "DOM 状态(滚动位置/未提交表单/SPA 路由)已重置。"
        if reason == "disconnected" and self._settings.user_data_dir:
            tail = "登录态保留(persistent profile)。" + tail
        return head + tail

    def pop_notice(self, session_id: str, task_id: str) -> str | None:
        """取出并清除 task 的待上报状态变更(一次性)。"""
        session = self._sessions.get(session_id)
        ts = session.tasks.get(task_id) if session else None
        if not ts:
            return None
        notice = ts.pending_notice
        ts.pending_notice = None
        return notice

    def pin(self, session_id: str, task_id: str) -> None:
        """操作进行中钉住 task, TTL 不回收。"""
        self._active_tasks.setdefault(session_id, set()).add(task_id)

    def unpin(self, session_id: str, task_id: str) -> None:
        self._active_tasks.get(session_id, set()).discard(task_id)

    def _touch(self, session_id: str, task_id: str) -> None:
        # 只刷新活跃时间; "操作中不回收"由 pin/unpin 在工具调用边界维护,
        # 否则 task 一旦用过就永远"活跃", TTL 回收永远触发不了。
        self._last_access.setdefault(session_id, {})[task_id] = monotonic()

    async def _register_settle_binding(self, ctx: Any) -> None:
        """context 级注册 __nexusSettleReport binding (每 context 一次)。

        页面内防抖计时器静默时调用它 → 回调置位对应 task 的 settle_event。
        CDP binding 不经页面 eval, 不受 CSP 限制。注册失败则退化为轮询兜底。
        """
        key = id(ctx)
        if key in self._watched_contexts:
            return
        self._watched_contexts.add(key)

        def _handler(source, *args):
            page = source.get("page") if isinstance(source, dict) else None
            owner = self._page_owners.get(id(page)) if page is not None else None
            if not owner:
                return
            sid, tid = owner
            session = self._sessions.get(sid)
            ts = session.tasks.get(tid) if session else None
            if ts:
                ts.settle_count += 1
                ts.settle_event.set()

        try:
            await ctx.expose_binding("__nexusSettleReport", _handler)
        except Exception as e:
            self._watched_contexts.discard(key)
            logger.debug("settle binding 注册失败, 回退轮询: %s", e)

    def _hook_context_page(self, session_id: str, ctx: Any, owner_task_id: str | None = None) -> None:
        """每 context 只挂一次 page 监听。

        共享 context (cdp/persistent) 下若按 task 各挂一次, 任意新标签会扇出到
        所有 task (pages 互相污染)。统一一个 handler: isolated 独占 context 用
        owner_task_id 精确归属; 共享 context 归"最近活跃"的 task — 触发弹窗/
        新标签的操作刚 touch 过它, 常态下归属正确。
        """
        key = id(ctx)
        if key in self._page_hooked_contexts:
            return
        self._page_hooked_contexts.add(key)

        def _on_page(page: Any) -> None:
            tid = owner_task_id or self._most_recent_task(session_id)
            if tid:
                self._on_new_page(session_id, tid, page)

        ctx.on("page", _on_page)

    def _most_recent_task(self, session_id: str) -> str | None:
        session = self._sessions.get(session_id)
        if not session or not session.tasks:
            return None
        access = self._last_access.get(session_id, {})
        return max(session.tasks, key=lambda t: access.get(t, 0.0))

    def _hook_page(self, session_id: str, task_id: str, page: Any) -> None:
        """登记 page→task 归属 + 挂导航/关闭/崩溃事件钩子。"""
        self._page_owners[id(page)] = (session_id, task_id)
        ts = self._task(session_id, task_id)

        def _notify_nav(*_args):
            ts.nav_event.set()

        def _on_close(*_args):
            self._page_owners.pop(id(page), None)  # 自清, 防 id 复用后脏路由
            if ts.died_reason is None:
                ts.died_reason = "closed"
            self.streams.invalidate_page(page, "页面已关闭")

        def _on_crash(*_args):
            ts.died_reason = "crashed"
            self.streams.invalidate_page(page, "页面已崩溃")

        try:
            page.on("framenavigated", _notify_nav)
            page.on("load", _notify_nav)
            page.on("close", _on_close)
            page.on("crash", _on_crash)
        except Exception:
            pass

    async def ensure_task(self, session_id: str, task_id: str) -> TaskState:
        """确保 task 存在并返回其状态 (自动建 page)。"""
        await self.get_page(session_id, task_id)
        return self._task(session_id, task_id)

    def _on_new_page(self, session_id: str, task_id: str, page: Any) -> None:
        if id(page) in self._page_owners:
            return  # 我们 create/heal 时 new_page 出来的 page 也会触发 context "page" 事件
        session = self._sessions.get(session_id)
        if not session or task_id not in session.tasks:
            logger.debug("session/task 未就绪, 跳过新标签事件: %s/%s", session_id, task_id)
            return
        ts = session.tasks[task_id]
        ts.pages.append(page)
        ts.active_page_idx = len(ts.pages) - 1
        ts.pending_new_page = page
        self._hook_page(session_id, task_id, page)
        if page.url and page.url != "about:blank":
            ts.last_url = page.url
        ts.nav_event.set()  # 事件驱动 wait_navigation
        logger.info("session=%s task=%s 新标签: %s", session_id, task_id, page.url)

    def consume_pending_new_page(self, session_id: str, task_id: str) -> Any:
        session = self._sessions.get(session_id)
        if not session:
            return None
        ts = session.tasks.get(task_id)
        if not ts:
            return None
        pending = ts.pending_new_page
        ts.pending_new_page = None
        return pending

    # ── 页面操作 ────────────────────────────────────────────────────

    # 文本输入族: role+name 不中时允许族内回退 (placeholder/label/CSS 候选)。
    # 回退只在同语义族内发生 —— 跨族乱试可能点错元素, 比报错更糟。
    TEXT_ENTRY_ROLES = frozenset({"textbox", "searchbox", "combobox"})
    _TEXT_ENTRY_CSS = ("textarea", "input[type=search]", "input[type=text]",
                       "input:not([type])", "[contenteditable=true]")

    @staticmethod
    async def _first_visible(loc: Any) -> Any:
        """多个命中时优先第一个可见元素 (站点常藏重复隐藏输入框)。"""
        try:
            for i in range(min(await loc.count(), 5)):
                el = loc.nth(i)
                if await el.is_visible():
                    return el
        except Exception:
            pass
        return loc.first

    async def _miss_message(self, page: Any, what: str, role: str | None = None) -> str:
        """定位失败 → 附当前页面同族元素清单(含 ref), 把死路变成 agent 的自救信号。"""
        base = f"找不到元素 {what}。"
        try:
            raw = await page.locator("body").aria_snapshot()
            nodes = _parse_aria_yaml(raw)
            in_entry_family = role in self.TEXT_ENTRY_ROLES
            family = self.TEXT_ENTRY_ROLES if in_entry_family else INTERACTIVE_ROLES
            hints = [n for n in nodes if n["role"] in family][:6]
            if hints:
                kind = "输入类" if in_entry_family else "可交互"
                lines = "\n".join(
                    f'  {n["role"]} "{n["name"]}" ref={n["ref"] or "-"}' for n in hints
                )
                return f"{base}\n当前页面可用{kind}元素:\n{lines}\n可直接用 ref 重试, 或 browser_snapshot 查看全部。"
        except Exception:
            pass
        return base + "请调用 browser_snapshot 查看当前可交互元素。"

    async def find_element(
        self,
        session_id: str,
        task_id: str,
        ref: str | None = None,
        role: str | None = None,
        name: str | None = None,
        selector: str | None = None,
        pos: str | None = None,
    ) -> Any:
        """定位元素: pos(坐标) > ref(快照句柄) > selector(显式CSS) > role+name > role > name(文本)。

        pos 原样返回供坐标操作。ref 走 Playwright aria-ref 选择器引擎。
        role+name 不中对文本输入族依次回退 placeholder → label → CSS 候选。
        """
        page = await self.get_page(session_id, task_id)
        if pos:
            return pos
        if selector:
            loc = page.locator(selector)
            if await loc.count() > 0:
                return await self._first_visible(loc)
            raise ValueError(f"找不到选择器 {selector} 对应的元素。")
        if ref:
            if not re.fullmatch(r"e\d+", ref):
                raise ValueError(f"ref 格式应为 e+数字 (来自 browser_snapshot 输出), 收到: {ref!r}")
            loc = page.locator(f"aria-ref={ref}")
            if await loc.count() > 0:
                return loc.first
            raise ValueError(f"ref={ref} 已失效 (导航或重新快照后 ref 会变化)。请重新 browser_snapshot 获取。")
        if role and name:
            for exact in (False, True):
                try:
                    loc = page.get_by_role(role, name=name, exact=exact)
                    if await loc.count() > 0:
                        return loc.first
                except Exception:
                    pass
            if role in self.TEXT_ENTRY_ROLES:
                for getter in (page.get_by_placeholder, page.get_by_label):
                    try:
                        loc = getter(name)
                        if await loc.count() > 0:
                            return loc.first
                    except Exception:
                        pass
                for css in self._TEXT_ENTRY_CSS:
                    try:
                        loc = page.locator(css)
                        if await loc.count() > 0:
                            return await self._first_visible(loc)
                    except Exception:
                        pass
            raise ValueError(await self._miss_message(page, f"role={role} name={name!r}", role))
        if role:
            try:
                loc = page.get_by_role(role)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass
            raise ValueError(await self._miss_message(page, f"role={role}", role))
        if name:
            try:
                loc = page.get_by_text(name, exact=False)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass
            raise ValueError(f"找不到包含文本 \"{name}\" 的元素。")
        raise ValueError("必须提供 pos、ref、selector、role+name 或 name 中的至少一个参数。")

    async def list_pages(self, session_id: str, task_id: str) -> list[dict]:
        session = self._sessions.get(session_id)
        if not session or task_id not in session.tasks:
            return []
        ts = session.tasks[task_id]
        result = []
        for i, page in enumerate(ts.pages):
            try:
                result.append({
                    "index": i,
                    "url": page.url,
                    "title": await page.title(),
                    "active": i == ts.active_page_idx,
                    "alive": not page.is_closed(),
                })
            except Exception:
                result.append({"index": i, "url": "unknown", "title": "unknown",
                               "active": i == ts.active_page_idx, "alive": False})
        return result

    async def switch_page(self, session_id: str, task_id: str, index: int) -> Any:
        ts = self._task(session_id, task_id)
        if not ts.pages:
            raise ValueError(f"session={session_id} task={task_id} 没有打开的页面")
        if index < 0 or index >= len(ts.pages):
            raise ValueError(f"页面索引 {index} 超出范围(共 {len(ts.pages)} 个)")
        ts.active_page_idx = index
        self._touch(session_id, task_id)
        return ts.pages[index]

    async def navigate(self, session_id: str, task_id: str, url: str, wait_until: str = "load") -> dict:
        # for_navigation=True: 自愈/回收恢复时不再先跳回旧 URL (调用方马上要导航)
        page = await self.get_page(session_id, task_id, for_navigation=True)
        ts = self._task(session_id, task_id)
        s = self._settings
        timeout = s.default_timeout_ms
        timed_out = False

        if wait_until == "networkidle":
            try:
                await asyncio.wait_for(page.goto(url, wait_until="load", timeout=timeout), timeout=timeout / 1000)
                try:
                    await asyncio.wait_for(page.wait_for_load_state("networkidle"), timeout=s.networkidle_timeout_ms / 1000)
                except asyncio.TimeoutError:
                    timed_out = True
                    logger.warning("networkidle 超时 (%dms), 继续", s.networkidle_timeout_ms)
            except Exception as e:
                return {"title": "", "url": url, "readyState": "unknown", "error": str(e),
                        "timed_out": "Timeout" in type(e).__name__}
        else:
            try:
                await page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as e:
                # timed_out 只标真正的超时; 关闭/崩溃等错误由上层分类提示
                return {"title": "", "url": url, "readyState": "unknown", "error": str(e),
                        "timed_out": "Timeout" in type(e).__name__}

        last = page.url or url
        if last != "about:blank":
            ts.last_url = last
        self.streams.invalidate_page(page, "页面已导航")  # 旧 DOM 上的流全部失效
        try:
            title = await page.title()
            ready_state = await page.evaluate("document.readyState")
            ts.last_title = title
        except Exception:
            title, ready_state = "", "unknown"
        return {"title": title, "url": page.url, "readyState": ready_state, "timed_out": timed_out}

    # ── 回收 ────────────────────────────────────────────────────────

    async def close_task(self, session_id: str, task_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        ts = session.tasks.pop(task_id, None)
        if ts:
            for page in ts.pages:
                self._page_owners.pop(id(page), None)  # 清理归属映射
                self.streams.invalidate_page(page, "task 已关闭")
                try:
                    await page.close()
                except Exception:
                    pass
            if ts.context:  # isolated
                self._watched_contexts.discard(id(ts.context))
                self._page_hooked_contexts.discard(id(ts.context))
                try:
                    await ts.context.close()
                except Exception:
                    pass
        self._last_access.get(session_id, {}).pop(task_id, None)
        self._active_tasks.get(session_id, set()).discard(task_id)
        if not session.tasks and session.shared_context is None:
            self._sessions.pop(session_id, None)
            self._last_access.pop(session_id, None)
            self._active_tasks.pop(session_id, None)

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            return
        for task_id in list(session.tasks):
            await self.close_task(session_id, task_id)
        self._last_access.pop(session_id, None)
        self._active_tasks.pop(session_id, None)

    async def _save_task_snapshot(self, session_id: str, task_id: str) -> dict:
        ts = self._task(session_id, task_id)
        if not ts.pages:
            return {}
        try:
            page = ts.pages[ts.active_page_idx]
            return {"url": page.url, "title": await page.title(), "evicted_at": monotonic()}
        except Exception:
            return {"url": "unknown", "evicted_at": monotonic()}

    def get_evicted_snapshot(self, session_id: str, task_id: str) -> dict | None:
        return self._evicted_snapshots.get(session_id, {}).pop(task_id, None)

    async def _evict_idle_tasks(self) -> None:
        now = monotonic()
        ttl = self._settings.context_ttl_sec
        for session_id in list(self._last_access.keys()):
            active = self._active_tasks.get(session_id, set())
            for task_id in list(self._last_access[session_id].keys()):
                if task_id in active:
                    continue
                elapsed = now - self._last_access[session_id].get(task_id, 0)
                if elapsed > ttl:
                    snapshot = await self._save_task_snapshot(session_id, task_id)
                    self._evicted_snapshots.setdefault(session_id, {})[task_id] = snapshot
                    logger.info(
                        "TTL 回收: session=%s task=%s 空闲 %.0fs, 快照已存 (url=%s)",
                        session_id, task_id, elapsed, snapshot.get("url", "unknown"),
                    )
                    await self.close_task(session_id, task_id)

    async def _ttl_cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._evict_idle_tasks()
        except asyncio.CancelledError:
            return

    async def reset_stale_browser(self, session_id: str, task_id: str) -> None:
        """浏览器被外部关闭时清陈旧状态, 下次 get_page 重建。"""
        session = self._sessions.get(session_id)
        if session:
            session.tasks.pop(task_id, None)
        self._last_access.get(session_id, {}).pop(task_id, None)
        self._active_tasks.get(session_id, set()).discard(task_id)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        self._persistent_context = None
        self._browser_ready = False
        self._browser_disconnected = False
        self._watched_contexts.clear()
        self._page_hooked_contexts.clear()
        logger.info("session=%s task=%s 陈旧浏览器已清, 将重建", session_id, task_id)

    async def close_all(self) -> None:
        if self._ttl_task:
            self._ttl_task.cancel()
            try:
                await self._ttl_task
            except asyncio.CancelledError:
                pass
            self._ttl_task = None
        for session_id in list(self._sessions.keys()):
            await self.close_session(session_id)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._browser_ready = False
        self._browser_disconnected = False
        self._watched_contexts.clear()
        self._page_hooked_contexts.clear()
        self._persistent_context = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ── 供查询 ──────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        out = []
        for sid, session in self._sessions.items():
            out.append({
                "session_id": sid,
                "task_count": len(session.tasks),
                "tasks": [
                    {"task_id": tid, "pages": len(ts.pages)} for tid, ts in session.tasks.items()
                ],
            })
        return out
