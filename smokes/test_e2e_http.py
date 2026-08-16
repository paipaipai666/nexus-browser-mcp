"""HTTP transport 冒烟: streamable-http 起服, 两个客户端各自隔离使用浏览器。

运行: .venv\\Scripts\\python.exe -m smokes.test_e2e_http
要求: playwright chromium 已安装
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

PORT = 8871


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server 进程退出, code={proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"端口 {port} 未就绪")


def _text(r) -> str:
    return r.content[0].text if hasattr(r.content[0], "text") else str(r)


async def _client(tag: str, url: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(f"http://127.0.0.1:{PORT}/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "browser_navigate" in [t.name for t in tools.tools]
            r = await session.call_tool("browser_navigate", {"url": url})
            text = _text(r)
            assert "已导航至" in text, f"[{tag}] {text}"
            r = await session.call_tool("browser_tasks", {})
            tasks_text = _text(r)
            r = await session.call_tool("browser_close_session", {})
            print(f"[smoke] client-{tag}: navigate ok, tasks: {tasks_text!r}")
            return tasks_text


async def main() -> None:
    # 本机代理(Clash 等 7890)会把 127.0.0.1 请求错误接管 → 502。显式绕开。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    env = {**os.environ,
           "BROWSER_TRANSPORT": "http", "BROWSER_HTTP_PORT": str(PORT),
           "BROWSER_HEADLESS": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "nexus_browser.server"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        await asyncio.to_thread(_wait_port, PORT, proc)
        from urllib.parse import quote
        a, b = await asyncio.gather(
            _client("a", "https://example.com"),
            _client("b", "data:text/html," + quote("<h1>client-b</h1>")),
        )
        # 两个 MCP session 各自只看到自己的 task (session 隔离)
        assert "default" in a and "default" in b
        print("[smoke] http PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
