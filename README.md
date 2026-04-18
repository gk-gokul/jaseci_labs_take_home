# AI Claims Processing Agent

This is my submission for the take-home assignment. It's a Python agent that reads a folder of claim documents, pulls out the important fields, checks if everything makes sense, decides what to do next, and writes a message to the customer if needed.

The whole thing runs locally with open-source models through Ollama. No paid APIs.

---

## Quick answer to the requirements

Going through the assignment checklist to show what's covered.

| Requirement | Where it lives |
|---|---|
| 1. Document intake (PDFs, images, text) | `ingest_1.py` handles all three |
| 2. Field extraction with confidence + reason | `tools_3.py` consolidates, `output_format.py` serializes |
| 3. Cross-document consistency + educated guess | `consolidate_field` in `tools_3.py` |
| 3. Duplicate handling | `reconcile_same_type_docs` in `tools_3.py` |
| 4. Status decision (complete / incomplete / needs_review) | `decide_status` in `tools_3.py` |
| 5. Multi-turn processing | `replies_5.py` parses reply, `agent.py` re-runs tools |
| 6. Interactive mode | Option 3 in `main.py` menu |
| 7. Conditional tool usage | `agent.py` — LLM picks next tool each iteration |
| 8. Claim prioritization | `prioritize` in `main.py` |
| 9. Output format | `output_format.py` produces the exact schema |
| Open-source LLM bonus | qwen2.5vl and qwen3:8b via Ollama |
| AI usage logs | `ai_usage/` folder |

---

## How to run

