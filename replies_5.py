import json
import re

import requests

from tools_3 import build_message_brief, values_match, normalize_date, validate_vin


ollama_url = "http://localhost:11434/api/generate"
parser_model = "qwen3:8b"


parser_system_prompt = """You are an assistant that reads a customer's reply to an insurance claim inquiry and extracts structured information.

You will be given:
1. A "brief" describing what was outstanding on the claim when we wrote to the customer
2. The customer's reply text

Extract the following and return ONLY a JSON object (no markdown fences, no preamble):

{
  "provided_fields": {
    "vin": null or "exact VIN string they provided",
    "date_of_loss": null or "YYYY-MM-DD",
    "insurance_payout": null or numeric,
    "loan_balance": null or numeric,
    "lender_name": null or "string",
    "policy_number": null or "string"
  },
  "confirmed_conflicts": [
    {"field": "date_of_loss", "customer_value": "2026-03-22", "note": "customer explicitly corrected the date"}
  ],
  "customer_questions": [
    "can you contact my lender directly?"
  ],
  "promised_documents": [
    {"document_type": "police_report", "note": "customer will send by next weekend"}
  ]
}

Rules:
- For provided_fields, ONLY include values the customer states as a definite answer. If they hedge ("I believe it's around X", "roughly X", "maybe a little more"), set that field to null. Vague estimates must not populate provided_fields.
- confirmed_conflicts should list fields where the customer explicitly addresses a conflict we flagged. Include their stated value.
- customer_questions are direct questions from the customer that we need to respond to (not rhetorical).
- promised_documents: include any document_type the customer says they will send, are sending, are attaching, are about to send, or have just found. Informal phrasings count: "attaching it now", "I'll send it today", "I just found it", "sending in a sec", "here's my X", "I have it, let me scan it". ALL of these mean the document is promised but not yet received. Always add it to promised_documents.
- If a field is not mentioned at all, use null.
- Do NOT invent values. Do NOT include every number mentioned in the reply — only ones the customer is stating as a firm answer to what we asked.
- ONLY use information that appears in the customer reply text below. Do NOT add information from the brief or from other claims.
- Return valid JSON only.
"""


def strip_thinking(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_reply_parser(brief, reply_text):
    prompt = (
        parser_system_prompt
        + "\n\nBrief of what's outstanding on this claim:\n"
        + json.dumps(brief, indent=2)
        + "\n\nCustomer reply text:\n"
        + reply_text
        + "\n\nReturn the JSON now. /no_think"
    )

    payload = {
        "model": parser_model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    response = requests.post(ollama_url, json=payload, timeout=300)
    response.raise_for_status()
    return response.json()["response"]


def parse_customer_reply(state):
    reply_doc = None
    for doc in state["documents"]:
        if doc["doc_type"] == "customer_reply":
            reply_doc = doc
            break

    if reply_doc is None:
        return None

    if reply_doc.get("raw_text") is None:
        return None

    brief = build_message_brief(state)
    raw = call_reply_parser(brief, reply_doc["raw_text"])
    cleaned = strip_thinking(raw)
    cleaned = strip_json_fences(cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "error": "json_parse_failed",
            "raw": raw,
            "reply_filename": reply_doc["filename"],
        }

    parsed["_reply_filename"] = reply_doc["filename"]
    return parsed


def merge_reply_into_state(state, parsed_reply):
    if parsed_reply is None or "error" in parsed_reply:
        return

    provided = parsed_reply.get("provided_fields", {}) or {}
    reply_filename = parsed_reply.get("_reply_filename", "customer_reply")

    for field_name, value in provided.items():
        if value is None:
            continue
        if field_name not in ("vin", "date_of_loss", "insurance_payout", "loan_balance"):
            continue

        current = state[field_name]
        current_value = current.get("value")

        if field_name == "vin":
            matches = values_match(current_value, value, "vin")
            is_valid_new = validate_vin(value).get("valid", False)
            if current_value is None and is_valid_new:
                state[field_name] = {
                    "value": str(value).strip().upper(),
                    "status": "present",
                    "confidence": "medium",
                    "sources": [{"filename": reply_filename, "value": value, "authority": "customer_reply"}],
                    "reason": "value provided by customer in reply",
                }
            elif not matches and current.get("status") == "invalid" and is_valid_new:
                state[field_name] = {
                    "value": str(value).strip().upper(),
                    "status": "present",
                    "confidence": "medium",
                    "sources": current.get("sources", []) + [
                        {"filename": reply_filename, "value": value, "authority": "customer_reply"}
                    ],
                    "reason": "customer provided valid vin replacing invalid document value",
                }
        elif field_name == "date_of_loss":
            customer_date = normalize_date(value)
            if customer_date and current_value is None:
                state[field_name] = {
                    "value": customer_date,
                    "status": "present",
                    "confidence": "medium",
                    "sources": [{"filename": reply_filename, "value": value, "authority": "customer_reply"}],
                    "reason": "date provided by customer in reply",
                }

    for conflict in parsed_reply.get("confirmed_conflicts", []) or []:
        field = conflict.get("field")
        customer_value = conflict.get("customer_value")
        if not field or customer_value is None:
            continue

        for issue in state["issues"]:
            if issue["type"] == "inconsistency" and issue.get("field") == field:
                issue["resolution"] = (
                    (issue.get("resolution") or "")
                    + f" | customer confirmed value: {customer_value}"
                ).strip(" |")
                issue["customer_confirmed"] = True

        current = state[field]
        canonicalized = customer_value
        if field == "date_of_loss":
            canonicalized = normalize_date(customer_value) or customer_value

        if current.get("status") == "present":
            current["reason"] = (
                (current.get("reason") or "")
                + f" | customer confirmed: {canonicalized}"
            ).strip(" |")
            current["confidence"] = "high"
            current["sources"] = current.get("sources", []) + [
                {"filename": reply_filename, "value": customer_value, "authority": "customer_reply"}
            ]

    for question in parsed_reply.get("customer_questions", []) or []:
        if question and question not in state["pending_customer_questions"]:
            state["pending_customer_questions"].append(question)

    for promise in parsed_reply.get("promised_documents", []) or []:
        doc_type = promise.get("document_type")
        note = promise.get("note", "")
        if doc_type:
            state["issues"].append({
                "type": "pending",
                "field": None,
                "description": f"customer has promised to send {doc_type}",
                "details": note,
                "recommended_value": None,
                "resolution": None,
            })


def process_customer_reply(state):
    parsed = parse_customer_reply(state)
    if parsed is None:
        return None

    merge_reply_into_state(state, parsed)

    from tools_3 import decide_status, decide_next_action_type

    status, reason = decide_status(state)
    state["status"] = status
    state["next_action"] = {
        "type": decide_next_action_type(state),
        "message": None,
        "reason": reason,
    }

    return parsed