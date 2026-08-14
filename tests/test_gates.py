"""gates 行为测试: HITL 规则匹配 + 审计落盘。"""

from __future__ import annotations

import json

from nexus_browser.gates import AuditLogger, hitl_required, load_hitl_rules


# ── HITL 规则 ─────────────────────────────────────────────────────
def test_no_rules_no_hitl():
    assert hitl_required([], "click", "button", "登录") is False


def test_action_only_match():
    rules = [{"action": "type"}]
    assert hitl_required(rules, "type", "textbox", "") is True
    assert hitl_required(rules, "click", "button", "登录") is False


def test_role_filter():
    rules = [{"action": "click", "role": "button"}]
    assert hitl_required(rules, "click", "button", "支付") is True
    assert hitl_required(rules, "click", "link", "支付") is False


def test_name_pattern_regex():
    rules = [{"action": "click", "name_pattern": "支付|确认"}]
    assert hitl_required(rules, "click", "button", "确认订单") is True
    assert hitl_required(rules, "click", "button", "取消") is False
    # 大小写不敏感
    rules = [{"action": "click", "name_pattern": "pay"}]
    assert hitl_required(rules, "click", "button", "Pay Now") is True


def test_all_fields_match():
    rules = [{"action": "click", "role": "button", "name_pattern": "删除"}]
    assert hitl_required(rules, "click", "button", "确认删除") is True
    assert hitl_required(rules, "click", "button", "确认编辑") is False


def test_load_rules_parses_config():
    rules = load_hitl_rules([{"action": "click", "name_pattern": "支付"}])
    assert rules == [{"action": "click", "name_pattern": "支付"}]


def test_bad_rule_skipped():
    # name_pattern 非法正则 → 该规则跳过, 不影响其它
    rules = [{"action": "click", "name_pattern": "("}, {"action": "type"}]
    assert hitl_required(rules, "type", "textbox", "x") is True
    assert hitl_required(rules, "click", "button", "x") is False


# ── 审计 ──────────────────────────────────────────────────────────
def test_audit_logger_appends_jsonl(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))
    logger.log(
        session_id="s1", task_id="t1", tool="browser_click",
        params={"pos": "1,2,3,4"}, risk="medium",
        hitl_triggered=True, duration_ms=12.3,
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "browser_click"
    assert entry["hitl_triggered"] is True
    assert entry["session_id"] == "s1"
    assert entry["task_id"] == "t1"


def test_audit_redacts_sensitive_params(tmp_path):
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(str(path))
    logger.log("s", "t", "browser_type", {"text": "password123"}, "medium")
    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert "password123" not in json.dumps(entry)
    assert "[redacted]" in json.dumps(entry)


def test_audit_creates_parent_dir(tmp_path):
    path = tmp_path / "a" / "b" / "audit.jsonl"
    logger = AuditLogger(str(path))
    logger.log("s", "t", "browser_click", {}, "low")
    assert path.exists()
