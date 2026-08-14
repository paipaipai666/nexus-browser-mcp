# Nexus Browser MCP — 实测踩坑记录（供修复参考）

> **修复状态（2026-08-14，全部已修复并验收）**
>
> | # | 状态 | 修复内容 |
> |---|------|----------|
> | A | ✅ | 入口名修正为 `nexus-browser-mcp`/`nexus-browser` 双入口；0.1.0 已发布 PyPI,`uvx` 实测拉起 |
> | B/C | ✅ | 根因是快照解析器正则丢节点(非 a11y 提取弱):两段式重写 + 定位链(ref 落地/输入族回退/失败自救清单) |
> | D | ✅ | 同 B 根因;行内文本进快照,reading 模式极简页可见正文 |
> | E | ✅ | 实测证明视口过滤原是惰性代码;现为 reading/interactive 视口内、full 全局(标 `[offscreen]`);滚动后自动 settle 修掉快照塌方 |
> | F | ✅ | `scope` 支持 ref(`e57`→`aria-ref=e57`)+ 5s 超时 + 明确报错 |
> | G | ✅ | 死亡观测 + 自愈:外部关闭/崩溃自动重建恢复 URL,`[状态变更]` 显式上报;TTL 回收从死代码修复为真实生效 |
> | H | ✅ | `browser_read(wait_stable=true)` 一次读全;`follow=true` 增量环形缓冲;新增 `browser_wait_stable`/`browser_wait_ms` |
> | I | ✅ | 弹窗重做:dialog 域优先 + 图标关闭钮 + 多轮防重复;授权类按钮(同意/接受)不再自动点 |
>
> 另有 4 个分析中新发现的 bug 一并修复:`ref` 死参数、`include_generic` 不可达、共享 context 新标签扇出、`_page_owners` 泄漏。

> 本文档记录以**真实 MCP 客户端驱动**方式（非单元测试）使用 `nexus-browser-mcp` 时踩到的所有坑。
> 测试方式：将本项目接入 WorkBuddy 的 MCP 系统后，直接调用 `browser_*` 工具真实导航、输入、读取、截图。
> 测试时间：2026-08-13。测试环境：Windows 11 / Python 3.13.12（venv）/ Playwright Chromium / 持久化 profile 带登录态。
> **本文只做记录，未改动任何 `src/` 代码。**

---

## 0. 背景与测试覆盖

- 接入配置（`~/.workbuddy/mcp.json`）：
  ```json
  "browser": { "type": "stdio", "command": "D:\\code\\nexus-browser-mcp\\.venv\\Scripts\\nexu-browser.exe", "disabled": false }
  ```
- 实测覆盖的能力：导航、快照（interactive/reading/full 三模式）、输入发送、读文本、截图、滚动、多 task 隔离、弹窗、登录态持久化、流式回复读取。
- 关键实测数据：18 个工具全部注册；事件驱动快照约 1.1s 出结果（6643 字符结构化）；`wait_navigation` 约 0.4s 触发；综合流程 15/16 通过（唯一 FAIL 是测试脚本自身断言写错，非 MCP 问题）。

---

## 1. 问题汇总（快速索引）

| # | 问题 | 严重度 | 类别 | 是否确认 bug |
|---|------|--------|------|--------------|
| A | `pyproject.toml` 入口名 typo（`nexu-browser` 少 s），README 的 `uvx nexus-browser-mcp` 跑不通 | 中 | 安装/配置 | 确认 |
| B | 标准 ARIA role 定位失败（Bing 搜索框 `combobox`/`textbox` 都找不到） | 高 | 元素定位 | 确认 |
| C | 登录后 SPA 输入框无法用 role/name/contenteditable 定位，最终靠 `textarea` 选择器 | 高 | 元素定位 | 确认 |
| D | 极简页（example.com）`browser_snapshot` 三模式都几乎为空，必须 `browser_read` | 中 | 快照 | 确认 |
| E | 滚动后快照信息大幅丢失（6643 → 638 字符），视口外被剔除 | 中 | 快照 | 确认 |
| F | `browser_snapshot` 的 `scope` 参数传 ref（`"e57"`）报语法错误 | 低 | API/文档 | 待作者确认 |
| G | 默认 task 页面被回收后未重建，复用语报错 `Target page, context or browser has been closed` | 严重 | 生命周期 | 确认（根因推测） |
| H | 流式回复需多次 `browser_read` 才能拿全 | 低 | 使用模式 | 非严格 bug |
| I | SPA 弹窗（豆包"Seedance 上线"）`browser_dismiss_popup` 处理不可靠 | 低/中 | 弹窗 | 待确认 |

