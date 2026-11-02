"""Embedding 统一入口：阿里云百炼 text-embedding-v4（OpenAI 兼容协议）。

约束：
- 入库与查询必须使用同一模型、同一维度，否则召回全乱；
- 单批最多 10 条，超出由本模块自动分批；
- 向量一律 L2 归一化后再存储/比较（归一化后内积等价余弦）。
"""
import math

from openai import OpenAI

from app import config

_client = OpenAI(
    base_url=config.EMBED_BASE_URL,
    api_key=config.EMBED_API_KEY,
    timeout=30,
    max_retries=2,
)

# 单行文本上限约 8192 tokens；客服问句远用不到，截断只作兜底
MAX_CHARS = 1000


def _prepare(text: str) -> str:
    return " ".join(str(text).split())[:MAX_CHARS]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_documents(texts: list[str]) -> list[list[float]]:
    """批量取向量，返回归一化后的 1024 维向量。"""
    if not texts:
        return []

    vectors: list[list[float]] = []
    batch_size = min(config.EMBED_BATCH, 10)
    for i in range(0, len(texts), batch_size):
        batch = [_prepare(t) for t in texts[i : i + batch_size]]
        resp = _client.embeddings.create(
            model=config.EMBED_MODEL,
            input=batch,
            dimensions=config.EMBED_DIM,
            encoding_format="float",
        )
        for item in resp.data:
            vec = _normalize(item.embedding)
            if len(vec) != config.EMBED_DIM:
                raise ValueError(
                    f"维度不一致：返回 {len(vec)} 维，配置 {config.EMBED_DIM} 维"
                )
            vectors.append(vec)
    return vectors


def embed_query(text: str) -> list[float]:
    """单条查询向量，与入库走完全相同的预处理与归一化。"""
    return embed_documents([text])[0]
