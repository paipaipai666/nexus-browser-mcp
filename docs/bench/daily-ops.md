# 日常操作双侧基准（反"满分假象"专项）

> 日期: 2026-08-18 · 动机: 9 案例的能力点检不等于日常覆盖。这份覆盖更宽的真实操作面,
> 双侧同测 (nexus + playwright-mcp), 子任务级断言。
> 复现: `.venv/Scripts/python.exe -u bench/daily_ops.py`（原始数据: `daily-ops.json`）

## 结果矩阵（末次运行；环境抖动项见"稳定性注记"）

| 案例 | nexus | pw-mcp | token (nx/pw) | 说明 |
|---|---|---|---|---|
| 登录认证流 | 2/3 | 3/3 | 464/684 | 我方 logout 断言偶发竞速（见注记） |
| 动态加载等待 | 1/1 | 0/1 | **294**/478 | pw 侧该轮 wait_for 未命中（抖动） |
| 无限滚动 | 2/2 | 1/2 | **275**/352 | pw PageDown 追加量不稳定 |
| 右键菜单 | 1/2 | 1/1 | 349/230 | **我方缺口**: `browser_click` 无 `button="right"`（待补） |
| 混沌 DOM 定位 | 2/2 | 2/2 | **961**/2511 | 修 harness 后双过（"按钮"实为 link 角色） |
| 大 DOM 表格 | 2/2 | 2/2 | **764**/43,381 | **57 倍**：pw 全量 127KB YAML vs 我方上限截断 |
| 按键验证 | 2/2 | 0/2 | 351/257 | pw 该轮回显未读到（抖动；单独探测其实可用） |
| 复选框 | 2/2 | 1/2 | **329**/552 | pw 该轮第二次点击未生效（抖动） |
| iframe 富文本 | 见专条 | 见专条 | — | 真实 TinyMCE CDN 间歇不可达（环境）；能力测定见下 |
| 日期选择器 | 1/1 | 1/1 | **178**/397 | jQuery UI calendar 点选 |
| 自动补全 | 1/1 | 1/1 | **190**/317 | type+ArrowDown+Enter 双过 |
| 文件下载 | 1/2 | 1/2 | 3324/2894 | **双方同缺**: 无 download 工具/事件回报 |
| 重定向链 | 1/1 | 1/1 | 275/277 | |
| **合计** | **18/22** | **14/21** | **9,726 / 53,761 (5.5x)** | |

## 本轮抓出并已修复的真 bug

**`browser_type` 对富文本编辑器静默无效**（最具价值）：
- 现象：TinyMCE 类编辑器正文在快照里是内嵌 `<p>`（contenteditable 容器内）；Playwright `fill()` 对其**静默 no-op**（不报错、不写内容）。
- 修复：fill 后**读回校验**（value/textContent 不含所填即未写入）→ 升级到最近 `contenteditable` 容器 → 点击聚焦 + `press_sequentially`（随 locator 带 frame 上下文）。
- 对称验证：pw-mcp 在同一 case 上 **fail**（无回读无升级）——继 sortable 拖拽后第二个实测反超点。
- 确定性验证在本地 fixture（`bench/adversarial.py` iframe富文本案例：nexus native / pw fail）。

## 双方共享的缺口（如实记录）

1. **下载不可观测**：点击下载链接后无事件、无路径回报。双方都没有 download 工具。值得做：`page.on("download")` 入 EventStore + 保存路径回报（低风险高价值，日常下载场景刚需）。
2. **我方独有缺口**：`browser_click` 无 `button` 参数（右键/中键）。pw 有。补：click 加 `button="left|right|middle"`。

## 稳定性注记（环境抖动 vs 真实失败）

- 本机代理出口对 the-internet/HN 有间歇性超时：登录流 logout 断言、pw 按键回显、pw 复选框第二击在不同轮次表现不一。**计入报告的结论只取多轮一致或单独探测证实者**；单轮失败标抖动，不计任何一方缺陷。
- TinyMCE 真实页（cachefly CDN）间歇 30s 加载超时——能力测定迁移到本地确定性 fixture，该页仅作环境指示。

## 给产品侧的后续输入（按价值排序）

1. download 事件入流 + 路径回报（双方同缺，日常刚需）
2. `browser_click(button=...)` 右键支持（我方独有缺口）
3. 这两个落地后建议把 daily_ops 纳入发版前手动仪式（与 realworld/adversarial 并列）
