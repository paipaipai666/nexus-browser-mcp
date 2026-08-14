"""补充验证: example.com 快照 (reading 模式) + task 隔离交叉验证。"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_CMD = [sys.executable, "-m", "nexus_browser.server"]


async def call(session, name, args=None):
    r = await session.call_tool(name, args or {})
    return r.content[0].text if r.content and hasattr(r.content[0], "text") else str(r)


async def main():
    os.environ["BROWSER_HEADLESS"] = "false"
    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== 验证 1: example.com 用 reading 模式快照 ===")
            text = await call(session, "browser_navigate", {"url": "https://example.com", "task_id": "iso-a"})
            print(f"导航: {text[:80]}")
            # example.com 是个极简页, interactive 快照可能只给 paragraph;
            # 用 browser_read 取真实文本
            text = await call(session, "browser_read", {"task_id": "iso-a"})
            print(f"read 内容: {text[:200]}")
            assert "Example Domain" in text, "应读到 Example Domain 文本"
            print("PASS: reading 模式能看到页面文本")

            print("\n=== 验证 2: task1 在 example.com, task2 去 bing, 互不污染 ===")
            await call(session, "browser_navigate", {"url": "https://example.com", "task_id": "iso-a"})
            await call(session, "browser_navigate", {"url": "https://cn.bing.com", "task_id": "iso-b"})

            a_snap = await call(session, "browser_read", {"task_id": "iso-a"})
            b_snap = await call(session, "browser_read", {"task_id": "iso-b"})

            print(f"task_a (example.com) 内容前100字: {a_snap[:100]}")
            print(f"task_b (bing.com) 内容前100字: {b_snap[:100]}")
            assert "Example" in a_snap, "task_a 应是 example.com 内容"
            assert "bing" in b_snap.lower() or "必应" in b_snap, "task_b 应是 bing 内容"
            print("PASS: 两个 task 内容完全隔离")

            print("\n=== 验证 3: 搜索框元素查找难度测试 ===")
            # 看 Bing 搜索框到底是什么 role
            snap = await call(session, "browser_snapshot", {"mode": "full", "include_generic": True, "task_id": "iso-b"})
            print(f"Bing 完整快照 (前800字):\n{snap[:800]}")

            await session.call_tool("browser_close_session", {})
            print("\n=== 全部验证通过 ===")


if __name__ == "__main__":
    asyncio.run(main())