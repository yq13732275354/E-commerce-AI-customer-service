"""LangGraph 编排：分诊 → 召回/渲染（或兜底）。节点复用 pipeline 的确定性函数。"""
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app import config
from app.pipeline import L3_SUGGEST, recall_and_compose, triage

CONF_THRESHOLD = 0.5


class ChatState(TypedDict, total=False):
    session_id: str
    message: str
    ship_status: str
    logistics_status: str
    active_scenario: str | None
    last_product_id: str | None
    scenario: str
    confidence: float
    is_followup: bool
    plan_code: str | None
    product_id: str | None
    fallback_level: str
    reply: str


def build_graph(conn, cfg: dict):
    def node_triage(state: ChatState) -> dict:
        t = triage(state["message"], state.get("active_scenario"))
        return {
            "scenario": t["scenario"],
            "confidence": t.get("confidence", 0.0),
            "is_followup": bool(t.get("is_followup", False)),
        }

    def node_recall(state: ChatState) -> dict:
        return recall_and_compose(
            conn,
            cfg,
            state["scenario"],
            state["message"],
            ship_status=state.get("ship_status", "未发货"),
            logistics_status=state.get("logistics_status", "等待揽件"),
            session_id=state.get("session_id"),
            is_followup=bool(state.get("is_followup", False)),
            product_id=state.get("last_product_id"),
        )

    def node_fallback(state: ChatState) -> dict:
        return {"plan_code": None, "fallback_level": "L3", "reply": L3_SUGGEST}

    g = StateGraph(ChatState)
    g.add_node("triage", node_triage)
    g.add_node("recall", node_recall)
    g.add_node("fallback", node_fallback)
    g.set_entry_point("triage")

    def route_after_triage(state: ChatState) -> str:
        if state["scenario"] in config.SCENARIOS and float(state.get("confidence") or 0) >= CONF_THRESHOLD:
            return "recall"
        return "fallback"

    g.add_conditional_edges("triage", route_after_triage, {"recall": "recall", "fallback": "fallback"})
    # 召回必然产出回复（L0 / L2 澄清 / L3 建议 / L4 转人工），不再二次兜底覆盖
    g.add_edge("recall", END)
    g.add_edge("fallback", END)
    return g.compile()


def run(conn, cfg: dict, state: ChatState) -> ChatState:
    return build_graph(conn, cfg).invoke(state)
