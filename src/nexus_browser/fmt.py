"""统一的结果格式:h错误/警告与正常文本,直接供 agent 消费。"""

from __future__ import annotations


def error(msg: str, detail: str = "", hint: str = "") -> str:
    parts = [f"ERROR: {msg}"]
    if detail:
        parts.append(f"DETAIL: {detail}")
    if hint:
        parts.append(f"HINT: {hint}")
    return "\n".join(parts)


def warning(msg: str, detail: str = "") -> str:
    parts = [f"WARNING: {msg}"]
    if detail:
        parts.append(f"DETAIL: {detail}")
    return "\n".join(parts)
