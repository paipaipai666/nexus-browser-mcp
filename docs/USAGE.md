# Browser Agent — 用法铁律

当宿主 agent(Claude Code / opencode / Pi)接入 `nexus-browser-mcp` 后,遵循以下规则以获得稳定结果。

## 核心循环

1. **`browser_navigate(url)`** — 打开页面。返回标题/URL/readyState + 首次快照。
2. **`browser_snapshot()`** — 拿当前可交互元素(含 `ref` 和 `[box=x,y,w,h]`)。基于确定性快照,自动等 DOM 静默。
   - **diff 心智模型**: 页面与上次视图一致时返回约 120 字符的 `[快照无变化]`(不报全文)。此时 **ref/pos 仍有效**——navigate 的回复里就带了首屏快照和 ref,直接操作即可; 想看全文用 `browser_snapshot(diff=false)`。`browser_wait` 结束时的末帧也会刷新基线, 等完元素紧接着 snapshot 通常同样命中 diff。
3. **定位并操作** — `browser_click` / `browser_type`,选以下定位参数(按优先级):
   - `pos='x,y,w,h'`(从 snapshot 的 box 复制,**最可靠**,无名称元素必须用它)
   - `ref='e12'`(snapshot 输出的句柄,导航/重拍后失效需重取)
   - `selector=...`(显式 CSS)
   - `role=... name=...`(语义定位;输入框族不中时自动回退 placeholder/label/常见输入框 CSS,错误消息会附当前可用元素清单及 ref)
4. **`browser_wait` 或 `browser_wait_navigation`** — 点击/提交后等 DOM 更新或跳转。等"停止变化"(流式回复生成完毕)用 **`browser_wait_stable`**;确需固定时长等待用 `browser_wait_ms(ms)`(上限=工具超时-5s)。

## 进阶交互

- **查找**:`browser_find(text)` — 按文本定位内容, 返回命中容器摘要 + 同容器可交互元素 ref (视口外也能查)。找"某行/某卡片/某商品"时用它, 别反复滚动快照。
- **键盘**:`browser_press_key(key)` — 真实键事件(isTrusted=true), Escape 关模态框/Tab 序/组合键(Control+a)。
- **悬停**:`browser_hover(ref/selector/pos)` — 真实鼠标移动, CSS :hover 菜单可用。
- **下拉**:`browser_select_option(values=[...], ref/selector)` — <select> 按 value/label 选。
- **上传**:`browser_upload_file(paths=[...], ref/selector)` — 文件出站, **每次需 confirmed=true**(HITL)。
- **拖拽**:`browser_drag(from_ref/from_selector, to_ref/to_selector)`;**后退**:`browser_navigate_back()`。
- **滚动即视图**:`browser_scroll` 直接回新视口 diff (报"无变化"=到顶/底), 不用再补 snapshot。
- **零反馈提示**: 点击后既无导航也无 DOM 变化时, 回复会附"页面无变化"提示——换定位, 别原地重试。
- **同名消歧**: 快照里同名同角色的元素自动附 `← 前文文本` (如卡片标题/行金额), 用它区分目标。

- **键盘**:`browser_press_key(key)` — 真实键事件(isTrusted=true), Escape 关模态框/Tab 序/组合键(Control+a)。
- **悬停**:`browser_hover(ref/selector/pos)` — 真实鼠标移动, CSS :hover 菜单可用。
- **下拉**:`browser_select_option(values=[...], ref/selector)` — <select> 按 value/label 选。
- **上传**:`browser_upload_file(paths=[...], ref/selector)` — 文件出站, **每次需 confirmed=true**(HITL)。
- **拖拽**:`browser_drag(from_ref/from_selector, to_ref/to_selector)`;**后退**:`browser_navigate_back()`。

## 对话框 (alert/confirm/prompt)

页面弹出对话框时, 该 task 的后续工具回复会前置 `[对话框等待决策]`(页面 JS 被冻结, 须尽快处置):

- **alert**: 纯通知, 自动 dismiss 并留痕 (browser_errors 可见)。
- **confirm/prompt**: **挂起等决策**。`browser_dialog_respond(accept=false)` 直接拒绝; `accept=true` 是替用户点"确定"——**每次需 confirmed=true**(先向用户展示对话框内容征得同意)。prompt 用 `prompt_text` 填文本。
- 超时(默认 20s, `BROWSER_DIALOG_TIMEOUT_MS`)无决策自动 dismiss 并留痕, 防页面永久冻结。

## 接管已打开的标签页 (cdp/持久化模式)

cdp 模式(`BROWSER_MODE=cdp`, 浏览器以 `--remote-debugging-port=9222` 启动)或持久化 profile 模式下, agent 可操作**用户已经打开的标签**:

