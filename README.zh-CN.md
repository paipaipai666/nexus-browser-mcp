[English](README.md) | 简体中文

# nexus-browser-mcp

**事件驱动确定性快照的浏览器操控 MCP 服务器。**

基于 Playwright,通过 Accessibility Tree(无障碍树)让 LLM 驱动浏览器——导航、点击、输入、读取、表单、多标签。与市面同类产品(Playwright MCP 等)的核心差异:

1. **确定性快照**:不靠固定间隔 `sleep` 硬等,而是注入 `MutationObserver` 记录最后一次 DOM 变异,由浏览器自身的 `requestAnimationFrame` 循环判定"页面已静默 `STABLE_WINDOW_MS`(默认 800ms)"后才提取快照。杜绝"快照抓在动画/加载中"的竞态。
2. **内嵌治理门**:HITL 规则(如点击"支付/确认"需人工)、`browser_evaluate` 默认禁用+无条件确认、JSONL 审计(含敏感参数脱敏)。
3. **多 task 隔离**:一个 MCP 连接(session)内可建多个独立 `task_id`,各自独立 BrowserContext(登录态互不污染),TTL 空闲回收、回收后再次使用时自动重建并恢复上次页面。
4. **死亡可观测 + 自愈**:标签页/浏览器被外部关闭或崩溃后,下次调用自动重建(持久化 profile 登录态不丢),并在工具返回前置 `[状态变更]` 通知,明确告知"恢复了什么、丢了什么",不再泄漏 Playwright 底层异常。
5. **开发者可观测性**:每个页面把 console 消息、未捕获 JS 异常、网络请求元数据(method/URL/status/失败原因,**绝不记 body**)记入带容量上限的环形缓冲;`browser_console` / `browser_errors` / `browser_network` 以 `since` 游标增量读取——agent 能回答"点了为什么没反应",而不是只能靠猜。

## 安装

```bash
pip install nexus-browser-mcp
# 或
uvx nexus-browser-mcp
```

安装后提供 `nexus-browser-mcp` / `nexus-browser` 两个可执行入口;兜底启动方式(一定可用):`python -m nexus_browser.server`。

依赖 `playwright` 及其浏览器内核:

```bash
pip install playwright && playwright install chromium
```

## 接入(任意 MCP 客户端)

