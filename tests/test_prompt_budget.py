"""Budget bounds and evidence excerpts, using explicitly synthetic references."""

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fresh_eyes.diagnostics import context
from fresh_eyes.models import ModelCaller, ModelError, ModelLedger, ModelSpec
from fresh_eyes.prompt_budget import NemotronPromptBound
from test_models import SyntheticTransport
from test_core import case_data
from fresh_eyes.contracts import Contract


class RequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = json.dumps({"normalizer": None, "model": {"type": "BPE"},
            "pre_tokenizer": {"type": "Sequence", "pretokenizers": [{"type": "Split"}, {"type": "ByteLevel", "add_prefix_space": False}]},
            "post_processor": {"type": "ByteLevel"}}).encode()
        (self.root / "nano-tokenizer.json").write_bytes(self.raw)
        (self.root / "nano-tokenizer_config.json").write_text(json.dumps({"chat_template": "SYNTHETIC TEMPLATE"}))
        self.profile = {"synthetic-nano": {"role": "nano", "tokenizer_sha256": hashlib.sha256(self.raw).hexdigest(),
                        "template_sha256": hashlib.sha256(b"SYNTHETIC TEMPLATE").hexdigest()}}
        self.patch = patch("fresh_eyes.prompt_budget.PROFILES", self.profile)
        self.patch.start()
        self.bound = NemotronPromptBound(self.root)
        self.spec = ModelSpec("synthetic-nano", 1_048_576, 512, "1", "3", "SYNTHETIC TEST RATES")
        self.messages = [{"role": "system", "content": "JSON"}, {"role": "user", "content": "中文🙂"}]

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_request_bound_counts_utf8_and_keeps_true_model_capacity(self):
        count, reference = self.bound(self.spec, self.messages)
        self.assertEqual(count, 2048 + 4 + len("中文🙂".encode()))
        ledger = ModelLedger(self.root / "ledger.sqlite3", "synthetic", "0.02")
        ModelCaller(SyntheticTransport([{}]), ledger, prompt_bound=self.bound).call("classify", self.spec, self.messages)
        call = ledger.summary()["calls"][0]
        self.assertEqual(call["input_tokens_upper_bound"], count)
        self.assertEqual(call["input_bound_reference"], reference)
        self.assertLess(Decimal(call["reservation_usd"]), Decimal("0.02"))
        self.assertEqual(self.spec.context_window, 1_048_576)
        self.assertGreater(self.spec.reservation, Decimal("1"))

    def test_tampered_reference_and_unsupported_message_shape_are_rejected(self):
        (self.root / "nano-tokenizer.json").write_bytes(self.raw + b" ")
        with self.assertRaisesRegex(ModelError, "reviewed profile"):
            NemotronPromptBound(self.root)
        for messages in [self.messages[1:], self.messages + [self.messages[1]],
                         [self.messages[0], {**self.messages[1], "name": "extra"}]]:
            with self.assertRaises(ModelError):
                self.bound(self.spec, messages)

    def test_insufficient_budget_prevents_dispatch_with_reviewed_bound(self):
        transport = SyntheticTransport([{}])
        caller = ModelCaller(transport, ModelLedger(self.root / "ledger.sqlite3", "test", "0.0001"), prompt_bound=self.bound)
        with self.assertRaisesRegex(ModelError, "insufficient"):
            caller.call("classify", self.spec, self.messages)
        self.assertEqual(transport.payloads, [])

    def test_provider_usage_above_bound_is_unknown_and_blocks_next_call(self):
        def exceeded(response):
            response["usage"] = {"prompt_tokens": 4000, "completion_tokens": 10, "total_tokens": 4010}
            return response
        transport = SyntheticTransport([{}], exceeded)
        ledger = ModelLedger(self.root / "ledger.sqlite3", "test", "0.02")
        caller = ModelCaller(transport, ledger, prompt_bound=self.bound)
        with self.assertRaisesRegex(ModelError, "exceeded"):
            caller.call("classify", self.spec, self.messages)
        with self.assertRaisesRegex(ModelError, "previous charge"):
            caller.call("diagnose", self.spec, self.messages)
        self.assertEqual(len(transport.payloads), 1)
        call = ledger.summary()["calls"][0]
        self.assertIsNone(call["calculated_usd"])
        self.assertEqual(call["provider_request_id"], "chatcmpl-synthetic")

    def test_nested_process_error_is_included_and_long_readme_is_explicitly_truncated(self):
        (self.root / "README.md").write_text("A" * 5000)
        contract = Contract.from_dict(case_data("a" * 40))
        evidence = context(contract, {"result": {"steps": [], "outputs": [
            {"name": "input.md", "process": {"stderr": "missing executable", "argv": ["tool"]}}
        ]}}, self.root)
        self.assertEqual(evidence["logs"][0]["process"]["stderr"], "missing executable")
        self.assertTrue(evidence["files"]["README.md"]["truncated"])
        self.assertIn("[TRUNCATED]", evidence["files"]["README.md"]["text"])

    def test_successful_installer_noise_is_compacted_without_losing_failure_or_raw_evidence(self):
        contract = Contract.from_dict(case_data("a" * 40))
        stdout = "Downloading package progress\n" * 200 + "Successfully installed package-1.2.3\n"
        attempt = {"result": {"steps": [{"exit_code": 0, "argv": ["pip", "install", "."], "stdout": stdout}],
            "outputs": [{"name": "fixture", "exit_code": 127, "process": {
                "argv": ["tool"], "executed_argv": ["tool"], "stderr": "command missing", "stdout": ""}}]}}
        evidence = context(contract, attempt, self.root)
        self.assertIn("OMITTED", evidence["logs"][0]["stdout"])
        self.assertIn("Successfully installed package-1.2.3", evidence["logs"][0]["stdout"])
        self.assertLess(len(evidence["logs"][0]["stdout"].encode()), 320)
        self.assertEqual(evidence["logs"][1]["process"]["stderr"], "command missing")
        self.assertEqual(evidence["logs"][1]["process"]["argv"], '["tool"]')
        self.assertEqual(attempt["result"]["steps"][0]["stdout"], stdout)
        self.assertIn("executed_argv", attempt["result"]["outputs"][0]["process"])

    def test_failed_setup_stdout_is_preserved_as_diagnostic_evidence(self):
        contract = Contract.from_dict(case_data("a" * 40))
        stdout = "Critical resolver failure: incompatible version.\n" + "x" * 450
        evidence = context(contract, {"result": {"steps": [{"exit_code": 1, "stdout": stdout}]}}, self.root)
        self.assertEqual(evidence["logs"][0]["stdout"], stdout)


if __name__ == "__main__":
    unittest.main()
