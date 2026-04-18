import json
import re
from pathlib import Path

from state_2 import (
    add_document,
    add_issue,
    authority_by_doc_type,
    field_authority_order,
    new_claim_state,
    new_document,
    new_issue,
    new_source,
    required_document_types,
    set_field,
    touch,
)


script_dir = Path(__file__).parent
cache_root = script_dir / "cache"


def validate_vin(vin):
    if vin is None:
        return {"valid": False, "reason": "no vin provided"}
    vin = str(vin).strip().upper()
    if len(vin) != 17:
        return {"valid": False, "reason": f"length {len(vin)}, expected 17", "value": vin}
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return {"valid": False, "reason": "invalid characters", "value": vin}
    return {"valid": True, "value": vin}


def load_claim_from_cache(claim_id):
    claim_cache = cache_root / claim_id
    if not claim_cache.exists():
        raise FileNotFoundError(f"no cache for {claim_id} at {claim_cache}. run ingest.py first.")

    state = new_claim_state(claim_id)

    cache_files = sorted(
        f for f in claim_cache.iterdir()
        if f.suffix == ".json" and not f.name.startswith("_")
    )

    for cf in cache_files:
        record = json.loads(cf.read_text(encoding="utf-8"))
        fields = record.get("fields", {})
        doc_type = fields.get("document_type", "unknown") or "unknown"

        flags = fields.get("visual_flags") or fields.get("text_flags") or []

        doc = new_document(
            filename=record["filename"],
            file_hash=record["file_hash"],
            source_kind=record["source_kind"],
            doc_type=doc_type,
            raw_text=record.get("raw_text"),
            fields=fields,
            flags=flags,
        )
        add_document(state, doc)

    return state


def check_completeness(state):
    present_types = {doc["doc_type"] for doc in state["documents"]}
    missing = [t for t in required_document_types if t not in present_types]
    state["missing_required"] = missing
    touch(state)

    for m in missing:
        add_issue(
            state,
            new_issue(
                issue_type="missing",
                description=f"required document not provided: {m}",
                field=None,
            ),
        )
    return missing


def reconcile_same_type_docs(state):
    by_type = {}
    for doc in state["documents"]:
        by_type.setdefault(doc["doc_type"], []).append(doc)

    kept_docs = []
    superseded = []
    duplicates = []

    for doc_type, docs in by_type.items():
        if len(docs) == 1:
            kept_docs.extend(docs)
            continue

        hash_groups = {}
        for d in docs:
            hash_groups.setdefault(d["file_hash"], []).append(d)

        deduped = []
        for h, group in hash_groups.items():
            deduped.append(group[0])
            for extra in group[1:]:
                duplicates.append(extra["filename"])

        if len(deduped) == 1:
            kept_docs.extend(deduped)
            continue

        revised_docs = [
            d for d in deduped
            if "revised_banner" in d.get("flags", []) or "superseded_notice" in d.get("flags", [])
        ]

        if len(revised_docs) == 1:
            winner = revised_docs[0]
            for d in deduped:
                if d["filename"] != winner["filename"]:
                    superseded.append(d["filename"])
            kept_docs.append(winner)
            add_issue(
                state,
                new_issue(
                    issue_type="revision",
                    description=f"{doc_type} was revised; using the revised version",
                    details=f"kept: {winner['filename']}, superseded: {[d['filename'] for d in deduped if d['filename'] != winner['filename']]}",
                    resolution=f"using values from {winner['filename']}",
                ),
            )
        else:
            kept_docs.extend(deduped)
            add_issue(
                state,
                new_issue(
                    issue_type="inconsistency",
                    description=f"multiple {doc_type} documents present with no clear revision marker",
                    details=f"files: {[d['filename'] for d in deduped]}",
                ),
            )

    state["documents"] = kept_docs
    state["superseded_documents"] = superseded
    state["duplicate_documents"] = duplicates
    touch(state)

    return {"kept": len(kept_docs), "superseded": superseded, "duplicates": duplicates}


def collect_field_sources(state, field_name):
    sources = []
    for doc in state["documents"]:
        value = doc["fields"].get(field_name)
        if value is None:
            continue
        sources.append({
            "filename": doc["filename"],
            "doc_type": doc["doc_type"],
            "authority": doc["authority"],
            "value": value,
        })
    return sources


def normalize_date(value):
    if value is None:
        return None
    s = str(value).strip()
    patterns = [
        (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", lambda m: (m.group(1), m.group(2), m.group(3))),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", lambda m: (m.group(3), m.group(1), m.group(2))),
        (r"^(\d{1,2})-(\d{1,2})-(\d{4})$", lambda m: (m.group(3), m.group(1), m.group(2))),
    ]
    for pattern, parts in patterns:
        m = re.fullmatch(pattern, s)
        if m:
            y, mo, d = parts(m)
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return s


