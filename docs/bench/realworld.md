# 真实站点基准: nexus-browser-mcp vs playwright-mcp

> 测量日期: 2026-08-17 · nexus `main` (含 bench 期间修复的两个 bug, 见下) · playwright-mcp@latest (24 工具)
> 复现: `.venv/Scripts/python.exe -u bench/realworld.py` (原始数据含逐步响应摘录: `realworld.json`)
> 与 `token-comparison.md` 的关系: 那份是 fixture 单场景协议层对比; 这份是**真实站点 + 真实操作流 + 完成率/耗时**。

## 方法

- 7 个场景 × 5-7 个子任务 = **42 个子任务/侧**: fixture(基线) + 百度搜索 + Bing 搜索 + DuckDuckGo 搜索 + 维基百科阅读/站内搜索 + Hacker News 浏览评论 + GitHub 仓库浏览
- 子任务全是可程序化判定的真实操作: 导航断言 / 快照内容断言 / 定位输入框并回车提交 / 点击指定链接 / `evaluate` 校验 URL 或标题
- 双方默认配置, 同一脚本化客户端; **失败不重试**; ref 按快照文本正则实时定位; 每步记录 成功否/耗时ms/token(cl100k, 请求+响应载荷)
- 环境: 系统代理出口在日本 → 站点以日文/繁体界面响应; 百度对双方均 `ERR_CONNECTION_CLOSED`（环境限制, 非产品差异, 保留作如实记录）

## 结果

| 场景 | nexus 子任务 | token | 耗时 | pw-mcp 子任务 | token | 耗时 |
|---|---|---|---|---|---|---|
| fixture | **7/7** | 1,641 | 6.0s | **7/7** | 14,287 | 2.1s |
| baidu-search | 2/7 ⚠环境 | 183 | 18.4s | 2/7 ⚠环境 | 751 | 23.6s |
| bing-search | **6/6** | 10,984 | 18.4s | 3/6 | 18,252 | 8.9s |
| duckduckgo-search | **6/6** | 2,286 | 19.5s | **6/6** | 13,593 | 11.3s |
| wikipedia-read | **6/6** | 10,341 | 27.5s | **6/6** | **504,190** | 11.5s |
| hackernews-browse | **5/5** | 3,513 | 14.8s | **5/5** | 13,943 | 9.6s |
| github-repo | **5/5** | 3,661 | 21.8s | **5/5** | 15,855 | 20.3s |
| **合计** | **37/42 (88%)** | **32,609** | **126.5s** | **34/42 (81%)** | **580,871** | **87.2s** |

## 发现

1. **token 总量 17.8 倍差距**（32.6k vs 580.9k）。剔除维基这一个离群场景后仍 3.4 倍（22.3k vs 76.7k）。维基 Python 条目单页快照 pw-mcp 每次 **~25.2 万 token**，场景内两次快照即 50 万——内容型长页是全量 YAML 策略的最坏情况，而 nexus 有 max_chars 上限 + diff。
2. **完成率差距来自确定性**: pw-mcp 的 Bing 场景两次运行都复现同一失败模式——navigate 返回时页面未就绪，紧接着的 snapshot 几乎为空（45~1,055 tok），输入框 ref 定位失败，后续连锁失败。nexus 的 navigate 等待 load + 快照前等 DOM 静默，首轮即可操作。这正是"事件驱动确定性快照"的卖点，被基准实测坐实。
3. **耗时 nexus 更高**（126.5s vs 87.2s）: 等 DOM 静默 + navigate 附首屏快照的时间税。换算到子任务: 3.4s vs 2.6s/个。确定性换取可行动的首屏——Bing 场景 pw 省了时间但任务没完成。
4. **百度场景双方均失败**（`ERR_CONNECTION_CLOSED`, 本机代理环境），如实保留: 它说明基准测的是真实网络栈，不是温室。

## 基准抓出的真 bug（已修，回归测试已钉）

- **`browser_evaluate(task_id='')` 幻影 task**: 唯一漏掉 `_default_task` 归一化的入口——门禁放行后 `ensure_task('')` 建出第二个空 task，对着 about:blank 求值返回 `结果: 0`/`''`。修复: 包装层归一化 + `ensure_task` 工厂层防御 + 测试断言"不得创建幻影 task"(`test_evaluate_default_task_empty_id`)。无本基准的细颗粒 eval 校验子任务，此 bug 不可见。
- **基准侧 harness**: pw-mcp 新版 schema `browser_click/type` 必填 `target`(=快照 ref)，旧名 `element` 已改为可选描述——前两轮 pw 侧的 type/click 失败系 harness 适配错误，已修正重跑，未计入对方产品缺陷。

## 边界

- 单次运行（每侧一遍），未做多次取均值; 实时站点内容有漂移（Bing 趋势、HN 榜），双方背靠背跑把漂移控制在分钟级
- 代理出口地域影响站点语言/内容; 换环境数字会变，相对格局（diff 上限 vs 全量）不会
- token 按 cl100k 计; 其他分词器绝对值不同、比值相近