> B 与 C 本质同类根因：**富客户端 / SPA 的输入框未被 accessibility tree 识别为标准控件**，单靠 role+name 定位会失败。

---

## 2. 详细记录

### Issue A — entry point 拼写错误（安装即用性）

- **现象**：`pyproject.toml` 的 `[project.scripts]` 里入口名写成了 `nexu-browser`（少了字母 `s`）。`pip install` 后生成的可执行文件是 `nexu-browser.exe`，与包名 `nexus-browser-mcp` 不一致。README 与所有客户端接入示例写的都是 `uvx nexus-browser-mcp` 或 `command: "nexus-browser"`。
- **影响**：用户照 README 配 `command: "uvx", args: ["nexus-browser-mcp"]` 会启动失败（找不到可执行入口）。实测中只能靠 `python -m nexus_browser.server` 或 typo 版 `nexu-browser.exe` 拉起。
- **复现**：查看 `pyproject.toml` 的 scripts 段；`pip install` 后 `which nexu-browser`。
- **Workaround（已验证）**：用 `python -m nexus_browser.server` 启动，或直接使用 typo 版 `.venv/Scripts/nexu-browser.exe`（文件存在且能作为 stdio server 正常工作）。
- **修复建议**：
  1. 把入口名改对：`nexus-browser`（建议在 `pyproject.toml` 修正，并同步 README 与 `docs/INTEGRATE.md`）。
  2. 修正后用 `uvx nexus-browser-mcp` 真实验证一次（uvx 按包名装包后执行其 console_scripts，脚本名错会导致 uvx 也拉不起）。
  3. 在 README 同时给出 `python -m nexus_browser.server` 这一定不会错的兜底启动方式。

---

### Issue B — 标准 ARIA role 定位失败（Bing 搜索框）

- **现象**：`browser_type` 传 `role=combobox, name="Search the web"` 失败；再试 `role=textbox` 也失败。最终只能用 CSS selector `textarea, input[type='search'], input[name='q']` 命中。
- **复现**：导航 `https://cn.bing.com` → 用 role+name 调 `browser_type` 输入。
- **根因推测**：Bing 把搜索框渲染为自定义组件（非标准 `<input type=search>` 的 ARIA 标注，或实际是带自定义 role 的 div），accessibility tree 提取对这类非标准组件不稳定。
- **Workaround（已验证）**：改用 CSS selector 兜底。
- **修复建议**：
  - 在 `browser_type` / `browser_click` 的定位逻辑里，role+name 找不到时**自动回退**到一组 CSS selector 候选（`textarea, input, [contenteditable], [role=textbox], [role=searchbox]` 等）。
  - 提供更强的"按 placeholder / 可见占位文本定位"能力（很多搜索框靠 placeholder 文本辨识）。
  - 在 snapshot 输出中，对输入框之类关键元素附带一个可直接用的 selector，降低 LLM 自行猜测成本。

---

### Issue C — 登录后 SPA 输入框定位失败（豆包 doubao.com）

- **现象（实测顺序）**：
  - `role=textbox, name="发消息..."` → ❌ 找不到
  - `selector=[contenteditable="true"]` → ❌ 找不到
  - `selector=textarea` → ✅ 成功，输入并回车发送
- **附加现象**：未登录时该输入框在 accessibility 树里是 `textbox`；登录后豆包把它换成（疑似）`textarea`，且未标为 textbox，accessibility 树时隐时现。刷新页面后输入框才稳定出现在树中。
- **根因推测**：登录后页面组件动态切换 / 延迟渲染，accessibility tree 捕获不全；输入框本质是 `textarea` 但未被正确标注。与 Issue B 同类。
- **Workaround（已验证）**：直接用 `selector=textarea`；必要时先 `browser_navigate` 刷新稳定页面再定位。
- **修复建议**：
  - 统一增强定位器优先级：**用户显式传 selector 时优先 selector**；未传 selector 时，role/name 失败自动回退到 selector 候选列表（textarea / [contenteditable] / input / [role=textbox]）。
  - 提供"按可见占位文本定位"的 helper，覆盖聊天框、评论框等常见场景。
  - （可选）对内容动态出现的元素，定位前先 `wait_for` 该 selector 出现。

---

### Issue D — 极简页快照几乎为空

