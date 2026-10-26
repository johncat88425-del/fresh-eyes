"""Transfer attributed team review records without changing case contracts."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .contracts import Contract, digest

FORMAT = "fresh-eyes-team-approvals-v1"
SCOPE = "team_acceptance_review"
CORE = {"status", "reviewed_by", "approved_at", "scope", "acceptance_sha256"}
ROW_FIELDS = CORE | {"case_id", "repo_url", "commit_sha", "upstream_maintainer_approval"}


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("A timezone-aware review timestamp is required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Review timestamps must include a timezone")


def contracts(project):
    found = {}
    for path in sorted((Path(project) / "cases").glob("*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "case_id" in data and "fixtures" in data:
            contract = Contract.from_dict(data)
            if data["case_id"] in found:
                raise ValueError("Duplicate registered case ID")
            found[data["case_id"]] = contract
    return found


@contextmanager
def read_database(data_dir):
    path = (Path(data_dir) / "runs.sqlite3").resolve()
    if not path.exists():
        yield None
        return
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        yield db
    finally:
        db.close()


def existing(db, case_id, acceptance):
    if db is None:
        return None
    row = db.execute("SELECT body FROM approvals WHERE case_id=? AND acceptance=?", (case_id, acceptance)).fetchone()
    return json.loads(row[0]) if row else None


def validate_row(row, registered):
    if not isinstance(row, dict) or set(row) != ROW_FIELDS:
        raise ValueError("Approval fields do not match the transfer format")
    case_id = row["case_id"]
    if not isinstance(case_id, str) or case_id not in registered:
        raise ValueError("Approval refers to an unregistered case")
    contract = registered[case_id]
    if (row["repo_url"] != contract.data["repo"]["url"] or row["commit_sha"] != contract.data["repo"]["commit"] or
            row["acceptance_sha256"] != contract.acceptance_sha256):
        raise ValueError("Case repo, commit or acceptance changed: " + case_id)
    if row["status"] != "approved" or row["scope"] != SCOPE or row["upstream_maintainer_approval"] is not False:
        raise ValueError("Only attributed team acceptance approvals may be transferred")
    reviewer = row["reviewed_by"]
    if not isinstance(reviewer, str) or reviewer != reviewer.strip() or not 1 <= len(reviewer) <= 100:
        raise ValueError("The original reviewer name is required")
    timestamp(row["approved_at"])


def export_approvals(project, data_dir, output):
    registered = contracts(project)
    rows = []
    with read_database(data_dir) as db:
        for case_id, contract in sorted(registered.items()):
            approval = existing(db, case_id, contract.acceptance_sha256)
            if not approval:
                continue
            row = {key: approval.get(key) for key in CORE}
            row.update(case_id=case_id, repo_url=contract.data["repo"]["url"],
                       commit_sha=contract.data["repo"]["commit"], upstream_maintainer_approval=False)
            validate_row(row, registered)
            rows.append(row)
    envelope = {"format": FORMAT, "schema_version": 1, "scope": SCOPE,
                "exported_at": datetime.now(timezone.utc).isoformat(), "approvals": rows,
                "payload_sha256": digest(rows),
                "trust_notice": "Unsigned team records. The checksum detects file changes, not reviewer identity or upstream approval."}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    return {"action": "exported", "approval_count": len(rows), "output": str(output.resolve()),
            "case_ids": [row["case_id"] for row in rows], "repository_executed": False, "model_calls": 0}


def load_transfer(project, source):
    source = Path(source)
    if source.stat().st_size > 256 * 1024:
        raise ValueError("Approval transfer exceeds 256 KiB")
    payload = json.loads(source.read_text())
    fields = {"format", "schema_version", "scope", "exported_at", "approvals", "payload_sha256", "trust_notice"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Invalid approval transfer envelope")
    if payload["format"] != FORMAT or type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["scope"] != SCOPE:
        raise ValueError("Unsupported approval transfer format or scope")
    timestamp(payload["exported_at"])
    rows = payload["approvals"]
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Expected at most 100 approval records")
    if not isinstance(payload["payload_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", payload["payload_sha256"]) or digest(rows) != payload["payload_sha256"]:
        raise ValueError("Approval payload checksum does not match")
    registered = contracts(project)
    seen = set()
    for row in rows:
        validate_row(row, registered)
        key = row["case_id"], row["acceptance_sha256"]
        if key in seen:
            raise ValueError("Duplicate case approval in transfer")
        seen.add(key)
    return rows, payload["payload_sha256"]


def plan(db, rows):
    fresh, unchanged = [], []
    for row in rows:
        old = existing(db, row["case_id"], row["acceptance_sha256"])
        if old:
            if any(old.get(key) != row[key] for key in CORE):
                raise ValueError("Conflicting existing approval; nothing imported: " + row["case_id"])
            unchanged.append(row["case_id"])
        else:
            fresh.append(row)
    return fresh, unchanged


def import_approvals(project, data_dir, source, *, apply=False):
    rows, checksum = load_transfer(project, source)
    with read_database(data_dir) as db:
        fresh, unchanged = plan(db, rows)
    if apply and fresh:
        from .api import Store
        store = Store(Path(data_dir))
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Recheck under the write lock before the first mutation.
            fresh, unchanged = plan(db, rows)
            for row in fresh:
                record = {key: row[key] for key in CORE}
                record["transfer"] = {"payload_sha256": checksum, "imported_at": datetime.now(timezone.utc).isoformat(),
                                      "identity_verified": False, "source": "unsigned_team_record"}
                from .approval_history import create
                create(db, row["case_id"], row["acceptance_sha256"], record, "imported")
    return {"action": "imported" if apply else "preview", "applied": apply,
            "imported_count": len(fresh) if apply else 0, "would_import_count": len(fresh) if not apply else 0,
            "unchanged_count": len(unchanged), "case_ids": [row["case_id"] for row in fresh],
            "reviewers": sorted({row["reviewed_by"] for row in rows}), "payload_sha256": checksum,
            "notice": "Preserves the attributed team review; does not authenticate its author or grant upstream approval.",
            "repository_executed": False, "model_calls": 0}