### Before you start
- Python 3.10 or newer
- [Ollama](https://ollama.ai) installed and running
- Pull the two models:
  ```
  ollama pull qwen2.5vl:latest
  ollama pull qwen3:8b
  ```
- Install the Python packages:
  ```
  pip install requests pypdf
  ```

### Folder layout
```
project/
├── claims/              # the 5 input folders go here
├── cache/               # made automatically (OCR results)
├── output/              # made automatically (final JSON per claim)
├── ai_usage/            # chat logs with Claude
├── ingest_1.py
├── state_2.py
├── tools_3.py
├── messages_4.py
├── replies_5.py
├── agent_6.py
├── output_format.py
├── main.py
└── README.md
```

### Step 1 — ingestion (one-time, already done)

The `cache/` folder in this submission already contains extraction results for all 5 claims. If you just want to see the system run, skip this step and go straight to Step 2.

If you want to re-run ingestion from scratch (for example, if you change a document or delete the cache):

```
python ingest_1.py all        # all 5 claims
python ingest_1.py CLM-001    # just one
```

A full from-scratch run takes about 15–20 minutes because vision OCR is roughly one minute per scanned image. Subsequent runs are instant - the ingester hashes each file and skips anything that's already cached.

### Step 2 - run the agent

```
python main.py
```

You'll get a menu:
```
1. Batch mode       - process all 5 claims, print prioritization
2. Single claim     - process one claim
3. Interactive chat - live conversation with the agent
4. Quit
```

- **Batch mode** runs all 5 claims, writes `output/CLM-00X.json` for each, and prints the processing order.
- **Single claim** does one claim at a time.
- **Interactive chat** lets you play the customer. Pick a claim, the agent sends its first message, you type a reply, the agent responds. Type `quit` to exit.

---

## My approach

### The big idea

The system has two stages. The first stage is slow and runs once. The second stage is fast and is where the agent actually thinks.

```
┌──────────────────┐       ┌────────────────────────────────┐
│  STAGE 1          │       │  STAGE 2                        │
│  Ingestion        │  ──▶ │  Agent reasoning                │
│  (slow, cached)   │       │  (fast, LLM picks tools)       │
│                   │       │                                 │
│  PDFs → pypdf     │       │  load cached fields             │
│  Images → VLM     │       │  reconcile, consolidate         │
│  Text → LLM       │       │  agent loop picks next tool     │
│  → cache/*.json   │       │  → output/*.json                │
└──────────────────┘       └────────────────────────────────┘
```

Why split it this way? Vision OCR on scanned images takes about a minute each. If every agent run re-did OCR, testing and iterating would take forever. Caching by file hash means the slow part only happens once, and the agent loop stays responsive.

### Files and what they do

| File | What it does |
|---|---|
| `ingest_1.py` | Reads every file in a claim folder. PDFs use `pypdf` for text, images use `qwen2.5vl`, text files use `qwen3`. Saves extraction as JSON in `cache/`. |
| `state_2.py` | The data model. Plain Python dicts. Holds constants like required document types and authority rankings. |
| `tools_3.py` | All the deterministic logic. Loading cached data, validating VINs, resolving conflicts across documents, deciding status, etc. |
| `messages_4.py` | Writes the email to the customer. Uses qwen3 with a structured input brief so the message stays grounded. |
| `replies_5.py` | Reads a customer reply (email) and pulls out what they provided, what conflicts they confirmed, and what questions they asked. |
| `agent_6.py` | The agent loop. Has a tool registry. Asks qwen3 which tool to call next, runs it, repeats until the agent says `done`. |
| `output_format.py` | Turns the internal state into the output schema shown in the assignment. |
| `main.py` | The menu that launches batch, single claim, or interactive mode. |

### The agent loop - how tool selection works at runtime

This is the part that makes this an agent and not just a pipeline.

Each turn, the agent sees:
1. A summary of the current claim state (what's missing, what's invalid, what conflicts exist, whether a reply is waiting)
2. The list of tools it can call, each with a description of when to use it
3. Which tools have already been called this run

It returns a single JSON: `{"tool": "tool_name", "reason": "why I picked this"}`. The loop runs that tool, logs it, and asks the agent again. Stops when the agent picks `done` (or after 6 iterations, which is a safety cap).

The five tools in the registry:

| Tool | Picked when |
|---|---|
| `parse_customer_reply` | A customer reply exists and hasn't been read yet |
| `draft_customer_message` | Action is `message_customer` and no message written yet |
| `escalate_to_human` | Action is `escalate` — the customer can't fix this |
| `finalize_claim` | Status is `complete` — ready to pay out |
| `done` | Nothing more to do |

The proof that the agent is actually deciding: every claim produces a **different tool trace**.

| Claim | Tools picked |
|---|---|
| CLM-001 | `finalize_claim → done` |
| CLM-002 | `parse_customer_reply → draft_customer_message → done` |
| CLM-003 | `escalate_to_human → done` |
| CLM-004 | `draft_customer_message → done` |
| CLM-005 | `parse_customer_reply → draft_customer_message → done` |

Same code, different decisions based on what each state actually looks like. Each decision is logged with the agent's reasoning in the `tools_used` field of the output JSON.

---

## Tool design - what I built and why

### Rule I followed

If plain Python can do it, don't use an LLM. If it needs judgment or has to read noisy text, use an LLM.

### Deterministic (plain Python, no model calls)

- VIN format check (regex, 17 alphanumeric, no I/O/Q)
- Date normalization so `02/14/2026` and `2026-02-14` compare as equal
- Numeric comparison with a small tolerance for rounding
- Consolidating a field across multiple documents (majority vote + authority weighting)
- Detecting a revised version of a document via keywords like `REVISED` and `SUPERSEDES`
- Deciding the claim status from the state
- Hashing files for cache lookups

### LLM-driven (qwen2.5vl or qwen3)

- Classifying and extracting fields from scanned images
- Reading a free-form customer email and pulling out structured info
- Writing the customer-facing email
- Picking which tool to run next in the agent loop

### Things I thought about but didn't build

- **Fuzzy duplicate detection.** File hash catches exact duplicates. Revisions are caught via banner keywords. Semantic dedup isn't needed for 5 claims.
- **Tesseract OCR as a backup.** I tested qwen2.5vl on all the messy cases (strikethroughs, redaction blobs, truncated VINs) and it handled them. A fallback that never fires is just extra code.
- **Direct lender API for CLM-005.** Elena offers to authorize a direct call to Bank of America. In production this would trigger a lender API call. Out of scope here — I capture it as a pending question.
- **Police records lookup for CLM-002.** James gives us the officer's name and badge number. A real system could pull the report that way. Marked as future work.
- **Answering GAP insurance questions.** The drafter refuses to make policy commitments. Unknown questions always get "a team member will follow up."
- **Automatic re-ingestion during interactive chat.** If the customer says "I'm attaching it now," the system acknowledges the promise but doesn't actually receive a file. Production would watch the folder for new files.

### Key design decisions 

**1. Each field has its own authority order.**

Different documents are trustworthy for different things:
```python
"vin":             ["police_report", "settlement_breakdown", "finance_agreement"]
"date_of_loss":    ["police_report", "settlement_breakdown", "finance_agreement"]
"insurance_payout": ["settlement_breakdown"]
"loan_balance":    ["finance_agreement", "settlement_breakdown"]
```

The police report is most trusted for dates because an officer was physically there. The finance agreement is most trusted for loan balance because it's the actual contract. The settlement is the only source for payouts. One global authority ranking would get this wrong.

**2. For VINs, I filter to valid-format candidates before ranking.**

If an authoritative source gives us a malformed VIN (OCR error), trusting it just because of source authority is wrong. I filter out format-invalid candidates first, then pick by authority among what's left. Without this, CLM-003 would have picked the police report's OCR-corrupted 16-character VIN over the settlement's clean 17-character one.

**3. Three tiers of document authority, not two.**

- `authoritative` - police report, finance agreement, settlement breakdown
- `supporting` - adjuster note, tow receipt
- `customer_reply` - customer emails

Supporting and customer-reply sources show up in the sources list so you can see what was reported, but they get filtered out when authoritative sources exist. This is what stops Elena's rough "around $35,000" estimate from overriding the settlement's precise $35,120.75.

**4. Consistency and validity are checked independently.**

Three documents agreeing on the same wrong VIN is still wrong. CLM-004 is the test: all four docs say `5YJ3E1EA7K` (10 chars). The cross-check says "consistent," the validator says "invalid." Both run. Status becomes `needs_review`.

**5. File extension beats model classification.**

The vision model once classified a customer email as a `finance_agreement` because it listed lender name, account number, and monthly payment - which looked like a finance agreement to the model. But `.txt` files are never finance agreements. I force `customer_reply` at the ingest step for all `.txt` files. Deterministic signals from the filesystem beat probabilistic guesses from the model.

**6. Customer confirmations bump confidence.**

When Elena in CLM-005 confirmed the date should be March 22 (not March 28 like our settlement said), the system:
- Tagged the issue with `customer_confirmed: true`
- Raised the field confidence to `high` because we now have 3 independent sources agreeing
- Told the drafter to thank her instead of re-asking

**7. A reply is not a resolution.**

Just because the customer replied doesn't mean the claim can move forward. The missing document is still missing. Both CLM-002 and CLM-005 stay `incomplete` after their replies because the blocker (missing police report, missing finance agreement) is still there. The reply adds context, not closure.

**8. The drafter has an authority rule.**

The drafter can ask for documents, acknowledge what was provided, and flag pending items. It cannot commit to policies, promise to contact third parties, or answer coverage questions. Any question outside its scope gets: "I've noted your question and a team member will follow up." No exceptions. This is enforced in the prompt.

---

## Model choices

- **qwen2.5vl:latest** for image OCR and field extraction from scanned documents. Tested on all the hard cases (strikethrough VIN corrections, partial redaction blobs, truncated VINs). Returns clean JSON.
- **qwen3:8b** for everything text-only: parsing customer emails, writing customer messages, and picking tools in the agent loop.

Both run locally via Ollama. No paid API calls anywhere.

---

## The five test claims and what happened

| Claim | What makes it tricky | Final status | Tools the agent picked |
|---|---|---|---|
| CLM-001 | Clean case; adjuster note has a strikethrough VIN | `complete` | `finalize → done` |
| CLM-002 | Police report missing; customer reply promises it and asks a GAP question | `incomplete` | `parse → draft → done` |
| CLM-003 | VIN mismatch across docs; two settlement PDFs, v2 supersedes v1 | `needs_review` | `escalate → done` |
| CLM-004 | VIN is only 10 characters; settlement has pending ACV; police report has a redaction | `needs_review` | `draft → done` |
| CLM-005 | Finance agreement missing; date conflict; customer corrects date and offers lender authorization | `incomplete` | `parse → draft → done` |

Each claim stresses a different class of problem — missing document, invalid field, cross-doc conflict, document supersession, customer correction — and each produces sensible output.

---

## Claim prioritization (my reasoning)

I ranked claims by who's blocking next and how fast each can close.

Primary order by action type:
1. `finalize` - ready to pay, no one is blocking
2. `escalate` - quick human decision with a clear recommendation
3. `message_customer` - waiting on someone external

Tie-breakers inside `message_customer`:
- Needs fresh outreach > already-acknowledged
- Waiting on internal appraisal goes last (out of our control)

My recommended order:

1. **CLM-001** (complete) - pay it now, no effort needed
2. **CLM-003** (needs_review) - reviewer just has to approve the VIN recommendation
3. **CLM-005** (incomplete) - customer needs a fresh follow-up
4. **CLM-002** (incomplete) - customer has already promised the document, just wait
5. **CLM-004** (needs_review) - waiting on our own appraisal, slowest to close

Output is also written to `output/_processing_order.json`.

---

## Output format

Every claim writes a JSON to `output/CLM-00X.json` that matches the schema in the assignment. An example from CLM-001:

```json
{
  "claim_id": "CLM-001",
  "status": "complete",
  "extracted_fields": {
    "vin": {
      "value": "1HGCM82633A004352",
      "confidence": "high",
      "source": "finance_agreement.png",
      "reason": "3 authoritative source(s) agree (total sources: 4)"
    },
    "date_of_loss": { "..." },
    "insurance_payout": { "..." },
    "loan_balance": { "..." }
  },
  "documents": {
    "identified": [ ... ],
    "missing": [],
    "duplicates": []
  },
  "issues": [],
  "next_action": {
    "type": "finalize",
    "message": null,
    "reason": "all required documents present and all fields resolved"
  },
  "tools_used": [
    {"tool": "finalize_claim", "reason": "...", "result": "claim marked ready to finalize"},
    {"tool": "done", "reason": "...", "result": "agent ended loop"}
  ]
}
```

See `output/` after running batch mode for all 5 full examples.

---

## What I'd do with more time

**Live attachment handling in interactive mode.**
Right now when the customer says "I'm attaching it now," the agent marks the document as promised but can't actually receive it. A real version would watch the claim folder for new files and trigger re-ingestion mid-conversation.

**A lender API adapter.**
CLM-005 Elena offered written authorization to contact Bank of America. A production system would have a `request_lender_payoff` tool that calls a lender API when she authorizes it.

**A police records lookup tool.**
CLM-002 James gives us the officer's name and badge number. A real system could pull the report directly from the department, saving the customer from finding their copy.

**Parallel OCR.**
Ollama handles concurrent requests. Running image OCR in parallel could cut first-time ingestion from 20 minutes to 5.

**A test harness.**
Ground-truth expected outputs per claim and a diff-based test runner. Would let me iterate on prompts without manually eyeballing JSON files.

**Prompt-aware caching.**
Right now cache is keyed by file hash only. If I change the extraction prompt, the cache still serves old extractions. Should include a prompt hash in the key.

**Persistent turn history.**
Interactive sessions don't resume. A production version would save state so an adjuster can pick up a conversation later.

**Claim-level extraction.**
Currently each document gets its own extraction call. A claim-level prompt that sees all documents together might resolve some ambiguities (like CLM-003's VIN) before consolidation even runs.

---

## Known limitations

- **Drafter occasionally gets optimistic.** qwen3:8b sometimes commits to things it shouldn't, like "we can contact your lender directly." The authority rule in the prompt catches most of it but not all. A larger model or a second validation pass would help.
- **Parser sometimes mixes info across calls.** qwen3:8b occasionally pulls content from the brief into the parsed reply. I fixed the worst cases by simplifying the output schema and adding explicit rules, but not perfectly.
- **No retry on bad JSON.** If either model returns broken JSON, we log it and return an error. A production version would retry with a clarifying prompt.
- **Cache isn't prompt-versioned.** If I change an extraction prompt, cached files aren't invalidated. Manual `rm cache/...` needed.
- **Interactive mode is single-session only.** State saves on exit but doesn't reload.

---

## How I used AI

I used Claude in a targeted way with small, task-specific prompts rather than long conversational threads. The full chat history is available here: [ai_usage/](https://claude.ai/share/9d336e0f-e4a1-46dc-8ab7-0a88f0ef61cd)

My approach was to define each module clearly (ingestion, state, tools, messaging, agent) and generate them with focused prompts that specified inputs, outputs, and constraints. Prompts were kept minimal and structured to return predictable outputs, usually in a single pass.

I avoided large, open-ended prompts and instead broke the system into smaller components, which made it easier to control behavior and reduce iteration. Most prompts were designed to produce code or structured JSON directly, so there was little need for follow-up refinement.

Claude was primarily used to accelerate implementation once the structure was clear. The overall architecture, data model, and decision logic were defined upfront and then implemented with targeted assistance.
The architecture decisions and the hard calls are mine. Claude was my sounding board and a fast typist when I was already sure what I wanted.
