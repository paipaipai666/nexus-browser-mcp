# 日常操作双侧基准（反"满分假象"专项）

> 日期: 2026-08-18 · 动机: 9 案例的能力点检不等于日常覆盖。这份覆盖更宽的真实操作面, 双侧同测。
> 复现: `.venv/Scripts/python.exe -u bench/daily_ops.py`（原始数据: `daily-ops.json`）
> **国内网络友好版**: 标准动作页为本地同构夹具（`bench/fixture_daily/`, 结构对齐 the-internet）,
> 下载文件走 npmmirror 国内 CDN, 仅 datepicker/autocomplete 用 jqueryui（直连可达）——全套不依赖代理。
> the-internet 版本曾成功跑出首轮（对方按键/复选框抖动即发现于彼），但出口间歇超时不可作常规依赖。

## 结果矩阵（本地化版终跑）

| 案例 | nexus | pw-mcp | token (nx/pw) |
|---|---|---|---|
| 登录认证流 | 3/3 | 3/3 | **275**/598 |
| 动态加载等待 | 1/1 | 1/1 | **171**/393 |
| 无限滚动 | 2/2 | 2/2 | **186**/297 |
| 右键菜单 | **2/2** | 0/1 | 287/278 |
| 混沌 DOM 定位 | 2/2 | 2/2 | **176**/372 |
| 大 DOM 表格 | 2/2 | 2/2 | **5,910**/8,514 |
| 按键验证 | 2/2 | 2/2 | **143**/274 |
| 复选框 | 2/2 | 2/2 | **212**/457 |
| 日期选择器 | 1/1 | 1/1 | **178**/397 |
| 自动补全 | 1/1 | 1/1 | **190**/317 |
| 文件下载 | **1/1** | 1/2 | **365**/394 |
| 重定向链 | 1/1 | 1/1 | **139**/254 |
| **合计** | **20/20** | **18/20** | **8,232 / 12,545** |

## 两个缺口清零（本轮落地）

1. **下载可观测**：`page.on("download")` 入 EventStore + 自动落盘（`~/.nexus-browser/downloads`，`BROWSER_DOWNLOAD_DIR` 可配）+ 触发它的 click 直接回报"文件名 → 路径"。pw 侧无等价物（0.5 分记缺口）。
2. **右键**：`browser_click(button="right|middle")` 落地。验证链完整：右键 → contextmenu → alert → 我方对话框治理自动 dismiss + `browser_errors` 留痕可查。pw 侧 0/1 是其 `handle_dialog` 的 modal 态时序问题（alert 先于调用被处理过/已消失）——单轮现象, 记其 UX 弱点不做定论。

## 本轮抓出并已修复的问题

1. **`accept_downloads` 误传裸 `launch()`**（我自己引入的回归）：context 级参数传进 BrowserType.launch 直接炸掉整个浏览器启动链——**基准 0/20 全军覆没的形态暴露的**, 单元测试全绿完全没发现（mock 层不触发真 launch）。教训: 启动参数变更必须配一次真实启动冒烟。
2. **`browser_type` 富文本静默无效**（上一轮已修）：fill 读回校验 + contenteditable 容器升级。
3. **reading 保真第二案**：`cell`/`heading` 的内容在 a11y **name 位**而非冒号 text 位——保真规则只看 text 导致大表格/悬停标题被误删。已改为 `text or name`, 测试钉住双形态。
4. 夹具自身两个 bug（初始高度不足无法滚动、登出链接未 preventDefault）——夹具也要过 scrutiny。

## 稳定性注记

- 本地化后全套双跑 ~150s, 无网络抖动项; jqueryui 两案例依赖直连（当前稳定）。
- pw 右键案例单轮 0/1（modal 时序）, 与其能力存在性不矛盾, 如实标注。

## 仪式化建议

`daily_ops`（能力日常面）+ `realworld`（真实站点）+ `adversarial`（能力边界）三套并列, 发版前手动跑; 全部国内网络友好。
