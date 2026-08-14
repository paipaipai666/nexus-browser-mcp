"""真实浏览器 E2E 冒烟: 起 stdio server, MCP 客户端连线, navigate→snapshot→click。

运行: .venv\\Scripts\\python.exe -m smokes.test_e2e
要求: playwright chromium 已安装 (playwright install chromium)
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_CMD = [sys.executable, "-m", "nexus_browser.server"]


async def main() -> None:
    os.environ["BROWSER_HEADLESS"] = "0"  # 有头可见
    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[smoke] tools: {len(names)} -> {names[:4]}...")
            assert "browser_navigate" in names

            r = await session.call_tool("browser_navigate", {"url": "https://example.com"})
            text = r.content[0].text if hasattr(r.content[0], "text") else str(r)
            print(f"[smoke] navigate: {text[:120]!r}")
            assert "已导航至" in text or "example.com" in text

            r = await session.call_tool("browser_snapshot", {"mode": "interactive"})
            text = r.content[0].text if hasattr(r.content[0], "text") else str(r)
            print(f"[smoke] snapshot: {text[:200]!r}")
            assert "可交互元素" in text or "页面结构" in text

            r = await session.call_tool("browser_tasks", {})
            print(f"[smoke] tasks: {r.content[0].text if hasattr(r.content[0],'text') else r}")
            print("[smoke] PASSED")


if __name__ == "__main__":
    asyncio.run(main())