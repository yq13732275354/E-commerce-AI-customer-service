"""店铺业务参数：把方案里的 [X] 一类占位符替换成真实数值。"""
import re
from pathlib import Path

import yaml

from app import config

_PLACEHOLDER = re.compile(r"\[[^\]]*\]")


def load(path: Path | None = None) -> dict:
    with open(path or config.SHOP_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def render(text: str, cfg: dict | None = None) -> str:
    cfg = cfg or load()
    mapping = cfg.get("placeholders", {})
    for key, value in mapping.items():
        text = text.replace(key, str(value))
    # 去掉话术里的写作提示与编辑说明
    text = re.sub(r"^告知用户[\s：,，]*", "", text)
    text = re.sub(r"（[^）]*根据(实际)?情况[^）]*）", "", text)
    text = text.replace("[]", "").replace("，。", "。").replace("。。", "。")
    return text.strip()


def remaining(text: str) -> list[str]:
    return _PLACEHOLDER.findall(text)
