import base64
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from pypdf import PdfReader


script_dir = Path(__file__).parent
claims_root = script_dir / "claims"
cache_root = script_dir / "cache"

ollama_url = "http://localhost:11434/api/generate"
vision_model = "qwen2.5vl:latest"


structured_prompt = """Extract the following fields from this insurance claim document.
Return ONLY a JSON object, no other text, no markdown fences.

Fields to extract (use null if not present or unreadable):
- document_type: one of [police_report, finance_agreement, settlement_breakdown, adjuster_note, tow_receipt, customer_reply, unknown]
- vin: exactly as shown in the document (even if it looks wrong or incomplete)
- date_of_loss: in YYYY-MM-DD format if a date is shown
- insurance_payout: numeric dollar amount, null if "TBD" or "pending"
- loan_balance: numeric dollar amount for outstanding loan balance
- claimant_name: full name of the claimant/borrower
- policy_number: as shown
- lender_name: name of the lender/financing company if present
- visual_flags: ONLY list items that are VISIBLY PRESENT in the image. Choose from:
  ["strikethrough_correction", "redaction_blob", "revised_banner",
   "superseded_notice", "pending_or_tbd_value", "handwritten_annotation",
   "stamp_processed", "stamp_official_copy", "illegible_section"]
  Use an empty list [] if none apply. Do NOT add items based on what similar documents usually have.

Rules:
- If a VIN shows correction marks or strikethroughs, extract the FINAL/CORRECTED value.
- If a field is redacted or illegible, set value to null.
- Do NOT invent or guess missing data.
- visual_flags must be GROUNDED - only include a flag if you can point to it in the image.
- Return VALID JSON only.
"""


text_extraction_prompt = """Extract the following fields from this insurance claim document text.
Return ONLY a JSON object, no other text, no markdown fences.

Fields to extract (use null if not present or unreadable):
- document_type: one of [police_report, finance_agreement, settlement_breakdown, adjuster_note, tow_receipt, customer_reply, unknown]
- vin: exactly as shown in the document
- date_of_loss: in YYYY-MM-DD format if a date is shown
- insurance_payout: numeric dollar amount, null if "TBD" or "pending"
- loan_balance: numeric dollar amount for outstanding loan balance
- claimant_name: full name of the claimant/borrower
- policy_number: as shown
- lender_name: name of the lender/financing company if present
- text_flags: list of notable keywords present. Choose from:
  ["revised_banner", "superseded_notice", "pending_or_tbd_value",
   "processed_stamp", "official_copy_stamp"]
  Use empty list [] if none apply.

Rules:
- Do NOT invent or guess missing data.
- Return VALID JSON only.

Document text:
"""


def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def call_vision_model(image_path):
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    payload = {
        "model": vision_model,
        "prompt": structured_prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    response = requests.post(ollama_url, json=payload, timeout=300)
    response.raise_for_status()
    return response.json()["response"]


def call_text_model(text_content):
    payload = {
        "model": vision_model,
        "prompt": text_extraction_prompt + text_content,
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    response = requests.post(ollama_url, json=payload, timeout=300)
    response.raise_for_status()
    return response.json()["response"]


def parse_model_output(raw):
    cleaned = strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"error": "json_parse_failed", "raw": raw}


def extract_pdf_text(pdf_path):
    reader = PdfReader(pdf_path)
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def extract_from_pdf(pdf_path):
    raw_text = extract_pdf_text(pdf_path)
    if not raw_text:
        return {
            "source_kind": "pdf_empty",
            "raw_text": "",
            "fields": {"error": "no extractable text in pdf"},
        }
    model_response = call_text_model(raw_text)
    fields = parse_model_output(model_response)
    return {
        "source_kind": "pdf",
        "raw_text": raw_text,
        "fields": fields,
    }


def extract_from_image(image_path):
    model_response = call_vision_model(image_path)
    fields = parse_model_output(model_response)
    return {
        "source_kind": "image",
        "raw_text": None,
        "fields": fields,
    }


def extract_from_text(text_path):
    raw_text = text_path.read_text(encoding="utf-8").strip()
    model_response = call_text_model(raw_text)
    fields = parse_model_output(model_response)
    if isinstance(fields, dict) and "error" not in fields:
        fields["document_type"] = "customer_reply"
    return {
        "source_kind": "text",
        "raw_text": raw_text,
        "fields": fields,
    }

def route_file(file_path):
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_from_pdf(file_path)
    if suffix in {".png", ".jpg", ".jpeg"}:
        return extract_from_image(file_path)
    if suffix == ".txt":
        return extract_from_text(file_path)
    return {
        "source_kind": "unsupported",
        "raw_text": None,
        "fields": {"error": f"unsupported file type: {suffix}"},
    }


def ingest_file(file_path, cache_dir):
    file_hash = hash_file(file_path)
    cache_file = cache_dir / f"{file_path.name}.json"

    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("file_hash") == file_hash:
            print(f"  cached  {file_path.name}")
            return cached

    print(f"  extract {file_path.name} ...")
    result = route_file(file_path)

    record = {
        "filename": file_path.name,
        "file_hash": file_hash,
        "source_kind": result["source_kind"],
        "raw_text": result["raw_text"],
        "fields": result["fields"],
    }

    cache_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def ingest_claim(claim_id):
    claim_folder = claims_root / claim_id
    if not claim_folder.exists():
        print(f"claim folder not found: {claim_folder}")
        return None

    cache_dir = cache_root / claim_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[{claim_id}]")
    files = sorted(f for f in claim_folder.iterdir() if f.is_file())

    records = []
    for f in files:
        record = ingest_file(f, cache_dir)
        records.append(record)

    manifest = {
        "claim_id": claim_id,
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "file_count": len(records),
        "files": [r["filename"] for r in records],
    }
    (cache_dir / "_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print(f"  done. {len(records)} files cached to {cache_dir}")
    return records


def ingest_all():
    if not claims_root.exists():
        print(f"claims_root not found: {claims_root}")
        return
    claim_folders = sorted(p for p in claims_root.iterdir() if p.is_dir())
    for cf in claim_folders:
        ingest_claim(cf.name)


def main():
    if len(sys.argv) < 2:
        print("usage:")
        print("  python ingest.py all")
        print("  python ingest.py CLM-001")
        sys.exit(1)

    target = sys.argv[1]
    if target == "all":
        ingest_all()
    else:
        ingest_claim(target)


if __name__ == "__main__":
    main()