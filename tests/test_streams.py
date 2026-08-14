"""StreamStore 流式缓冲: 增量/替换/容量缝合/页面 backstop/失效保留。"""

from __future__ import annotations

from nexus_browser.streams import StreamStore


class _P:
    """假 page: 只需 id 稳定。"""


def test_first_record_stores_full_text():
    store = StreamStore()
    st, created = store.get_or_create(_P(), ".msg")
    assert created
    assert store.record(st, "你好") == "你好"
    assert store.full_text(st) == "你好"


def test_append_returns_only_suffix():
    store = StreamStore()
    st, _ = store.get_or_create(_P(), ".msg")
    store.record(st, "你好")
    assert store.record(st, "你好,世界") == ",世界"
    assert store.full_text(st) == "你好,世界"


def test_no_change_empty_delta():
    store = StreamStore()
    st, _ = store.get_or_create(_P(), ".msg")
    store.record(st, "abc")
    assert store.record(st, "abc") == ""


def test_replacement_keeps_old_and_marks():
    """占位符被整体替换(流式回复常态): 旧内容不丢, 打替换标记。"""
    store = StreamStore()
    st, _ = store.get_or_create(_P(), ".msg")
    store.record(st, "30 秒")
    assert store.record(st, "这是一段全新的回复") == "这是一段全新的回复"
    text = store.full_text(st)
    assert "[内容被替换]" in text
    assert "30 秒" in text


def test_stream_cap_drops_oldest_with_seam():
    store = StreamStore(char_cap=20)
    st, _ = store.get_or_create(_P(), ".msg")
    store.record(st, "a" * 15)
    store.record(st, "a" * 15 + "b" * 10)  # 增量 "b"*10 → 总量 25 > 20
    text = store.full_text(st)
    assert "[...已丢弃" in text
    assert st.dropped_chars == 15
    assert st.char_count <= 20
    assert "b" * 10 in text  # 最新内容保留


def test_single_chunk_overflow_also_dropped():
    """单次采样就超限 → 也丢(只留缝合标记), 不许永远超容量。"""
    store = StreamStore(char_cap=10)
    st, _ = store.get_or_create(_P(), ".msg")
    store.record(st, "x" * 50)
    assert st.char_count == 0
    assert st.dropped_chars == 50
    assert "[...已丢弃 50 字符...]" in store.full_text(st)


def test_page_cap_backstop_drops_coldest_stream():
    store = StreamStore(char_cap=100, page_cap=50)
    p = _P()
    s1, _ = store.get_or_create(p, ".a")
    s2, _ = store.get_or_create(p, ".b")
    store.record(s1, "x" * 40)
    s1.last_chunk_ts = 0  # 强制 s1 更"冷" (monotonic 同 tick 下排序不确定)
    store.record(s2, "y" * 40)  # 总 80 > 50 → 从 s1 丢
    assert s1.char_count + s2.char_count <= 50
    assert s1.dropped_chars > 0
    assert s2.dropped_chars == 0  # 新流不动


def test_invalidate_keeps_buffer_blocks_reuse():
    store = StreamStore()
    p = _P()
    st, _ = store.get_or_create(p, ".msg")
    store.record(st, "部分内容")
    assert store.invalidate_page(p, "页面已导航") == 1
    assert st.dead == "页面已导航"
    assert store.full_text(st) == "部分内容"  # 缓冲保留可读
    assert store.find(p, ".msg") is None      # 但不再被复用
    # 同选择器新建 → 新流
    st2, created = store.get_or_create(p, ".msg")
    assert created and st2 is not st


def test_get_or_create_resumes_same_target():
    store = StreamStore()
    p = _P()
    st1, c1 = store.get_or_create(p, ".msg")
    st2, c2 = store.get_or_create(p, ".msg")
    assert c1 and not c2 and st1 is st2
    _, c3 = store.get_or_create(p, ".other")
    assert c3
