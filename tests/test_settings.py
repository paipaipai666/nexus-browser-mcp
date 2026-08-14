"""Settings 行为测试。"""

from __future__ import annotations

from nexus_browser.settings import BrowserSettings


def test_defaults():
    s = BrowserSettings()
    assert s.mode == "isolated"
    assert s.cdp_endpoint == "http://localhost:9222"
    assert s.headless is False
    assert s.viewport_width == 1280
    assert s.viewport_height == 720
    assert s.default_timeout_ms == 30000
    assert s.tool_timeout_ms == 60000
    assert s.context_ttl_sec == 600
    assert s.allow_js_execution is False
    assert s.snapshot_max_nodes == 100
    assert s.stable_window_ms == 800
    assert s.stable_timeout_ms == 3000
    assert s.stable_required == 2
    assert s.hitl_rules == []
    assert s.audit_path == ""
    assert s.channel == ""
    assert s.user_data_dir == ""
    assert s.resolve_audit_path().name == "audit.jsonl"
    assert s.resolve_audit_path().parent.name == ".nexus-browser"


def test_channel_and_userdata_env(monkeypatch):
    monkeypatch.setenv("BROWSER_CHANNEL", "chrome")
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", r"C:\Users\me\AppData\Local\Google\Chrome\User Data")
    s = BrowserSettings()
    assert s.channel == "chrome"
    assert "User Data" in s.user_data_dir


def test_env_override(monkeypatch):
    monkeypatch.setenv("BROWSER_MODE", "cdp")
    monkeypatch.setenv("BROWSER_STABLE_WINDOW_MS", "800")
    monkeypatch.setenv("BROWSER_ALLOW_JS_EXECUTION", "true")
    s = BrowserSettings()
    assert s.mode == "cdp"
    assert s.stable_window_ms == 800
    assert s.allow_js_execution is True


def test_mode_validation():
    import pytest

    with pytest.raises(ValueError):
        BrowserSettings(mode="invalid")


def test_window_ms_clamped():
    import pytest

    # zhe 必须 >=100
    with pytest.raises(ValueError):
        BrowserSettings(stable_window_ms=50)
    # 允许合法边界
    assert BrowserSettings(stable_window_ms=100).stable_window_ms == 100
