"""会话状态与消息历史（与 plans 共用 data/plans.db）。"""
import json
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    active_scenario  TEXT,
    offered_plans    TEXT NOT NULL DEFAULT '[]',
    pending_slot     TEXT,
    last_product_id  TEXT,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    plan_code   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 兼容旧库：sessions 增加 last_product_id（跨商品串味过滤用）
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "last_product_id" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_product_id TEXT")
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_or_create(conn: sqlite3.Connection, session_id: str) -> dict:
    row = conn.execute(
        "SELECT session_id, active_scenario, offered_plans, pending_slot, last_product_id "
        "FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if row:
        return {
            "session_id": row[0],
            "active_scenario": row[1],
            "offered_plans": json.loads(row[2] or "[]"),
            "pending_slot": row[3],
            "last_product_id": row[4],
        }
    conn.execute(
        "INSERT INTO sessions(session_id, active_scenario, offered_plans, pending_slot, updated_at) VALUES(?,?,?,?,?)",
        (session_id, None, "[]", None, _now()),
    )
    conn.commit()
    return {
        "session_id": session_id,
        "active_scenario": None,
        "offered_plans": [],
        "pending_slot": None,
        "last_product_id": None,
    }


def save_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    active_scenario: str | None,
    offered_plans: list[str],
    pending_slot: str | None = None,
    last_product_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE sessions SET active_scenario=?, offered_plans=?, pending_slot=?, "
        "last_product_id=?, updated_at=? WHERE session_id=?",
        (
            active_scenario,
            json.dumps(offered_plans[-5:], ensure_ascii=False),
            pending_slot,
            last_product_id,
            _now(),
            session_id,
        ),
    )
    conn.commit()


def append(conn: sqlite3.Connection, session_id: str, role: str, content: str, plan_code: str | None = None) -> None:
    conn.execute(
        "INSERT INTO messages(session_id, role, content, plan_code, created_at) VALUES(?,?,?,?,?)",
        (session_id, role, content, plan_code, _now()),
    )
    conn.commit()


def recent(conn: sqlite3.Connection, session_id: str, limit: int = 6) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def last_user(conn: sqlite3.Connection, session_id: str) -> str:
    """最近一条用户消息，用于追问改写（拼上轮主题）。"""
    row = conn.execute(
        "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else ""
