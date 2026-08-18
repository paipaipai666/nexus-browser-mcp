# 真实站点基准: nexus-browser-mcp vs playwright-mcp

> 测量日期: 2026-08-17 · nexus `main` (含基准期间修复的四个 bug, 见下) · playwright-mcp@latest (24 工具)
> 复现: `.venv/Scripts/python.exe -u bench/realworld.py` (原始数据含逐步响应摘录: `realworld.json`)
> 与 `token-comparison.md` 的关系: 那份是 fixture 单场景协议层对比; 这份是**真实站点 + 真实操作流 + 完成率/耗时**。

## 方法

- 7 个场景 × 5-7 个子任务 = **42 个子任务/侧**: fixture(基线) + 百度搜索 + Bing 搜索 + DuckDuckGo 搜索 + 维基百科阅读/站内搜索 + Hacker News 浏览评论 + GitHub 仓库浏览
- 子任务全是可程序化判定的真实操作: 导航断言 / 快照内容断言 / 定位输入框并回车提交 / 点击指定链接 / `evaluate` 校验 URL 或标题
- 双方默认配置, 同一脚本化客户端; **失败不重试**; ref 按快照文本正则实时定位; 每步记录 成功否/耗时ms/token(cl100k, 请求+响应载荷)
- 环境: 系统代理出口在日本 → 站点以日文/繁体界面响应; 百度对双方均 `ERR_CONNECTION_CLOSED`（环境限制, 非产品差异, 保留作如实记录）

## 结果（watcher 修复 + nav 成本修复后复测）

| 场景 | nexus 子任务 | token | 耗时 | pw-mcp 子任务 | token | 耗时 |
|---|---|---|---|---|---|---|
| fixture | **7/7** | 1,575 | 6.0s | **7/7** | 14,287 | 2.1s |
| baidu-search | 2/7 ⚠环境 | 181 | 32.9s | 1/7 ⚠环境 | 442 | 62.6s |
| bing-search | **6/6** | 2,756 | 10.8s | 3/6 | 6,172 | 6.6s |
| duckduckgo-search | **6/6** | 2,076 | 10.4s | **6/6** | 13,744 | 13.2s |
| wikipedia-read | **6/6** | 4,934 | 24.5s | 4/6 | **504,528** | 64.3s |
| hackernews-browse | **5/5** | 3,486 | 4.6s | **5/5** | 13,959 | 5.1s |
| github-repo | **5/5** | 3,205 | 18.3s | **5/5** | 13,278 | 24.8s |
| **合计** | **37/42 (88%)** | **18,213** | **107.5s** | **31/42 (74%)** | **566,410** | **178.8s** |

## 发现

1. **token 总量 31 倍差距**（18.2k vs 566.4k）。剔除维基离群场景后仍 **4.6 倍**（13.3k vs 61.9k）。维基 Python 条目单页快照 pw-mcp 每次 **~25.2 万 token**——内容型长页是全量 YAML 策略的最坏情况; nexus 有 max_chars 上限 + diff。
2. **耗时与完成率双优**: 107.5s vs 178.8s, 37/42 vs 31/42。watcher 修复消除了我方每次快照的 3s 空转; nav 成本修复再省 22% token。pw 侧维基本轮单快照耗 64s（25 万 token 的提取与传输）, Bing/维基场景反复复现"navigate 返回时页面未就绪 → 首轮快照近空 → 连锁失败"。
3. **百度双方均失败**（`ERR_CONNECTION_CLOSED`, 本机代理环境）, 如实保留: 基准测的是真实网络栈, 不是温室。

## 基准抓出的真 bug（全部已修, 回归测试已钉）

- **watcher 跨站导航死亡**（影响最大）: `build_watcher_js` 在 document-start 观察 `document.documentElement`（此刻可为 null）→ 抛错被 catch 吞掉, 但安装旗标已立 → 永久死 watcher, 此后每个 `wait_stable` 空转满 3s 超时。修复: 观察目标退化 `?? document` + 旗标移到安装成功后。**真实用户跨站浏览时每个快照都多付 3s**, 无本基准不可见。
- **`browser_evaluate(task_id='')` 幻影 task**: 唯一漏 `_default_task` 归一化的入口 → `ensure_task('')` 建幻影 task 对 about:blank 求值。修复: 包装层归一化 + `ensure_task` 工厂层防御 + 分发层单点归一化（`_guarded_call`）三层根治。
- **衍生的机制升级**: 突变时间线闭环（拍摄前后比对 `__nexusLastMutation`, 已静默页单次拍摄即成立, 替代连拍确认）; reading 模式保真契约（带文本的 listitem/cell 保留）; wait 末帧入 diff 基线（等元素→读页面链路命中 diff）。
- **harness 侧**: pw-mcp 新版 schema `browser_click/type` 必填 `target`(=快照 ref), 旧名 `element` 改可选——前两轮 pw 侧 type/click 失败系 harness 适配错误, 已修正重跑, 未计入对方产品缺陷。

## 边界

- 单次运行（每侧一遍）, 未做多次取均值; 实时站点内容有漂移, 双方背靠背跑把漂移控制在分钟级
- 代理出口地域影响站点语言/内容; 换环境数字会变, 相对格局（diff/上限 vs 全量）不会
- token 按 cl100k 计; 其他分词器绝对值不同、比值相近
