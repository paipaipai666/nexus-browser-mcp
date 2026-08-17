# Token 对比实测: nexus-browser-mcp vs playwright-mcp

> 测量日期: 2026-08-17 · nexus v0.2.2 (`3602494`) · playwright-mcp@latest (npx 当日拉取, 24 工具)
> 复现: `.venv/Scripts/python.exe bench/compare.py` (原始数据: `token-comparison.json`)

## 方法

- 双方以 stdio 起服务, **默认配置**, 同一脚本化 MCP 客户端驱动完全相同的 10 步动作序列（工具语义一一映射）
- 计量点在 **JSON-RPC 载荷层**: 每次 tools/call 的请求+响应字节数, token 按 cl100k_base 计
- 测试页为仓库内本地 fixture (`bench/fixture/`, 120 链接+表单 / 80 行表格, 经 127.0.0.1 HTTP 托管——playwright-mcp 默认屏蔽 file://)
- 不进 LLM, 不受模型发挥影响; 这是协议层的确定性对比

## 结果

| 步骤 | nexus | playwright-mcp | 倍数 |
|---|---:|---:|---:|
| navigate → 页1 | 1,388 | 164 | 0.1x |
| snapshot #1 | 1,369 | 6,931 | 5.1x |
| **snapshot #2 (页面无变化)** | **77** | **6,931** | **90x** |
| type | 24 | 57 | 2.4x |
| click | 12 | 42 | 3.5x |
| snapshot #3 (DOM 变更后) | 1,372 | 6,931 | 5.1x |
| console 读取 | 104 | 86 | 0.8x |
| evaluate | 102 | 37 | 0.4x |
| navigate → 页2 | 263 | 151 | 0.6x |
| snapshot #4 | 246 | 4,702 | 19x |
| **合计** | **4,957** | **26,032** | **5.3x** |

## 结论与边界

1. **主要差距在快照策略**: playwright-mcp 每次 snapshot 全量返回 YAML; nexus 在页面无变化时返回 ~120 字符的 diff 提示（90 倍单步差距）。真实 agent 工作流里重复读同一页面是常态（轮询、表单多步、确认状态）, 该杠杆被反复触发。
2. **细节文本也更省**: nexus 快照行内联 `[box=…]` 等属性但结构更紧凑（页2 快照 246 vs 4,702, 19 倍——表格场景阅读模式收敛静态文本）。
3. **navigate 步 nexus 更贵**（1,388 vs 164）: nexus 的 navigate 默认附带首屏快照基线, playwright-mcp 的 navigate 不附。随后第一次显式 snapshot nexus 仍全量（digest 按代际记录, 首次比对无基线）——此处有可优化空间（navigate 与 snapshot 共享 digest 基线）。
4. **诚实边界**: 对手的快照同样是 a11y 文本而非截图, 本对比测的是 diff/上限/紧凑渲染的增量价值, 不是"文本 vs 图片"的数量级差。双方默认配置, 未使用对方的高级开关（`--caps` 等）。
5. 字符口径: 10,306 vs 66,165（6.4 倍）, 与 token 口径一致。
