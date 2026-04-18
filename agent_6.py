import json
import re

import requests

from tools_3 import build_message_brief
from messages_4 import draft_customer_message
from replies_5 import parse_customer_reply, merge_reply_into_state


ollama_url = "http://localhost:11434/api/generate"
agent_model = "qwen3:8b"
max_iterations = 6


def _has_unparsed_reply(state):
    has_reply = any(d["doc_type"] == "customer_reply" for d in state["documents"])
    has_been_parsed = any(
        t["tool"] == "parse_customer_reply" for t in state.get("tools_used", [])
    )
    return has_reply and not has_been_parsed


def _has_drafted_message_this_turn(state):
    tools = state.get("tools_used", [])
    last_parse_index = -1
    for i, t in enumerate(tools):
        if t["tool"] == "parse_customer_reply":
            last_parse_index = i
    for i, t in enumerate(tools):
        if t["tool"] == "draft_customer_message" and i > last_parse_index:
            return True
    return False


def tool_parse_customer_reply(state):
    parsed = parse_customer_reply(state)
    if parsed is None:
        return {"ok": False, "summary": "no customer reply found"}
    if "error" in parsed:
        return {"ok": False, "summary": f"parser error: {parsed.get('error')}"}

    merge_reply_into_state(state, parsed)

    from tools_3 import decide_status, decide_next_action_type
    status, reason = decide_status(state)
    state["status"] = status
    state["next_action"] = {
        "type": decide_next_action_type(state),
        "message": None,
        "reason": reason,
    }

    summary_bits = []
    provided = parsed.get("provided_fields", {}) or {}
    provided_non_null = [k for k, v in provided.items() if v is not None]
    if provided_non_null:
        summary_bits.append(f"extracted fields: {provided_non_null}")
    if parsed.get("confirmed_conflicts"):
        fields = [c["field"] for c in parsed["confirmed_conflicts"]]
        summary_bits.append(f"customer confirmed: {fields}")
    if parsed.get("customer_questions"):
        summary_bits.append(f"questions: {len(parsed['customer_questions'])}")
    if parsed.get("promised_documents"):
        summary_bits.append(f"promised docs: {[p['document_type'] for p in parsed['promised_documents']]}")

    return {
        "ok": True,
        "summary": "; ".join(summary_bits) if summary_bits else "reply parsed, no new info",
    }


def tool_draft_customer_message(state):
    message = draft_customer_message(state)
    if message is None:
        return {"ok": False, "summary": "no message drafted (action is not message_customer)"}
    state["next_action"]["message"] = message
    return {"ok": True, "summary": f"drafted {len(message.split())} word message"}


def tool_escalate_to_human(state):
    state["next_action"] = {
        "type": "escalate",
        "message": None,
        "reason": state["next_action"]["reason"] if state.get("next_action") else "escalated to human reviewer",
    }
    unresolved = [
        i for i in state["issues"]
        if i["type"] == "inconsistency" and not i.get("customer_confirmed")
    ]
    if unresolved:
        fields = [i["field"] for i in unresolved]
        return {"ok": True, "summary": f"escalated due to conflicts in: {fields}"}
    return {"ok": True, "summary": "escalated to human reviewer"}


def tool_finalize_claim(state):
    state["next_action"] = {
        "type": "finalize",
        "message": None,
        "reason": "all required documents present and all fields resolved",
    }
    return {"ok": True, "summary": "claim marked ready to finalize"}


tool_registry = {
    "parse_customer_reply": {
        "description": "Parse the customer's reply email to extract field values, conflict confirmations, questions, and promised documents. Pick this when a customer_reply document exists in the claim and has not yet been parsed.",
        "function": tool_parse_customer_reply,
    },
    "draft_customer_message": {
        "description": "Write a warm, specific email to the customer describing what's missing, invalid, or conflicting. Pick this when the current action is 'message_customer' and you haven't drafted a message yet this turn. After a reply is parsed, you may draft again in the same turn to respond to the customer's update.",
        "function": tool_draft_customer_message,
    },
    "escalate_to_human": {
        "description": "Mark the claim for human reviewer. Pick this when the current action is 'escalate', meaning there are unresolved document conflicts that the customer cannot reasonably resolve alone (e.g., VIN mismatch across official documents).",
        "function": tool_escalate_to_human,
    },
    "finalize_claim": {
        "description": "Mark the claim as ready to finalize and pay out. Pick this when the current action is 'finalize' and status is 'complete'.",
        "function": tool_finalize_claim,
    },
    "done": {
        "description": "No more tools needed. Pick this when the correct action has been taken for the current claim state, and further tool calls would be redundant.",
        "function": None,
    },
}


