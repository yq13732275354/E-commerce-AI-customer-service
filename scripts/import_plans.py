"""把三场景语料 MD 导入 SQLite plans 表。

用法（项目根目录）：
    python scripts/import_plans.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.stores import plan_db  # noqa: E402

SECTION = re.compile(r"scenario\s*=\s*(\w+)")
ID_LINE = re.compile(r">\s*id:\s*([a-z]+)-(\d+-\d+-\d+)")
MARKERS = ("【条件分支】", "【TODO·缺失补写】")


def parse(path: Path) -> list[dict]:
    entries: list[dict] = []
    scenario: str | None = None
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur is None:
            return
        body = "\n".join(cur.pop("body")).strip()
        cur["content"] = re.sub(r"\n{2,}", "\n", body)
        entries.append(cur)
        cur = None

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and SECTION.search(line):
            flush()
            scenario = SECTION.search(line).group(1)
            continue
        if line.startswith("### "):
            flush()
            title = line[4:].strip()
            todo = any(m in title for m in MARKERS if "TODO" in m)
            for m in MARKERS:
                title = title.replace(m, "")
            cur = {
                "scenario": scenario,
                "title": title.strip(),
                "has_branches": 0,
                "todo": int(todo),
                "id_full": None,
                "body": [],
            }
            continue
        if cur is None:
            continue
        m = ID_LINE.search(line)
        if m:
            cur["id_full"] = f"{m.group(1)}-{m.group(2)}"
            cur["scenario"] = m.group(1)
            cur["has_branches"] = int("has_branches: true" in line)
            cur["todo"] = int(cur["todo"] or "status: TODO" in line)
        elif line.startswith(">"):
            if "status: TODO" in line:
                cur["todo"] = 1
            continue
        else:
            cur["body"].append(line)

    flush()

    rows = []
    for e in entries:
        if not e["id_full"]:
            print(f"[WARN] 缺少 id：{e['title']}", file=sys.stderr)
            continue
        scenario = e["scenario"]
        rows.append(
            {
                "code": e["id_full"],
                "scenario": scenario,
                "inner_code": e["id_full"].split("-", 1)[1],
                "title": e["title"],
                "content": e["content"],
                "has_branches": e["has_branches"],
                "todo": e["todo"],
            }
        )
    return rows


def main() -> None:
    if not config.CORPUS_PATH.exists():
        sys.exit(f"语料文件不存在：{config.CORPUS_PATH}")
    rows = parse(config.CORPUS_PATH)
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = plan_db.connect()
    plan_db.init_db(conn)
    conn.execute("DELETE FROM plans")
    for row in rows:
        plan_db.upsert(conn, row)
    conn.commit()
    print(f"导入完成：{len(rows)} 条 → {config.DB_PATH}")
    print("场景分布：", plan_db.counts(conn))
    todo = [r["code"] for r in rows if r["todo"]]
    print("TODO 条目：", todo or "无")


if __name__ == "__main__":
    main()
