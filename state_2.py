from datetime import datetime


required_document_types = [
    "police_report",
    "finance_agreement",
    "settlement_breakdown",
]


authority_by_doc_type = {
    "police_report": "authoritative",
    "finance_agreement": "authoritative",
    "settlement_breakdown": "authoritative",
    "adjuster_note": "supporting",
    "tow_receipt": "supporting",
    "customer_reply": "customer_reply",
    "unknown": "supporting",
}


field_authority_order = {
    "vin": ["police_report", "settlement_breakdown", "finance_agreement"],
    "date_of_loss": ["police_report", "settlement_breakdown", "finance_agreement"],
    "insurance_payout": ["settlement_breakdown"],
    "loan_balance": ["finance_agreement", "settlement_breakdown"],
}


def now():
    return datetime.now().isoformat(timespec="seconds")


def new_document(filename, file_hash, source_kind, doc_type, raw_text, fields, flags):
    return {
        "filename": filename,
        "file_hash": file_hash,
        "source_kind": source_kind,
        "doc_type": doc_type,
        "authority": authority_by_doc_type.get(doc_type, "supporting"),
        "raw_text": raw_text,
        "fields": fields,
        "flags": flags,
    }


def new_field():
    return {
        "value": None,
        "status": "missing",
        "confidence": "n/a",
        "sources": [],
        "reason": None,
    }


def new_source(filename, value, authority):
    return {
        "filename": filename,
        "value": value,
        "authority": authority,
    }


def new_issue(issue_type, description, field=None, details=None, recommended_value=None, resolution=None):
    return {
        "type": issue_type,
        "field": field,
        "description": description,
        "details": details,
        "recommended_value": recommended_value,
        "resolution": resolution,
    }


def new_turn_record(turn_number, input_kind, input_summary, agent_message, status_at_end):
    return {
        "turn": turn_number,
        "timestamp": now(),
        "input_kind": input_kind,
        "input_summary": input_summary,
        "agent_message": agent_message,
        "status_at_end": status_at_end,
    }


def new_claim_state(claim_id):
    return {
        "claim_id": claim_id,
        "created_at": now(),
        "updated_at": now(),

        "documents": [],
        "superseded_documents": [],
        "duplicate_documents": [],

        "vin": new_field(),
        "date_of_loss": new_field(),
        "insurance_payout": new_field(),
        "loan_balance": new_field(),

        "missing_required": [],
        "issues": [],
        "pending_customer_questions": [],

        "turn_history": [],

        "status": "incomplete",
        "next_action": None,
    }


def touch(state):
    state["updated_at"] = now()


def set_field(state, field_name, value=None, status="missing", confidence="n/a", sources=None, reason=None):
    state[field_name] = {
        "value": value,
        "status": status,
        "confidence": confidence,
        "sources": sources if sources is not None else [],
        "reason": reason,
    }
    touch(state)


def add_document(state, document):
    state["documents"].append(document)
    touch(state)


def add_issue(state, issue):
    state["issues"].append(issue)
    touch(state)


def set_status(state, status, next_action=None):
    state["status"] = status
    state["next_action"] = next_action
    touch(state)


def add_turn(state, turn_record):
    state["turn_history"].append(turn_record)
    touch(state)