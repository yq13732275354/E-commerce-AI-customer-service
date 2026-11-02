"""LLM 统一入口（M1a 只需结构化 JSON 与自由文本两种调用）。"""
import json
import os
import time
from typing import Any

# 避免启动时联网拉取模型价格表（本地环境常超时）
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from litellm import completion

from app import config

litellm.drop_params = True
litellm.suppress_debug_info = True


def _call(messages: list[dict], temperature: float, json_mode: bool) -> str:
    kwargs: dict[str, Any] = {
        "model": config.MODEL,
        "api_key": config.API_KEY,
        "messages": messages,
        "temperature": temperature,
        "timeout": 40,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = completion(**kwargs)
    return resp.choices[0].message.content or ""


def chat_text(system: str, user: str, temperature: float = 0.9) -> str:
    return _call(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature,
        False,
    ).strip()


def chat_json(system: str, user: str, temperature: float = 0.0) -> dict:
    """严格 JSON 输出；解析失败重试一次，再失败抛异常。"""
    for attempt in range(2):
        raw = _call(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature if attempt == 0 else 0.0,
            True,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt == 1:
                raise
            time.sleep(0.3)
    raise RuntimeError("unreachable")
