"""M1a 冒烟评测：逐条跑真实 LLM，统计选号命中率。

用法：python evals/run_eval.py [cases.jsonl]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.pipeline import answer  # noqa: E402
from app.stores import kb_db, shop_config  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else config.ROOT / "evals" / "cases_smoke.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    conn = kb_db.connect()
    cfg = shop_config.load()
    passed = 0
    rows = []

    for i, c in enumerate(cases, 1):
        t0 = time.time()
        res = answer(
            c["q"],
            ship_status=c.get("ship_status", "未发货"),
            logistics_status=c.get("logistics_status", "等待揽件"),
            conn=conn,
            cfg=cfg,
        )
        ok = res["plan_code"] in (c.get("codes") or [c["code"]])
        passed += ok
        rows.append(
            {
                "q": c["q"],
                "expect": c["code"],
                "actual": res["plan_code"],
                "scenario": res["scenario"],
                "fallback": res["fallback_level"],
                "ok": ok,
                "ms": int((time.time() - t0) * 1000),
            }
        )
        print(f"{i:02d} {'✅' if ok else '❌'} {c['q'][:24]:<26} → {res['plan_code']}  {rows[-1]['ms']}ms")

    out = config.ROOT / "evals" / "last_run.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    acc = passed / len(cases)
    print(f"\n选号命中率：{passed}/{len(cases)} = {acc:.1%}  明细：{out}")
    sys.exit(0 if acc >= 0.9 else 1)


if __name__ == "__main__":
    main()
