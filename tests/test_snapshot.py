"""snapshot 模块行为测试: 稳定性判据 + a11y 管线。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus_browser.snapshot import (
    _format_a11y_tree,
    _parse_aria_yaml,
    _snapshot_raw,
    _truncate_by_priority,
    build_watcher_js,
    ensure_watcher,
    get_stable_tree,
    wait_dom_settled,
)


# ── a11y 解析/格式化 ──────────────────────────────────────────────
def test_parse_aria_yaml():
    raw = (
        '- heading "Welcome" [ref=s1e1]\n'
        '- button "Login" [ref=s1e3] [visible]\n'
        '- generic [ref=s2e1]:\n'
        '    - textbox [ref=s2e2]'
    )
    nodes = _parse_aria_yaml(raw)
    assert nodes[0]["role"] == "heading"
    assert nodes[0]["name"] == "Welcome"
    assert nodes[0]["ref"] == "s1e1"
    assert nodes[1]["attrs"] == "visible"
    # generic 带子节点: 无冒号截断失败的行跳过? 这里两行都应被识别
    assert nodes[2]["role"] == "generic"
    assert nodes[3]["role"] == "textbox"


# 真实捕获的 aria_snapshot(mode="ai", boxes=True) 输出 (2026-08 探针)。
# 旧正则对这三类行全部静默丢弃: 属性在 ref 前 / 多属性组 / 冒号后行内文本。
REAL_EXAMPLE_COM = (
    "- generic [ref=e2] [box=256,108,768,122]:\n"
    '  - heading "Example Domain" [level=1] [ref=e3] [box=256,108,768,30]\n'
    "  - paragraph [ref=e4] [box=256,154,768,40]: This domain is for use in documentation examples "
    "without needing permission. Avoid use in operations.\n"
    "  - paragraph [ref=e5] [box=256,210,768,20]:\n"
    '    - link "Learn more" [ref=e6] [cursor=pointer] [box=256,210,87,20]:\n'
    "      - /url: https://iana.org/domains/example\n"
)

REAL_BING_SEARCHBOX = '      - searchbox "输入搜索词" [active] [ref=e6] [box=356,148,549,45]\n'


def test_parse_real_fixture_nothing_dropped():
    """example.com 真实输出: 5 个节点全部存活 (旧解析器丢 3 个)。"""
    nodes = _parse_aria_yaml(REAL_EXAMPLE_COM)
    roles = [n["role"] for n in nodes]
    assert roles == ["generic", "heading", "paragraph", "paragraph", "link"]


def test_parse_attrs_any_order_and_multiplicity():
    """[level=1] 在 [ref] 前; link 有两个附加属性组 —— 全部保留。"""
    nodes = _parse_aria_yaml(REAL_EXAMPLE_COM)
    heading = nodes[1]
    assert heading["ref"] == "e3"
    assert heading["attrs"] == "level=1"
    assert heading["box"] == [256.0, 108.0, 768.0, 30.0]
    link = nodes[4]
    assert link["ref"] == "e6"
    assert link["attrs"] == "cursor=pointer"


def test_parse_inline_text_and_url_attachment():
    """paragraph 行内文本 → text; /url 伪节点挂到父 link。"""
    nodes = _parse_aria_yaml(REAL_EXAMPLE_COM)
    assert nodes[2]["text"].startswith("This domain is for use")
    assert nodes[4]["url"] == "https://iana.org/domains/example"
    assert nodes[4]["role"] == "link"


def test_parse_bing_searchbox_active_before_ref():
    """Bing 搜索框: [active] 在 [ref] 前 —— Issue B 的直接根因。"""
    nodes = _parse_aria_yaml(REAL_BING_SEARCHBOX)
    assert len(nodes) == 1
    assert nodes[0]["role"] == "searchbox"
    assert nodes[0]["name"] == "输入搜索词"
    assert nodes[0]["ref"] == "e6"
    assert nodes[0]["attrs"] == "active"


def test_format_includes_text_and_url():
    nodes = [{"role": "paragraph", "name": "", "ref": "e4", "attrs": "", "depth": 1,
              "box": [1, 2, 3, 4], "text": "正文内容", "url": ""},
             {"role": "link", "name": "More", "ref": "e6", "attrs": "", "depth": 2,
              "box": None, "text": "", "url": "https://x.com"}]
    out = _format_a11y_tree(nodes, show_url=True)
    assert ": 正文内容" in out
    assert "→ https://x.com" in out
    assert "→" not in _format_a11y_tree(nodes, show_url=False)


def test_include_generic_overrides_mode_filter():
    """include_generic 是显式覆盖, 不被 mode 分支吞掉 (旧版经 MCP 枚举不可达)。"""
    from nexus_browser.snapshot import assemble_snapshot

    nodes = [
        {"role": "generic", "name": "", "ref": "e2", "attrs": "", "depth": 0, "box": None},
        {"role": "button", "name": "OK", "ref": "e3", "attrs": "", "depth": 1, "box": None},
    ]
    out = assemble_snapshot(nodes, (1280, 720), mode="reading", include_generic=True)
    roles = [n["role"] for n in out["detail"]]
    assert "generic" in roles and "button" in roles
    # 不开 generic 时 reading 只剩 button
    out2 = assemble_snapshot(nodes, (1280, 720), mode="reading", include_generic=False)
    assert [n["role"] for n in out2["detail"]] == ["button"]


def test_format_a11y_tree_includes_box_for_unnamed():
    nodes = [
        {"role": "textbox", "name": "", "ref": "s1e2", "attrs": "",
         "box": [400, 620, 760, 24], "viewport_status": "visible"},
        {"role": "button", "name": "Login", "ref": "s1e3", "attrs": "",
         "viewport_status": "visible"},
    ]
    text = _format_a11y_tree(nodes)
    assert "[box=400,620,760,24]" in text
    assert '"Login"' in text


# ── 优先级截断 ────────────────────────────────────────────────────
def test_truncate_priority_in_viewport_interactive_first():
    nodes = [
        {"role": "paragraph", "viewport_status": "visible", "name": "r1"},
        {"role": "button", "viewport_status": "visible", "name": "inter"},
        {"role": "button", "viewport_status": "offscreen", "name": "off"},
        {"role": "heading", "viewport_status": "visible", "name": "rd"},
    ]
    out = _truncate_by_priority(nodes, max_nodes=2)
    assert [n["name"] for n in out] == ["inter", "r1"]


def test_truncate_unbounded():
    nodes = [{"role": "button", "viewport_status": "visible", "name": "b"}]
    assert _truncate_by_priority(nodes, max_nodes=5) == nodes


# ── watcher 注入 ──────────────────────────────────────────────────
def test_settle_watcher_js_registers_observer():
    js = build_watcher_js(800)
    assert "MutationObserver" in js
    assert "__nexusLastMutation" in js
    # 事件驱动: 防抖 setTimeout + binding 回调
    assert "setTimeout" in js
    assert "__nexusSettleReport" in js
    assert "800" in js


async def test_ensure_watcher_calls_add_init_script():
    from nexus_browser.settings import BrowserSettings

    page = AsyncMock()
    await ensure_watcher(page, BrowserSettings(stable_window_ms=800))
    page.add_init_script.assert_awaited_once()
    assert "__nexusSettleReport" in page.add_init_script.call_args.args[0]


# ── 稳定性判据三态 ────────────────────────────────────────────────
class _Page:
    """假 Page: 提供 evaluate 读 __nexusLastMutation。"""

    def __init__(self, last_mutation_ms=None):
        self.add_init_script = AsyncMock()
        self.evaluate = AsyncMock(return_value=last_mutation_ms)


class _Task:
    """假 TaskState: binding 回调会递增 settle_count 并 set event。"""

    def __init__(self):
        self.settle_count = 0
        self.settle_event = asyncio.Event()


async def test_wait_settled_initial_quiet_single_check():
    """已静默 → 单次 evaluate 立即返回, 不走事件也不轮询。"""
    import time

    from nexus_browser.settings import BrowserSettings

    s = BrowserSettings(stable_window_ms=800)
    page = _Page(last_mutation_ms=time.time() * 1000 - 5000)  # 5s 前
    task = _Task()
    await wait_dom_settled(page, s, task=task)
    page.evaluate.assert_awaited_once()
    assert task.settle_count == 0


async def test_wait_settled_event_path_wakes_on_binding():
    """事件驱动: binding 回调递增 settle_count → 立即唤醒, 无轮询。"""
    import time

    from nexus_browser.settings import BrowserSettings

    s = BrowserSettings(stable_window_ms=800, stable_timeout_ms=3000, stable_poll_ms=50)
    page = _Page(last_mutation_ms=time.time() * 1000)  # 刚变异, 未静默
    task = _Task()

    async def fire():
        await asyncio.sleep(0.05)
        task.settle_count += 1
        task.settle_event.set()

    asyncio.create_task(fire())
    t0 = time.time()
    await wait_dom_settled(page, s, task=task)
    elapsed = time.time() - t0
    assert elapsed < 1.0  # 事件唤醒, 远小于 3s 超时
    page.evaluate.assert_awaited_once()  # 只有初始检查, 零轮询


async def test_wait_settled_event_path_timeout_graceful():
    """事件永不触发 → 超时优雅降级, 不抛。"""
    import time

    from nexus_browser.settings import BrowserSettings

    s = BrowserSettings(stable_window_ms=800, stable_timeout_ms=500)
    page = _Page(last_mutation_ms=time.time() * 1000)
    task = _Task()  # 无回调
    assert await wait_dom_settled(page, s, task=task) is None


async def test_wait_settled_polling_fallback_no_task():
    """task=None (无 binding) → 轮询兜底, 静默后返回。"""
    import time

    from nexus_browser.settings import BrowserSettings

    s = BrowserSettings(stable_window_ms=100, stable_poll_ms=30, stable_timeout_ms=1000)
    page = _Page()
    stale = time.time() * 1000 - 5000
    page.evaluate = AsyncMock(side_effect=[time.time() * 1000, stale, stale])
    await wait_dom_settled(page, s, task=None)
    assert page.evaluate.await_count >= 2


async def test_wait_settled_passes_timeout_gracefully():
    """evaluate 一直抛异常(如 CSP 拦注入) → 优雅降级, 不抛。"""
    from nexus_browser.settings import BrowserSettings

    page = _Page()
    page.evaluate = AsyncMock(side_effect=Exception("CSP blocked"))
    s = BrowserSettings(stable_timeout_ms=500, stable_poll_ms=50)
    # 不应抛异常 —— 优雅降级继续
    assert await wait_dom_settled(page, s) is None


# ── get_stable_tree: observer 窗口 + REQUIRED 兜底 ────────────────
def _raw_stable(snapshot):
    """连续 REQUIRED 次快照一致才返回解析结果。"""
    return snapshot


async def test_get_stable_tree_returns_parsed_nodes():
    """稳定窗口内内容不再变化, 一次成功解析。"""
    from nexus_browser.settings import BrowserSettings

    raw = '- button "Login" [ref=s1e3]\n'
    locator = MagicMock()
    locator.aria_snapshot = AsyncMock(return_value=raw)
    page = MagicMock()
    page.locator.return_value = locator
    page.evaluate = AsyncMock(return_value=1)  # epoch ms, 距现在远超窗口 → 立即静默
    s = BrowserSettings(stable_required=1)
    nodes = await get_stable_tree(page, None, s)
    assert nodes[0]["role"] == "button"
    assert nodes[0]["name"] == "Login"


async def test_get_stable_tree_waits_until_consecutive_equal():
    """内容先变后定: 第一次快照被 REQUIRED 判定不一致而重试。"""
    from nexus_browser.settings import BrowserSettings

    raws = [
        '- button "A" [ref=s1]\n',   # 第一次
        '- button "B" [ref=s2]\n',   # 第二次变化
        '- button "B" [ref=s2]\n',   # 第三次与上次一致
        '- button "B" [ref=s2]\n',   # 稳定组 ≥3: B,B,B
        '- button "B" [ref=s2]\n',
    ]
    locator = MagicMock()
    locator.aria_snapshot = AsyncMock(side_effect=raws)
    page = MagicMock()
    page.locator.return_value = locator
    page.evaluate = AsyncMock(return_value=1)  # epoch ms, 距现在远超窗口 → 立即静默
    s = BrowserSettings(stable_required=3)
    nodes = await get_stable_tree(page, None, s)
    assert nodes[0]["name"] == "B"


async def test_get_stable_tree_timeout_graceful():
    """永不稳定的页面: 超时降级, 仍返回最后一次拍到的树。"""
    from nexus_browser.settings import BrowserSettings

    raws = ['- button "X" [ref=s1]\n', '- button "Y" [ref=s2]\n'] * 20
    locator = MagicMock()
    locator.aria_snapshot = AsyncMock(side_effect=raws)
    page = MagicMock()
    page.locator.return_value = locator
    page.evaluate = AsyncMock(return_value=1)  # epoch ms, 距现在远超窗口 → 立即静默
    s = BrowserSettings(stable_required=3, stable_timeout_ms=500, stable_confirm_gap_ms=500)
    nodes = await get_stable_tree(page, None, s)
    assert nodes[0]["name"] in ("X", "Y")


# ── scope 参数 (Issue F): CSS / ref 双格式 ──────────────────────────


def _scope_page(return_value="- main\n", side_effect=None):
    loc = MagicMock()
    loc.aria_snapshot = AsyncMock(return_value=return_value, side_effect=side_effect)
    page = MagicMock()
    page.locator = MagicMock(return_value=loc)
    return page


async def test_snapshot_raw_scope_ref_rewritten():
    """scope='e57' → aria-ref 选择器引擎 (快照 ref 直接用)。"""
    page = _scope_page()
    await _snapshot_raw(page, "e57")
    page.locator.assert_called_with("aria-ref=e57")


async def test_snapshot_raw_scope_css_passthrough():
    page = _scope_page()
    await _snapshot_raw(page, "main.content")
    page.locator.assert_called_with("main.content")


async def test_snapshot_raw_scope_miss_clear_error():
    """scope 未命中 → 明确 ValueError (旧行为: 干等 30s 超时)。"""
    page = _scope_page(side_effect=Exception("Timeout 5000ms exceeded"))
    with pytest.raises(ValueError, match="scope 未匹配"):
        await _snapshot_raw(page, "e99")


# ── Web Vitals 格式化 ─────────────────────────────────────────────


def test_format_vitals_full():
    from nexus_browser.snapshot import format_vitals
    data = {"fcp": 120.4, "lcp": 340, "cls": 0.021, "inp": 48, "ttfb": 80,
            "dcl": 200, "load": 350, "navUrl": "https://a/",
            "resources": [{"name": "https://a/x.png", "type": "img", "duration": 420}]}
    out = format_vitals(data, "https://a/")
    assert "FCP 120ms" in out and "LCP 340ms" in out and "CLS 0.021" in out
    assert "420ms [img] https://a/x.png" in out
    assert "SPA" not in out  # 同源导航不标注


def test_format_vitals_partial_and_spa_note():
    from nexus_browser.snapshot import format_vitals
    out = format_vitals({"fcp": 120, "cls": 0, "navUrl": "https://a/"}, "https://a/page2")
    assert "FCP 120ms" in out and "LCP -" in out  # 缺失显示 -
    assert "SPA 导航不重置" in out  # navUrl ≠ 当前 URL → 标注来源


def test_format_vitals_no_data():
    from nexus_browser.snapshot import format_vitals
    assert "暂无性能数据" in format_vitals(None, "https://a/")
    assert "暂无性能数据" in format_vitals({}, "https://a/")


# ── 快照 diff 指纹 ────────────────────────────────────────────────


def test_nodes_digest_ignores_refs():
    """同内容不同代际 ref → digest 相同 (ref 剥离)。"""
    from nexus_browser.snapshot import nodes_digest
    a = [{"depth": 0, "role": "button", "name": "登录", "ref": "s1e3",
          "attrs": "", "box": [10, 20, 100, 30], "text": ""}]
    b = [dict(a[0], ref="s2e3")]
    assert nodes_digest(a) == nodes_digest(b)


def test_nodes_digest_changes_on_content():
    """name/box/text/节点数 任一变化 → digest 变 (宁可全量, 不谎报无变化)。"""
    from nexus_browser.snapshot import nodes_digest
    base = [{"depth": 0, "role": "button", "name": "登录", "ref": "s1e3",
             "attrs": "", "box": [10, 20, 100, 30], "text": ""}]
    d0 = nodes_digest(base)
    assert nodes_digest([dict(base[0], name="退出")]) != d0
    assert nodes_digest([dict(base[0], box=[0, 20, 100, 30])]) != d0  # 滚动/布局变化
    assert nodes_digest([dict(base[0], text="hi")]) != d0
    assert nodes_digest([base[0], dict(base[0], name="第二个")]) != d0


