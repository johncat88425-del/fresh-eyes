"""Synthetic legacy history and concurrent corrections; no real reviews edited."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fresh_eyes.api import Store, APIError


class ApprovalHistoryTests(unittest.TestCase):
    def test_legacy_snapshot_is_once_only_and_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = {"status": "approved", "reviewed_by": "Synthetic Legacy Reviewer", "approved_at": "2026-01-01T00:00:00Z"}
            db = sqlite3.connect(root / "runs.sqlite3")
            try:
                db.execute("CREATE TABLE approvals (case_id TEXT, acceptance TEXT, body TEXT, PRIMARY KEY(case_id,acceptance))")
                db.execute("INSERT INTO approvals VALUES ('test','hash',?)", (json.dumps(record),))
                db.commit()
            finally:
                db.close()
            store = Store(root)
            self.assertEqual(store.approval('test', 'hash')['approved_at'], record['approved_at'])
            store = Store(root)
            events = store.approval_events('test', 'hash')
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['event_type'], 'legacy_snapshot')
            for statement in ['DELETE FROM approval_history', "UPDATE approval_history SET body='{}'"]:
                with self.assertRaises(sqlite3.IntegrityError), store.connection() as db:
                    db.execute(statement)
            self.assertEqual(len(store.approval_events('test', 'hash')), 1)

    def test_only_one_concurrent_correction_can_use_a_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            store.approve('test', 'hash', {'reviewed_by': 'Synthetic Original'})
            def correct(n):
                try:
                    store.correct_approval('test', 'hash', reviewer=f'Synthetic Correction {n}', actor='Synthetic Operator', reason='test', expected_revision=1)
                    return True
                except APIError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sum(pool.map(correct, [1,2])), 1)
            self.assertEqual(len(store.approval_events('test', 'hash')), 2)
