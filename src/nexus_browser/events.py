"""页面事件环形缓冲: console / pageerror / network 元数据。

与 agent 的契约(与 streams.py 同构):
- 事件按 page 归属, 每条带全局单调 seq; 读取用 since-cursor 增量(每 page×reader 一个游标)。
- 只记元数据: console 文本/位置, 异常 message, 请求 method/url/status/失败原因。
  绝不记 body —— 体量不可控、携带敏感数据、且是页面方完全控制的注入面。
- 容量: 单页条数上限, 溢出丢最旧并计数; 单条文本/URL 截断。丢事件可以, 丢得无声无息不行。
- 失效: 页面关闭/崩溃 → 标记 dead, 迟到事件丢弃, 缓冲仍可读, 读取时显式报告。
- 导航不丢历史, 打 nav 分界事件(主框架 framenavigated)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

KIND_CONSOLE = "console"
KIND_PAGEERROR = "pageerror"
KIND_REQUEST = "request"
KIND_NAV = "nav"
KIND_DIALOG = "dialog"   # alert/confirm/prompt/beforeunload: 出现与处置全程留痕
KIND_DOWNLOAD = "download"  # 文件下载: 文件名/来源 URL/落盘路径


@dataclass
class Event:
    seq: int
    kind: str
    ts: float
    level: str = ""             # console: log/warning/error/...; pageerror: error
    text: str = ""              # console/pageerror 文本; nav: url
    location: str = ""          # console: url:line
    method: str = ""            # request
    url: str = ""               # request/nav
    status: int | None = None   # request: None=网络层失败(未收到响应)
    failure: str = ""           # requestfailed 原因
    resource_type: str = ""
    handle: Any = None          # Response 句柄(仅部分 request; 不序列化/不格式化, 供按需取 body)

    @property
    def failed(self) -> bool:
        """网络层失败 或 HTTP >= 400。"""
        return bool(self.failure) or (self.status is not None and self.status >= 400)


@dataclass
class PageBuffer:
    events: deque = field(default_factory=deque)
    dropped: int = 0
    dead: str | None = None     # 失效原因, None=存活


class EventStore:
    """事件注册表 + seq/容量/失效/游标语义。纯逻辑, 不碰 Playwright。"""

    def __init__(self, max_entries: int = 500, text_cap: int = 500, handle_max: int = 50) -> None:
        self.max_entries = max_entries
        self.text_cap = text_cap
        self.handle_max = handle_max  # 每页保留响应句柄的最近请求条数 (句柄占浏览器内存)
        self._pages: dict[int, PageBuffer] = {}
        self._seq = 0
        self._cursors: dict[tuple[int, str], int] = {}

    # ── 写入 (core 事件钩子调用) ────────────────────────────────────

    def record(
        self,
        page: Any,
        kind: str,
        *,
        level: str = "",
        text: str = "",
        location: str = "",
        method: str = "",
        url: str = "",
        status: int | None = None,
        failure: str = "",
        resource_type: str = "",
        handle: Any = None,
    ) -> None:
        buf = self._pages.get(id(page))
        if buf is None:
            buf = PageBuffer()
            self._pages[id(page)] = buf
        if buf.dead:
            return  # 死页迟到事件(关闭后的 console 等)不再收
        text = self._clip(text)
        url = self._clip(url, 300)
        location = self._clip(location, 300)
        self._seq += 1
        buf.events.append(Event(
            seq=self._seq, kind=kind, ts=monotonic(), level=level, text=text,
            location=location, method=method, url=url, status=status,
            failure=self._clip(failure, 200), resource_type=resource_type,
            handle=handle,
        ))
        while len(buf.events) > self.max_entries:
            buf.events.popleft()
            buf.dropped += 1
        if handle is not None:
            self._enforce_handle_cap(buf)

    def _enforce_handle_cap(self, buf: PageBuffer) -> None:
        """只保留最近 handle_max 条请求的响应句柄, 更早的释放(元数据保留)。"""
        held = [e for e in buf.events if e.handle is not None]
        for e in held[: max(0, len(held) - self.handle_max)]:
            e.handle = None

    def _clip(self, s: str, cap: int | None = None) -> str:
        cap = cap or self.text_cap
        return s[:cap] + "…" if len(s) > cap else s

    # ── 读取 ────────────────────────────────────────────────────────

    def read(
        self,
        page: Any,
        reader: str,
        *,
        since: int | None = None,
        kinds: set[str] | None = None,
        match: Any = None,
        limit: int = 50,
    ) -> tuple[list[Event], PageBuffer | None, int]:
        """返回 (命中事件, 页缓冲, 未显示条数)。

        since=None → 该 (page, reader) 上次读到的位置之后(增量);
        since=0 → 全量。读完把游标推到本次最后一条, 下次接着来。
        过滤后零命中不推进游标(下次重扫, 结果相同, 无损失)。
        """
        buf = self._pages.get(id(page))
        if buf is None:
            return [], None, 0
        if since is None:
            since = self._cursors.get((id(page), reader), 0)
        since = max(0, since)
        picked: list[Event] = []
        total = 0
        for e in buf.events:
            if e.seq <= since:
                continue
            if kinds is not None and e.kind not in kinds:
                continue
            if match is not None and not match(e):
                continue
            total += 1
            if len(picked) < limit:
                picked.append(e)
        if picked:
            self._cursors[(id(page), reader)] = picked[-1].seq
        return picked, buf, total - len(picked)

    def find(self, page: Any, seq: int) -> Event | None:
        """按 seq 查当前页缓冲中的事件 (browser_network_body 定位用)。"""
        buf = self._pages.get(id(page))
        if buf is None:
            return None
        for e in buf.events:
            if e.seq == seq:
                return e
        return None

    # ── 生命周期 ────────────────────────────────────────────────────

    def invalidate_page(self, page: Any, reason: str) -> None:
        """页面关闭/崩溃 → 缓冲标记 dead, 保留可读。"""
        buf = self._pages.get(id(page))
        if buf is not None:
            buf.dead = reason

    def drop_page(self, page: Any) -> None:
        """page 对象被替换(自愈重建)/新页挂接前调用: 清掉同 id 旧缓冲与游标。

        防 id() 复用后新页继承死缓冲或脏游标(与 core._page_owners 自清同因)。
        """
        pid = id(page)
        self._pages.pop(pid, None)
        for key in [k for k in self._cursors if k[0] == pid]:
            del self._cursors[key]
