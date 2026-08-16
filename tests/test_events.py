"""EventStore 事件缓冲: seq/增量游标/容量/截断/失效保留/页面替换清理。"""

from __future__ import annotations

from nexus_browser.events import (
    KIND_CONSOLE,
    KIND_NAV,
    KIND_PAGEERROR,
    KIND_REQUEST,
    EventStore,
)


class _P:
    """假 page: 只需 id 稳定。"""


def _console(store, page, text, level="log"):
    store.record(page, KIND_CONSOLE, level=level, text=text)


def test_incremental_cursor_returns_only_new():
    store = EventStore()
    p = _P()
    _console(store, p, "a")
    evs, _, _ = store.read(p, "r")
    assert [e.text for e in evs] == ["a"]
    _console(store, p, "b")
    evs, _, more = store.read(p, "r")
    assert [e.text for e in evs] == ["b"] and more == 0
    evs, _, _ = store.read(p, "r")
    assert evs == []


def test_since_zero_returns_full_history():
    store = EventStore()
    p = _P()
    _console(store, p, "a")
    store.read(p, "r")
    _console(store, p, "b")
    evs, _, _ = store.read(p, "r", since=0)
    assert [e.text for e in evs] == ["a", "b"]


def test_independent_cursors_per_reader():
    store = EventStore()
    p = _P()
    _console(store, p, "a")
    store.read(p, "reader-1")
    _console(store, p, "b")
    evs1, _, _ = store.read(p, "reader-1")
    evs2, _, _ = store.read(p, "reader-2")
    assert [e.text for e in evs1] == ["b"]      # 增量
    assert [e.text for e in evs2] == ["a", "b"]  # 新 reader 从头


def test_max_entries_drops_oldest_and_counts():
    store = EventStore(max_entries=3)
    p = _P()
    for i in range(5):
        _console(store, p, f"m{i}")
    evs, buf, _ = store.read(p, "r", since=0)
    assert [e.text for e in evs] == ["m2", "m3", "m4"]
    assert buf.dropped == 2


def test_text_cap_truncates():
    store = EventStore(text_cap=5)
    p = _P()
    _console(store, p, "x" * 10)
    evs, _, _ = store.read(p, "r", since=0)
    assert evs[0].text == "xxxxx…"


def test_limit_paging_reports_more():
    store = EventStore()
    p = _P()
    for i in range(5):
        _console(store, p, f"m{i}")
    evs, _, more = store.read(p, "r", limit=2)
    assert [e.text for e in evs] == ["m0", "m1"] and more == 3
    evs, _, more = store.read(p, "r", limit=2)  # 游标推进, 接着读
    assert [e.text for e in evs] == ["m2", "m3"] and more == 1


def test_kinds_and_match_filtering():
    store = EventStore()
    p = _P()
    _console(store, p, "info", level="log")
    _console(store, p, "bad", level="error")
    store.record(p, KIND_PAGEERROR, level="error", text="boom")
    store.record(p, KIND_REQUEST, method="GET", url="https://a/x", status=200)
    store.record(p, KIND_REQUEST, method="GET", url="https://a/y", status=500)
    evs, _, _ = store.read(p, "r", since=0, kinds={KIND_CONSOLE},
                           match=lambda e: e.level == "error")
    assert [e.text for e in evs] == ["bad"]
    # failed 属性: 网络层失败或 HTTP>=400
    evs, _, _ = store.read(p, "r2", since=0, kinds={KIND_REQUEST},
                           match=lambda e: e.failed)
    assert [e.url for e in evs] == ["https://a/y"]


def test_network_failure_without_response_marked_failed():
    store = EventStore()
    p = _P()
    store.record(p, KIND_REQUEST, method="POST", url="https://a/api",
                 status=None, failure="net::ERR_FAILED")
    evs, _, _ = store.read(p, "r", since=0, kinds={KIND_REQUEST})
    assert evs[0].failed is True
    assert evs[0].status is None


def test_invalidate_blocks_new_records_but_readable():
    store = EventStore()
    p = _P()
    _console(store, p, "before")
    store.invalidate_page(p, "页面已关闭")
    _console(store, p, "late")  # 迟到事件丢弃
    evs, buf, _ = store.read(p, "r", since=0)
    assert [e.text for e in evs] == ["before"]
    assert buf.dead == "页面已关闭"


def test_drop_page_clears_buffer_and_cursors():
    """id 复用防护: 新页挂接前 drop, 旧缓冲与游标不得残留。"""
    store = EventStore()
    p = _P()
    _console(store, p, "old")
    store.read(p, "r")
    store.drop_page(p)
    _console(store, p, "new")
    evs, _, _ = store.read(p, "r")  # 游标已清 → 从头读, 但只有新事件
    assert [e.text for e in evs] == ["new"]


def test_nav_marker_recorded():
    store = EventStore()
    p = _P()
    store.record(p, KIND_NAV, url="https://a/", text="https://a/")
    evs, _, _ = store.read(p, "r", since=0, kinds={KIND_NAV})
    assert evs[0].url == "https://a/"


# ── 响应句柄 (browser_network_body) ───────────────────────────────


def test_handle_cap_releases_oldest():
    """句柄只保留最近 handle_max 条, 更早的释放但元数据保留。"""
    store = EventStore(handle_max=2)
    p = _P()
    for i in range(4):
        store.record(p, KIND_REQUEST, method="GET", url=f"https://a/{i}",
                     status=200, handle=object())
    evs, _, _ = store.read(p, "r", since=0, kinds={KIND_REQUEST})
    assert [e.url for e in evs] == [f"https://a/{i}" for i in range(4)]
    assert evs[0].handle is None and evs[1].handle is None  # 老的已释放
    assert evs[2].handle is not None and evs[3].handle is not None


def test_find_by_seq():
    store = EventStore()
    p = _P()
    store.record(p, KIND_REQUEST, method="GET", url="https://a/x", status=200)
    seq = store._seq
    assert store.find(p, seq).url == "https://a/x"
    assert store.find(p, seq + 99) is None
    assert store.find(_P(), seq) is None  # 别的页查不到
