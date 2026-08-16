"""真实浏览器 E2E 冒烟: console/pageerror/network 事件捕获 + 三个观测工具。

运行: .venv\\Scripts\\python.exe -m smokes.test_e2e_observability
要求: playwright chromium 已安装 (playwright install chromium)
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import quote

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 页面制造三类信号: console.error / 未捕获异常 / 网络层失败请求
# 另含若干交互元素, 让快照全文有一定体量 (验证 diff 抑制省 token)
PAGE = "data:text/html," + quote("""<!doctype html><html><body><h1>obs</h1>
<nav><a href="#a">首页</a><a href="#b">文档</a><a href="#c">关于我们</a></nav>
<main><form><input type="text" placeholder="搜索关键词"><button>提交</button></form>
<ul><li>第一条消息</li><li>第二条消息</li><li>第三条消息</li></ul>
<button>保存</button><button>取消</button><button>导出数据</button></main>
<script>
console.log("obs-page-ready");
console.error("boom-console");
setTimeout(() => { throw new Error("boom-pageerror"); }, 100);
fetch("http://127.0.0.1:9/api").catch(() => {});
</script></body></html>""")


def _text(r) -> str:
    return r.content[0].text if hasattr(r.content[0], "text") else str(r)


async def main() -> None:
    env = {**os.environ, "BROWSER_HEADLESS": "1", "BROWSER_ALLOW_NETWORK_BODY": "1"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "nexus_browser.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            for t in ("browser_console", "browser_errors", "browser_network"):
                assert t in names, f"工具未注册: {t}"
            print(f"[smoke] tools: {len(names)} 个, 观测三件套已注册")

            r = await session.call_tool("browser_navigate", {"url": PAGE})
            assert "已导航至" in _text(r), _text(r)
            await session.call_tool("browser_wait_ms", {"ms": 1200})  # 等脚本集: console/pageerror +100ms

            # 连接拒绝事件落地有抖动: 轮询至出现 (上限 ~6s)
            async def wait_network_event():
                for _ in range(6):
                    await session.call_tool("browser_wait_ms", {"ms": 1000})
                    rr = await session.call_tool("browser_network", {"since": 0})
                    if "127.0.0.1:9" in _text(rr):
                        return _text(rr)
                return _text(rr)

            r = await session.call_tool("browser_console", {"level": "error", "since": 0})
            text = _text(r)
            print(f"[smoke] console(level=error):\n{text}")
            assert "boom-console" in text, text

            r = await session.call_tool("browser_errors", {"since": 0})
            text = _text(r)
            print(f"[smoke] errors:\n{text}")
            assert "boom-pageerror" in text, text
            assert "boom-console" in text, text  # console.error 并入

            text = await wait_network_event()
            print(f"[smoke] network(failed_only):\n{text}")
            assert "127.0.0.1:9" in text and "FAILED" in text, text

            # 增量游标: 再读一次应无新增
            r = await session.call_tool("browser_errors", {})
            text = _text(r)
            assert "无新增" in text or "未发现异常" in text, text

            # 快照 diff: 静态页二次快照 → 只回"无变化", 审计 out_chars 骤降
            r1 = await session.call_tool("browser_snapshot", {})
            t1 = _text(r1)
            assert "可交互元素" in t1 or "页面结构" in t1, t1
            r2 = await session.call_tool("browser_snapshot", {})
            t2 = _text(r2)
            print(f"[smoke] snapshot#1: {len(t1)} chars, snapshot#2: {len(t2)} chars")
            assert "快照无变化" in t2, t2
            assert len(t2) < len(t1) // 2, f"diff 未省 token: {len(t2)} vs {len(t1)}"
            # diff=false 强制全量
            r3 = await session.call_tool("browser_snapshot", {"diff": False})
            assert "快照无变化" not in _text(r3), _text(r3)

            # 性能指标: 真实站点有计时数据; data: 页优雅降级
            r = await session.call_tool("browser_navigate", {"url": "https://example.com"})
            await session.call_tool("browser_wait_ms", {"ms": 800})
            r = await session.call_tool("browser_perf", {})
            text = _text(r)
            print(f"[smoke] perf:\n{text}")
            assert "TTFB" in text and "LCP" in text, text

            # 响应体按需单取: 确认门 → confirmed=true 拿到 HTML
            import re as _re
            r = await session.call_tool("browser_network",
                                        {"failed_only": False, "url_pattern": "example.com", "since": 0})
            m = _re.search(r"#(\d+) GET https://example\.com", _text(r))
            assert m, _text(r)
            seq = int(m.group(1))
            r = await session.call_tool("browser_network_body", {"seq": seq})
            assert "CONFIRMATION_REQUIRED" in _text(r), _text(r)
            r = await session.call_tool("browser_network_body", {"seq": seq, "confirmed": True})
            text = _text(r)
            assert "Example Domain" in text, text[:200]
            print(f"[smoke] network_body: 确认门→放行, 取回 {len(text)} 字符 HTML")

            await session.call_tool("browser_close_session", {})
            print("[smoke] observability PASSED")


if __name__ == "__main__":
    asyncio.run(main())
