"""plans 表：61 条方案的唯一存储。复合主键 (scenario, code)，统一前缀编号。"""
import sqlite3
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    code        TEXT PRIMARY KEY,   -- 完整前缀编号，如 logistics-2-1-2
    scenario    TEXT NOT NULL,
    inner_code  TEXT NOT NULL,      -- 去前缀编号，如 2-1-2
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    has_branches INTEGER NOT NULL DEFAULT 0,
    todo        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plans_scenario ON plans(scenario);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def upsert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT INTO plans (code, scenario, inner_code, title, content, has_branches, todo)
           VALUES (:code, :scenario, :inner_code, :title, :content, :has_branches, :todo)
           ON CONFLICT(code) DO UPDATE SET
             title=excluded.title, content=excluded.content,
             has_branches=excluded.has_branches, todo=excluded.todo""",
        row,
    )


def list_scenario(conn: sqlite3.Connection, scenario: str) -> list[dict]:
    rows = conn.execute(
        "SELECT code, title FROM plans WHERE scenario=? ORDER BY code", (scenario,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_plan(conn: sqlite3.Connection, full_code: str) -> dict | None:
    row = conn.execute("SELECT * FROM plans WHERE code=?", (full_code,)).fetchone()
    return dict(row) if row else None


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT scenario, COUNT(*) AS n FROM plans GROUP BY scenario"
    ).fetchall()
    return {r["scenario"]: r["n"] for r in rows}
