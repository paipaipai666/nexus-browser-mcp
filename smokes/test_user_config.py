"""用用户 mcp.json 里的实际 command 路径验证: D:\code\nexus-browser-mcp\.venv\Scripts\nexu-browser.exe"""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CMD = r"D:\code\nexus-browser-mcp\.venv\Scripts\nexu-browser.exe"


async def main():
    params = StdioServerParameters(command=CMD, args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"OK: {CMD} 作为 stdio MCP server 握手成功")
            print(f"工具数: {len(names)}")
            print(f"工具: {names}")
            # 顺手实测一个真实导航, 确认端到端可用
            r = await session.call_tool("browser_navigate", {"url": "https://example.com"})
            txt = r.content[0].text if r.content and hasattr(r.content[0], "text") else str(r)
            print(f"\n实测 browser_navigate(example.com):\n{txt[:150]}")
            assert "已导航至" in txt
            print("\n结论: 你的 mcp.json 配置端到端可用 ✅")


if __name__ == "__main__":
    asyncio.run(main())
