"""CLI 单轮问答。

用法（项目根目录）：
    python scripts/chat_cli.py "我买的什么时候发货？"
    python scripts/chat_cli.py "地址写错了怎么改" --ship-status 已发货 --logistics-status 等待揽件
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import answer  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--ship-status", default="未发货")
    parser.add_argument("--logistics-status", default="等待揽件")
    args = parser.parse_args()

    res = answer(
        args.message,
        ship_status=args.ship_status,
        logistics_status=args.logistics_status,
    )
    print(
        f"[分诊] {res['scenario']} (confidence={res['confidence']}, "
        f"followup={res['is_followup']})"
    )
    print(f"[选号] {res['plan_code'] or '无匹配'}  [{res['fallback_level']}]")
    print(f"[回复] {res['reply']}")


if __name__ == "__main__":
    main()
