# 新工具真实场景实测（第一波+第二波 7 工具的回归猎杀）

> 日期: 2026-08-17 · 范围: press_key / hover / select_option / upload_file / navigate_back / drag / dialog_respond
> 与 adversarial.md 的区别: 那份用 hermetic fixture 证"能力存在"; 这份用真实站点/标准测试场证"站得住"。
> 只测 nexus（目标是找我们的问题）。复现: `.venv/Scripts/python.exe -u bench/newtools_real.py [子串过滤...]`

## 状态总览

| 案例 | 站点 | 状态 |
|---|---|---|
| pointer 系拖拽 (sortable) | jqueryui.com | ✅ 已跑, **抓出真 bug 并修复**（见下） |
| GitHub 键盘快捷键 | github.com | ⚠️ 环境阻塞后待复跑 |
| HTML5 拖拽 | 本地 fixture 回归 | ✅ 通过（分段路径回归） |
| select 下拉 / 文件上传 / hover / 对话框三部曲 / 维基 hovercard / 搜索后退流 | the-internet / wikipedia / duckduckgo | ⏸️ 本机直连不可达, 待代理恢复补跑 |

## 已确认的发现

### 1. `drag_to` 对 pointer 系库静默无效 → 已改分段拖拽（真 bug, 已修复）

- **现象**：jQuery UI sortable 上 `browser_drag` 返回成功但顺序未变（`Item 1,2,3…` 原样）——静默无效比报错更糟。
- **根因**：Playwright `drag_to` 的事件序列对需要 hover 激活 + 中间点移动的 pointer 系库不够拟人。
- **修复**：分段路径（hover 源 150ms → down → 10 步过中间点 move → 落点偏目标下半部 → up），HTML5 原生 dnd 与 pointer 系通吃（同一真实输入管线）。
- **验证**：修复后 sortable 实测 Item 1 → 第 3 位 ✓；HTML5 fixture 回归 ✓。
- **对手对称性**：pw-mcp `browser_drag` 同用 `drag_to` 原语，疑似同病——待代理恢复后对称验证；若成立为反超点。

### 2. 系统代理陈旧 = 全站超时无诊断 → 新增 `BROWSER_PROXY`（环境级发现）

- **现象**：系统代理（127.0.0.1:7890）停止后，Chromium 默认跟随系统代理 → 所有站点 `ERR_CONNECTION_TIMED_OUT`，无任何诊断提示。
- **处置**：`BROWSER_PROXY=none` → `--no-proxy-server` 绕过系统代理；或显式 `http://host:port`。
- 这是实测过程本身的副产品：基准环境故障即真实用户故障。

## 待补跑（代理恢复后）

the-internet 五连（dropdown / upload+HITL 门 / hovers / javascript_alerts 三部曲含 prompt 填文本 / drag_and_drop 复测）、维基正文链接 hovercard、DDG 搜索→进结果→`navigate_back`→ref 失效重取组合流、GitHub `/` 快捷键聚焦。对称验证 pw-mcp 的 `browser_drag` 在 sortable 上是否同样静默无效。

原始数据: `newtools-real.json`（当前为部分运行）。
