"""Bind attributed post-run human patch reviews to exact exported evidence."""
import json
from pathlib import Path
import re

from .approval_io import timestamp
from .contracts import InvalidCase

FORMAT = "fresh-eyes-result-human-reviews-v1"
IDENTITY = ("case_id", "group", "run_id", "acceptance_sha256", "report_sha256")
FIELDS = set(IDENTITY) | {"verdict", "reviewer", "reviewed_at", "note"}


def template(result):
    return {"format": FORMAT, "scope": "team_patch_output_review", "freeze_sha256": result["freeze_sha256"],
            "notice": "Unsigned attributed team review. A draft is not an approval; the engine result is never rewritten.",
            "reviews": [{**{k: r[k] for k in IDENTITY}, "verdict": "draft", "reviewer": None,
                         "reviewed_at": None, "note": ""} for r in result["runs"]
                        if r["run_state"] == "completed" and r["result_status"] == "verified_fix"]}


def apply_reviews(result, path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 262144:
        raise InvalidCase("Unsupported human result review file")
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise InvalidCase("Duplicate human result review field")
            obj[k] = v
        return obj
    body = json.loads(path.read_text(), object_pairs_hook=unique)
    if (not isinstance(body, dict) or body.get("format") != FORMAT or body.get("scope") != "team_patch_output_review" or
            body.get("freeze_sha256") != result["freeze_sha256"] or not isinstance(body.get("reviews"), list)):
        raise InvalidCase("Human result reviews do not match this evaluation freeze")
    records = {tuple(r.get(k) for k in IDENTITY): r for r in result["runs"]
               if r["run_state"] == "completed" and r["result_status"] == "verified_fix"}
    seen, updates = set(), {}
    for entry in body["reviews"]:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise InvalidCase("Human result review fields differ from the template")
        if not all(isinstance(entry[k], str) for k in IDENTITY):
            raise InvalidCase("Human result review identity must be exact strings")
        key = tuple(entry[k] for k in IDENTITY)
        if key in seen or key not in records or not re.fullmatch(r"[a-f0-9]{64}", entry["report_sha256"]):
            raise InvalidCase("Duplicate or mismatched human result review identity/report")
        seen.add(key)
        if not isinstance(entry["note"], str) or len(entry["note"]) > 4000:
            raise InvalidCase("Human review note must be bounded text")
        if entry["verdict"] == "draft":
            if entry["reviewer"] is not None or entry["reviewed_at"] is not None:
                raise InvalidCase("Draft human review cannot assert attribution")
            continue
        if (entry["verdict"] not in {"accepted", "rejected"} or not isinstance(entry["reviewer"], str) or
                entry["reviewer"] != entry["reviewer"].strip() or not 1 <= len(entry["reviewer"]) <= 100):
            raise InvalidCase("Human result review requires accepted/rejected and an actual reviewer")
        if not isinstance(entry["reviewed_at"], str):
            raise InvalidCase("Human result review requires a timezone-aware timestamp")
        timestamp(entry["reviewed_at"])
        updates[key] = {k: entry[k] for k in ("verdict", "reviewer", "reviewed_at", "note", "report_sha256")}
        updates[key]["scope"] = "team_patch_output_review"
    for key, review in updates.items():
        records[key]["human_review"] = review
