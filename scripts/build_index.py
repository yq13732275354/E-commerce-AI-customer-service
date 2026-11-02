"""构建向量索引：kb_pairs（真值） → kb_docs / kb_vec（可重建缓存）。

用法：
  python scripts/build_index.py                 # 增量：只嵌入新增/变更文本
  python scripts/build_index.py --full          # 全量：清空后重建
  python scripts/build_index.py --published-only # 只索引已发布语料（上线口径）

流程（技术方案 §6）：
  1. 把 data/corpus/*.jsonl 同步进 kb_pairs（真值表，人可读、可 diff）
  2. 每条知识对拆成 1 条标准问 + N 条同义问，写 kb_docs
  3. 按 text_hash 增量：文本未变则复用已有向量，不重复调 Embedding
  4. 批量取向量（每批 10 条），显式 rowid 对齐写入 kb_vec
  5. 写 kb_meta（模型 / 维度 / 语料版本 / 构建时间 / 条数）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sqlite_vec  # noqa: E402

from app import config  # noqa: E402
from app.llm.embedding import embed_documents  # noqa: E402
from app.stores import kb_db  # noqa: E402

CORPUS_DIR = ROOT / "data" / "corpus"


# ---------------------------------------------------------------- 真值表同步


def _json(value, default=None):
    return json.dumps(value, ensure_ascii=False) if value is not None else default


def _sync_corpus(conn: sqlite3.Connection, files: list[Path]) -> tuple[int, str]:
    """把语料 JSONL upsert 进 kb_pairs，返回（条数, 语料版本哈希）。"""
    digest = hashlib.sha256()
    count = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode())
        digest.update(raw)
        for line in raw.decode("utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            row = json.loads(line)
            conn.execute(
                """INSERT INTO kb_pairs (
                       pair_id, scenario, sub_scenario, product_id,
                       canonical_question, paraphrases, answer,
                       conditions, keywords, requires_order,
                       policy_level, status, source, version, updated_at
                   ) VALUES (
                       :pair_id, :scenario, :sub_scenario, :product_id,
                       :canonical_question, :paraphrases, :answer,
                       :conditions, :keywords, :requires_order,
                       :policy_level, :status, :source, :version, :updated_at
                   )
                   ON CONFLICT(pair_id) DO UPDATE SET
                       scenario=excluded.scenario,
                       sub_scenario=excluded.sub_scenario,
                       product_id=excluded.product_id,
                       canonical_question=excluded.canonical_question,
                       paraphrases=excluded.paraphrases,
                       answer=excluded.answer,
                       conditions=excluded.conditions,
                       keywords=excluded.keywords,
                       requires_order=excluded.requires_order,
                       policy_level=excluded.policy_level,
                       status=excluded.status,
                       source=excluded.source,
                       version=excluded.version,
                       updated_at=excluded.updated_at""",
                {
                    "pair_id": row["pair_id"],
                    "scenario": row["scenario"],
                    "sub_scenario": row.get("sub_scenario") or "",
                    "product_id": row.get("product_id"),
                    "canonical_question": row["canonical_question"],
                    "paraphrases": _json(row.get("paraphrases") or [], "[]"),
                    "answer": row["answer"],
                    "conditions": _json(row.get("conditions")),
                    "keywords": _json(row.get("keywords") or [], "[]"),
                    "requires_order": int(row.get("requires_order") or 0),
                    "policy_level": row.get("policy_level") or "stable",
                    "status": row.get("status") or "draft",
                    "source": row.get("source"),
                    "version": int(row.get("version") or 1),
                    "updated_at": now,
                },
            )
            count += 1
    return count, digest.hexdigest()


# ---------------------------------------------------------------- 文档组装


def _embed_text(row: sqlite3.Row, question: str, product_name: str = "") -> str:
    """embed 文本 = 场景 + 子场景 + 商品名 + 关键词 + 问句；answer 不进向量。"""
    keywords = json.loads(row["keywords"] or "[]")
    prefix = [row["scenario"], row["sub_scenario"], product_name, *keywords]
    parts = [p for p in prefix if p]
    parts.append(question)
    return " ".join(parts)


def _collect_docs(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> list[dict]:
    """一条知识对拆成 1 条标准问 + N 条同义问，返回待建索引的文档列表。"""
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM kb_pairs WHERE status IN ({placeholders}) ORDER BY pair_id",
        statuses,
    ).fetchall()
    names = kb_db.product_names()

    docs: list[dict] = []
    for row in rows:
        questions = [(row["canonical_question"], "canonical")]
        questions += [(q, "paraphrase") for q in json.loads(row["paraphrases"] or "[]")]
        product_name = names.get(row["product_id"] or "", "")

        seen: set[str] = set()
        for question, kind in questions:
            text = " ".join(question.split())
            if not text or text in seen:  # 同义问与标准问重复时只留一条
                continue
            seen.add(text)
            embed_text = _embed_text(row, text, product_name)
            docs.append(
                {
                    "pair_id": row["pair_id"],
                    "scenario": row["scenario"],
                    "kind": kind,
                    "text": text,
                    "embed_text": embed_text,
                    "text_hash": hashlib.sha256(embed_text.encode()).hexdigest(),
                }
            )
    return docs


# ---------------------------------------------------------------- 建索引


def build(conn: sqlite3.Connection, *, full: bool, include_draft: bool) -> dict:
    started = time.perf_counter()

    corpus_files = sorted(CORPUS_DIR.glob("*.jsonl"))
    if not corpus_files:
        raise SystemExit(f"未找到语料文件：{CORPUS_DIR}/*.jsonl")
    pair_count, corpus_version = _sync_corpus(conn, corpus_files)

    # 维度变化（换模型 / 改降维）时 vec0 表结构必须随之重建
    meta_dim = kb_db.get_meta(conn, "embed_dim")
    if meta_dim and int(meta_dim) != config.EMBED_DIM:
        print(f"[warn] 索引维度 {meta_dim} 与配置 {config.EMBED_DIM} 不一致，转为全量重建")
        full = True

    if full:
        conn.execute("DROP TABLE IF EXISTS kb_vec")
        conn.execute("DELETE FROM kb_docs")
    kb_db.init_db(conn)

    statuses = ("published", "reviewed") + (("draft",) if include_draft else ())
    docs = _collect_docs(conn, statuses)
    if not docs:
        raise SystemExit(f"kb_pairs 中没有可建索引的语料（status={statuses}）")

    existing = {
        (r["pair_id"], r["kind"], r["text"]): (r["rowid"], r["text_hash"])
        for r in conn.execute("SELECT rowid, pair_id, kind, text, text_hash FROM kb_docs")
    }
    desired = {(d["pair_id"], d["kind"], d["text"]) for d in docs}

    # 已不在语料中的条目：索引与文档一并删除
    removed = [(rowid, key) for key, (rowid, _) in existing.items() if key not in desired]
    for rowid, _ in removed:
        conn.execute("DELETE FROM kb_vec WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM kb_docs WHERE rowid=?", (rowid,))

    # 文本未变则复用向量；新增或变更才需要重新调 Embedding
    to_embed: list[dict] = []
    reused = 0
    for doc in docs:
        key = (doc["pair_id"], doc["kind"], doc["text"])
        old = existing.get(key)
        if old and old[1] == doc["text_hash"]:
            reused += 1
            continue
        if old:  # 文本哈希变了：旧向量作废
            conn.execute("DELETE FROM kb_vec WHERE rowid=?", (old[0],))
            conn.execute("DELETE FROM kb_docs WHERE rowid=?", (old[0],))
        to_embed.append(doc)

    vectors = embed_documents([d["embed_text"] for d in to_embed]) if to_embed else []
    for doc, vec in zip(to_embed, vectors):
        cursor = conn.execute(
            "INSERT INTO kb_docs (pair_id, kind, text, text_hash) VALUES (?, ?, ?, ?)",
            (doc["pair_id"], doc["kind"], doc["text"], doc["text_hash"]),
        )
        conn.execute(
            "INSERT INTO kb_vec (rowid, embedding, scenario) VALUES (?, ?, ?)",
            (cursor.lastrowid, sqlite_vec.serialize_float32(vec), doc["scenario"]),
        )

    doc_count = conn.execute("SELECT COUNT(*) AS n FROM kb_docs").fetchone()["n"]
    kb_db.apply_meta(
        conn,
        {
            "embed_model": config.EMBED_MODEL,
            "embed_dim": str(config.EMBED_DIM),
            "corpus_version": corpus_version,
            "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "doc_count": str(doc_count),
        },
    )
    conn.commit()

    return {
        "pairs": pair_count,
        "docs": doc_count,
        "embedded": len(to_embed),
        "reused": reused,
        "removed": len(removed),
        "dim": config.EMBED_DIM,
        "seconds": time.perf_counter() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="构建向量索引 kb_docs / kb_vec")
    parser.add_argument("--full", action="store_true", help="清空后全量重建")
    parser.add_argument(
        "--published-only",
        action="store_true",
        help="只索引 published/reviewed 语料（默认连 draft 一起建，联调期产品语料仍是 draft）",
    )
    args = parser.parse_args()

    conn = kb_db.connect()
    conn.executescript(kb_db.PAIRS_SCHEMA)
    stats = build(conn, full=args.full, include_draft=not args.published_only)
    conn.close()

    print(
        "索引完成："
        f"知识对 {stats['pairs']} 条 → 文档 {stats['docs']} 条"
        f"（新嵌入 {stats['embedded']}，复用 {stats['reused']}，删除 {stats['removed']}）"
        f"，维度 {stats['dim']}，耗时 {stats['seconds']:.1f}s"
    )


if __name__ == "__main__":
    main()
