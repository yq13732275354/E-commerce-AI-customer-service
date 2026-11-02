"""全局配置：路径、模型、数据文件位置。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK", "")

DB_PATH = ROOT / "data" / "plans.db"
SHOP_CONFIG = ROOT / "data" / "shop_config.yaml"
PRODUCT_CATALOG = ROOT / "data" / "corpus" / "product_catalog.yaml"
PROMPTS_DIR = ROOT / "app" / "prompts"

# 语料源文件（默认取项目上一级，实际路径由 .env 的 CORPUS_PATH / LARK_DIR 指定）
CORPUS_PATH = Path(
    os.getenv("CORPUS_PATH", ROOT.parent / "智能客服方案库-三场景问答语料.md")
)
LARK_DIR = Path(os.getenv("LARK_DIR", ROOT.parent / "lark_files"))

# Embedding（阿里云百炼 text-embedding-v4，OpenAI 兼容协议）
EMBED_BASE_URL = os.getenv(
    "EMBED_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "10"))  # 百炼单批上限 10，不可调大

# 召回开关：plans（旧清单选号）| vector（向量检索），灰度切流用
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "plans")
# 相似度下限：按语料实测校准（产品语料 0.55，物流/支付待校准）
RECALL_THRESHOLD = float(os.getenv("RECALL_THRESHOLD", "0.70"))

SCENARIOS = ("product", "logistics", "payment", "aftersales")

# 各场景相似度下限：不同语料分布不可复用同一阈值（evals/recall_eval.py --calibrate 产出）
RECALL_THRESHOLDS = {
    s: float(os.getenv(f"RECALL_THRESHOLD_{s.upper()}", os.getenv("RECALL_THRESHOLD", "0.70")))
    for s in SCENARIOS
}
