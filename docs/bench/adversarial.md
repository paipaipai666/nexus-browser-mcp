# 对抗性能力基准: 瞄准 nexus 的缺口（问题清单, 非宣传材料）

> 日期: 2026-08-17 · 动机: realworld 基准只测"双方都会做的题", 赢可能只是对手失误。
> 这份反过来: 用 playwright-mcp 的独有工具集当靶子, 测我们的能力覆盖缺口。
> 计三档: ✅原生 = 专用工具完成; 🟡逃生舱 = evaluate/JS 绕过; ❌ = 完全做不了。
> 复现: `.venv/Scripts/python.exe -u bench/adversarial.py`（本地 fixture, hermetic）

## 结果矩阵

| 案例 | nexus | pw-mcp | 调用数 (nx/pw) |
|---|---|---|---|
| hover 菜单（仅 mouseenter） | 🟡 | ✅ | 4/4 |
| select 下拉 | 🟡 | ✅ | 6/3 |
| 文件上传 | ❌ | ✅ | 1/4 |
| 键盘 Esc | 🟡 | ✅ | 4/4 |
| confirm 对话框（需接受） | 🟡 | ✅ | 6/4 |
| HTML5 拖拽 | 🟡 | ✅ | 3/3 |
| 批量表单（5 字段） | ✅ 365tok | ✅ 483tok | 8/4 |
| 后退导航 | 🟡 | ✅ | 4/4 |
| iframe 深交互 | ✅ | ✅ | 4/4 |

## 问题清单（按严重度）

### P0 — 完全做不了
1. **文件上传**：无工具；浏览器安全模型禁止 JS 设 `input.files`，逃生舱不存在。表单自动化硬缺口。修复方向：`browser_upload_file(selector, paths)` 包 `set_input_files`，HITL 中风险门。

### P1 — 逃生舱可行但有硬伤
2. **无 `press_key`**：synthetic `KeyboardEvent` 的 `isTrusted=false`——检查可信事件的站点直接拒绝；Tab 序/快捷键/组合键完全覆盖不了。日频最高，优先补。
3. **无 `hover`**：本 fixture 用 JS 监听所以逃生舱能过；**CSS `:hover` 纯样式菜单 JS 无法触发**（无真实鼠标移动）——此类页面我们完全不可达。
4. **confirm 对话框无法接受**：Playwright 默认 auto-dismiss → `confirm()` 返回 false → 首次点击无效；逃生舱 stub `window.confirm` 是篡改页面行为。且符合我们治理哲学的正确解法存在：**对话框出现 = 风险决策点，应入事件流 + HITL 询问**，现在 agent 连"弹过对话框"都无法观测。
5. **无 drag**：synthetic DragEvent 序列能骗过本 fixture；pointer-events 系拖拽库（react-dnd 等多数实现）骗不过。
6. **select 不可见**：`option` 角色不在 `INTERACTIVE_ROLES`，快照里看不到选项，只能逃生舱赋值。补 `browser_select_option` 是平凡封装。

### P2 — 能做但低效
7. **批量表单**：8 调用 vs 4（对手 `fill_form` 一次填完）。token 反而我们更省（365 vs 483，回复更紧凑），但往返翻倍。低优先级。
8. **后退导航**：`history.back()` 逃生舱可用，平凡封装可补。

### 已验证的优势（不是问题的）
- **iframe 深交互**：f-前缀 ref 原生点击通过——之前声称的优势这次有了实测背书。
- 批量表单 token 更低、全场景 token 均低于对手（167-365 vs 295-483/案例）。

## 结构性结论

**默认配置下这份清单更难看**：`BROWSER_ALLOW_JS_EXECUTION` 默认关 → 所有 🟡 在默认部署里全是 ❌，即 9 个案例只有 2 个原生可做。这是治理优先的代价，诚实地说：**我们用操作覆盖面换了 token 效率（7-25x）+ 治理（HITL/审计/多任务）+ 观测性**。对手（含 `browser_run_code_unsafe` 任意代码）走的是"能力全集"路线。

补洞建议顺序（日频 × 可行性）：`press_key` > `upload_file` > `hover` > 对话框事件+HITL > `select_option` > `navigate_back` > drag > fill_form。

## 附：harness 侧修正记录（对方失败必须是真失败）
- `browser_handle_dialog` 须在对话框打开后调用（modal 态语义），修正顺序后对方通过
- `browser_file_upload` 须先点开 file chooser，修正后对方通过
- 我方 iframe 首轮失败是 harness 用了 diff 命中的短消息找 ref（navigate 基线副作用）——`diff=false` 显式全量后通过。**副产品发现：agent 在 navigate 后立即 snapshot 会收到"[快照无变化]"，需要心智模型知道 ref 在 navigate 回复里**——文档应写明（USAGE.md）。
