"""把 v1.0 plans.db 的存量方案迁移成向量检索语料（技术方案 §3.2 / §4）。

做法：
  1. 读取 plans 表（跳过 todo 空方案）
  2. 清洗话术（去“告知用户”前缀、去平台名、统一语气）
  3. 分支方案（含 ${订单发货状态} 等）拆成“按状态分情况”的单条答案
  4. 调 LLM 为每条标准问补 4 条口语同义问 + 3 个检索关键词（带本地缓存）
  5. 输出 data/corpus/{logistics,payment,aftersales}_qa.jsonl（真值，可 diff）

用法：
  python scripts/migrate_plans.py              # 已存在则跳过同义问生成（走缓存）
  python scripts/migrate_plans.py --no-llm     # 不调模型，仅用规则生成同义问（离线可跑）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.llm.client import chat_json  # noqa: E402

CORPUS_DIR = ROOT / "data" / "corpus"
CACHE_PATH = CORPUS_DIR / ".migrate_cache.json"

SUB_SCENARIO = {
    "logistics": {"1": "delivery_time", "2": "address", "3": "exception"},
    "payment": {"1": "pay_fail", "2": "pay_status", "3": "refund"},
    "aftersales": {"1": "apply", "2": "warranty", "3": "progress"},
}

# 原文档用了全角/半角混排的引号，两种都要匹配
# 原文形如 ${订单发货状态}="未发货"，中间可能带 ${ } 包裹
STATUS_RE = re.compile(r"(订单发货状态|订单物流状态)\s*\}?\s*=\s*[\"“”]([^\"“”]+)[\"“”]")
SENTENCE_END = ("。", "！", "？", "；")
DANGLING = "为的和把将被与或如在对从及"


# ---------------------------------------------------------------- 清洗


def clean(text: str) -> str:
    """去掉写作提示与平台专名，统一标点。"""
    text = text.strip()
    text = re.sub(r"^(您好[，,]?|小主[，,]?)", "", text)
    text = re.sub(r"^(首先)?安抚用户(情绪)?[，,]?(然后)?", "", text)
    text = text.replace("抖音商城", "店铺").replace("抖音", "平台")
    text = text.replace("哦。", "。").replace("哟，", "，").replace("哟。", "。")
    text = re.sub(r"（[^）]*根据(实际)?(业务)?情况[^）]*）", "", text)
    text = text.replace("参考方案：", "")
    text = re.sub(r"告知用户[\s：,，]*", "", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _shorten(text: str, limit: int = 60) -> str:
    """分支话术按句截断，保留可执行的下一步，避免半个句子。"""
    text = clean(text).replace("\n", "")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(p) for p in SENTENCE_END)
    if idx > 20:
        return cut[: idx + 1]
    while cut and cut[-1] in DANGLING:
        cut = cut[:-1]
    return cut + "。"


def expand_branches(content: str) -> str:
    """把 ${订单发货状态}/${订单物流状态} 决策树压成“按状态分情况”的一句话术。"""
    ship = ""
    logistics = ""
    branches: list[tuple[str, str]] = []
    for raw in content.splitlines():
        line = raw.strip().lstrip("-").strip()
        found = STATUS_RE.findall(line)
        for key, value in found:
            if key == "订单发货状态":
                ship, logistics = value, ""
            else:
                logistics = value
        if "参考方案：" in line:
            label = f"{ship}（{logistics}）" if logistics else ship
            branches.append((label, _shorten(line.split("参考方案：", 1)[1])))
    if not branches:
        return _shorten(content)

    # 同一状态的多个物流子状态合并展示
    merged: list[tuple[str, str]] = []
    for label, text in branches:
        if merged and merged[-1][1] == text:
            merged[-1] = (f"{merged[-1][0]}、{label}", text)
        else:
            merged.append((label, text))
    parts = [f"{label}时：{text}" for label, text in merged]
    answer = "分情况处理：" + "；".join(parts)
    answer = answer.replace("。；", "。").replace("；；", "；")
    return answer


# ---------------------------------------------------------------- 同义问


def _rule_paraphrases(title: str) -> list[str]:
    """离线兜底：标题本身就是标准问，做几种轻量改写。"""
    base = title.rstrip("？?")
    variants = [base, f"{base}怎么弄", f"{base}怎么办"]
    return list(dict.fromkeys(v for v in variants if v != title))


PARA_PROMPT = """你是电商客服语料助手。为给定的标准问生成用户真实的口语问法。
要求：
1. 每条生成 4 条，可带错别字、省略、简称、语气词，但不得改变意图；
2. 不得与标准问完全相同；
3. 同时给出 3 个检索关键词（2~4 字，覆盖核心实体与动作）。
只输出 JSON，形如 {"items":[{"pair_id":"...","paraphrases":["..."],"keywords":["..."]}]}"""


def gen_paraphrases(items: list[dict], cache: dict, use_llm: bool) -> dict:
    todo = [it for it in items if it["pair_id"] not in cache]
    if use_llm and todo:
        for i in range(0, len(todo), 8):
            batch = todo[i : i + 8]
            payload = [{"pair_id": b["pair_id"], "question": b["title"]} for b in batch]
            try:
                out = chat_json(
                    PARA_PROMPT,
                    json.dumps(payload, ensure_ascii=False),
                    temperature=0.3,
                )
                for row in out.get("items", []):
                    pid = row.get("pair_id")
                    if pid not in {b["pair_id"] for b in batch}:
                        continue
                    cache[pid] = {
                        "paraphrases": [p for p in row.get("paraphrases", []) if p][:4],
                        "keywords": [k for k in row.get("keywords", []) if k][:3],
                    }
            except Exception as e:  # noqa: BLE001 模型抖动时退回规则
                print(f"[warn] 同义问生成失败，{len(batch)} 条走规则兜底：{e}")
    for it in items:
        if it["pair_id"] not in cache:
            cache[it["pair_id"]] = {
                "paraphrases": _rule_paraphrases(it["title"]),
                "keywords": [],
            }
    return cache


# ---------------------------------------------------------------- 主流程


def _policy_level(scenario: str, code: str, content: str) -> str:
    if scenario == "payment" and code.startswith(("payment-2-1-12", "payment-3")):
        return "sensitive"
    if scenario == "aftersales" and code.startswith("aftersales-3"):
        return "sensitive"
    if "[" in content:
        return "configurable"
    return "stable"


def _requires_order(scenario: str, code: str) -> int:
    if scenario == "logistics" and code.startswith("logistics-2"):
        return 1
    if scenario == "payment" and code.startswith("payment-2"):
        return 1
    return 0


def migrate(use_llm: bool) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM plans WHERE todo=0 AND length(content)>0 ORDER BY code"
        )
    ]
    print(f"读取方案 {len(rows)} 条（已跳过 todo/空正文）")

    cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    items = [{"pair_id": r["code"], "title": r["title"]} for r in rows]
    cache = gen_paraphrases(items, cache, use_llm)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    buckets: dict[str, list[dict]] = {"logistics": [], "payment": [], "aftersales": []}
    for r in rows:
        code = r["code"]
        scenario = r["scenario"]
        sub = SUB_SCENARIO[scenario].get(code.split("-")[1], "general")
        answer = expand_branches(r["content"]) if r["has_branches"] else clean(r["content"])
        entry = cache[code]
        buckets[scenario].append(
            {
                "pair_id": code,
                "scenario": scenario,
                "sub_scenario": sub,
                "product_id": None,
                "canonical_question": r["title"],
                "paraphrases": entry["paraphrases"],
                "answer": answer,
                "conditions": None,
                "keywords": entry["keywords"],
                "requires_order": _requires_order(scenario, code),
                "policy_level": _policy_level(scenario, code, r["content"]),
                "status": "published",
                "source": "existing_plan",
                "version": 1,
                "updated_at": now,
            }
        )

    for scenario, rows_out in buckets.items():
        path = CORPUS_DIR / f"{scenario}_qa.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows_out:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        print(f"写入 {path.name}：{len(rows_out)} 条  sha256={digest}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="不调模型，仅规则生成同义问")
    args = parser.parse_args()
    if not config.DB_PATH.exists():
        sys.exit("plans.db 不存在，请先运行 python scripts/import_plans.py")
    migrate(use_llm=not args.no_llm)


if __name__ == "__main__":
    main()