**opencode**(`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "browser": {
      "type": "local",
      "command": ["uvx", "nexus-browser-mcp"],
      "enabled": true
    }
  }
}
```

**Claude Code**(`.mcp.json`,项目根):

```json
{
  "mcpServers": {
    "browser": {
      "type": "stdio",
      "command": "uvx",
      "args": ["nexus-browser-mcp"]
    }
  }
}
```

**Pi Coding Agent**:读取标准 MCP 配置 —— 项目 `.mcp.json` 或用户全局 `~/.config/mcp/mcp.json`,stdio 默认 transport:

```json
{
  "mcpServers": {
    "browser": {
      "command": "uvx",
      "args": ["nexus-browser-mcp"]
    }
  }
}
```

详细见 `docs/INTEGRATE.md`。

## 使用你自己的浏览器(带登录态)

默认 `isolated` 模式启动 Playwright 内置 Chromium,**不带你的 cookie/登录态**。要用你自己的浏览器,二选一:

**方式 A — 直接加载你的浏览器 profile(推荐,最省事)**

用系统 Chrome 加载你平时的用户数据目录(Cookie/登录态/书签都在):

```
BROWSER_CHANNEL=chrome
BROWSER_USER_DATA_DIR="C:\Users\你的用户名\AppData\Local\Google\Chrome\User Data"
```

> 注意:用自己的 User Data 时,进程会占用浏览器,期间你自己开 Chrome 会冲突。建议复制一份 profile 或用独立的 `--user-data-dir` 指向一个专用目录。

**推荐做法:工具专用 profile(不与日常浏览器冲突)**

用 `BROWSER_CHANNEL=chrome` + 指向一个专用 user data 目录(如 `C:\Users\<你>\.nexus-browser\chrome-profile`):

```
BROWSER_CHANNEL=chrome
BROWSER_USER_DATA_DIR="C:\Users\你的用户名\.nexus-browser\chrome-profile"
```

首次使用需要在 agent 调浏览器工具时弹出的专用 Chrome 里**登录一次**目标网站,之后 cookie 永久保存在该 profile,agent 从此自带登录态;且与你日常浏览器完全隔离,互不干扰。

**方式 B — CDP 连接运行中的 Chrome**

先启动: `chrome --remote-debugging-port=9222`,然后 `BROWSER_MODE=cdp`。

> 若 CDP 连接失败,服务器现在会**明确报错**(不再静默启动全新浏览器),提示你先启动调试端口浏览器。

CDP(或持久化 profile)模式下 agent 还能**接管你已经打开的标签页**: `browser_list_pages` 会把它们列在"外部标签页"区, `browser_adopt_page(ext_index)` 将该标签收进当前 task(之后快照/点击/读取全可用)。接管一律要求 `confirmed=true`——它把该页面的全部读写权(含登录态)交给 agent。

## 配置(环境变量)

所有可选项通过 `BROWSER_` 前缀环境变量覆盖:

| 变量 | 默认 | 说明 |
|---|---|---|
| `BROWSER_MODE` | `isolated` | `isolated`(隔离新浏览器) / `cdp`(连你的 Chrome) |
| `BROWSER_CDP_ENDPOINT` | `http://localhost:9222` | CDP 地址 |
| `BROWSER_CHANNEL` | `""` | 系统浏览器通道: `chrome`/`msedge` 等(空=Playwright 内置 Chromium) |
| `BROWSER_USER_DATA_DIR` | `""` | 用户数据目录(带登录态)。设置后为共享登录态的 persistent context, 多 task 共享。空=全新 profile |
| `BROWSER_HEADLESS` | `false` | 无头模式(仅 isolated) |
| `BROWSER_DEFAULT_TIMEOUT_MS` | `30000` | Playwright 单次操作超时(导航等) |
| `BROWSER_TOOL_TIMEOUT_MS` | `60000` | 单次工具调用的外层超时护栏(超时返回 ERROR, 不挂死) |
| `BROWSER_STABLE_WINDOW_MS` | `800` | DOM 静默窗口: 无变异持续多久判"稳定" |
| `BROWSER_STABLE_REQUIRED` | `2` | 稳定后连拍确认快照数(防纯动画/非 DOM 变化) |
| `BROWSER_STABLE_TIMEOUT_MS` | `3000` | 稳定性等待总超时, 超时优雅降级 |
| `BROWSER_SNAPSHOT_MAX_NODES` | `100` | 快照最大节点数 |
| `BROWSER_CONTEXT_TTL_SEC` | `600` | 空闲 task 自动回收(秒) |
| `BROWSER_STREAM_CHAR_CAP` | `16000` | 单条流式缓冲最大字符数(溢出丢最旧,保留丢弃标记) |
| `BROWSER_STREAM_PAGE_CAP` | `64000` | 单页面全部流的总字符上限 |
| `BROWSER_EVENT_MAX_ENTRIES` | `500` | 单页事件(console/异常/请求)条数上限, 溢出丢最旧并计数 |
| `BROWSER_EVENT_TEXT_CAP` | `500` | 单条事件文本截断长度 |
| `BROWSER_EVENT_HANDLE_MAX` | `50` | 单页保留响应句柄的最近请求条数(供按需取 body) |
| `BROWSER_ALLOW_NETWORK_BODY` | `false` | 是否允许 `browser_network_body`(响应体含敏感数据, 默认关) |
| `BROWSER_NETWORK_BODY_CAP` | `4000` | 单条响应体返回字符上限 |
| `BROWSER_TRANSPORT` | `stdio` | 传输方式: `stdio` / `http`(streamable-http, 远程/多客户端) |
| `BROWSER_HTTP_HOST` | `127.0.0.1` | HTTP 绑定地址; 非 localhost 必须设 `BROWSER_HTTP_TOKEN`(否则拒绝启动) |
| `BROWSER_HTTP_PORT` | `8817` | HTTP 端口 |
| `BROWSER_HTTP_TOKEN` | `""` | HTTP 传输 Bearer token |
| `BROWSER_ALLOW_JS_EXECUTION` | `false` | 是否允许 `browser_evaluate`(开启后无条件 HITL) |
| `BROWSER_HITL_RULES` | `[]` | JSON 数组: HITL 规则, 如 `[{"action":"click","name_pattern":"支付|确认"}]` |
| `BROWSER_AUDIT_PATH` | `~/.nexus-browser/audit.jsonl` | 审计日志路径 |
| `BROWSER_DIALOG_TIMEOUT_MS` | `20000` | 挂起的 confirm/prompt 无决策自动 dismiss 超时(事件流留痕) |
| `BROWSER_DOWNLOAD_DIR` | `~/.nexus-browser/downloads` | 下载落盘目录(点击即回报文件名+路径) |
| `BROWSER_PROXY` | `""` | `none`=以 `--no-proxy-server` 启动浏览器(绕过系统代理) |

## 工具

33 个工具:`browser_navigate`、`browser_navigate_back`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_hover`、`browser_press_key`、`browser_select_option`、`browser_upload_file`(HITL 确认)、`browser_drag`、`browser_dialog_respond`(confirm/prompt 挂起待 agent/用户决策, accept 须 `confirmed=true`)、`browser_adopt_page`(接管浏览器已开标签, cdp/持久化模式, HITL 确认)、`browser_read`、`browser_screenshot`、`browser_evaluate`、`browser_wait`、`browser_wait_stable`、`browser_wait_ms`、`browser_scroll`、`browser_scroll_to`、`browser_wait_navigation`、`browser_dismiss_popup`、`browser_list_pages`、`browser_switch_page`,观测工具 `browser_console`、`browser_errors`、`browser_network`、`browser_perf`、`browser_network_body`,及 4 个生命周期工具 `browser_tasks`、`browser_close_task`、`browser_list_sessions`、`browser_close_session`。

观测(排障):页面建立后所有 console 输出、未捕获异常、请求元数据持续入缓冲;`browser_errors()` 一次调用给出"JS 异常 + console.error + 失败请求"合并视图,三个工具均支持 `since` 增量游标(省略=接着上次读,`0`=全量)与 `limit` 分页。

性能:`browser_perf()` 返回 FCP/LCP/CLS/INP、导航计时与最慢 5 条资源。响应体可用 `browser_network_body(seq)` 按需单取——默认关闭(`BROWSER_ALLOW_NETWORK_BODY`)、逐次 `confirmed=true` 确认、硬字符上限、body 绝不进审计日志。

HITL 确认闭环:任何被拦截的调用先回 `CONFIRMATION_REQUIRED`,用户在对话中同意后 agent 以 `confirmed=true` 重调(HITL 规则、`browser_evaluate`、`browser_network_body` 通用)。

## Token 成本:实测,非宣称

同一 10 步任务、双方默认配置、在 JSON-RPC 载荷层计量(cl100k;方法与原始数据见 [docs/bench/token-comparison.md](docs/bench/token-comparison.md),`bench/compare.py` 可复现):

| | nexus-browser-mcp | playwright-mcp |
|---|---:|---:|
| 10 步任务总计 | **3,460 tok** | 26,032 tok |
| 导航后首次快照 | **65 tok** | 6,931 tok |

**总量 7.5x;重复快照 100x**——后者是 agent 循环的主导成本(轮询、多步表单、状态确认)。

真实站点基准(7 场景 × 5-7 可验证子任务:百度/Bing/DDG 搜索、维基阅读、HackerNews、GitHub——[docs/bench/realworld.md](docs/bench/realworld.md)):**子任务完成 37/42 vs 31/42**,**18.2k vs 566.4k tok(31x)**,耗时 **107s vs 179s**。企业任务套件(过滤/排序、仪表盘读数、知识库答题、多步下单、跨店比价——[docs/bench/enterprise-ops.md](docs/bench/enterprise-ops.md)):**三家服务端全部 21/21(对 playwright-mcp 与 chrome-devtools-mcp);token 3.6k vs 5.1k vs 10.9k**。规模化套件(**106 案例 / 184 子任务、三方、种子化确定性夹具**——[docs/bench/scale-ops.md](docs/bench/scale-ops.md)):**完成率 184/184 vs 180/184 vs 178/184;token 153.5k vs 180.5k vs 231.1k(1.00 : 1.18 : 1.51)**——竞品缺口是稳定的零分(富文本 iframe 写入、下载观测、右键),不是噪声。

## HTTP 传输(远程/多客户端)

默认 stdio(单客户端)。远程或多客户端场景可起 streamable-http 服务:

```bash
BROWSER_TRANSPORT=http BROWSER_HTTP_PORT=8817 nexus-browser-mcp
```

每个 MCP session 获得独立 `session_id`(task 级隔离照旧)。安全硬规则:绑定非 localhost 且未设 `BROWSER_HTTP_TOKEN` 时**拒绝启动**——裸奔的浏览器控制端口等于把本机浏览器交给网络;设 token 后请求须带 `Authorization: Bearer <token>`。

流式内容(AI 回复等):`browser_read(wait_stable=true)` 等 DOM 静默后一次读全;`browser_read(selector=..., follow=true)` 增量跟踪,每次只回新增部分,`full=true` 取缓冲全文。`browser_wait_stable` / `browser_wait_ms` 提供事件驱动等待与纯等待两种原语。

快照 diff:重复 `browser_snapshot` 若与上次逐节点一致(ref 除外——Playwright 按代际重编号),只回约 120 字符的"无变化"通知而非全量树,旧 ref 经代际链式映射仍然有效;`diff=false` 强制全量。任何真实变化(内容/位置/属性)都返回全量——不做部分合并,不给过期视图。

多数工具接受可选 `task_id`(不传则用默认 task)。见 `docs/` 中的用法指南。

## 开发

```bash
uv venv
uv pip install -e ".[dev]"
python -m pytest tests -q
ruff check src tests
python -m smokes.test_e2e           # 真实浏览器冒烟
python -m smokes.test_e2e_interact  # 表单 + 多 task 冒烟
python -m smokes.test_e2e_observability  # console/异常/网络观测冒烟
```

## License

MIT
