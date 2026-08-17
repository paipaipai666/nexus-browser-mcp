# 新工具真实场景实测（第一波+第二波 7 工具的回归猎杀）

> 日期: 2026-08-17 · 范围: press_key / hover / select_option / upload_file / navigate_back / drag / dialog_respond
> 与 adversarial.md 的区别: 那份用 hermetic fixture 证"能力存在"; 这份用真实站点/标准测试场证"站得住"。
> 复现: `.venv/Scripts/python.exe -u bench/newtools_real.py`（原始数据: `newtools-real.json`）

## 结果：28/28 子任务全绿

| 案例 | 站点 | 子任务 |
|---|---|---|
| select 真实下拉 | the-internet.herokuapp.com/dropdown | 3/3（label 选、value 回切、值校验） |
| 真实文件上传 | the-internet /upload | 3/3（**未 confirmed 必拒**、HITL 后上传、服务端回显文件名） |
| hover 真实悬停 | the-internet /hovers | 2/2（computed 样式验证 + 悬停区链接入快照） |
| HTML5 拖拽 | the-internet /drag_and_drop | 1/1（A↔B 交换） |
| pointer 系拖拽 | jqueryui.com sortable | 1/1（Item1→位3，**分段拖拽修复后**） |
| 对话框三部曲 | the-internet /javascript_alerts | 7/7（alert 自动 dismiss+留痕、挂起通知前置、accept 未 confirmed 必拒、accept/dismiss/prompt 填文本全流程） |
| GitHub 键盘快捷键 | github.com 仓库页 | 2/2（`/` 聚焦搜索、Escape 退出） |
| 维基 hovercard | zh.wikipedia.org | 2/2（坐标精确定位 + 弹出内容验证） |
| 搜索→后退组合流 | bing.com + zh.wikipedia.org | 7/7（搜索跳转、**结果开新标签→枚举→切回**、同标签进条目→navigate_back 回退） |

## 实测抓出并处置的问题

1. **`drag_to` 对 pointer 系库静默无效**（真 bug，已修复）：sortable 返回成功但顺序不变 → 分段拖拽路径（hover→down→10 步 move→偏下部落点→up）。**对手对称验证成立**：pw-mcp `browser_drag`（同一 drag_to 原语）同样静默失败——我们的分段拖拽是实测反超点。
2. **系统代理陈旧 = 全站超时无诊断**（环境级发现，已修复）：新增 `BROWSER_PROXY`（`none` 绕过 / 显式 server）。
3. **DDG 首页已 AI 聊天优先**（2026 产品现实，非双方缺陷）：Enter/搜索按钮不再进网页结果页。组合流改用 Bing + 维基。
4. **Bing 结果 `target=_blank`**：点击进新标签是正确行为（Issue N 机制工作正常）——"后退"语义属于同标签导航；新标签流用 `list_pages`/`switch_page`。基准已拆为两条流分别验证。
5. **次要保真边界** [INFERENCE]：the-internet 悬停 caption 的 h5 小字未进 reading 快照（可操作链接正常入快照）——覆盖层小字体的边缘情况，agent 实操不受影响。

## 公平性记录

- 失败重试只针对环境抖动（代理出口对 HN 的间歇性超时），产品失败零重试
- pw-mcp 对称验证只在其声称的工具上做（browser_drag / sortable）
