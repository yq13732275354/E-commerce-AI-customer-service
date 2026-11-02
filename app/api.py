"""FastAPI 服务层：单轮/多轮问答 + 会话落库 + SSE 流式 + 本地聊天页。"""
import json
import time
import uuid
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import config
from app.graph import CONF_THRESHOLD, run as run_graph
from app.pipeline import L3_SUGGEST, recall_and_compose, triage
from app.stores import kb_db, plan_db, session_db, shop_config

app = FastAPI(title="小咪客服 · Python 版", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    conn = kb_db.connect()
    session_db.init_db(conn)
    kb_db.init_db(conn)
    conn.close()


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=2000)
    ship_status: str = "未发货"
    logistics_status: str = "等待揽件"


class ChatOut(BaseModel):
    session_id: str
    reply: str
    scenario: str
    plan_code: str | None = None
    fallback_level: str
    is_followup: bool = False


@app.get("/health")
def health() -> dict[str, Any]:
    conn = kb_db.connect()
    try:
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {
            "ok": True,
            "retrieval_mode": config.RETRIEVAL_MODE,
            "plans": plan_db.counts(conn),
            "kb": kb_db.stats(conn),
            "sessions": sessions,
        }
    finally:
        conn.close()


class ReindexIn(BaseModel):
    full: bool = False
    include_draft: bool = True


@app.post("/admin/reindex")
def reindex(body: ReindexIn | None = None) -> dict[str, Any]:
    """知识热更新：重建 kb_docs / kb_vec，不发版即生效。"""
    from scripts.build_index import build

    conn = kb_db.connect()
    try:
        stats = build(
            conn,
            full=bool(body.full if body else False),
            include_draft=bool(body.include_draft if body else True),
        )
    finally:
        conn.close()
    return {"ok": True, **stats}


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    conn = kb_db.connect()
    try:
        session_id = body.session_id or uuid.uuid4().hex
        sess = session_db.get_or_create(conn, session_id)
        cfg = shop_config.load()

        result = run_graph(
            conn,
            cfg,
            {
                "session_id": session_id,
                "message": body.message,
                "ship_status": body.ship_status,
                "logistics_status": body.logistics_status,
                "active_scenario": sess["active_scenario"],
                "last_product_id": sess["last_product_id"],
            },
        )

        scenario = result.get("scenario", "none")
        active = scenario if scenario in config.SCENARIOS else sess["active_scenario"]
        offered = list(sess["offered_plans"])
        if result.get("plan_code") and result["plan_code"] not in offered:
            offered.append(result["plan_code"])
        # 商品咨询命中后记住 SKU，后续"那多少钱"这类追问才不会串味
        last_product = result.get("product_id") or sess["last_product_id"]

        session_db.save_session(
            conn,
            session_id,
            active_scenario=active,
            offered_plans=offered,
            pending_slot=sess["pending_slot"],
            last_product_id=last_product,
        )
        session_db.append(conn, session_id, "user", body.message)
        session_db.append(conn, session_id, "assistant", result.get("reply", ""), result.get("plan_code"))

        return ChatOut(
            session_id=session_id,
            reply=result.get("reply", ""),
            scenario=scenario,
            plan_code=result.get("plan_code"),
            fallback_level=result.get("fallback_level", "L0"),
            is_followup=bool(result.get("is_followup", False)),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"内部错误：{e}") from e
    finally:
        conn.close()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunks(text: str, size: int = 10) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]


@app.post("/chat/stream")
def chat_stream(body: ChatIn) -> StreamingResponse:
    """SSE 事件契约：meta → stage → delta → done（见方案 §3.6）。"""

    def gen() -> Iterator[str]:
        conn = kb_db.connect()
        t0 = time.time()
        try:
            session_id = body.session_id or uuid.uuid4().hex
            sess = session_db.get_or_create(conn, session_id)
            cfg = shop_config.load()

            yield _sse(
                "meta",
                {
                    "session_id": session_id,
                    "trace_id": uuid.uuid4().hex,
                    "retrieval_mode": config.RETRIEVAL_MODE,
                },
            )

            yield _sse("stage", {"stage": "triage"})
            t = triage(body.message, sess["active_scenario"])

            scenario = t["scenario"]
            reply = L3_SUGGEST
            plan_code = None
            level = "L3"
            top_score = None
            last_product = sess["last_product_id"]
            if scenario in config.SCENARIOS and float(t.get("confidence") or 0) >= CONF_THRESHOLD:
                yield _sse("stage", {"stage": "recall"})
                out = recall_and_compose(
                    conn,
                    cfg,
                    scenario,
                    body.message,
                    ship_status=body.ship_status,
                    logistics_status=body.logistics_status,
                    session_id=session_id,
                    is_followup=bool(t.get("is_followup", False)),
                    product_id=sess["last_product_id"],
                )
                reply, plan_code, level = out["reply"], out["plan_code"], out["fallback_level"]
                top_score = out.get("score")
                last_product = out.get("product_id") or sess["last_product_id"]

            yield _sse("stage", {"stage": "compose"})
            for chunk in _chunks(reply):
                yield _sse("delta", {"text": chunk})
                time.sleep(0.03)

            active = scenario if scenario in config.SCENARIOS else sess["active_scenario"]
            offered = list(sess["offered_plans"])
            if plan_code and plan_code not in offered:
                offered.append(plan_code)
            session_db.save_session(
                conn,
                session_id,
                active_scenario=active,
                offered_plans=offered,
                pending_slot=sess["pending_slot"],
                last_product_id=last_product,
            )
            session_db.append(conn, session_id, "user", body.message)
            session_db.append(conn, session_id, "assistant", reply, plan_code)

            yield _sse(
                "done",
                {
                    "session_id": session_id,
                    "scenario": scenario,
                    "plan_code": plan_code,
                    "fallback_level": level,
                    "top_score": top_score,
                    "is_followup": bool(t.get("is_followup", False)),
                    "latency_ms": int((time.time() - t0) * 1000),
                },
            )
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"message": f"内部错误：{e}"})
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index() -> FileResponse:
    path = config.ROOT / "web" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="web/index.html 不存在")
    return FileResponse(path)
