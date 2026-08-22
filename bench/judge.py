"""LLM-as-Judge (WebJudge-lite): 裁判模型消费轨迹判定开放性成败 + 与硬验证器校准一致率。

WebJudge 论文做法: 关键点拆解 → 关键帧筛选 → 综合裁定。我们的适配:
轨迹是 DOM/工具事件而非截图 (等效观测), 判定输入 = 任务 + 关键点 + 轨迹摘要 + 终答。
校准模式 (默认): 在已有硬验证结论的轨迹上跑裁判, 报一致率/混淆矩阵;
一致率 < ~80% 则裁判不可用于无硬验证器的任务 (业界门槛, Online-Mind2Web ~85%)。

用法:
  set SILICONFLOW_API_KEY=...
  .venv/Scripts/python.exe -u bench/judge.py [--limit=30] [--side=nexus] [--model=...]
产出: docs/bench/judge-calibration.json
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "bench"
TRACES = OUT / "e2e-traces"

API_BASE = os.environ.get("BENCH_API_BASE", "https://api.siliconflow.cn/v1")
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
MODEL = os.environ.get("BENCH_MODEL", "deepseek-ai/DeepSeek-V4-Flash")

JUDGE_SYS = (
    "你是浏览器 agent 轨迹的裁判。给定任务、执行轨迹摘要和 agent 的最终回答,"
    "判断任务是否真正完成。注意: agent 说完成了不代表真完成 (看轨迹里的实际动作与页面反馈);"
    "步骤多不代表失败 (看最终状态)。输出严格 JSON: "
    '{"success": true/false, "key_points": ["..."], "reason": "一句话"}')


def _call(messages: list[dict]) -> dict:
    body = json.dumps({"model": MODEL, "messages": messages, "temperature": 0,
                       "max_tokens": 512, "response_format": {"type": "json_object"}},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(f"{API_BASE}/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _load_verdicts() -> dict[str, bool]:
    """e2e-llm.json → {(side, task, round): ok}"""
    d = json.loads((OUT / "e2e-llm.json").read_text(encoding="utf-8"))
    out = {}
    for side, rounds in d.items():
        for rd in rounds:
            for x in rd["results"]:
                out[(side, x["id"], rd["round"])] = bool(x["ok"])
    return out


def _task_prompt(task_id: str) -> str:
    sys.path.insert(0, str(Path(__file__).parent))
    import e2e_llm
    for t in e2e_llm.TASKS:
        if t["id"] == task_id:
            return t["prompt"]
    return "(未知任务)"


def _digest(trace: list[dict]) -> str:
    """轨迹压缩: 保留动作序列+短响应, 丢弃大快照正文 (裁判不需要全文)。一般 <3k 字符。"""
    parts = []
    for ev in trace:
        if ev["ev"] == "tool":
            resp = ev.get("resp", "")
            parts.append(f"→ {ev['name']}({json.dumps(ev.get('args', {}), ensure_ascii=False)[:80]})"
                         f" ⇒ {resp[:150].splitlines()[0] if resp else ''}")
        elif ev["ev"] == "verdict":
            parts.append(f"[硬验证器: {'通过' if ev.get('ok') else '拒绝'} {ev.get('note', '')}]")
            parts.append(f"[终答: {ev.get('final', '')[:200]}]")
        elif ev["ev"] == "model_error":
            parts.append(f"[模型错误: {ev.get('err', '')}]")
    return "\n".join(parts)


def main() -> None:
    if not API_KEY:
        raise SystemExit("缺 SILICONFLOW_API_KEY")
    limit, side_f, n = 30, None, 0
    for x in sys.argv[1:]:
        if x.startswith("--limit="):
            limit = int(x.split("=", 1)[1])
        elif x.startswith("--side="):
            side_f = x.split("=", 1)[1]
    verdicts = _load_verdicts()
    rows = []
    files = sorted(TRACES.glob("*.jsonl"))
    # 失败轨迹稀少 → 先全收失败样本, 再补成功样本到 limit
    def want(f):
        side, rest = f.stem.split("-", 1)
        task, rnd = rest.rsplit("-r", 1)
        return side, task, int(rnd)
    cands = [f for f in files if (side_f is None or f.stem.startswith(side_f))]
    fails = [f for f in cands if not verdicts.get((*want(f)[:2], want(f)[2]), True)]
    oks = [f for f in cands if verdicts.get((*want(f)[:2], want(f)[2]), False)]
    picked = (fails + oks)[:limit]
    for f in picked:
        side, task_id, rnd = want(f)
        hard = verdicts.get((side, task_id, rnd))
        trace = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        prompt = (f"任务: {_task_prompt(task_id)}\n\n轨迹摘要:\n{_digest(trace)}\n\n"
                  "判定该任务是否真正完成。")
        for wait in (0, 8, 20):
            if wait:
                time.sleep(wait)
            try:
                resp = _call([{"role": "system", "content": JUDGE_SYS},
                              {"role": "user", "content": prompt}])
                break
            except Exception as e:
                resp = None
                err = str(e)
        if resp is None:
            print(f"{f.stem}: 裁判调用失败 {err[:60]}", file=sys.stderr)
            continue
        try:
            j = json.loads(resp["choices"][0]["message"]["content"])
            jsuccess = bool(j.get("success"))
        except Exception:
            print(f"{f.stem}: 裁判输出非 JSON", file=sys.stderr)
            continue
        agree = jsuccess == hard
        rows.append({"trace": f.name, "side": side, "task": task_id, "round": rnd,
                     "hard": hard, "judge": jsuccess, "agree": agree,
                     "reason": (j.get("reason") or "")[:100]})
        n += 1
        print(f"{'✓' if agree else '✗'} {f.stem}: hard={hard} judge={jsuccess} "
              f"{(j.get('reason') or '')[:60]}", flush=True)
    tp = sum(1 for r in rows if r["hard"] and r["judge"])
    fn = sum(1 for r in rows if r["hard"] and not r["judge"])
    fp = sum(1 for r in rows if not r["hard"] and r["judge"])
    tn = sum(1 for r in rows if not r["hard"] and not r["judge"])
    agree_n = sum(1 for r in rows if r["agree"])
    summary = {"model": MODEL, "n": n, "agreement": agree_n / n if n else None,
               "confusion": {"tp": tp, "fn": fn, "fp": fp, "tn": tn}, "rows": rows}
    (OUT / "judge-calibration.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n裁判={MODEL} 样本={n} 一致率={agree_n}/{n} "
          f"({agree_n / n:.0%})" if n else "无样本")
    print(f"混淆: tp={tp} fn={fn} fp={fp} tn={tn}")
    print("门槛: ≥80% 可用于开放性任务判定 (Online-Mind2Web 参考 ~85%)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
