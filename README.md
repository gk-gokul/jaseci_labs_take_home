# AI Claims Processing Agent

A local-first, agent-driven system for processing total-loss vehicle insurance claims. Reads a claim folder of mixed documents (PDFs, scanned images, customer emails), extracts key fields, reconciles conflicts across sources, decides what to do next, and talks to the customer when needed.

Built end-to-end on open-source models running locally via Ollama — no external APIs, no paid model calls.

---

## How to run

### Prerequisites
- Python 3.10+
- [Ollama](https://ollama.ai) running locally
- Models pulled:
  ```
  ollama pull qwen2.5vl:latest
  ollama pull qwen3:8b
  ```
- Python dependencies:
  ```
  pip install requests pypdf
  ```

### Folder layout
```
project/
├── claims/              # input — CLM-001 ... CLM-005 folders
├── cache/               # auto-created, OCR extractions
├── output/              # auto-created, final JSON per claim
├── ingest_1.py
├── state_2.py
├── tools_3.py
├── messages_4.py
├── replies_5.py
├── agent.py
├── output_format.py
└── main.py
```

### Step 1: Ingest (slow, one-time)
Runs OCR + field extraction on every document in every claim. Results are cached by file hash, so re-runs are instant.

```
python ingest_1.py all        # all 5 claims
python ingest_1.py CLM-001    # one claim
```

Takes roughly 15–20 minutes first time (vision OCR is ~1 min per image). Subsequent runs skip cached files automatically.

### Step 2: Run the agent
```
python main.py
```

You'll see a menu:
```
1. Batch mode       — process all 5 claims, print prioritization
2. Single claim     — process one claim
3. Interactive chat — live conversation with the agent
4. Quit
```

- **Batch mode** writes one JSON per claim to `output/` and prints the prioritization order.
- **Single claim** processes one claim.
- **Interactive chat** lets you play the customer. Pick a claim, the agent sends its first message, you reply, the agent responds. Type `quit` to exit.

---

## Approach and architecture

### Two-stage design

The system is deliberately split into a slow ingestion stage (runs once, cached) and a fast agent stage (runs many times, reasons over cached data).

```
┌─────────────────┐      ┌──────────────────────────────────┐
│  Ingestion      │      │  Agent reasoning                 │
│  (slow, once)   │ ───▶ │  (fast, LLM-driven)              │
│                 │      │                                  │
│  PDFs → pypdf   │      │  load cached extractions         │
│  Images → VLM   │      │  reconcile + consolidate         │
│  Texts → LLM    │      │  agent loop picks tools          │
│                 │      │  (parse reply / draft / escalate) │
│  → cache/*.json │      │  → output/*.json                 │
└─────────────────┘      └──────────────────────────────────┘
```

Why this split:
- Vision OCR is ~1 minute per image. Without caching, every agent iteration or prompt change forces re-extraction. Unusable.
- Interactive mode stays responsive because the agent never touches images directly — just cached JSON.
- Development iteration goes from minutes to seconds.

### File-by-file responsibilities

| File | Purpose |
|---|---|
| `ingest_1.py` | Extracts fields from PDFs (pypdf), images (qwen2.5vl), and text files (qwen3). Hashes and caches per file. |
| `state_2.py` | The claim state data model as plain dicts. Constants for authority mappings and required document types. |
| `tools_3.py` | Deterministic processing tools: load from cache, consolidate fields across documents, resolve conflicts, validate VINs, decide status. |
| `messages_4.py` | Customer message drafter (qwen3). Takes a structured brief, writes a warm, specific email. |
| `replies_5.py` | Customer reply parser (qwen3). Extracts provided values, confirmed conflicts, questions, and promised documents from free-form email text. Merges parsed results back into state. |
| `agent.py` | The agent loop. Wraps tools in a registry with descriptions. Calls qwen3 to pick the next tool based on state, runs it, logs to `tools_used`, repeats until `done`. |
| `output_format.py` | Serializes internal state to the README's expected output schema. |
| `main.py` | Launcher menu: batch, single claim, or interactive chat. |

### The agent loop

This is the part that satisfies the "agent decides at runtime" requirement directly.

The agent sees a compact state summary and a tool registry with descriptions. On each iteration it returns `{"tool": "name", "reason": "..."}` and the loop executes that tool. Termination is when the agent picks `done`, with a max of 6 iterations as a safety rail.

Five tools are registered:

| Tool | When the agent should pick it |
|---|---|
| `parse_customer_reply` | A customer reply exists and hasn't been parsed yet |
| `draft_customer_message` | Action is `message_customer` and no message has been drafted since the last reply |
| `escalate_to_human` | Action is `escalate` (conflict the customer can't resolve) |
| `finalize_claim` | Status is `complete` |
| `done` | Nothing more to do |

The payoff is that every claim produces a **different tool sequence**, visible in the output JSON's `tools_used` field:

| Claim | Tool trace |
|---|---|
| CLM-001 | `finalize_claim → done` |
| CLM-002 | `parse_customer_reply → draft_customer_message → done` |
| CLM-003 | `escalate_to_human → done` |
| CLM-004 | `draft_customer_message → done` |
| CLM-005 | `parse_customer_reply → draft_customer_message → done` |

Same code, five different runtime decisions based on state content.

---

## Tool design rationale

### What stays deterministic, what uses the LLM

**Rule of thumb: if a regex or dict lookup can do it, don't burn LLM tokens.**

Deterministic (plain Python, no model calls):
- VIN format check — 17 alphanumeric characters, excludes I/O/Q
- Date normalization across formats (`02/14/2026` ↔ `2026-02-14`)
- Numeric comparison with rounding tolerance
- Cross-document field consolidation with majority vote + authority weighting
- Revision detection via keyword flags (`REVISED`, `SUPERSEDES`)
- Status decision tree
- File hashing for cache lookups

LLM-driven:
- Document classification from images (qwen2.5vl)
- Field extraction from noisy scans, including strikethrough handling (qwen2.5vl)
- Free-form customer email parsing — extracting fields, questions, and promises from prose (qwen3)
- Customer message drafting with tone and context (qwen3)
- Tool selection at runtime (qwen3)

### What the tool registry enables

Each tool is a Python function plus a description. The agent's job is just to pick a name — tools read everything they need from the state themselves. This keeps the agent's reasoning simple (closed enum of tool names, no arguments to hallucinate) and makes every tool independently testable.

### Things considered but deliberately skipped

- **Fuzzy document deduplication.** File hash comparison handles exact duplicates. Near-duplicates are handled separately via revision markers (`REVISED`, `SUPERSEDES`). Semantic dedup would be overkill for 5 claims.
- **Tesseract OCR fallback.** I tested qwen2.5vl on all the hard cases (strikethrough VINs, redaction blobs, rotated scans). It handled them correctly. A fallback chain would be belt-and-suspenders that never fires.
- **Direct lender contact API.** CLM-005's Elena offers to authorize direct contact with Bank of America. In production this would trigger a lender API call. For this exercise, the system captures it as a pending question for a human reviewer.
- **Police record lookup service.** CLM-002's James mentions officer badge numbers that a real system could use to pull the report. Out of scope — captured as a promised document instead.
- **GAP insurance policy logic.** CLM-002's GAP question is a policy question, not a document question. Escalated to a human, per the drafter's authority rule (no policy commitments).
- **Automatic re-ingestion on live attachments.** Interactive mode handles "I'm attaching it now" by marking the document as promised but can't actually receive the file. A production version would hook into an attachment upload flow.

### Key design decisions

**1. Field-specific authority ordering.**
Different fields trust different sources:
```python
"vin":           ["police_report", "settlement_breakdown", "finance_agreement"]
"date_of_loss":  ["police_report", "settlement_breakdown", "finance_agreement"]
"insurance_payout": ["settlement_breakdown"]
"loan_balance":  ["finance_agreement", "settlement_breakdown"]
```
Police reports are authoritative for dates (officer was physically present). Finance agreements are authoritative for loan balances (contract document). Settlements are the only source for payouts. VIN ordering puts settlement above finance because finance agreements are typed by lenders and prone to data-entry typos (this actually resolved CLM-003 correctly).

**2. Format-valid-first VIN candidate filter.**
When authoritative sources disagree on a VIN, filter to format-valid candidates before ranking. Without this, the system could confidently pick a malformed VIN just because it came from a high-authority source. On CLM-003, this skipped the police report's OCR-corrupted 16-char VIN in favor of the settlement's valid 17-char VIN.

**3. Three tiers of document authority, not two.**
- `authoritative` — police report, finance agreement, settlement breakdown
- `supporting` — adjuster note, tow receipt
- `customer_reply` — customer emails

Supporting and customer-reply sources appear in the `sources` list for transparency but are filtered out of value-matching when authoritative sources exist. This stops Elena's rough `$35,000` estimate from overriding the settlement's precise `$35,120.75`.

**4. Consistency and validity are checked independently.**
Three documents agreeing on the same invalid VIN is still invalid. CLM-004 is the test case: all four documents report `5YJ3E1EA7K` (10 chars). The cross-check passes ("consistent"), the validator catches it ("invalid"). Both run.

**5. Source-kind overrides model classification when deterministic signals exist.**
The VLM initially classified CLM-005's customer email as a `finance_agreement` based on its structured content (lender name, account number, monthly payment). `.txt` files are never finance agreements — we force `document_type: customer_reply` at ingest. Deterministic filesystem signals beat probabilistic model inference.

**6. Customer-confirmed conflicts bump confidence, not just resolution.**
When Elena explicitly confirmed `03/22/2026` in her reply to a date conflict, the system upgrades the field confidence to `high` and tags the issue with `customer_confirmed: true`. The drafter then thanks her for catching it instead of re-asking.

**7. A reply is not a resolution.**
Status can only advance when the original blocker is resolved. Both CLM-002 and CLM-005 stay `incomplete` after parsing their customer replies because the missing document is still missing — the reply added context but didn't close the blocker.

**8. The drafter has a strict authority rule.**
The drafter can request documents, acknowledge what was provided, and note pending items. It cannot commit to policy decisions, promise third-party contact, or answer coverage questions. Unknown questions get "I've noted your question and a team member will follow up" — always, no exceptions. This is encoded in the prompt.

---

## Model choices

- **qwen2.5vl:latest** for image OCR and structured field extraction. Tested on all hard cases: strikethrough VIN corrections, partial redaction blobs, rotated scans, truncated VINs. Handles each correctly with one-shot structured JSON output.
- **qwen3:8b** for everything text-only: parsing customer replies, drafting messages, and tool selection. Fast, good at structured output, supports tool-calling style reasoning.

Both run locally via Ollama. No external API dependencies.

---

## Handling the five test claims

| Claim | Failure mode | Expected status | Tool trace |
|---|---|---|---|
| CLM-001 | Clean case; adjuster note has strikethrough VIN | `complete` | `finalize → done` |
| CLM-002 | Police report missing; customer reply promises it, asks GAP question | `incomplete` | `parse → draft → done` |
| CLM-003 | VIN mismatch across docs; two settlement PDFs with v2 superseding v1 | `needs_review` | `escalate → done` |
| CLM-004 | Invalid VIN (10 chars); redaction blob on police report; pending ACV | `needs_review` | `draft → done` |
| CLM-005 | Missing finance agreement; date mismatch; customer corrects date + offers lender authorization | `incomplete` | `parse → draft → done` |

Each claim isolates a different class of problem — missing documents, invalid fields, cross-doc conflicts, document supersession, multi-turn context, customer-originated corrections. All five produce correct outputs.

---

## Prioritization

Prioritization ranks claims by who's blocking and how quickly the claim can close.

Primary rank (action type):
1. `finalize` — ready to pay, no human or customer input needed
2. `escalate` — quick human review with a clear recommendation
3. `message_customer` — waiting on external input

Secondary rank (within `message_customer`):
- Needs fresh outreach → ranked ahead of already-acknowledged
- Waiting on internal appraisal → ranked last (out of our control)

For the 5 test claims:
1. **CLM-001** (`complete`) — finalize immediately
2. **CLM-003** (`needs_review`) — human reviewer, VIN recommendation ready
3. **CLM-005** (`incomplete`) — waiting on fresh customer outreach
4. **CLM-002** (`incomplete`) — customer already engaged, document promised
5. **CLM-004** (`needs_review`) — waiting on internal appraisal, slowest to close

---

## What I'd build with more time

**Attachment handling in interactive mode.**
Currently "I'm attaching the police report now" marks the document as promised but doesn't actually ingest anything. A real version would watch the claim folder for new files and trigger re-ingestion mid-conversation.

**A lender API adapter for CLM-005's Elena scenario.**
She offered to authorize direct contact with Bank of America. A production system would have a `request_lender_payoff` tool that integrates with a secure lending API, triggered when the customer provides written authorization.

**A police records lookup tool for CLM-002's James scenario.**
He provided the officer name and badge number. A production system could query a records API to fetch the report directly, reducing customer friction.

**Parallel OCR.**
Ollama supports concurrent requests. Running image OCR in parallel would cut first-time ingestion from ~20 minutes to ~5.

**Better classification for unlabeled attachments.**
The VLM occasionally misclassifies supporting documents. A second pass with a text-only model reading the structured extraction could validate the classification.

**Persistent turn history across sessions.**
Currently the agent loses turn context when the process restarts. A production version would persist turn history to the claim state on disk so an adjuster can pick up where they left off.

**An automated test harness.**
Ground-truth expected outputs per claim and a diff-based test runner. Would let me iterate on prompts without manually reading 5 JSON files each time.

**Batched field extraction prompts.**
Currently one prompt per document. A claim-level prompt that looks at all documents together could resolve some cross-document ambiguities (like the CLM-003 VIN) before the consolidation step even runs.

---

## Known limitations

- **Occasional drafter optimism.** qwen3:8b occasionally commits to things it shouldn't ("we can contact your lender directly"). The authority rule in the prompt catches most of this but not all. A larger model or a validation pass would help.
- **Parser hallucinations on unrelated content.** qwen3:8b sometimes pulls content across calls when asked for many output buckets. I mitigated this by dropping unused fields from the schema and adding explicit rules ("ONLY use information from the reply below"). Not perfect.
- **No retry logic on malformed JSON.** If either qwen3 or qwen2.5vl returns invalid JSON, the parser logs the raw output and returns an error dict. Works for diagnosis; a production version would retry with clarifying prompts.
- **Cache doesn't version extraction prompts.** If I change the extraction prompt, cached files still use the old extraction. Currently I delete the cache manually. Should hash the prompt alongside the file.
- **Interactive mode is single-session.** State is saved on exit but not reloaded — each session starts fresh.

---

## How I used AI to build this

I worked with Claude as a pair-programmer throughout. Logs are in `ai_usage/`. The back-and-forth covered:

- Test-data analysis before coding — understanding what each claim was actually testing, which shaped the data model
- OCR validation — testing qwen2.5vl against the hardest documents (strikethrough, redaction, truncated VINs) before committing to it
- Architecture tradeoffs — pushed back on an overly-elaborate free-form agent loop in favor of a simpler tool-picker with a closed enum
- Specific debugging — e.g., the CLM-003 VIN bug where the OCR-corrupted police report was confidently chosen as the answer. The fix was the format-valid-first filter.
- Prompt iteration — each drafter and parser prompt went through 3–4 revisions based on actual outputs.

The decisions are mine. The code is mine. Claude was the sounding board and the fast typist.
