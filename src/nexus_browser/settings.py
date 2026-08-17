"""BrowserSettings — env 可覆盖的浏览器 MCP 配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrowserSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BROWSER_")

    mode: str = Field(default="isolated", description="isolated=无状态新浏览器, cdp=连接用户浏览器")
    cdp_endpoint: str = Field(default="http://localhost:9222")
    channel: str = Field(default="", description="浏览器通道: chrome/msedge/chromium 等(空=Playwright 内置)")
    user_data_dir: str = Field(default="", description="用户数据目录(带 cookie/登录态), 空=全新 profile")
    headless: bool = Field(default=False, description="无头模式(仅 isolated)")
    proxy: str = Field(default="", description="显式代理 server (http://host:port); 'none' = --no-proxy-server 绕过系统代理 (陈旧系统代理会让全站超时)")
    viewport_width: int = Field(default=1280, ge=320, le=3840)
    viewport_height: int = Field(default=720, ge=240, le=2160)
    default_timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    tool_timeout_ms: int = Field(default=60000, ge=1000, le=600000, description="单次工具调用外层超时护栏")
    networkidle_timeout_ms: int = Field(default=5000, ge=1000, le=30000)
    dialog_timeout_ms: int = Field(default=20000, ge=2000, le=120000,
                                   description="挂起对话框无决策自动 dismiss 超时(ms)")
    context_ttl_sec: int = Field(default=600, ge=60, le=3600, description="空闲 task 自动回收(秒)")
    screenshot_dir: str = Field(default="", description="截图保存目录, 空则 ~/.nexus-browser/screenshots")
    allow_js_execution: bool = Field(default=False, description="browser_evaluate 开关")
    allow_network_body: bool = Field(default=False, description="browser_network_body 开关(响应体含敏感数据, 默认关)")
    network_body_cap: int = Field(default=4000, ge=200, le=200000, description="单条响应体返回字符上限")
    snapshot_max_nodes: int = Field(default=100, ge=10, le=1000)
    # 事件驱动稳定性
    stable_window_ms: int = Field(default=800, ge=100, le=10000, description="无 DOM 变异静默窗口")
    stable_poll_ms: int = Field(default=100, ge=20, le=2000, description="静默轮询间隔(ms)")
    stable_timeout_ms: int = Field(default=3000, ge=500, le=60000)
    stable_required: int = Field(default=2, ge=1, le=5, description="稳定后确认快照数(兜底)")
    stable_confirm_gap_ms: int = Field(default=150, ge=0, le=5000, description="REQUIRED 确认间小间隔")
    # 流式缓冲 (browser_read follow)
    stream_char_cap: int = Field(default=16000, ge=1000, le=1000000, description="单条流式缓冲最大字符数, 溢出丢最旧")
    stream_page_cap: int = Field(default=64000, ge=4000, le=4000000, description="单页面全部流的总字符上限")
    # 事件缓冲 (browser_console/errors/network, 只记元数据不记 body)
    event_max_entries: int = Field(default=500, ge=50, le=10000, description="单页事件条数上限, 溢出丢最旧")
    event_text_cap: int = Field(default=500, ge=100, le=10000, description="单条事件文本截断长度")
    event_handle_max: int = Field(default=50, ge=5, le=500, description="单页保留响应句柄的最近请求条数(供按需取 body)")
    # 治理
    hitl_rules: list[dict[str, str]] = Field(
        default_factory=list,
        description="HITL 规则: [{action, role, name_pattern}]",
    )
    audit_path: str = Field(
        default="",
        description="审计 JSONL 路径, 空则 ~/.nexus-browser/audit.jsonl",
    )
    # 传输 (默认 stdio; http=streamable-http 供远程/多客户端)
    transport: str = Field(default="stdio", description="传输方式: stdio / http")
    http_host: str = Field(default="127.0.0.1", description="HTTP 绑定地址; 非 localhost 必须设 BROWSER_HTTP_TOKEN")
    http_port: int = Field(default=8817, ge=1, le=65535)
    http_token: str = Field(default="", description="HTTP Bearer token, 空=仅 localhost 可用")

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        v = (v or "isolated").strip().lower()
        if v not in {"isolated", "cdp"}:
            raise ValueError(f"不支持的浏览器模式: {v}, 可选 isolated/cdp")
        return v

    @field_validator("transport")
    @classmethod
    def _transport_valid(cls, v: str) -> str:
        v = (v or "stdio").strip().lower()
        if v not in {"stdio", "http"}:
            raise ValueError(f"不支持的传输方式: {v}, 可选 stdio/http")
        return v

    def resolve_audit_path(self) -> Path:
        if self.audit_path:
            return Path(self.audit_path).expanduser()
        return Path.home() / ".nexus-browser" / "audit.jsonl"
