"""单轮问答主链路：分诊 → 召回（plans 或 vector）→ 取答案原文 → 渲染。"""
import json
from typing import Any

from jinja2 import Template

from app import config
from app.llm.client import chat_json, chat_text
from app.stores import kb_db, plan_db, session_db, shop_config
from app.tools.order import get_order

SYSTEM = "你是电商客服系统的执行器，只输出符合要求的 JSON 或纯文本。"
L3_SUGGEST = "听起来您想咨询的问题我还没完全理解～可以说一下是发货、支付、退换货还是商品本身的问题吗？"
L4_HANDOFF = "这个问题我还在确认中，稍后由人工帮您跟进～"
MID_CONF_SUFFIX = "如果和您的情况不完全一致，把订单号发我，我帮您核实。"
MID_CONF_SUFFIX_PRODUCT = "如果和您家的情况不完全一致，告诉我具体场景，我再帮您判断。"


def _prompt(name: str, **ctx: Any) -> str:
    return Template((config.PROMPTS_DIR / name).read_text(encoding="utf-8")).render(**ctx)


def triage(message: str, active: str | None = None) -> dict:
    out = chat_json(
        SYSTEM,
        _prompt(
            "triage.j2",
            history=f"当前场景={active}" if active else "（无）",
            active_scenario=active or "无",
            message=message,
        ),
    )
    scenario = out.get("scenario", "none")
    if scenario not in config.SCENARIOS or float(out.get("confidence", 0) or 0) < 0.5:
        scenario = "none"
    out["scenario"] = scenario
    return out


def select_plan(conn, scenario: str, message: str) -> str | None:
    plans = plan_db.list_scenario(conn, scenario)
    out = chat_json(
        SYSTEM,
        _prompt("select_plan.j2", plans=plans, scenario=scenario, message=message),
    )
    code = out.get("code")
    return code if code in {p["code"] for p in plans} else None


def select_pair(candidates: list[dict], scenario: str, message: str) -> str | None:
    """分差不足时让模型在 top-5 里选号；只输出 pair_id，答案仍由代码取原文。"""
    out = chat_json(
        SYSTEM,
        _prompt("select_pair.j2", candidates=candidates, scenario=scenario, message=message),
    )
    pair_id = out.get("pair_id")
    return pair_id if pair_id in {c["pair_id"] for c in candidates} else None


def pick_branch(content: str, ship_status: str, logistics_status: str) -> str:
    return chat_text(
        SYSTEM,
        _prompt(
            "branch_pick.j2",
            content=content,
            ship_status=ship_status,
            logistics_status=logistics_status,
        ),
        temperature=0.0,
    )


def _ask_instead(candidates: list[dict]) -> str:
    """低分或跨商品歧义时，把相似问题列出来让用户选，不硬答。"""
    questions = [c["canonical_question"] for c in candidates[:3]]
    lines = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return f"我想确认一下您问的是哪个：\n{lines}\n也可以再补充一句具体情况～"


def _conditions_match(conditions: str | None, order) -> bool:
    if not conditions:
        return True
    wanted = json.loads(conditions)
    actual = {
        "ship_status": order.ship_status,
        "logistics_status": order.logistics_status,
        "pay_status": order.pay_status,
    }
    return all(actual.get(k) == v for k, v in wanted.items() if k in actual)


