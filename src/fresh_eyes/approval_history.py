"""Append-only attributed approval events; active approval is only a pointer."""
import copy
from datetime import datetime, timezone
import json


def stamp():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS approval_history (case_id TEXT, acceptance TEXT, revision INTEGER, event_type TEXT, recorded_at TEXT, body TEXT NOT NULL, PRIMARY KEY(case_id, acceptance, revision))")
    db.execute("CREATE TRIGGER IF NOT EXISTS approval_history_no_update BEFORE UPDATE ON approval_history BEGIN SELECT RAISE(ABORT, 'Approval history is append-only'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS approval_history_no_delete BEFORE DELETE ON approval_history BEGIN SELECT RAISE(ABORT, 'Approval history is append-only'); END")
    for case_id, acceptance, raw in db.execute("SELECT case_id, acceptance, body FROM approvals").fetchall():
        if db.execute("SELECT 1 FROM approval_history WHERE case_id=? AND acceptance=?", (case_id, acceptance)).fetchone():
            continue
        record = json.loads(raw)
        record["review_revision"] = 1
        append(db, case_id, acceptance, record, "legacy_snapshot")
        db.execute("UPDATE approvals SET body=? WHERE case_id=? AND acceptance=?", (json.dumps(record), case_id, acceptance))


def append(db, case_id, acceptance, record, event_type):
    db.execute("INSERT INTO approval_history VALUES (?, ?, ?, ?, ?, ?)",
               (case_id, acceptance, record["review_revision"], event_type, stamp(), json.dumps(record)))


def create(db, case_id, acceptance, record, event_type="approved"):
    record = copy.deepcopy(record)
    record["review_revision"] = 1
    append(db, case_id, acceptance, record, event_type)
    db.execute("INSERT INTO approvals VALUES (?, ?, ?)", (case_id, acceptance, json.dumps(record)))
    return record


def history(db, case_id, acceptance):
    return [{"revision": row[0], "event_type": row[1], "recorded_at": row[2], "review": json.loads(row[3])}
            for row in db.execute("SELECT revision,event_type,recorded_at,body FROM approval_history WHERE case_id=? AND acceptance=? ORDER BY revision", (case_id, acceptance))]