def build_agent_state_summary(state):
    return {
        "claim_id": state["claim_id"],
        "status": state["status"],
        "current_action": state["next_action"]["type"] if state.get("next_action") else None,
        "action_reason": state["next_action"]["reason"] if state.get("next_action") else None,
        "documents_present": [d["doc_type"] for d in state["documents"]],
        "missing_required": list(state["missing_required"]),
        "has_unparsed_customer_reply": _has_unparsed_reply(state),
        "has_drafted_message_this_turn": _has_drafted_message_this_turn(state),
        "issues": [
            {
                "type": i["type"],
                "field": i.get("field"),
                "customer_confirmed": i.get("customer_confirmed", False),
            }
            for i in state["issues"]
        ],
        "message_already_drafted": bool(
            state.get("next_action") and state["next_action"].get("message")
        ),
    }


picker_system_prompt = """You are an agent deciding which tool to call next for an insurance claim.

You will be given:
1. A summary of the current claim state
2. A list of available tools with descriptions
3. A list of tools already called this run

Your job: pick EXACTLY ONE tool to call next, OR pick "done" if no more tools are needed.

Return ONLY a JSON object of the form:
{"tool": "tool_name", "reason": "one short sentence explaining why"}

Decision rules:
- If has_unparsed_customer_reply is true, you SHOULD parse it before doing anything else — the reply may change the status and action.
- If the current_action is "message_customer" and has_drafted_message_this_turn is false, you SHOULD draft a message.
- If the current_action is "escalate" and escalate_to_human has not yet been called, you SHOULD escalate.
- If the current_action is "finalize" and finalize_claim has not yet been called, you SHOULD finalize.
- Do NOT call the same tool twice unless a reply was parsed in between (which may require a second message draft).
- Pick "done" only when the correct terminal action for this state has been taken.

Return valid JSON only, no markdown, no preamble.
"""


def strip_thinking(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_picker(state, tools_used):
    state_summary = build_agent_state_summary(state)
    tools_list = {name: t["description"] for name, t in tool_registry.items()}

    prompt = (
        picker_system_prompt
        + "\n\nCurrent claim state summary:\n"
        + json.dumps(state_summary, indent=2)
        + "\n\nAvailable tools:\n"
        + json.dumps(tools_list, indent=2)
        + "\n\nTools already called this run:\n"
        + json.dumps([t["tool"] for t in tools_used], indent=2)
        + "\n\nPick the next tool. /no_think"
    )

    payload = {
        "model": agent_model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    response = requests.post(ollama_url, json=payload, timeout=300)
    response.raise_for_status()
    raw = response.json()["response"]
    cleaned = strip_thinking(raw)
    cleaned = strip_json_fences(cleaned)

    try:
        decision = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"tool": "done", "reason": f"picker returned invalid json; forcing done. raw: {raw[:200]}"}

    tool_name = decision.get("tool", "done")
    if tool_name not in tool_registry:
        return {"tool": "done", "reason": f"picker chose unknown tool '{tool_name}'; forcing done"}

    return {
        "tool": tool_name,
        "reason": decision.get("reason", ""),
    }


def run_agent_loop(state, verbose=False):
    if "tools_used" not in state:
        state["tools_used"] = []

    for iteration in range(1, max_iterations + 1):
        decision = call_picker(state, state["tools_used"])
        tool_name = decision["tool"]

        if verbose:
            print(f"  [iter {iteration}] agent picked: {tool_name}  —  {decision['reason']}")

        if tool_name == "done":
            state["tools_used"].append({
                "tool": "done",
                "reason": decision["reason"],
                "result": "agent ended loop",
            })
            break

        tool_fn = tool_registry[tool_name]["function"]
        result = tool_fn(state)

        state["tools_used"].append({
            "tool": tool_name,
            "reason": decision["reason"],
            "result": result["summary"],
            "ok": result["ok"],
        })

        if verbose:
            print(f"             result: {result['summary']}")
    else:
        if verbose:
            print(f"  [max iterations reached, forcing done]")
        state["tools_used"].append({
            "tool": "done",
            "reason": "max iterations reached",
            "result": "forced termination",
        })

    return state