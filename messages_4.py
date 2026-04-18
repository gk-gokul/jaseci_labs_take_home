import json
import re

import requests

from tools_3 import build_message_brief


ollama_url = "http://localhost:11434/api/generate"
drafter_model = "qwen3:8b"


drafter_system_prompt = """You are a helpful insurance claims assistant writing brief, warm messages to customers about their total-loss auto claims.

Your job: given a structured brief about what's missing, invalid, conflicting, or unresolved, write ONE short email (4-10 sentences) to the customer.

Core rule: ONLY address items that actually appear in the brief. Do NOT volunteer guidance, tips, or explanations about topics not listed in the brief. If a field is not in invalid_fields, do not mention it. If no documents are missing, do not ask for documents.

Authority rule: You can ONLY request documents, acknowledge what was provided, or note that items are pending. You CANNOT commit to policy decisions, contact other parties on the customer's behalf, or answer coverage questions. If the customer asked a question that requires a policy or process commitment (e.g., "can you contact my lender?", "do I need GAP documentation?"), respond with "I've noted your question and a team member will follow up" and nothing more. Do NOT say "yes we can do that" or "you don't need that" — defer.

Rules:
- Address the customer by first name if a claimant_name is provided.
- Be warm and professional. Do not be robotic or overly formal.
- Reference the claim by the policy_number (e.g. "your claim AUT-7829301"). NEVER mention the internal claim_id (like "CLM-002"). If no policy_number is provided, do not mention any claim number at all.
- For missing_documents: ask for each specifically. If a finance_agreement is missing, note that a recent statement or payoff letter from the lender is also acceptable.
- For promised_documents: acknowledge the customer said they'll send it. Reassure them there's no rush. Do NOT also put the same document in a "please send" request.
- For invalid_fields containing a VIN: explain that the full 17-character VIN can be found on the vehicle registration or the driver-side door jamb. Only mention VIN locations when VIN is an invalid_field.
- For pending_fields: reassure the customer that no action is needed on their part for that item.
- For conflicts (unresolved): ask the customer to confirm the correct value. Use plain language, not technical field names.
- For customer_confirmed_conflicts: thank the customer for catching it, confirm you'll update the record to their stated value, do NOT ask them to confirm again.
- For pending_customer_questions: acknowledge each one briefly with "I've noted your question and a team member will follow up." Do NOT answer substantively.
- Do NOT invent policy details, timelines, or amounts not provided in the brief.
- Do NOT list technical field names like "date_of_loss" or "loan_balance"; rephrase naturally ("date of the accident", "loan balance").
- End with a simple closing like "Thanks," or "Best,". Do NOT include a signature block, do NOT write "[Your Name]", do NOT include placeholders, do NOT include contact info.
- Return ONLY the email body. No subject line, no markdown, no preamble, no "Here's the email:".
"""


def call_drafter(brief):
    prompt = (
        drafter_system_prompt
        + "\n\nBrief:\n"
        + json.dumps(brief, indent=2)
        + "\n\nWrite the email body now. /no_think"
    )

    payload = {
        "model": drafter_model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 8192},
    }
    response = requests.post(ollama_url, json=payload, timeout=300)
    response.raise_for_status()
    return response.json()["response"]


def strip_thinking(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def scrub_signature_placeholders(text):
    signoff_words = {"best", "thanks", "regards", "sincerely", "cheers"}
    lines = text.splitlines()
    cleaned = []
    prev_was_signoff = False
    for line in lines:
        stripped = line.strip().rstrip(",").rstrip(".").lower()
        if re.search(r"\[[A-Za-z ]+\]", line):
            continue
        if re.fullmatch(r"\s*(your name|agent name|insurance agent|claims team|the team)\s*", line, re.IGNORECASE):
            continue
        is_signoff = stripped in signoff_words or stripped in {f"{w} regards" for w in signoff_words}
        if is_signoff and prev_was_signoff:
            continue
        prev_was_signoff = is_signoff
        cleaned.append(line)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned).strip()


def draft_customer_message(state):
    if state["next_action"]["type"] != "message_customer":
        return None

    brief = build_message_brief(state)
    raw = call_drafter(brief)
    message = strip_thinking(raw)
    message = scrub_signature_placeholders(message)
    return message