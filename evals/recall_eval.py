"""检索质量评测：Recall@k / MRR / 误答率 / 阈值校准（技术方案 §7.3）。

只评测“召回 + 取号”这一段，不调用分诊与选号模型，因此可回归、可复现、无随机性。

用法：
  python evals/recall_eval.py                      # dev + test 各跑一遍
  python evals/recall_eval.py --split test         # 只跑 test
  python evals/recall_eval.py --calibrate          # 在 dev 上扫阈值，输出建议值
  python evals/recall_eval.py --e2e                # 端到端（含分诊，慢，消耗 token）

指标口径：
  R@k    前 k 个候选里出现期望 pair_id 的比例
  意图@1  top1 与期望同场景、同子场景（商品咨询还要同 product_id）——
          用户问“承重多少”时命中“体重大能坐吗”属于可接受，不算答错
  MRR    期望 pair_id 排名倒数的均值
  误答    top1 相似度 ≥ 阈值，但 top1 与期望的意图不一致 —— 最危险的指标
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.stores import kb_db  # noqa: E402

EVAL_DIR = ROOT / "evals"
SCENARIOS = ("product", "logistics", "payment", "aftersales")


def load(split: str) -> list[dict]:
    path = EVAL_DIR / f"recall_{split}.jsonl"
    if not path.exists():
        raise SystemExit(f"缺少评测集：{path}")
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _pair_meta(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT pair_id, scenario, sub_scenario, product_id FROM kb_pairs"
    ).fetchall()
    return {r["pair_id"]: dict(r) for r in rows}


def run(conn, cases: list[dict], meta: dict[str, dict]) -> list[dict]:
    rows = []
    for case in cases:
        candidates = kb_db.search(conn, case["q"], case["scenario"])
        ids = [c["pair_id"] for c in candidates]
        rank = ids.index(case["expect"]) + 1 if case["expect"] in ids else 0
        top = candidates[0] if candidates else None
        want = meta.get(case["expect"], {})
        got = meta.get(top["pair_id"], {}) if top else {}
        intent_ok = bool(
            top
            and got.get("sub_scenario") == want.get("sub_scenario")
            and (case["scenario"] != "product"
                 or got.get("product_id") == want.get("product_id"))
        )
        rows.append(
            {
                "q": case["q"],
                "scenario": case["scenario"],
                "expect": case["expect"],
                "top1": top["pair_id"] if top else None,
                "top1_score": top["score"] if top else 0.0,
                "rank": rank,
                "ok": rank == 1,
                "intent_ok": intent_ok,
                "hit5": rank > 0,
                "latency_ms": top["latency_ms"] if top else None,
            }
        )
    return rows


def report(rows: list[dict], *, scenario: str | None = None) -> dict:
    items = [r for r in rows if scenario is None or r["scenario"] == scenario]
    if not items:
        return {}
    n = len(items)
    r1 = sum(r["rank"] == 1 for r in items) / n
    r3 = sum(1 <= r["rank"] <= 3 for r in items) / n
    r5 = sum(r["hit5"] for r in items) / n
    mrr = sum((1 / r["rank"]) if r["rank"] else 0 for r in items) / n
    return {"n": n, "R@1": r1, "R@3": r3, "R@5": r5, "MRR": mrr,
            "intent": sum(r["intent_ok"] for r in items) / n,
            "avg_score": sum(r["top1_score"] for r in items) / n}


def print_table(rows: list[dict]) -> None:
    print(f"{'场景':<10}{'N':>4}{'R@1':>8}{'意图@1':>9}{'R@3':>8}{'R@5':>8}{'MRR':>8}{'top1分':>9}")
    for s in SCENARIOS:
        rep = report(rows, scenario=s)
        if not rep:
            continue
        print(
            f"{s:<10}{rep['n']:>4}{rep['R@1']:>8.1%}{rep['intent']:>9.1%}"
            f"{rep['R@3']:>8.1%}{rep['R@5']:>8.1%}{rep['MRR']:>8.3f}{rep['avg_score']:>9.3f}"
        )
    rep = report(rows)
    print(
        f"{'合计':<10}{rep['n']:>4}{rep['R@1']:>8.1%}{rep['intent']:>9.1%}"
        f"{rep['R@3']:>8.1%}{rep['R@5']:>8.1%}{rep['MRR']:>8.3f}{rep['avg_score']:>9.3f}"
    )


def calibrate(conn, rows: list[dict], min_coverage: float = 0.8) -> dict[str, float]:
    """逐场景扫阈值：在作答率 ≥ min_coverage 的前提下，最大化“意图答对 - 意图答错”。

    用意图级而不是 pair 级，是因为同子场景的近似条目答出去不算事故；
    真正要压的是跨意图误答。阈值只负责“答不答”，跨意图歧义交给分差 + 选号。
    """
    out: dict[str, float] = {}
    for scenario in SCENARIOS:
        items = [r for r in rows if r["scenario"] == scenario]
        if not items:
            continue
        pairs = conn.execute(
            "SELECT COUNT(*) FROM kb_pairs WHERE scenario=?", (scenario,)
        ).fetchone()[0]
        base = 1 / max(pairs, 1)
        print(f"\n[{scenario}] 随机基线 R@1 ≈ {base:.3f}（该场景 {pairs} 条知识对）")
        print(f"{'阈值':>6}{'作答率':>9}{'意图对':>7}{'意图错':>7}{'精确率':>9}")
        best = (-99, 0.70)
        for i in range(30, 91):
            t = i / 100
            answered = [r for r in items if r["top1_score"] >= t]
            if len(answered) / len(items) < min_coverage:
                break
            right = sum(r["intent_ok"] for r in answered)
            wrong = len(answered) - right
            score = right - wrong
            if score > best[0] or (score == best[0] and t > best[1]):
                best = (score, t)
            if i % 5 == 0:
                prec = right / len(answered) if answered else 0
                print(
                    f"{t:>6.2f}{len(answered) / len(items):>9.0%}"
                    f"{right:>7}{wrong:>7}{prec:>9.0%}"
                )
        out[scenario] = best[1]
        print(f"  → 建议阈值 {best[1]:.2f}（utility={best[0]}，作答率 ≥ {min_coverage:.0%}）")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--calibrate", action="store_true", help="在 dev 上校准各场景阈值")
    args = parser.parse_args()

    conn = kb_db.connect()
    meta = _pair_meta(conn)
    splits = ["dev", "test"] if args.split == "all" else [args.split]
    results: dict[str, list[dict]] = {}

    for split in splits:
        rows = run(conn, load(split), meta)
        results[split] = rows
        print(f"\n===== {split} 集 =====")
        print_table(rows)
        missed = [r for r in rows if not r["ok"]]
        for r in missed[:8]:
            flag = "≈" if r["intent_ok"] else "✗"
            print(f"  {flag} {r['q']}  期望 {r['expect']}  实际 {r['top1']}  分 {r['top1_score']:.3f}")

    if args.calibrate:
        rows = results.get("dev") or run(conn, load("dev"), meta)
        print("\n===== 阈值校准（dev 集） =====")
        suggested = calibrate(conn, rows)
        print("\n可直接写入 .env：")
        for scenario, t in suggested.items():
            print(f"RECALL_THRESHOLD_{scenario.upper()}={t:.2f}")

    # 当前阈值下的“误答 / 漏答”体检
    print("\n===== 当前阈值体检 =====")
    for split, rows in results.items():
        thr = kb_db.threshold_map()
        wrong = [r for r in rows if r["top1_score"] >= thr[r["scenario"]] and not r["intent_ok"]]
        miss = [r for r in rows if r["intent_ok"] and r["top1_score"] < thr[r["scenario"]]]
        print(
            f"{split}: 跨意图误答 {len(wrong)} 条，漏答（本可答对但低于阈值）{len(miss)} 条"
            + (f"，误答样例：{[(r['q'], r['top1']) for r in wrong[:3]]}" if wrong else "")
        )

    out = EVAL_DIR / "recall_last_run.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细：{out}")
    print(f"检索模式：{config.RETRIEVAL_MODE}　知识库：{kb_db.stats(conn)}")


if __name__ == "__main__":
    main()
