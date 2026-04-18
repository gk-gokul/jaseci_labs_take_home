import json
import sys
from pathlib import Path

from tools_3 import process_claim
from agent_6 import run_agent_loop
from replies_5 import parse_customer_reply, merge_reply_into_state
from messages_4 import draft_customer_message
from output_format import to_output_format


script_dir = Path(__file__).parent
output_root = script_dir / "output"
claim_ids = ["CLM-001", "CLM-002", "CLM-003", "CLM-004", "CLM-005"]


def save_claim_output(state):
    output_root.mkdir(parents=True, exist_ok=True)
    out_path = output_root / f"{state['claim_id']}.json"
    formatted = to_output_format(state)
    out_path.write_text(json.dumps(formatted, indent=2), encoding="utf-8")
    return out_path


def append_turn(state, turn_number, input_kind, input_summary, agent_message):
    state["turn_history"].append({
        "turn": turn_number,
        "timestamp": state["updated_at"],
        "input_kind": input_kind,
        "input_summary": input_summary,
        "agent_message": agent_message,
        "status_at_end": state["status"],
    })


def print_header(text):
    print(f"\n{'=' * 70}")
    print(f"  {text}")
    print('=' * 70)


def print_indented(text, prefix="    "):
    for line in text.splitlines():
        print(f"{prefix}{line}")


def process_one_claim(claim_id, verbose=True):
    if verbose:
        print_header(claim_id)

    state = process_claim(claim_id)
    run_agent_loop(state, verbose=verbose)

    if verbose:
        print(f"\n  final status: {state['status']}")
        print(f"  final action: {state['next_action']['type']}")
        if state['next_action'].get('message'):
            print(f"\n  message to customer:")
            print_indented(state['next_action']['message'])

    save_claim_output(state)
    return state


def prioritize(states):
    rank_order = {"finalize": 0, "escalate": 1, "message_customer": 2}

    def rank_key(s):
        action_type = s["next_action"]["type"] if s["next_action"] else "message_customer"
        has_pending_field = any(
            s[f]["status"] == "pending"
            for f in ("vin", "date_of_loss", "insurance_payout", "loan_balance")
        )
        has_promised = any(
            i["type"] == "pending" and "promised to send" in (i.get("description") or "")
            for i in s["issues"]
        )
        primary = rank_order.get(action_type, 3)
        secondary = 2 if has_pending_field else (1 if has_promised else 0)
        return (primary, secondary)

    return sorted(states, key=rank_key)


def explain_rank(state):
    action = state["next_action"]["type"] if state["next_action"] else None
    reason = state["next_action"]["reason"] if state["next_action"] else "unknown"

    if action == "finalize":
        return "ready to finalize — all documents present and consistent"
    if action == "escalate":
        return f"needs human reviewer — {reason}"

    has_pending_field = any(
        state[f]["status"] == "pending"
        for f in ("vin", "date_of_loss", "insurance_payout", "loan_balance")
    )
    if has_pending_field:
        return f"waiting on internal appraisal — {reason}"

    has_promised = any(
        i["type"] == "pending" and "promised to send" in (i.get("description") or "")
        for i in state["issues"]
    )
    if has_promised:
        return f"waiting on customer (already acknowledged) — {reason}"

    return f"waiting on customer outreach — {reason}"


def run_batch_mode():
    print_header("BATCH MODE — processing all 5 claims")

    states = []
    for cid in claim_ids:
        state = process_one_claim(cid, verbose=True)
        states.append(state)

    print_header("PROCESSING ORDER")

    ordered = prioritize(states)
    processing_order = []
    for i, s in enumerate(ordered, 1):
        reason = explain_rank(s)
        print(f"  {i}. {s['claim_id']}  ({s['status']})  — {reason}")
        processing_order.append({"claim_id": s["claim_id"], "reason": reason})

    summary_path = output_root / "_processing_order.json"
    summary_path.write_text(
        json.dumps({"processing_order": processing_order}, indent=2),
        encoding="utf-8",
    )

    print(f"\n  outputs written to: {output_root}")


