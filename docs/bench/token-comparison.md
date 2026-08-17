# Token 对比实测: nexus-browser-mcp vs playwright-mcp

> 测量日期: 2026-08-17 · nexus v0.2.2+ (`main`, 含 navigate 基线播种优化) · playwright-mcp@latest (npx 当日拉取, 24 工具)
> 复现: `.venv/Scripts/python.exe bench/compare.py` (原始数据: `token-comparison.json`)

## 方法

- 双方以 stdio 起服务, **默认配置**, 同一脚本化 MCP 客户端驱动完全相同的 10 步动作序列（工具语义一一映射）
- 计量点在 **JSON-RPC 载荷层**: 每次 tools/call 的请求+响应字节数, token 按 cl100k_base 计
- 测试页为仓库内本地 fixture (`bench/fixture/`, 120 链接+表单 / 80 行表格, 经 127.0.0.1 HTTP 托管——playwright-mcp 默认屏蔽 file://)
- 不进 LLM, 不受模型发挥影响; 这是协议层的确定性对比

## 结果

| 步骤 | nexus | playwright-mcp | 倍数 |
|---|---:|---:|---:|
| navigate → 页1（附首屏快照+ref基线） | 1,430 | 164 | 0.1x |
| snapshot #1（导航后） | **77** | 6,931 | **90x** |
| snapshot #2（页面无变化） | **77** | 6,931 | **90x** |
| type | 24 | 57 | 2.4x |
| click | 12 | 42 | 3.5x |
| snapshot #3（DOM 变更后，全量） | 1,372 | 6,931 | 5.1x |
| console 读取 | 104 | 86 | 0.8x |
| evaluate | 102 | 37 | 0.4x |
| navigate → 页2 | 307 | 151 | 0.5x |
| snapshot #4（导航后，diff 命中） | **76** | 4,702 | **62x** |
| **合计** | **3,581** | **26,032** | **7.3x** |

字符口径: 7,373 vs 66,165（**9.0 倍**）。

## 结论与边界

1. **差距的主引擎是快照策略**: playwright-mcp 每次 snapshot 全量返回 YAML（该 fixture 页 ~6,900 tok）; nexus 页面无变化时返回 ~120 字符的 diff 提示（90 倍单步差距）。真实 agent 工作流里重复读同一页面是常态（轮询、表单多步、状态确认）, 该杠杆被反复触发。
2. **navigate 播种基线**: nexus 的 navigate 默认附首屏快照并记录 digest/ref 基线, 因此导航后的第一次显式 snapshot 直接命中 diff（步骤 #1/#4）。代价是 navigate 单步比对手贵（1,430 vs 164）——对手 navigate 不附快照, 但 agent 几乎必然紧接一次 snapshot, 合并计算（1,507 vs 7,095）仍是 4.7 倍差距。
3. **全量场景也省**: DOM 变更后的全量快照 1,372 vs 6,931（5.1 倍）——nexus 的行内属性格式更紧凑, 阅读模式收敛静态文本。
4. **诚实边界**: 对手快照同样是 a11y 文本而非截图, 本对比测的是 diff/基线/紧凑渲染的增量价值, 不是"文本 vs 图片"的数量级差。双方默认配置, 未用对方的高级开关（`--caps` 等）。fixture 规模固定; 更大页面差距会按比例放大（全量 YAML 随页面线性增长, diff 命中恒定 ~120 字符）。