1. `browser_list_pages` — "外部标签页"区列出未被接管的既有标签 (ext 索引)。
2. `browser_adopt_page(ext_index=...)` — 收进当前 task 并置为当前页; **每次需 confirmed=true**(接管后 agent 可读写该页全部内容含登录态, 先向用户展示清单征得同意)。
3. 之后快照/点击/读取照常; 事件观测(console/网络/下载)自接管时刻起, 之前的history 不可追溯。

isolated 模式(默认)每 task 独立浏览器, 无外部标签可接管。

## 流式内容(AI 回复)

- **一次读全**:`browser_read(selector=..., wait_stable=true, max_wait_ms=...)` — 等 DOM 静默(默认 800ms 无变异)后再读,免轮询。
- **增量跟踪**:`browser_read(selector=..., follow=true)` — 每条 (页面, selector) 一条流,每次只回新增部分;内容被整体替换时保留旧文并打 `[内容被替换]`;缓冲满丢最旧,缝合处标 `[...已丢弃 N 字符...]`。`full=true` 取缓冲全文;页面导航/关闭后流标记失效但缓冲仍可读。

## 状态变更通知

页面/浏览器被外部关闭或崩溃后,下次调用**自动重建**(持久化 profile 登录态不丢)并尽量恢复上次 URL,返回前置 `[状态变更] ...` 说明恢复了什么、DOM 状态(滚动/表单/路由)已重置。看到通知意味着旧 ref/box 全部失效,需重新 `browser_snapshot`。TTL 空闲回收(默认 600s)后首次使用同样自动重建恢复。

## 排障(页面"没反应"时)

页面建立后所有 console 输出、未捕获 JS 异常、网络请求元数据(method/URL/status/失败原因,不含 body)持续记入缓冲:

- **`browser_errors()`** — 一站式: JS 异常 + console.error + 失败请求(网络层失败或 HTTP≥400)合并视图,按时间排序。点了没反应、页面白屏、流程卡住时**先调它**。
- **`browser_console(level=..., pattern=...)`** — 按级别/正则过滤 console;`level="error"` 只看错误。
- **`browser_network(failed_only=false, url_pattern=...)`** — 全部请求元数据;默认 `failed_only=true` 只看失败。

三个工具都是增量游标:省略 `since` 接着上次读,`since=0` 全量;超 `limit` 未显示的再调一次自动继续。导航分界以 `── 导航: URL` 标记;缓冲失效(页面关闭/崩溃)会显式标注。

## [最优] / [不适用]

| 工具 | [最优] | [不适用] |
|---|---|---|
| `browser_snapshot` | 理解页面结构、找可交互元素、决策下一步 | 提取大段文本(用 read) |
| `browser_read` | 读文章/表格文本 | 判断有哪些可交互元素 |
| `browser_errors` | 操作无反应/页面异常时定位根因 | 常规浏览(不要每次操作后都调) |
| `browser_console` / `browser_network` | 按级别/URL 精细排查 | 读页面正文内容 |
| `browser_perf` | 页面慢/加载异常: 看 TTFB(后端慢)还是资源重 | 逐资源 body 排查(用 network_body) |
| `browser_network_body` | 确需看单个响应体(需开启+confirmed) | 大 body/二进制(只报字节数) |
| `browser_dismiss_popup` | 登录弹窗/cookie 同意/广告 | — |
| `browser_evaluate` | (禁用) | — |

## 关键规则

- **导航后 ref 全部失效**,必须重新 `browser_snapshot` 拿新 ref/box。
- **重复快照若与上次逐节点一致**,只回"无变化"通知(省 token),**上次快照的 ref/pos 仍有效**可直接操作;需重看全文用 `browser_snapshot(diff=false)`。
- **无名称的元素**(如纯图标按钮)必须用 `pos` 坐标操作,别猜 role/name。
- SPA/JS 重页面(抖音、B站等)如果 snapshot 内容少,加 `include_generic=true` 或 `mode='full'`,这些页面内容在非语义 div 里。
- 点击后页面若有跳转,用 `browser_wait_navigation(url_contains=...)`;若只是局部更新,用 `browser_wait(text=...)`。
- 默认所有操作跑在同一 `task_id`。多任务并行或要隔离登录态时,显式传不同 `task_id`(`browser_tasks` 查看、`browser_close_task` 清理)。
- 命中 HITL 规则(如点击"支付|确认")会返回 `CONFIRMATION_REQUIRED`——此时必须向用户说明并询问;**用户同意后用 `confirmed=true` 重调同一操作**,不可自行换定位参数绕过。
- 只读抓取已知 URL 内容优先用 web_fetch(更快更省);浏览器留给需要交互的场景。

## 生命周期

任务结束或要清空时:`browser_tasks` 看有哪些,`browser_close_task(task_id=...)` 逐个关,或 `browser_close_session` 一把全关(Cookie/登录态也随之释放)。