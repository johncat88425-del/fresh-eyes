"""The draft-acceptance command previews without a key and never registers or runs a case."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fresh_eyes.startup import inspect_repo
import test_acceptance_drafts as drafts
import test_github_startup as source_tests

ROOT = Path(__file__).resolve().parents[1]


class DraftAcceptanceCommandTests(unittest.TestCase):
    def test_preview_reads_no_key_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            inspected = Path(tmp) / "inspected"
            raw = source_tests.archive({"README.md": drafts.README.encode(), "pyproject.toml": b"[project]\n"})
            info = inspect_repo("https://github.com/unit/project", None, inspected / "source", reader=source_tests.Reader(raw))
            (inspected / "inspection.json").write_text(json.dumps(info))
            result = subprocess.run([sys.executable, "-B", "-m", "fresh_eyes", "draft-acceptance", "--inspection", str(inspected),
                                     "--model-config", str(ROOT / "evaluation/models-development-output4096-20261022.json"),
                                     "--output", str(Path(tmp) / "draft")],
                                    capture_output=True, text=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads(result.stdout)
            self.assertEqual((preview["action"], preview["key_read"], preview["model_calls"]), ("preview", False, 0))
            self.assertIn("Ultra", preview["model"])
            self.assertFalse((Path(tmp) / "draft").exists())


if __name__ == "__main__":
    unittest.main()
