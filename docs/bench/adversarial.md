# 对抗性能力基准: 瞄准 nexus 的缺口（问题清单, 非宣传材料）

> 日期: 2026-08-17 · 动机: realworld 基准只测"双方都会做的题", 赢可能只是对手失误。
> 这份反过来: 用 playwright-mcp 的独有工具集当靶子, 测我们的能力覆盖缺口。
> 计三档: ✅原生 = 专用工具完成; 🟡逃生舱 = evaluate/JS 绕过; ❌ = 完全做不了。
> 复现: `.venv/Scripts/python.exe -u bench/adversarial.py`（本地 fixture, hermetic）

## 结果矩阵

### 修复后复测（第一波补洞落地, commit 本轮）

| 案例 | nexus | pw-mcp | token (nx/pw) |
|---|---|---|---|
| hover 菜单 | ✅原生 | ✅ | **155**/454 |
| select 下拉 | ✅原生 | ✅ | **154**/295 |
| 文件上传 | ✅原生（HITL confirmed） | ✅ | **178**/411 |
| 键盘 Esc | ✅原生（真实 CDP 键事件） | ✅ | **163**/314 |
| confirm 对话框（需接受） | ✅原生（挂起→HITL 决策→respond，第二波） | ✅ | **317**/333 |
| HTML5 拖拽 | ✅原生（真实输入管线） | ✅ | **128**/313 |
| 批量表单 | ✅ | ✅ | **365**/483 |
| 后退导航 | ✅原生 | ✅ | **235**/332 |
| iframe 深交互 | ✅原生 | ✅ | **350**/409 |

**9/9 原生完成, 且全部案例 token 低于对手。** 对话框案例走的是我们独有的治理闭环: 挂起 → 事件留痕 → agent/用户经 HITL 决策 → `browser_dialog_respond`——比对手的"接受/拒绝"工具多一层确认, 但确认对话框本来就该是用户的决定。

### 修复前（首轮, 留档对照）

| 案例 | nexus | pw-mcp |
|---|---|---|
| hover 菜单（仅 mouseenter） | 🟡 | ✅ |
| select 下拉 | 🟡 | ✅ |
| 文件上传 | ❌ | ✅ |
| 键盘 Esc | 🟡 | ✅ |
| confirm 对话框（需接受） | 🟡 | ✅ |
| HTML5 拖拽 | 🟡 | ✅ |
| 批量表单（5 字段） | ✅ | ✅ |
| 后退导航 | 🟡 | ✅ |
| iframe 深交互 | ✅ | ✅ |

## 首轮发现的问题清单与处置（按严重度）

### P0 — 完全做不了 → 已修复
1. **文件上传**：无工具；浏览器安全模型禁止 JS 设 `input.files`，逃生舱不存在。→ `browser_upload_file(selector/ref, paths, confirmed)`（set_input_files + 无条件 HITL 确认门 + 路径入审计）。

### P1 — 逃生舱有硬伤 → 五个已修复, 对话框移交第二波
2. **无 `press_key`** → `browser_press_key(key)`：真实 CDP 键事件（isTrusted=true），Escape/Tab/组合键全通。
3. **无 `hover`** → `browser_hover(...)`：真实鼠标移动，CSS `:hover` 与 JS mouseenter 均触发。
4. **confirm 对话框无法接受** → **第二波已落地**: `page.on("dialog")` 入 EventStore（可观测）+ confirm/prompt 挂起等决策（alert 无决策直接 dismiss）+ 触发及后续工具回复前置 `[对话框等待决策]` + `browser_dialog_respond(accept, prompt_text, confirmed)`（accept 必须 confirmed=true，dismiss 自由）+ 超时自动 dismiss 兜底留痕（`BROWSER_DIALOG_TIMEOUT_MS` 默认 20s，防页面永久冻结）。
5. **无 drag** → `browser_drag(from_*, to_*)`：真实输入管线。
6. **select 不可见** → `browser_select_option(values, ...)`（`option` 角色本就在 INTERACTIVE_ROLES, 首轮快照不可见是 harness 用了 diff 短消息）。

### P2 — 低效 → 后退已修复, fill_form 不做
7. **批量表单** 8 调用 vs 4：token 反而我们更省；往返数差异不值得加 `fill_form` 维护面。**不做**。
8. **后退导航** → `browser_navigate_back()`（go_back + 空历史明确提示）。

### 已验证的优势（复测仍成立）
- **iframe 深交互**：f-前缀 ref 原生点击通过。
- **token 效率**：对抗场景全案例低于对手（128-365 vs 295-483）。

## 附：harness 侧修正记录（对方失败必须是真失败）
- `browser_handle_dialog` 须在对话框打开后调用（modal 态语义），修正顺序后对方通过
- `browser_file_upload` 须先点开 file chooser，修正后对方通过
- 我方 iframe 首轮失败是 harness 用了 diff 命中的短消息找 ref（navigate 基线副作用）——`diff=false` 显式全量后通过。**副产品发现：agent 在 navigate 后立即 snapshot 会收到"[快照无变化]"，需要心智模型知道 ref 在 navigate 回复里**——文档应写明（USAGE.md）。
