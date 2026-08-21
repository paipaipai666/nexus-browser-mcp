# E2E LLM 基准: 真模型工具循环驱动三方 MCP

> 此前八套基准全是脚本 resolver —— 脚本路径是替 agent 预判的, 不是真实使用。
> 本层补上业界标配 (OpenBrowser MCP benchmark / Online-Mind2Web 方法论):
> **真 LLM 自主开工具循环**, 同任务提示逐字喂三方, 量成功率/步数/模型 API token/时长。
> 复现: `set SILICONFLOW_API_KEY=... && .venv/Scripts/python.exe -u bench/e2e_llm.py`
> (原始数据 `e2e-llm.json`; 模型 deepseek-ai/DeepSeek-V4-Flash, temperature=0)

## 方法

- agent = SiliconFlow 免费档模型; MCP 工具自动桥接为 OpenAI tools; 步数上限 20
- 12 任务按参考步数分层: easy ≤5 / medium 6-10 / hard ≥11 (Online-Mind2Web 惯例)
- 全部硬验证器 (DOM 状态/闭式答案字符串匹配), 不用 LLM 裁判; 反捷径 (必须操作目标站点)
- 指标: 成功率 / 步数 / **模型 API token (usage input+output, 含每轮重放的工具模式与对话史)**
  / MCP 响应字符 / 时长

## 首跑 (2026-08-21, 单轮) + 修正重跑

| task | tier | nexus | pw | cdt |
|---|---|---|---|---|
| filter-first-row | easy | ✓ 4步 | ✓ 4步 | ✓ 4步 |
| buy-cheapest | easy | ✓ 15步 | ✓ 8步 | ✓ 7步 |
| dash-read | easy | ✓ 3步 | ✓ 2步 | ✓ 3步 |
| grid-sort-buy | easy | ✓ 5步 | ✓ 6步 | ✓ 12步 |
| max-amt-of-name | medium | ✓ 4步 | ✓ 4步 | ✓ 4步 |
| wizard-flow | medium | ✓ 20步 | ✓ 21步 | ✓ 20步 |
| row-action | medium | 首轮✗/重跑✓(卡20步) | 首轮✓/重跑✗ | ✓ (两轮均卡20步) |
| kb-owner | medium | ✓ 4步 | ✓ 2步 | ✓ 2步 |
| last-page-order | hard | ✓ 7步 | ✓ 4步 | ✓ 4步 |
| price-compare | hard | ✓ 1步 | ✓ 2步 | ✓ 2步 |
| modal-flow | hard | ✓ 20步 | ✓ 20步 | ✓ 18步 |
| multi-filter-read | hard | ✓ 5步 | ✓ 4步 | ✓ 5步 |
| **成功率 (两轮合并)** | | **13/13 任务过过至少一轮** | 同左 | 同左 |

API token 总量 (首轮全量): **nexus 1,098k / pw 894k / cdt 1,157k**。

## 发现 (比成功率更重要的)

1. **E2E 层 token 排序与协议层倒挂**: MCP 响应字符上我们远省, 但模型 API token 上
   首轮我们 1.10M > pw 0.89M。原因构成: (a) pw 快照"一眼看全"在简单任务上省往返
   (dash-read pw 2 步 15.4k vs 我们 3 步 24.1k); (b) 我们 navigate 附视图 + CONFIRMATION
   重调各加一轮 prompt 重放。每轮调用都把工具模式+对话史重新计费, 步数多的方案天然吃亏。
   **省 token 的正解不只是压单次响应, 还要让步数收敛更快。**
2. **row-action 两轮换边失败** (nexus 首轮✗→重跑✓; pw 首轮✓→重跑✗; cdt 两轮均卡 20 步线):
   20 个同名"删除"按钮的消歧任务对当前模型是能力边界, 单轮结果 = 噪声。
   **任务级结论必须 N≥3 轮 + CI, 单次 SR 不可做宣发材料。**
3. **基准设施自检逮住两个 bug**: (a) 验证器没剥 nexus repr 引号 (假阴性 buy-cheapest);
   (b) seed=9 的随机数据里根本没有任务要求的客户名 (不可解任务)。都以 JSON 取证定位修复。
4. **cap 边缘任务主导成本**: 撞 20 步上限的任务单任务 180-290k API token, 是顺过任务的 5-10 倍。
   "撞 cap"本身应作为独立指标报告 (本套件 12 任务中 nexus 2 次 / pw 3 次 / cdt 3 次)。

## 下一步

- 多轮运行 (N=3) + bootstrap CI; 裁判模型层 (WebJudge 式关键点评估) 覆盖开放性任务
- Docker 自托管真实开源应用 (Gitea/WordPress 级复杂度) 替代手写夹具做 v3 任务源
- 模型升级对照组 (V4-Flash vs 更大模型) 分离"模型能力"与"MCP 设计"变量
