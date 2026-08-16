"""治理门: HITL 规则匹配 + JSONL 审计。

从 AgentNexus 迁移 (_check_hitl_rules / AuditEntry), 独立成库内逻辑。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {"text", "expression", "url", "selector", "value"}


def load_hitl_rules(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """规范化 HITL 规则列表, 丢弃非法正则的规则。"""
    clean = []
    for rule in rules or []:
        pattern = rule.get("name_pattern")
        if pattern:
            try:
                re.compile(pattern)
            except re.error:
                logger.warning("HITL 规则 name_pattern 非法, 跳过: %r", pattern)
                continue
        clean.append(rule)
    return clean


def hitl_required(rules: list[dict[str, str]], action: str, role: str | None, name: str | None) -> bool:
    """任一规则匹配 → 需人工确认。"""
    for rule in load_hitl_rules(rules):
        if rule.get("action") and rule["action"] != action:
            continue
        if rule.get("role") and rule.get("role") != (role or ""):
            continue
        if rule.get("name_pattern"):
            if not re.search(rule["name_pattern"], name or "", re.IGNORECASE):
                continue
        return True
    return False


class AuditLogger:
    """JSONL 审计: 每工具调用一行, 敏感参数脱敏。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(
        self,
        session_id: str,
        task_id: str,
        tool: str,
        params: dict,
        risk: str,
        hitl_triggered: bool = False,
        duration_ms: float = 0.0,
        error: str | None = None,
        in_chars: int = 0,
        out_chars: int = 0,
    ) -> None:
        entry = {
            "ts": time.time(),
            "session_id": session_id,
            "task_id": task_id,
            "tool": tool,
            "params": _redact(params),
            "risk": risk,
            "hitl_triggered": hitl_triggered,
            "duration_ms": round(duration_ms, 1),
            "in_chars": in_chars,
            "out_chars": out_chars,
            "error": error,
        }
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _redact(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if k in _SENSITIVE_KEYS and isinstance(v, str) and v:
            out[k] = "[redacted]"
        else:
            out[k] = v
    return out