- **现象**：`https://example.com`（一段文字 + 一个 "More information..." 链接），`interactive` / `reading` / `full` 三种模式 `browser_snapshot` 都几乎拿不到内容（interactive 返回"无可交互元素"，reading/full 也几乎为空）。必须 `browser_read` 才读到正文 "Example Domain / This domain is for use in illustrative examples..."。
- **复现**：导航 example.com → 三种模式各调一次 snapshot。
- **根因推测**：accessibility tree 抽取对纯文本块 / 简单链接极弱，full 模式可能仍按"角色"过滤掉了纯文本节点。
- **Workaround（已验证）**：用 `browser_read` 兜底读正文。
- **修复建议**：
  - `reading` / `full` 模式应保留页面可见纯文本（哪怕无交互元素）。
  - 当某模式下"无可交互元素"时，**附带一段可见文本摘要**，避免用户被迫再发一次 `browser_read` 调用。

---

### Issue E — 滚动后快照信息大幅丢失

- **现象**：滚动前 full 快照约 6643 字符，向下滚动后快照骤降到 638 字符——视口外元素被剔除。对搜索结果页、信息流等长列表页，只返回当前视口内容，需要反复调用。
- **复现**：导航 Bing 结果页 → 调一次 full 快照记录长度 → `browser_scroll` 后再调一次 full 快照。
- **根因推测**：snapshot 基于视口可见性裁剪节点。
- **修复建议**：
  - 提供"全文档快照"选项（不按视口裁剪），或标记每个节点的可见性（visible/offscreen）而非直接丢弃。
  - 支持按 region / 区块分页导出，提升信息密集型页的可用性。

---

### Issue F — `scope` 参数语法报错

- **现象**：`browser_snapshot` 传 `scope: "e57"`（ref id）报语法不对。
- **复现**：导航 doubao.com → `browser_snapshot(mode="full", scope="e57")`。
- **根因推测（待确认）**：`scope` 预期可能是 CSS selector / XPath，而非 snapshot 输出的 ref id；文档与工具 schema 未明确其取值格式。
- **修复建议**：
  - 明确 `scope` 接受的格式（CSS selector / ref / XPath），并在 `docs/` 与工具 JSON schema 中写清楚。
  - 若设计上支持 ref，应与 snapshot 输出的 ref 格式保持一致；否则在 schema 里把它标为 selector 类型，避免用户误传 ref。

---

### Issue G — 默认 task 页面被回收后未重建（最严重）

- **现象**：不传 `task_id` 用默认 task，第一次 `browser_navigate` 成功；**间隔稍长后**（可能触发 TTL 空闲回收或 context 关闭）再用默认 task 调任意工具，报错：
  ```
  Target page, context or browser has been closed
  ```
  换一个**新的 `task_id`** 后立刻正常。
- **复现**：`browser_navigate(默认task)` → 等待一段时间（分钟级）→ 再次用默认 task 调 `browser_navigate` / `browser_snapshot` → 报错。
- **根因推测（高可信）**：默认 task 的 page / context 被回收（TTL 或空闲关闭），但 session 内存里的 page 引用未清除、也未做 `is_closed` 检测，`get_page()` 直接返回陈旧引用，调用 Playwright 时抛错。
- **影响**：长期会话（陪聊、定时任务、两次操作间隔稍长）会**随机失败**，且 LLM 不知道要换 task_id，体验差。
- **Workaround（已验证）**：长时间间隔后换 `task_id`，或每次使用前显式 `browser_close_session` 再新建。
- **修复建议（P0）**：
  1. `get_page()` 应检测 page / context 是否已关闭（`page.is_closed()` / `context.is_closed()`）；若已关闭则**在同一 task_id 下重建**（从空白页或上次 url 恢复），而不是抛错。
  2. TTL 回收前清理 session 引用，使下次调用自动建新 page。
  3. 至少应在报错信息中明确提示："默认 task 已被回收，请指定新的 `task_id` 或重启会话"，而不是返回底层 Playwright 异常。

---

### Issue H — 流式回复需多次读取

- **现象**：豆包回复是逐字流式生成。第一次 `browser_read` 只拿到占位/部分文本（如只显示"30 秒"），需连续读 2~3 次才完整。
- **复现**：发送消息 → 立即 `browser_read` → 再读 → 再读。
- **根因**：回复文本在生成中，snapshot / read 抓到中间态。
- **修复建议（非严格 bug，体验优化）**：
  - 提供 `browser_wait` 等待"某元素文本停止变化 / 出现结束标记"的能力。
  - 或给 `browser_read` 加 `wait_for_stable` 参数（等待稳定性窗口后再读），减少轮询次数。

---

### Issue I — SPA 弹窗 `browser_dismiss_popup` 处理不可靠

