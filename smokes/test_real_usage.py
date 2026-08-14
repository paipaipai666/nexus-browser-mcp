"""真实使用测试: 以 MCP 客户端身份连接服务器, 驱动浏览器做实际操作。

测试场景:
1. 导航到 Bing 搜索引擎
2. 快照页面, 检查可交互元素
3. 输入搜索词并回车
4. 等待导航完成
5. 读取搜索结果
6. 截图保存
7. 滚动页面
8. 多 task 隔离验证
9. 生命周期工具验证
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_CMD = [sys.executable, "-m", "nexus_browser.server"]


async def call(session, name, args=None):
    """调用工具并返回文本结果。"""
    r = await session.call_tool(name, args or {})
    if r.content and hasattr(r.content[0], "text"):
        return r.content[0].text
    return str(r)


async def main():
    os.environ["BROWSER_HEADLESS"] = "false"  # 有头模式, 能看到浏览器
    os.environ["BROWSER_TOOL_TIMEOUT_MS"] = "30000"

    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:])
    results = []  # (step, status, detail)

    def log(step, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        results.append((step, status, detail))
        print(f"  [{status}] {step}: {detail[:120] if detail else ''}")

    print("=" * 70)
    print("  nexus-browser-mcp 真实使用测试")
    print("=" * 70)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 0. 列出工具 ──
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"\n[0] 注册工具数: {len(names)}")
            print(f"    工具列表: {names}")
            log("工具注册", len(names) == 18, f"共 {len(names)} 个工具")

            # ── 1. 导航到 Bing ──
            print("\n[1] 导航到 https://www.bing.com ...")
            t0 = time.time()
            text = await call(session, "browser_navigate", {"url": "https://www.bing.com"})
            elapsed = time.time() - t0
            print(f"    耗时: {elapsed:.1f}s")
            print(f"    返回 (前200字):\n    {text[:200]}")
            ok = "已导航至" in text or "bing.com" in text.lower()
            log("导航 Bing", ok, f"耗时 {elapsed:.1f}s")

            # ── 2. 快照 - 检查可交互元素 ──
            print("\n[2] 获取页面快照 (interactive 模式)...")
            t0 = time.time()
            text = await call(session, "browser_snapshot", {"mode": "interactive"})
            elapsed = time.time() - t0
            print(f"    耗时: {elapsed:.1f}s")
            print(f"    快照 (前400字):\n    {text[:400]}")
            ok = "可交互元素" in text or "页面结构" in text
            log("快照 interactive", ok, f"耗时 {elapsed:.1f}s, 内容长度 {len(text)}")

            # ── 3. 输入搜索词 ──
            print("\n[3] 在搜索框输入 'Playwright MCP server' ...")
            text = await call(session, "browser_type", {
                "text": "Playwright MCP server",
                "role": "combobox",
                "name": "Search the web",
                "press_enter": True,
            })
            print(f"    结果: {text[:100]}")
            # 如果 combobox 定位失败, 尝试 textbox
            if "失败" in text or "找不到" in text:
                print("    combobox 失败, 尝试 textbox ...")
                text = await call(session, "browser_type", {
                    "text": "Playwright MCP server",
                    "role": "textbox",
                    "press_enter": True,
                })
                print(f"    结果: {text[:100]}")
            if "失败" in text or "找不到" in text:
                print("    textbox 也失败, 尝试 selector ...")
                text = await call(session, "browser_type", {
                    "text": "Playwright MCP server",
                    "selector": "textarea, input[type='search'], input[name='q']",
                    "press_enter": True,
                })
                print(f"    结果: {text[:100]}")
            ok = "已输入" in text
            log("输入搜索词", ok, text[:80])

            # ── 4. 等待导航 ──
            print("\n[4] 等待搜索结果页面加载...")
            t0 = time.time()
            text = await call(session, "browser_wait_navigation", {"timeout": 10000})
            elapsed = time.time() - t0
            print(f"    耗时: {elapsed:.1f}s")
            print(f"    结果: {text[:150]}")
            ok = "导航" in text or "complete" in text or "超时" not in text
            log("等待导航", ok, f"耗时 {elapsed:.1f}s")

            # ── 5. 读取搜索结果 ──
            print("\n[5] 读取搜索结果页面内容...")
            text = await call(session, "browser_read", {"max_chars": 2000})
            print(f"    内容 (前500字):\n    {text[:500]}")
            ok = len(text) > 50 and "error" not in text.lower()
            log("读取搜索结果", ok, f"内容长度 {len(text)}")

            # ── 6. 截图 ──
            print("\n[6] 截图...")
            text = await call(session, "browser_screenshot", {})
            print(f"    结果: {text}")
            ok = "已保存" in text or ".png" in text
            log("截图", ok, text[:100])

            # ── 7. 滚动 ──
            print("\n[7] 向下滚动 500px...")
            text = await call(session, "browser_scroll", {"direction": "down", "amount": 500})
            print(f"    结果: {text}")
            ok = "已向" in text and "滚动" in text
            log("滚动", ok, text[:80])

            # ── 8. 再读一次快照看是否变化 ──
            print("\n[8] 滚动后再次快照...")
            text = await call(session, "browser_snapshot", {"mode": "interactive"})
            print(f"    快照 (前300字):\n    {text[:300]}")
            ok = len(text) > 0
            log("滚动后快照", ok, f"内容长度 {len(text)}")

            # ── 9. 多 task 隔离 ──
            print("\n[9] 多 task 隔离: 创建 task2, 导航到 example.com...")
            text = await call(session, "browser_navigate", {
                "url": "https://example.com", "task_id": "task2",
            })
            print(f"    task2 导航: {text[:120]}")
            ok = "已导航至" in text
            log("task2 导航", ok, text[:80])

            # task2 快照不应包含 Bing 搜索结果
            text = await call(session, "browser_snapshot", {"mode": "interactive", "task_id": "task2"})
            print(f"    task2 快照 (前200字): {text[:200]}")
            ok = "Example Domain" in text or "example" in text.lower()
            log("task2 隔离验证", ok, "task2 内容与 task1 不同")

            # ── 10. 生命周期工具 ──
            print("\n[10] 生命周期: browser_tasks ...")
            text = await call(session, "browser_tasks", {})
            print(f"    tasks: {text}")
            ok = "task2" in text or "default" in text
            log("browser_tasks", ok, text[:100])

            print("\n[11] browser_list_sessions ...")
            text = await call(session, "browser_list_sessions", {})
            print(f"    sessions: {text}")
            ok = "task" in text.lower() or "session" in text.lower()
            log("browser_list_sessions", ok, text[:100])

            # ── 12. 关闭 task2 ──
            print("\n[12] 关闭 task2...")
            text = await call(session, "browser_close_task", {"task_id": "task2"})
            print(f"    结果: {text}")
            ok = "已关闭" in text
            log("close_task", ok, text[:80])

            # ── 13. browser_evaluate 安全门 ──
            print("\n[13] browser_evaluate 安全门测试...")
            text = await call(session, "browser_evaluate", {"expression": "1+1"})
            print(f"    结果: {text[:120]}")
            ok = "未启用" in text or "禁止" in text or "CONFIRMATION" in text
            log("evaluate 安全门", ok, "JS 执行被正确拦截")

            # ── 14. 关闭 session ──
            print("\n[14] 关闭 session...")
            text = await call(session, "browser_close_session", {})
            print(f"    结果: {text}")
            ok = "已关闭" in text
            log("close_session", ok, text[:80])

    # ── 汇总 ──
    print("\n" + "=" * 70)
    print("  测试汇总")
    print("=" * 70)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    for step, status, detail in results:
        marker = "OK" if status == "PASS" else "XX"
        print(f"  [{marker}] {step}: {detail[:80]}")
    print(f"\n  通过: {passed} / {len(results)}, 失败: {failed}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
