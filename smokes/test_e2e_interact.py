"""真实浏览器 E2E 冒烟扩展: click/type/find_element + 多 task 隔离。

运行: .venv\\Scripts\\python.exe -m smokes.test_e2e_interact
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_CMD = [sys.executable, "-m", "nexus_browser.server"]

HTML = """<!doctype html><html><body>
<button name="click_me">点击我</button>
<input id="name" placeholder="请输入" aria-label="姓名">
<div id="result"></div>
<script>
  const btn = document.querySelector('button');
  btn.addEventListener('click', () => {
    const v = document.querySelector('#name').value;
    document.querySelector('#result').textContent = '已收到: ' + v;
  });
</script>
</body></html>"""


async def main() -> None:
    os.environ["BROWSER_HEADLESS"] = "0"
    page_path = Path("smokes/page.html").resolve()
    page_path.write_text(HTML, encoding="utf-8")
    url = page_path.as_uri()

    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            r = await session.call_tool("browser_navigate", {"url": url, "task_id": "t1"})
            print(f"[1] nav t1: {r.content[0].text[:80]!r}")

            r = await session.call_tool("browser_snapshot", {"mode": "interactive", "task_id": "t1"})
            snap = r.content[0].text
            print(f"[2] snap t1:\n{snap[:400]}")

            r = await session.call_tool("browser_type", {"text": "张三", "role": "textbox", "name": "姓名", "task_id": "t1"})
            print(f"[3] type: {r.content[0].text[:80]!r}")

            r = await session.call_tool("browser_click", {"role": "button", "name": "点击我", "task_id": "t1"})
            print(f"[4] click: {r.content[0].text[:80]!r}")

            r = await session.call_tool("browser_read", {"selector": "#result", "task_id": "t1"})
            print(f"[5] read result: {r.content[0].text!r}")
            assert "张三" in r.content[0].text, "表单闭环失败"

            # 多 task 隔离: t2 应是空白新页
            r = await session.call_tool("browser_snapshot", {"mode": "interactive", "task_id": "t2"})
            t2_snap = r.content[0].text
            print(f"[6] snap t2 (应无 点击我): {'点击我' not in t2_snap}")

            r = await session.call_tool("browser_tasks", {})
            print(f"[7] tasks:\n{r.content[0].text}")
            assert "t1" in r.content[0].text and "t2" in r.content[0].text

            print("[SMOKE-INTERACT] PASSED")


if __name__ == "__main__":
    asyncio.run(main())