- **现象**：打开豆包时弹出"Seedance 2.5 视频模型上线"提示层，调 `browser_dismiss_popup` 后仍有残留/需忽略。该弹窗是 DOM 元素而非浏览器原生 dialog。
- **复现**：导航 doubao.com → `browser_dismiss_popup`。
- **根因推测（待确认）**：`browser_dismiss_popup` 主要处理原生 `dialog` 事件，对 DOM 层自绘弹窗（关闭/X/"我知道了"按钮）无能为力。
- **修复建议**：
  - `dismiss_popup` 同时处理常见 DOM 弹窗：扫描带"关闭 / X / 取消 / 我知道了 / 稍后"等语义的按钮并点击。
  - 或暴露一个"按文本点击关闭按钮"的通用能力，交给 LLM 处理 SPA 弹窗。

---

## 3. 已验证正常 / 值得保留的特性（对照）

- ✅ 18 个 `browser_*` 工具全部注册，调度正常。
- ✅ 事件驱动确定性快照稳定（约 1.1s 出结果，无竞态），内容结构化丰富。
- ✅ `wait_navigation` 极快（约 0.4s 触发，事件驱动非轮询）。
- ✅ 多 task 隔离是真的隔离：task_a 在 example.com、task_b 在 bing.com，内容互不污染。
- ✅ 安全门设计合理：`browser_evaluate` 默认禁用，提示明确，开启后无条件 HITL。
- ✅ 登录态持久化有效：用户用持久化 profile 后，doubao.com 左侧出现"云盘/技能/项目/最近对话"及账号名，说明 cookie 保留。
- ✅ Windows 下 `.exe` launcher 作为 stdio server 实测可用（用户 `nexu-browser.exe` 已接入 WorkBuddy 成功，工具注入正常，端到端调用通过）。

---

## 4. 修复优先级建议

- **P0（阻断长期使用）**：G — 默认 task stale page 不重建。
- **P1（阻碍主流站点自主操作）**：B、C — 输入框 / 表单元素的定位健壮性（role+name 失败自动回退 selector 候选 + 按 placeholder 定位）。
- **P2（体验与可维护性）**：A — entry point typo（影响安装即用）；D、E — 快照对极简页 / 长列表页的信息完整性；I — SPA 弹窗处理。
- **P3（打磨）**：F — `scope` 文档 / schema 明确；H — 流式回复等待能力。

---

## 5. 附：复现用的关键调用序列（节选）

```text
# 示例 B/C：定位失败回退
browser_navigate(url="https://cn.bing.com")
browser_type(role="combobox", name="Search the web", text="...")   # ❌
browser_type(selector="textarea, input[type='search']", text="...") # ✅

# 示例 G：默认 task 回收
browser_navigate(url="https://example.com")        # 成功（默认 task）
# ... 间隔数分钟 ...
browser_navigate(url="https://www.doubao.com/chat/") # ❌ Target page...closed
browser_navigate(url="https://www.doubao.com/chat/", task_id="doubao1") # ✅

# 示例 C：豆包输入框
browser_type(role="textbox", name="发消息...", text="...")   # ❌
browser_type(selector="[contenteditable='true']", text="...") # ❌
browser_type(selector="textarea", text="...", press_enter=true) # ✅

# 示例 D：极简页
browser_snapshot(mode="reading")  # 几乎为空
browser_read()                    # ✅ 拿到正文
```

