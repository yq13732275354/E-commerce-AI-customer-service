"""知识库四张表的读写与向量召回。

kb_pairs = 真值（人可读、可 diff）；kb_docs / kb_vec = 派生索引，可随时整表重建。
"""
import sqlite3
import time
from pathlib import Path

import sqlite_vec
import yaml

from app import config
from app.llm.embedding import embed_query

# 召回参数：阈值只在 dev 集上校准（技术方案 §7.3）
THRESHOLD = config.RECALL_THRESHOLD  # 全局兜底值，实际按场景取 threshold()
K = 20            # KNN 取多少条再按 pair_id 去重
TOP_N = 5         # 去重后留给选号的最大候选数
GAP = 0.05        # top1 - top2 的分差，够大就直接取 top1，省一次模型调用
MID_CONFIDENCE = 0.80  # 高于此值直接答；0.70~0.80 加一句“可核实”


def threshold(scenario: str) -> float:
    """场景级阈值：产品语料口语化、分数偏低，物流/支付模板化、分数偏高。"""
    return config.RECALL_THRESHOLDS.get(scenario, config.RECALL_THRESHOLD)


def threshold_map() -> dict[str, float]:
    return {s: threshold(s) for s in config.SCENARIOS}


PAIRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_pairs (
    pair_id            TEXT PRIMARY KEY,
    scenario           TEXT NOT NULL,
    sub_scenario       TEXT NOT NULL,
    product_id         TEXT,
    canonical_question TEXT NOT NULL,
    paraphrases        TEXT,               -- JSON 数组：一条知识对的同义问
    answer             TEXT NOT NULL,
    conditions         TEXT,               -- JSON：{"ship_status": "shipped"}
    keywords           TEXT,               -- JSON 数组
    requires_order     INTEGER DEFAULT 0,
    policy_level       TEXT NOT NULL,
    status             TEXT NOT NULL,
    source             TEXT,
    version            INTEGER NOT NULL DEFAULT 1,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_pairs_scenario ON kb_pairs(scenario);

CREATE TABLE IF NOT EXISTS kb_docs (
    rowid     INTEGER PRIMARY KEY,
    pair_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,               -- canonical / paraphrase
    text      TEXT NOT NULL,
    text_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_docs_pair ON kb_docs(pair_id);

CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

KNN_SQL = """
SELECT d.pair_id, v.distance
FROM kb_vec v
JOIN kb_docs d ON d.rowid = v.rowid
WHERE v.embedding MATCH ? AND k = ? AND v.scenario = ?
ORDER BY v.distance
"""


def vec_schema(dim: int | None = None) -> str:
    """scenario 必须声明为元数据列（写成 +scenario 会变成辅助列，无法参与 KNN 过滤）。"""
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(
    embedding FLOAT[{dim or config.EMBED_DIM}],
    scenario TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(PAIRS_SCHEMA)
    conn.executescript(vec_schema())
    conn.commit()


def apply_meta(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_pair(conn: sqlite3.Connection, pair_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM kb_pairs WHERE pair_id=?", (pair_id,)).fetchone()
    return dict(row) if row else None


def product_names() -> dict[str, str]:
    """商品事实表里的 product_id → 商品名，用于组装 embed 文本与追问改写。"""
    if not config.PRODUCT_CATALOG.exists():
        return {}
    data = yaml.safe_load(config.PRODUCT_CATALOG.read_text(encoding="utf-8")) or {}
    return {p["product_id"]: p["name"] for p in data.get("products", [])}


def product_aliases() -> list[str]:
    """商品名与品类词：判断用户这句话是否已经指名了商品。"""
    if not config.PRODUCT_CATALOG.exists():
        return []
    data = yaml.safe_load(config.PRODUCT_CATALOG.read_text(encoding="utf-8")) or {}
    aliases: list[str] = []
    for product in data.get("products", []):
        aliases += [product.get("name", ""), product.get("category", "")]
    return [a for a in aliases if a]


def search(
    conn: sqlite3.Connection, message: str, scenario: str, *, k: int = K
) -> list[dict]:
    """场景内 KNN → 按 pair_id 去重（取最小距离）→ 按相似度倒序。

    kb_vec 存的是归一化向量，`distance` 为 L2 距离，相似度 = 1 - d²/2。
    """
    started = time.perf_counter()
    vector = embed_query(message)
    rows = conn.execute(
        KNN_SQL, (sqlite_vec.serialize_float32(vector), k, scenario)
    ).fetchall()

    best: dict[str, dict] = {}
    for row in rows:
        score = max(0.0, 1 - (row["distance"] ** 2) / 2)
        pair_id = row["pair_id"]
        if pair_id not in best or score > best[pair_id]["score"]:
            best[pair_id] = {"pair_id": pair_id, "score": round(score, 4)}

    pair_ids = list(best)
    if pair_ids:
        marks = ",".join("?" for _ in pair_ids)
        pairs = conn.execute(
            f"SELECT pair_id, canonical_question, product_id, sub_scenario "
            f"FROM kb_pairs WHERE pair_id IN ({marks})",
            pair_ids,
        ).fetchall()
        for pair in pairs:
            item = best[pair["pair_id"]]
            item.update(
                canonical_question=pair["canonical_question"],
                product_id=pair["product_id"],
                sub_scenario=pair["sub_scenario"],
            )

    candidates = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    for item in candidates:
        item["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return candidates[:TOP_N]


def stats(conn: sqlite3.Connection) -> dict:
    return {
        "pairs": conn.execute("SELECT COUNT(*) FROM kb_pairs").fetchone()[0],
        "docs": conn.execute("SELECT COUNT(*) FROM kb_docs").fetchone()[0],
        "vectors": conn.execute("SELECT COUNT(*) FROM kb_vec").fetchone()[0],
        "embed_model": get_meta(conn, "embed_model"),
        "embed_dim": get_meta(conn, "embed_dim"),
        "built_at": get_meta(conn, "built_at"),
    }