def canonicalize(value, field_name):
    if value is None:
        return None
    if field_name == "date_of_loss":
        return normalize_date(value)
    if field_name in ("insurance_payout", "loan_balance"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if field_name == "vin":
        return str(value).strip().upper()
    return value


def values_match(a, b, field_name):
    if a is None or b is None:
        return False
    if field_name in ("insurance_payout", "loan_balance"):
        try:
            return abs(float(a) - float(b)) < 0.01
        except (TypeError, ValueError):
            return str(a).strip() == str(b).strip()
    if field_name == "date_of_loss":
        return normalize_date(a) == normalize_date(b)
    return str(a).strip().upper() == str(b).strip().upper()


def consolidate_field(state, field_name):
    sources = collect_field_sources(state, field_name)

    if not sources:
        set_field(
            state,
            field_name,
            value=None,
            status="missing",
            confidence="n/a",
            sources=[],
            reason="no documents reported this field",
        )
        return

    authoritative = [s for s in sources if s["authority"] == "authoritative"]

    pool = authoritative if authoritative else sources

    value_groups = []
    for s in pool:
        matched = False
        for group in value_groups:
            if values_match(group["value"], s["value"], field_name):
                group["sources"].append(s)
                matched = True
                break
        if not matched:
            value_groups.append({"value": s["value"], "sources": [s]})

    source_records = [
        new_source(s["filename"], s["value"], s["authority"]) for s in sources
    ]

    if len(value_groups) == 1:
        chosen = value_groups[0]
        confidence = "high" if len(chosen["sources"]) >= 2 else "medium"
        pool_label = "authoritative" if authoritative else "reporting"
        set_field(
            state,
            field_name,
            value=canonicalize(chosen["value"], field_name),
            status="present",
            confidence=confidence,
            sources=source_records,
            reason=f"{len(chosen['sources'])} {pool_label} source(s) agree (total sources: {len(sources)})",
        )
        return

    authority_order = field_authority_order.get(field_name, [])

    candidate_groups = value_groups
    format_filter_applied = False
    if field_name == "vin":
        valid_groups = [
            g for g in value_groups
            if validate_vin(g["value"])["valid"]
        ]
        if valid_groups:
            candidate_groups = valid_groups
            format_filter_applied = True

    best_group = None
    best_rank = len(authority_order) + 99
    for group in candidate_groups:
        for src in group["sources"]:
            rank = authority_order.index(src["doc_type"]) if src["doc_type"] in authority_order else 999
            if rank < best_rank:
                best_rank = rank
                best_group = group

    if best_group is None:
        best_group = max(candidate_groups, key=lambda g: len(g["sources"]))

    disagreeing = [
        f"{s['filename']}={s['value']}"
        for g in value_groups if g is not best_group
        for s in g["sources"]
    ]
    agreeing = [s["filename"] for s in best_group["sources"]]

    reason_note = (
        "conflict detected; filtered to format-valid candidates, then chose highest-authority source."
        if format_filter_applied
        else "conflict detected; chose value from highest-authority source."
    )

    set_field(
        state,
        field_name,
        value=canonicalize(best_group["value"], field_name),
        status="present",
        confidence="medium",
        sources=source_records,
        reason=f"{reason_note} agree: {agreeing}. disagree: {disagreeing}",
    )

    add_issue(
        state,
        new_issue(
            issue_type="inconsistency",
            field=field_name,
            description=f"{field_name} values differ across documents",
            details=f"values: {[g['value'] for g in value_groups]}",
            recommended_value=canonicalize(best_group["value"], field_name),
            resolution=f"chose value reported by {agreeing[0]} based on field authority",
        ),
    )


def validate_field_values(state):
    vin_field = state["vin"]
    if vin_field["value"] is not None:
        result = validate_vin(vin_field["value"])
        if not result["valid"]:
            vin_field["status"] = "invalid"
            vin_field["reason"] = (
                (vin_field.get("reason") or "")
                + f" | format check failed: {result['reason']}"
            ).strip(" |")
            add_issue(
                state,
                new_issue(
                    issue_type="invalid",
                    field="vin",
                    description=f"vin format check failed: {result['reason']}",
                    details=f"value: {vin_field['value']}",
                ),
            )

    payout = state["insurance_payout"]
    if payout["value"] is None and any(
        d["doc_type"] == "settlement_breakdown" and "pending_or_tbd_value" in d.get("flags", [])
        for d in state["documents"]
    ):
        payout["status"] = "pending"
        payout["reason"] = "settlement breakdown shows pending/tbd value"
        add_issue(
            state,
            new_issue(
                issue_type="pending",
                field="insurance_payout",
                description="insurance payout is pending on the settlement breakdown",
            ),
        )

    touch(state)


def decide_status(state):
    required_fields = ["vin", "date_of_loss", "insurance_payout", "loan_balance"]

    if state["missing_required"]:
        missing = ", ".join(state["missing_required"])
        return "incomplete", f"missing required document(s): {missing}"

    issue_types = [i["type"] for i in state["issues"]]

    if "invalid" in issue_types:
        invalid_fields = [i["field"] for i in state["issues"] if i["type"] == "invalid"]
        return "needs_review", f"invalid field(s) require human review: {invalid_fields}"

    unresolved_conflicts = [
        i for i in state["issues"]
        if i["type"] == "inconsistency" and not i.get("customer_confirmed")
    ]
    if unresolved_conflicts:
        conflict_fields = [i["field"] for i in unresolved_conflicts]
        return "needs_review", f"unresolved conflict(s) in: {conflict_fields}"

    for field_name in required_fields:
        field = state[field_name]
        if field["status"] in ("missing", "pending"):
            return "incomplete", f"{field_name} is {field['status']}"
        if field["status"] in ("invalid", "unreadable"):
            return "needs_review", f"{field_name} status is {field['status']}"

    low_confidence_fields = [
        f for f in required_fields
        if state[f]["confidence"] == "low"
    ]
    if low_confidence_fields:
        return "needs_review", f"low confidence on: {low_confidence_fields}"

    return "complete", "all required documents present, all fields resolved, no conflicts"


def decide_next_action_type(state):
    if state["status"] == "complete":
        return "finalize"

    if state["status"] == "incomplete":
        return "message_customer"

    if state["status"] == "needs_review":
        has_invalid_field = any(
            state[f]["status"] == "invalid"
            for f in ("vin", "date_of_loss", "insurance_payout", "loan_balance")
        )
        if has_invalid_field:
            return "message_customer"
        return "escalate"

    return "escalate"


def build_message_brief(state):
    brief = {
        "claim_id": state["claim_id"],
        "status": state["status"],
        "claimant_name": None,
        "policy_number": None,
        "missing_documents": list(state["missing_required"]),
        "promised_documents": [],
        "invalid_fields": [],
        "pending_fields": [],
        "conflicts": [],
        "customer_confirmed_conflicts": [],
        "pending_customer_questions": list(state["pending_customer_questions"]),
        "last_agent_message": None,
    }

    for doc in state["documents"]:
        if brief["claimant_name"] is None:
            name = doc["fields"].get("claimant_name")
            if name:
                brief["claimant_name"] = name
        if brief["policy_number"] is None:
            policy = doc["fields"].get("policy_number")
            if policy and str(policy).startswith("AUT"):
                brief["policy_number"] = policy

    for field_name in ("vin", "date_of_loss", "insurance_payout", "loan_balance"):
        field = state[field_name]
        if field["status"] == "invalid":
            brief["invalid_fields"].append({
                "field": field_name,
                "value": field["value"],
                "reason": field.get("reason"),
            })
        if field["status"] == "pending":
            brief["pending_fields"].append({
                "field": field_name,
                "reason": field.get("reason"),
            })

    for issue in state["issues"]:
        if issue["type"] == "inconsistency":
            if issue.get("customer_confirmed"):
                brief["customer_confirmed_conflicts"].append({
                    "field": issue["field"],
                    "confirmed_value": issue.get("recommended_value"),
                })
            else:
                brief["conflicts"].append({
                    "field": issue["field"],
                    "recommended_value": issue.get("recommended_value"),
                    "description": issue["description"],
                })
        if issue["type"] == "pending" and "promised to send" in (issue.get("description") or ""):
            description = issue.get("description", "")
            doc_type = description.replace("customer has promised to send ", "").strip()
            brief["promised_documents"].append({
                "document_type": doc_type,
                "note": issue.get("details"),
            })

    if state["turn_history"]:
        last_turn = state["turn_history"][-1]
        brief["last_agent_message"] = last_turn.get("agent_message")

    return brief


def process_claim(claim_id):
    state = load_claim_from_cache(claim_id)
    reconcile_same_type_docs(state)
    check_completeness(state)
    for field in ("vin", "date_of_loss", "insurance_payout", "loan_balance"):
        consolidate_field(state, field)
    validate_field_values(state)
    status, reason = decide_status(state)
    state["status"] = status
    state["next_action"] = {
        "type": decide_next_action_type(state),
        "message": None,
        "reason": reason,
    }
    return state