> 截图证据留存于 `C:\Users\29218\.nexus-browser\screenshots\`（如 `screenshot_cbd57c68.png` = Bing 结果页；`screenshot_ec7dd150.png` = 豆包聊天页）。

---

## 6. 修复后复测结果（2026-08-13 第二轮，用户修复后）

> 复测方式：与首轮相同——WorkBuddy 接入后直接调 `browser_*` 工具真实驱动浏览器。
> 验证顺序：先比对工具 schema 变化，再逐项真实操作。结果与首轮踩坑一一对应。

### 6.1 schema 层面可见的修复痕迹（直接读工具定义确认）

| 工具 | 变化 | 对应 Issue |
|---|---|---|
| `browser_type` | 定位优先级改为 `pos → ref → selector → role+name(不中自动回退 placeholder/label/常见输入框CSS)` | B、C |
| `browser_snapshot` | 新增 `include_offscreen`；`mode=full` 自动包含全页面（视口外标 `offscreen`）；新增 `wait_stable` | E、D |
| `browser_read` | 新增 `wait_stable`（等 DOM 静默一次读全）+ `follow`（增量跟踪流式） | H |
| `browser_dismiss_popup` | 改为"关闭按钮→取消→Escape"三级回退，能处理 DOM 层 dialog | I |
| `pyproject.toml` | `[project.scripts]` 同时注册 `nexus-browser-mcp` 和 `nexus-browser` 两个入口 | A |

### 6.2 逐项复测结论

| Issue | 状态 | 复测方式与结果 |
|---|---|---|
| A（typo 入口名） | ✅ 已修复 | `pyproject.toml` 现注册 `nexus-browser-mcp` + `nexus-browser` 双入口；`uvx nexus-browser-mcp` 与 `nexus-browser` 均可启动。 |
| B（Bing 搜索框 role 定位） | ⚠️ 部分修复 | `role=searchbox, name="输入搜索词"` 成功定位+填充+回车搜索（全流程 OK）。但 `role=textbox`（错误 role）触发回退后命中 Bing 外层 `div.sb_form_c`（aria-label wrapper，非可填充元素），`clear` 报 "Element is not an input/textarea/select/contenteditable"。**正确 role 可用；回退逻辑对"wrapper div 包裹 input"处理不干净**。 |
| C（豆包输入框 role 定位） | ✅ 已修复 | 未登录态 `role=textbox name="发消息..."` 成功输入；重新登录后 `role=textbox name="发消息或按住空格说话..."` 也成功输入并发送。输入框稳定出现在 a11y 树。 |
| D（极简页快照） | ✅ 已修复 | `example.com` 的 `reading` 模式完整拿到 `Example Domain` 标题 + 段落正文 + `Learn more` 链接，无需再 fallback `browser_read`。 |
| E（视口外元素保留） | ✅ 已修复 | Bing 结果页 `mode=full, include_offscreen=true` 完整列出全部结果（含 `[offscreen]` 标注，一直到 y=3218 页脚），并自动附带每个链接真实 URL。不再滚动后丢失。 |
| F（scope 参数） | ✅ 已修复 | `scope="h1"` 正常限制快照范围（仅返回 `Example Domain` 标题）。首轮传 ref `e57` 报语法错，现接受 CSS selector 格式，正常。 |
| G（默认 task 重建，P0） | ✅ 已修复（重建路径） | `browser_close_task(task_id="")` 关闭默认 task 后，`browser_navigate(task_id="", url="example.com")` **自动重建成功**，不再报 `Target page, context or browser has been closed`。重建逻辑生效。 |
| H（流式回复读取） | ✅ 已修复 | `browser_read(selector="main", wait_stable=true)` 一次读全豆包回复（"我是豆包，字节跳动自研的 AI 助手…"），免首轮那种轮询 3 次。 |
| I（弹窗 dismiss） | ✅ 已修复 | 豆包 DOM 层 `dialog` 弹窗"可设置去除「AI 生成」水印"，`browser_dismiss_popup` 自动点击 dialog 内"关闭"按钮成功关闭。 |

### 6.3 验证后仍需关注的点（非阻塞）

1. **B 的回退瑕疵**：当 `role+name` 回退到 `get_by_label` 命中"外层 aria-label wrapper div"（不可编辑）时，应向下查找可编辑子元素（input/textarea/contenteditable）或跳过非可填充元素选最近的 editable，否则 LLM 用错 role 仍会 `fill` 失败。建议修复。
2. **G 的 TTL 自动回收场景未在本轮触发**：默认 task TTL 为 600s，本轮复测时间不足以等到自动回收。已验证 `close_task → navigate` 重建路径；TTL 回收走同一 `get_page` 检测+重建逻辑，建议用户长时间（>10 分钟）闲置后实测确认一次。
3. **A 的连带提醒（已修正，2026-08-13 复核）**：`~/.workbuddy/mcp.json` 的 `command` 现已指向正确入口名 `D:\code\nexus-browser-mcp\.venv\Scripts\nexus-browser.exe`（带 s，正确拼写）。此前曾为 typo 版 `nexu-browser.exe`（少 s），已被更新覆盖。配置不再因 typo 失效。注意事项：若改用 `pip install`（非 editable）部署，`.venv` 内的 `nexus-browser.exe` 同样由入口名生成，拼写需与 `pyproject.toml` 的 `project.scripts` 一致。
4. **登录态未跨 task 共享**：`doubao1`（新 task）初始为未登录态，默认 task 才有登录态——说明未设 `BROWSER_USER_DATA_DIR` 时各 task 独立 context 不共享 cookie。如需跨 task 带登录态，需配置 persistent profile（README 方式 A）。这非 bug，但影响"新建 task 直接聊豆包"的体验。
