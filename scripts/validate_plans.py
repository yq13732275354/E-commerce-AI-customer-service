"""数据校验：编号格式、重复、TODO、占位符残留、决策树引用一致性。

用法（项目根目录）：python scripts/validate_plans.py
退出码 0 = 通过（警告不影响），1 = 存在必须修复的问题。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.stores import plan_db, shop_config  # noqa: E402

CODE_RE = re.compile(r"^(logistics|payment|aftersales)-\d+-\d+-\d+$")
REF_RE = re.compile(r"方案\s*(\d+-\d+-\d+)")
TREE_FILES = {
    "快递场景问题.txt": "logistics",
    "支付场景问题.txt": "payment",
    "售后场景问题.txt": "aftersales",
}


def main() -> None:
    errors: list[str] = []
    warnings: list[str] = []

    if not config.DB_PATH.exists():
        sys.exit("plans.db 不存在，请先运行 python scripts/import_plans.py")

    conn = plan_db.connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM plans").fetchall()]
    cfg = shop_config.load()

    # 1. 数量与场景
    counts = plan_db.counts(conn)
    for s in config.SCENARIOS:
        if s == "product":
            continue  # 产品咨询走 kb_pairs，不在 plans 表
        if counts.get(s, 0) == 0:
            errors.append(f"场景 {s} 没有任何方案")
    print("场景分布：", counts, "总计", len(rows))

    # 2. 编号格式 / 前缀与场景一致
    for r in rows:
        if not CODE_RE.match(r["code"]):
            errors.append(f"编号格式非法：{r['code']}")
        if not r["code"].startswith(r["scenario"] + "-"):
            errors.append(f"编号前缀与场景不一致：{r['code']} / {r['scenario']}")

    # 3. TODO 条目（警告，不阻塞）
    todo = [r["code"] for r in rows if r["todo"] or not r["content"].strip()]
    if todo:
        warnings.append("待补写方案：" + ", ".join(todo))

    # 4. 占位符必须全部可替换
    for r in rows:
        left = shop_config.remaining(shop_config.render(r["content"], cfg))
        if left:
            errors.append(f"{r['code']} 占位符未映射：{left}")

    # 5. 非分支方案不应含 ${} 变量
    for r in rows:
        if "${" in r["content"] and not r["has_branches"]:
            errors.append(f"{r['code']} 含未标记的分支变量")

    # 6. 决策树引用 ↔ 方案实体 双向核对
    known = {r["code"] for r in rows}
    for fname, scenario in TREE_FILES.items():
        path = config.LARK_DIR / fname
        if not path.exists():
            warnings.append(f"决策树文件缺失：{fname}")
            continue
        refs = {f"{scenario}-{m}" for m in REF_RE.findall(path.read_text(encoding="utf-8"))}
        missing = sorted(refs - known)
        if missing:
            errors.append(f"{fname} 引用了不存在的方案：{missing}")

    # 输出
    print()
    for w in warnings:
        print(f"[WARN] {w}")
    for e in errors:
        print(f"[ERROR] {e}")
    if not errors:
        print(f"\n✅ 校验通过：{len(rows)} 条方案，{len(warnings)} 条警告，0 个错误")
    else:
        print(f"\n❌ 校验失败：{len(errors)} 个错误 / {len(warnings)} 条警告")
        sys.exit(1)


if __name__ == "__main__":
    main()
