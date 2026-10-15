"""准备好资料后的规划子图。节点显式生成、覆盖修复、门禁和有限重排。

不包含浏览器采集、地图请求、报告写出或索引。SQLite检查点以稳定run_id为thread_id。
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from core.model_calls import FatalModelError, request_json
from core.planning_runs import BudgetExhausted, InvalidPlanError, RunCancelled, RunError
from pipeline import planner
from pipeline.planning_policy import evaluate, prefer, restore_quality
from pipeline.qc import finalize


class PlanningState(TypedDict, total=False):
    run_id: str
    outer_round: int
    extra_issues: list[str]
    raw: dict
    base_plan: dict
    base_issues: list[str]
    second_raw: dict
    candidate: dict
    assessed: dict
    best: dict
    accepted: bool
    stop_reason: str
    complete: bool


def build_graph(store, rid, saver, *, client_factory=None, pause_after=None, hook=None):
    row = store.validate(rid)
    s, policy = row["snapshot"], row["policy"]
    profiles = s["profiles"]
    user, banks = planner.prepare_plan_request(
        s["city"], s["days"], s["hotel"], profiles, s.get("travel_lines") or [], s.get("preferences") or "",
        s.get("preference_mode") or "均衡", heat_summary=s.get("heat_summary") or "",
        guide_hints=s.get("guide_hints"), draft_plan=s.get("draft_plan"),
        points_by_spot=s.get("spot_points"))
    if policy == "hard-first-v1" and s.get("constraints", {}).get("excluded_spots"):
        user += "\n用户明确排除（硬约束）：" + "、".join(s["constraints"]["excluded_spots"])

    def guarded(name, func):
        def node(state):
            store.guard(rid)
            store.put_step(rid, "current_node", name)
            store.event(rid, name, "node_started")
            update = func(state)
            store.event(rid, name, "node_completed")
            if hook:
                hook(name, {**state, **update})
            return update
        return node

    def call(state, name, prompt):
        return request_json(store, rid, name, f"round-{state['outer_round']}:{name}",
                            planner.PLAN_SYSTEM, prompt, client_factory=client_factory)

    def post(plan):
        return planner.rebalance_days(planner._filter_fabricated_food(deepcopy(plan), set()), profiles, s["days"])

    def generate(state):
        return {"raw": call(state, "generate", user)}

    def normalize(state):
        plan = planner._normalize_plan(state["raw"], set(profiles), s["days"], banks)
        return {"base_plan": plan, "base_issues": planner._coverage_issues(plan, profiles, s["days"])}

    def coverage_repair(state):
        note = ("\n\n上一版规划存在以下问题，本版必须修正：\n" +
                "\n".join(f"- {x}" for x in state["base_issues"] + state["extra_issues"]))
        return {"second_raw": call(state, "coverage_repair", user + note)}

    def select_coverage(state):
        second = planner._normalize_plan(state["second_raw"], set(profiles), s["days"], banks)
        count = len(planner._coverage_issues(second, profiles, s["days"]))
        why = "coverage_not_improved"
        if policy == "hard-first-v1":
            adopt, why = prefer(evaluate(post(state["base_plan"]), s, policy),
                                evaluate(post(second), s, policy), policy)
        else:
            adopt = count < len(state["base_issues"]) or (state["extra_issues"] and count <= len(state["base_issues"]))
            if adopt:
                why = "legacy_coverage"
        store.event(rid, "select_coverage", "candidate_compared", why, "adopt" if adopt else "reject")
        return {"base_plan": second if adopt else state["base_plan"]}

    def finish_candidate(state):
        plan = post(state["base_plan"])
        if not plan.get("days") and policy == "legacy-v1":
            raise RunError("规划生成失败：未产出有效行程")
        return {"candidate": plan}

    def quality(state):
        return {"assessed": evaluate(state["candidate"], s, policy)}

    def compare(state):
        if policy == "hard-first-v1" and not state["assessed"]["constraints"]["contract_valid"]:
            store.put_step(rid, "latest_rejected", state["assessed"])
            store.event(rid, "compare", "candidate_compared", "invalid_contract", "reject")
            return {"accepted": False, "stop_reason": "invalid_contract"}
        if not state.get("best"):
            store.put_step(rid, "latest_best", state["assessed"])
            return {"best": state["assessed"], "accepted": True, "stop_reason": ""}
        adopt, reason = prefer(state["best"], state["assessed"], policy)
        store.event(rid, "compare", "candidate_compared", reason, "adopt" if adopt else "reject")
        if adopt:
            store.put_step(rid, "latest_best", state["assessed"])
        return {"best": state["assessed"] if adopt else state["best"], "accepted": adopt,
                "stop_reason": "" if adopt else reason}

    def needs_outer(state):
        if not state.get("best"):
            return "begin_outer" if state["outer_round"] < s.get("repair_rounds", 1) else "finish"
        best = state["best"]
        fails = any(c["status"] == "fail" for c in best["quality"]["checks"])
        if policy == "hard-first-v1":
            fails = fails or bool(best["constraints"]["hard_ids"])
        if state["accepted"] and fails and state["outer_round"] < s.get("repair_rounds", 1):
            return "begin_outer"
        return "finish"

    def begin_outer(state):
        reference = state.get("best") or state["assessed"]
        extra = list(reference["quality"]["issues"])
        if policy == "hard-first-v1":
            extra += [x["message"] for x in reference["constraints"]["issues"]]
        return {"outer_round": state["outer_round"] + 1, "extra_issues": extra}

    def finish(state):
        if not state.get("best"):
            raise InvalidPlanError("有限修复后仍未生成符合天数、点位与结构契约的行程；请调整需求后新建任务")
        best = deepcopy(state["best"])
        report = restore_quality(best["quality"])
        finalize(report, repair_rounds=state["outer_round"])
        best["quality"] = report.to_dict()
        unresolved = report.fails or (policy == "hard-first-v1" and best["constraints"]["hard_ids"])
        reason = state.get("stop_reason") or ("repair_limit" if unresolved else "quality_complete")
        return {"best": best, "complete": True, "stop_reason": reason}

    graph = StateGraph(PlanningState)
    for name, node in (("generate", generate), ("normalize", normalize),
                       ("coverage_repair", coverage_repair), ("select_coverage", select_coverage),
                       ("finish_candidate", finish_candidate), ("quality", quality),
                       ("compare", compare), ("begin_outer", begin_outer), ("finish", finish)):
        graph.add_node(name, guarded(name, node))
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "normalize")
    graph.add_conditional_edges("normalize", lambda st: "coverage_repair" if st["base_issues"] or
                               st["extra_issues"] else "finish_candidate")
    graph.add_edge("coverage_repair", "select_coverage")
    graph.add_edge("select_coverage", "finish_candidate")
    graph.add_edge("finish_candidate", "quality")
    graph.add_edge("quality", "compare")
    graph.add_conditional_edges("compare", needs_outer)
    graph.add_edge("begin_outer", "generate")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver, interrupt_after=pause_after)


def execute(store, rid, *, client_factory=None, pause_after=None, hook=None):
    """调用方须持有同run lease，覆盖整个图和后处理。"""
    row = store.validate(rid)
    if row["status"] in {"cancelled", "budget_exhausted", "fatal", "invalid_plan"}:
        raise RunError("该run已停止，恢复不重置取消、模型错误或预算状态")
    saved = store.step(rid, "graph_output")
    if saved is not None:
        return saved
    connection = sqlite3.connect(store.checkpoint_path, timeout=30, check_same_thread=False)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        saver = SqliteSaver(connection)
        graph = build_graph(store, rid, saver, client_factory=client_factory,
                            pause_after=pause_after, hook=hook)
        config = {"configurable": {"thread_id": rid}, "recursion_limit": 100}
        previous = graph.get_state(config)
        store.update(rid, status="running", stop_reason=None)
        if previous.values and not previous.next:
            output = previous.values
        else:
            initial = None if previous.values else {"run_id": rid, "outer_round": 0,
                                                     "extra_issues": [], "complete": False}
            output = graph.invoke(initial, config, durability="sync")
        if output.get("complete"):
            store.put_step(rid, "graph_output", output)
            store.update(rid, status="graph_complete", stop_reason=output["stop_reason"])
        else:
            store.update(rid, status="paused", stop_reason="checkpoint_pause")
        return output
    except RunCancelled:
        store.update(rid, status="cancelled", stop_reason="cancel_requested")
        raise
    except BudgetExhausted:
        store.update(rid, status="budget_exhausted", stop_reason="http_attempt_budget_exhausted")
        raise
    except FatalModelError:
        store.update(rid, status="fatal", stop_reason="model_configuration_error")
        raise
    except InvalidPlanError:
        store.update(rid, status="invalid_plan", stop_reason="invalid_contract")
        raise
    except Exception as exc:
        store.update(rid, status="failed", stop_reason=type(exc).__name__)
        raise
    finally:
        connection.close()