def _recall_vector(
    conn,
    cfg: dict,
    scenario: str,
    message: str,
    *,
    session_id: str | None,
    is_followup: bool,
    product_id: str | None,
    ship_status: str,
    logistics_status: str,
) -> dict:
    query = message
    if is_followup and session_id:
        if scenario == "product" and product_id:
            # 商品追问：“那能坐多重” → 补上商品名，避免上轮话题把向量带偏
            name = kb_db.product_names().get(product_id)
            if name and not any(a in message for a in kb_db.product_aliases()):
                query = f"{name} {message}"
        else:
            context = session_db.last_user(conn, session_id)
            if context and context != message:
                query = f"{context[:40]} {message}"  # 追问改写：拼上轮主题，避免孤立短句

    candidates = kb_db.search(conn, query, scenario)
    if not candidates:
        return {"plan_code": None, "fallback_level": "L3", "reply": L3_SUGGEST}

    top = candidates[0]
    if top["score"] < kb_db.threshold(scenario):
        return {"plan_code": None, "fallback_level": "L2", "reply": _ask_instead(candidates)}

    gap = top["score"] - (candidates[1]["score"] if len(candidates) > 1 else 0.0)

    if scenario == "product":
        if product_id:
            same = [c for c in candidates if c["product_id"] == product_id]
            if same and same[0]["score"] >= top["score"] - 0.05:
                candidates = same + [c for c in candidates if c["product_id"] != product_id]
                top = candidates[0]
                gap = top["score"] - (
                    candidates[1]["score"] if len(candidates) > 1 else 0.0
                )
        elif gap < kb_db.GAP and len(
            {c["product_id"] for c in candidates[:3] if c["product_id"]}
        ) > 1:
            # 没上下文、又跨多个商品且分差极小：先澄清，避免答错 SKU
            return {"plan_code": None, "fallback_level": "L2", "reply": _ask_instead(candidates)}

    if gap >= kb_db.GAP:
        pair_id = top["pair_id"]
    else:
        pair_id = select_pair(candidates[: kb_db.TOP_N], scenario, message)
    if not pair_id:
        return {"plan_code": None, "fallback_level": "L4", "reply": L4_HANDOFF}

    pair = kb_db.get_pair(conn, pair_id)
    if not pair:
        return {"plan_code": None, "fallback_level": "L4", "reply": L4_HANDOFF}

    if pair["conditions"]:
        order = get_order(ship_status=ship_status, logistics_status=logistics_status)
        if not _conditions_match(pair["conditions"], order):
            return {"plan_code": None, "fallback_level": "L4", "reply": L4_HANDOFF}

    reply = shop_config.render(pair["answer"], cfg)
    if top["score"] < kb_db.MID_CONFIDENCE:
        suffix = MID_CONF_SUFFIX_PRODUCT if scenario == "product" else MID_CONF_SUFFIX
        reply = f"{reply}{suffix}"
    return {
        "plan_code": pair_id,
        "product_id": pair["product_id"],
        "score": top["score"],
        "fallback_level": "L0",
        "reply": reply,
    }


def recall_and_compose(
    conn,
    cfg: dict,
    scenario: str,
    message: str,
    *,
    ship_status: str = "未发货",
    logistics_status: str = "等待揽件",
    session_id: str | None = None,
    is_followup: bool = False,
    product_id: str | None = None,
) -> dict:
    """召回 → 取答案原文 → 渲染。返回 plan_code / fallback_level / reply。"""
    if config.RETRIEVAL_MODE == "vector":
        return _recall_vector(
            conn,
            cfg,
            scenario,
            message,
            session_id=session_id,
            is_followup=is_followup,
            product_id=product_id,
            ship_status=ship_status,
            logistics_status=logistics_status,
        )

    code = select_plan(conn, scenario, message)
    if not code:
        return {"plan_code": None, "fallback_level": "L3", "reply": L3_SUGGEST}

    plan = plan_db.get_plan(conn, code)
    if not plan or plan["todo"] or not plan["content"].strip():
        return {"plan_code": code, "fallback_level": "L4", "reply": L4_HANDOFF}

    if plan["has_branches"]:
        order = get_order(ship_status=ship_status, logistics_status=logistics_status)
        reply = pick_branch(plan["content"], order.ship_status, order.logistics_status)
        if reply.strip() == "NO_MATCH":
            return {"plan_code": code, "fallback_level": "L4", "reply": L4_HANDOFF}
    else:
        reply = plan["content"]

    return {
        "plan_code": code,
        "fallback_level": "L0",
        "reply": shop_config.render(reply, cfg),
    }


def answer(
    message: str,
    *,
    ship_status: str = "未发货",
    logistics_status: str = "等待揽件",
    active: str | None = None,
    session_id: str | None = None,
    product_id: str | None = None,
    conn=None,
    cfg: dict | None = None,
) -> dict:
    conn = conn or kb_db.connect()
    cfg = cfg or shop_config.load()

    t = triage(message, active)
    result = {
        "scenario": t["scenario"],
        "confidence": t.get("confidence"),
        "is_followup": t.get("is_followup", False),
        "plan_code": None,
        "fallback_level": "L3",
        "reply": L3_SUGGEST,
    }
    if t["scenario"] == "none":
        return result

    result.update(
        recall_and_compose(
            conn,
            cfg,
            t["scenario"],
            message,
            ship_status=ship_status,
            logistics_status=logistics_status,
            session_id=session_id,
            is_followup=bool(t.get("is_followup", False)),
            product_id=product_id,
        )
    )
    return result
