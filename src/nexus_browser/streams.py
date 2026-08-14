"""流式内容环形缓冲: 按 (page, selector) watch 目标累积增量文本。

设计约定(与 agent 的契约):
- 每个 watch(page+selector)一条流; page 仅作生命周期索引, 不做内容归属。
- 增量: 纯追加只存后缀; 非纯追加(占位符被整体替换)整段重存并打 [内容被替换] 标记。
- 容量: 单流字符上限, 溢出丢最旧 chunk, 缝合处保留 [...已丢弃 N 字符...] 标记;
  page 总量 backstop 防多流打爆内存。丢内容可以, 丢得无声无息不行。
- 失效: 页面导航/关闭/崩溃 → 该页所有流标记 dead, 缓冲仍可读, 读取时显式报告。
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

REPLACED_MARK = "[内容被替换]"


@dataclass
class StreamState:
    stream_id: str
    page_id: int
    selector: str
    cap: int
    chunks: deque = field(default_factory=deque)
    char_count: int = 0          # 不含缝合标记
    dropped_chars: int = 0       # 累计被丢弃字符数
    last_text: str = ""
    last_chunk_ts: float = 0.0
    dead: str | None = None      # 失效原因, None=存活


class StreamStore:
    """流注册表 + 增量/容量/失效语义。纯逻辑, 不碰 Playwright。"""

    def __init__(self, char_cap: int = 16000, page_cap: int = 64000) -> None:
        self.char_cap = char_cap
        self.page_cap = page_cap
        self._streams: dict[str, StreamState] = {}
        self._by_page: dict[int, set[str]] = {}

    # ── 查询 ────────────────────────────────────────────────────────

    def get(self, stream_id: str) -> StreamState | None:
        return self._streams.get(stream_id)

    def find(self, page: Any, selector: str) -> StreamState | None:
        for sid in self._by_page.get(id(page), ()):
            st = self._streams.get(sid)
            if st and st.selector == selector and st.dead is None:
                return st
        return None

    def get_or_create(self, page: Any, selector: str) -> tuple[StreamState, bool]:
        st = self.find(page, selector)
        if st:
            return st, False
        st = StreamState(stream_id=uuid.uuid4().hex[:8], page_id=id(page), selector=selector, cap=self.char_cap)
        self._streams[st.stream_id] = st
        self._by_page.setdefault(id(page), set()).add(st.stream_id)
        return st, True

    # ── 增量写入 ────────────────────────────────────────────────────

    def record(self, st: StreamState, text: str) -> str:
        """写入一次页面文本采样, 返回本次增量(可能为空串)。"""
        if text == st.last_text:
            return ""
        if st.last_text and text.startswith(st.last_text):
            delta = text[len(st.last_text):]
        elif not st.last_text:
            delta = text
        else:
            # 非纯追加: 占位符/内容被整体替换
            delta = text
            self._append(st, REPLACED_MARK)
        st.last_text = text
        if delta:
            self._append(st, delta)
        return delta

    def _append(self, st: StreamState, chunk: str) -> None:
        st.chunks.append(chunk)
        st.char_count += len(chunk)
        st.last_chunk_ts = monotonic()
        self._enforce_stream_cap(st)
        self._enforce_page_cap(st.page_id)

    def _enforce_stream_cap(self, st: StreamState) -> None:
        # 允许丢空: 单 chunk 超限也丢, 只留缝合标记 — agent 可改用普通 read 取 DOM 现状
        while st.char_count > st.cap and st.chunks:
            dropped = st.chunks.popleft()
            if not dropped.startswith("[...已丢弃"):
                st.char_count -= len(dropped)
                st.dropped_chars += len(dropped)
        self._refresh_seam(st)

    def _enforce_page_cap(self, page_id: int) -> None:
        sids = self._by_page.get(page_id)
        if not sids:
            return
        streams = [self._streams[s] for s in sids if s in self._streams]
        total = sum(st.char_count for st in streams)
        if total <= self.page_cap:
            return
        # 从最久未更新的流开始丢
        for st in sorted(streams, key=lambda s: s.last_chunk_ts):
            while total > self.page_cap and st.chunks:
                dropped = st.chunks.popleft()
                if not dropped.startswith("[...已丢弃"):
                    st.char_count -= len(dropped)
                    st.dropped_chars += len(dropped)
                    total -= len(dropped)
            self._refresh_seam(st)

    @staticmethod
    def _refresh_seam(st: StreamState) -> None:
        """缝合标记只反映累计丢弃数; 标记本身不占容量。"""
        if st.dropped_chars <= 0:
            return
        seam = f"[...已丢弃 {st.dropped_chars} 字符...]"
        if st.chunks and st.chunks[0].startswith("[...已丢弃"):
            st.chunks[0] = seam
        else:
            st.chunks.appendleft(seam)

    # ── 生命周期 ────────────────────────────────────────────────────

    def invalidate_page(self, page: Any, reason: str) -> int:
        """页面关闭/崩溃/导航 → 该页所有流失效(缓冲保留可读)。返回失效数量。"""
        n = 0
        for sid in self._by_page.pop(id(page), ()):
            st = self._streams.get(sid)
            if st and st.dead is None:
                st.dead = reason
                n += 1
        return n

    def full_text(self, st: StreamState) -> str:
        return "".join(st.chunks)
