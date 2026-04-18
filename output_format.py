import json


def primary_source(field):
    sources = field.get("sources") or []
    if not sources:
        return None

    authority_rank = {"authoritative": 0, "supporting": 1, "customer_reply": 2}
    ranked = sorted(sources, key=lambda s: authority_rank.get(s.get("authority"), 99))
    return ranked[0]["filename"]


def format_field(field):
    return {
        "value": field.get("value"),
        "confidence": field.get("confidence"),
        "source": primary_source(field),
        "reason": field.get("reason"),
    }


def format_issue(issue):
    out = {
        "type": issue["type"],
        "description": issue["description"],
    }
    if issue.get("details"):
        out["details"] = issue["details"]
    if issue.get("field"):
        out["field"] = issue["field"]
    if issue.get("recommended_value") is not None:
        out["recommended_value"] = issue["recommended_value"]
    if issue.get("resolution"):
        out["resolution"] = issue["resolution"]
    if issue.get("customer_confirmed"):
        out["customer_confirmed"] = True
    return out


def format_document_list(state):
    identified = []
    for doc in state["documents"]:
        identified.append({
            "file": doc["filename"],
            "type": doc["doc_type"],
            "authority": doc.get("authority"),
        })
    return {
        "identified": identified,
        "missing": list(state["missing_required"]),
        "duplicates": list(state.get("duplicate_documents", [])),
        "superseded": list(state.get("superseded_documents", [])),
    }


def format_tools_used(state):
    out = []
    for entry in state.get("tools_used", []):
        out.append({
            "tool": entry["tool"],
            "reason": entry.get("reason"),
            "result": entry.get("result"),
        })
    return out


def format_turn_history(state):
    out = []
    for turn in state.get("turn_history", []):
        out.append({
            "turn": turn.get("turn"),
            "input_kind": turn.get("input_kind"),
            "status_at_end": turn.get("status_at_end"),
            "agent_message": turn.get("agent_message"),
        })
    return out


def to_output_format(state):
    next_action = state.get("next_action") or {}
    return {
        "claim_id": state["claim_id"],
        "status": state["status"],
        "extracted_fields": {
            "vin": format_field(state["vin"]),
            "date_of_loss": format_field(state["date_of_loss"]),
            "insurance_payout": format_field(state["insurance_payout"]),
            "loan_balance": format_field(state["loan_balance"]),
        },
        "documents": format_document_list(state),
        "issues": [format_issue(i) for i in state["issues"]],
        "next_action": {
            "type": next_action.get("type"),
            "message": next_action.get("message"),
            "reason": next_action.get("reason"),
        },
        "pending_customer_questions": list(state.get("pending_customer_questions", [])),
        "tools_used": format_tools_used(state),
        "turn_history": format_turn_history(state),
    }


def to_output_json(state):
    return json.dumps(to_output_format(state), indent=2)