def pick_claim(prompt_text):
    print("\n  available claims:")
    for i, cid in enumerate(claim_ids, 1):
        print(f"    {i}. {cid}")
    while True:
        choice = input(f"\n  {prompt_text}: ").strip()
        if not choice:
            continue
        if choice.upper() in claim_ids:
            return choice.upper()
        if choice.isdigit() and 1 <= int(choice) <= len(claim_ids):
            return claim_ids[int(choice) - 1]
        print("  invalid choice, try again")


def run_single_claim_mode():
    print_header("SINGLE CLAIM MODE")
    selected = pick_claim("pick a claim (1-5 or ID like CLM-001)")
    process_one_claim(selected, verbose=True)
    print(f"\n  output written to: {output_root / (selected + '.json')}")


def strip_cached_reply(state):
    has_reply = any(d["doc_type"] == "customer_reply" for d in state["documents"])
    if not has_reply:
        return state

    state["documents"] = [d for d in state["documents"] if d["doc_type"] != "customer_reply"]
    state["issues"] = []
    state["missing_required"] = []
    state["pending_customer_questions"] = []
    state["tools_used"] = []

    from tools_3 import (
        reconcile_same_type_docs,
        check_completeness,
        consolidate_field,
        validate_field_values,
        decide_status,
        decide_next_action_type,
    )

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


def run_interactive_mode():
    print_header("INTERACTIVE MODE")
    selected = pick_claim("pick a claim to chat about (1-5 or ID)")

    print_header(f"CHAT — {selected}")
    print("  (you are the customer. type your reply, or 'quit' to exit)")

    state = process_claim(selected)
    state = strip_cached_reply(state)

    run_agent_loop(state, verbose=False)

    turn = 1
    if state["next_action"].get("message"):
        print(f"\nagent:")
        print_indented(state["next_action"]["message"])
        append_turn(state, turn, "initial", None, state["next_action"]["message"])
    else:
        print(f"\n  (no customer message — action is {state['next_action']['type']})")
        save_claim_output(state)
        return

    while True:
        print()
        try:
            reply = input("you (customer) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[exiting]")
            break

        if not reply:
            continue
        if reply.lower() in {"quit", "exit", "q"}:
            print("[exiting]")
            break

        turn += 1

        state["documents"] = [d for d in state["documents"] if not d["filename"].startswith("live_reply_turn_")]
        synthetic_reply = {
            "filename": f"live_reply_turn_{turn}.txt",
            "file_hash": f"live-turn-{turn}",
            "source_kind": "text",
            "doc_type": "customer_reply",
            "authority": "customer_reply",
            "raw_text": reply,
            "fields": {},
            "flags": [],
        }
        state["documents"].append(synthetic_reply)

        if state.get("next_action"):
            state["next_action"]["message"] = None

        parsed = parse_customer_reply(state)
        if parsed and "error" not in parsed:
            merge_reply_into_state(state, parsed)

        from tools_3 import decide_status, decide_next_action_type
        status, reason = decide_status(state)
        state["status"] = status
        state["next_action"] = {
            "type": decide_next_action_type(state),
            "message": None,
            "reason": reason,
        }

        message = draft_customer_message(state)
        append_turn(state, turn, "customer_reply", reply[:200], message)

        if message:
            print(f"\nagent:")
            print_indented(message)
        else:
            print(f"\n  (action is {state['next_action']['type']} — nothing more to say)")
            break

    save_claim_output(state)
    print(f"\n  output written to: {output_root / (state['claim_id'] + '.json')}")


def show_menu():
    print("\n" + "=" * 70)
    print("  AI CLAIMS PROCESSING AGENT")
    print("=" * 70)
    print("\n  1. Batch mode       — process all 5 claims, print prioritization")
    print("  2. Single claim     — process one claim")
    print("  3. Interactive chat — live conversation with the agent")
    print("  4. Quit")


def main():
    while True:
        show_menu()
        choice = input("\n  select option (1-4): ").strip()

        if choice == "1":
            run_batch_mode()
        elif choice == "2":
            run_single_claim_mode()
        elif choice == "3":
            run_interactive_mode()
        elif choice == "4" or choice.lower() in {"quit", "exit", "q"}:
            print("\n[goodbye]\n")
            sys.exit(0)
        else:
            print("  invalid option, try again")


if __name__ == "__main__":
    main()