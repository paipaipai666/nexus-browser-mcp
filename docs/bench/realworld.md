# 真实站点基准: nexus-browser-mcp vs playwright-mcp

> 测量日期: 2026-08-17 · nexus `main` (含基准期间修复的四个 bug, 见下) · playwright-mcp@latest (24 工具)
> 复现: `.venv/Scripts/python.exe -u bench/realworld.py` (原始数据含逐步响应摘录: `realworld.json`)
> 与 `token-comparison.md` 的关系: 那份是 fixture 单场景协议层对比; 这份是**真实站点 + 真实操作流 + 完成率/耗时**。

## 方法

- 7 个场景 × 5-7 个子任务 = **42 个子任务/侧**: fixture(基线) + 百度搜索 + Bing 搜索 + DuckDuckGo 搜索 + 维基百科阅读/站内搜索 + Hacker News 浏览评论 + GitHub 仓库浏览
- 子任务全是可程序化判定的真实操作: 导航断言 / 快照内容断言 / 定位输入框并回车提交 / 点击指定链接 / `evaluate` 校验 URL 或标题
- 双方默认配置, 同一脚本化客户端; **失败不重试**; ref 按快照文本正则实时定位; 每步记录 成功否/耗时ms/token(cl100k, 请求+响应载荷)
- 环境: 系统代理出口在日本 → 站点以日文/繁体界面响应; 百度对双方均 `ERR_CONNECTION_CLOSED`（环境限制, 非产品差异, 保留作如实记录）

## 结果（watcher 跨站导航修复后复测）

| 场景 | nexus 子任务 | token | 耗时 | pw-mcp 子任务 | token | 耗时 |
|---|---|---|---|---|---|---|
| fixture | **7/7** | 1,647 | 5.7s | **7/7** | 14,287 | 2.0s |
| baidu-search | 2/7 ⚠环境 | 186 | 12.9s | 2/7 ⚠环境 | 751 | 23.6s |
| bing-search | **6/6** | 6,522 | 13.0s | 3/6 | 18,133 | 8.8s |
| duckduckgo-search | **6/6** | 2,289 | 11.1s | **6/6** | 13,593 | 10.7s |
| wikipedia-read | **6/6** | 5,439 | 18.1s | **6/6** | **505,084** | 12.2s |
| hackernews-browse | **5/5** | 3,518 | 8.9s | **5/5** | 13,921 | 9.1s |
| github-repo | **5/5** | 3,664 | 17.2s | **5/5** | 15,855 | 20.5s |
| **合计** | **37/42 (88%)** | **23,265** | **86.9s** | **34/42 (81%)** | **581,624** | **87.0s** |

## 发现

1. **token 总量 25 倍差距**（23.3k vs 581.6k）。剔除维基离群场景后仍 **4.3 倍**（17.8k vs 76.5k）。维基 Python 条目单页快照 pw-mcp 每次 **~25.2 万 token**（场景内两次即 50 万）——内容型长页是全量 YAML 策略的最坏情况; nexus 有 max_chars 上限 + diff。
2. **耗时已追平**（86.9s vs 87.0s）: 基准前三轮复测 nexus 恒 ~127s, 根因是 watcher 在跨站导航后死亡（见下）导致每次快照空转 3s 超时; 修复后 HN 单页快照 3.2s→36ms。按已完成子任务折算: 2.35s vs 2.56s/个。
3. **完成率差距来自确定性**: pw-mcp 的 Bing 场景三次运行复现同一失败——navigate 返回时页面未就绪, 首轮 snapshot 几乎为空（45 tok）, 输入框 ref 定位失败, 连锁失败。nexus 的 navigate 等 load + 快照前等 DOM 静默, 首轮即可操作。
4. **百度双方均失败**（`ERR_CONNECTION_CLOSED`, 本机代理环境）, 如实保留: 基准测的是真实网络栈, 不是温室。

## 基准抓出的真 bug（全部已修, 回归测试已钉）

- **watcher 跨站导航死亡**（影响最大）: `build_watcher_js` 在 document-start 观察 `document.documentElement`（此刻可为 null）→ 抛错被 catch 吞掉, 但安装旗标已立 → 永久死 watcher, 此后每个 `wait_stable` 空转满 3s 超时。修复: 观察目标退化 `?? document` + 旗标移到安装成功后。**真实用户跨站浏览时每个快照都多付 3s**, 无本基准不可见。
- **`browser_evaluate(task_id='')` 幻影 task**: 唯一漏 `_default_task` 归一化的入口 → `ensure_task('')` 建幻影 task 对 about:blank 求值。修复: 包装层归一化 + `ensure_task` 工厂层防御 + 分发层单点归一化（`_guarded_call`）三层根治。
- **衍生的机制升级**: 突变时间线闭环（拍摄前后比对 `__nexusLastMutation`, 已静默页单次拍摄即成立, 替代连拍确认）; reading 模式保真契约（带文本的 listitem/cell 保留）; wait 末帧入 diff 基线（等元素→读页面链路命中 diff）。
- **harness 侧**: pw-mcp 新版 schema `browser_click/type` 必填 `target`(=快照 ref), 旧名 `element` 改可选——前两轮 pw 侧 type/click 失败系 harness 适配错误, 已修正重跑, 未计入对方产品缺陷。

## 边界

- 单次运行（每侧一遍）, 未做多次取均值; 实时站点内容有漂移, 双方背靠背跑把漂移控制在分钟级
- 代理出口地域影响站点语言/内容; 换环境数字会变, 相对格局（diff/上限 vs 全量）不会
- token 按 cl100k 计; 其他分词器绝对值不同、比值相近
