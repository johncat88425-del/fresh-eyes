"""Capacity calculations are scenarios, never spending authorization."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("budget_plan", Path(__file__).resolve().parents[1] / "evaluation/budget_plan.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class BudgetPlan(unittest.TestCase):
    def test_common_capacity_and_hypothetical_route_math(self):
        ledger = {"calls": [{"stage": stage, "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
                            for stage in ("classify", "diagnose", "classify_r2", "diagnose_r2")]}
        models = {"nano": {"input_per_million": "0.06", "output_per_million": "0.24"},
                  "ultra": {"input_per_million": "1", "output_per_million": "3"}}
        r = budget.calculate(ledger, models)
        self.assertEqual(r["required_configured_capacity_usd"], "0.36")
        self.assertEqual(r["max_model_requests"], 40)
        self.assertEqual(r["same_tokens_scenarios"]["D"]["same_tokens_one_case_usd"], "0.0052")
        self.assertFalse(r["spending_authorized"])
        self.assertFalse(budget.calculate(ledger, models, batch_cap="0.30")["capacity_valid"])

    def test_invalid_counts_or_nonfinite_caps_are_rejected(self):
        for options in ({"cases": 0}, {"case_cap": "NaN"}, {"batch_cap": "Infinity"}):
            with self.assertRaises(ValueError):
                budget.calculate({"calls": []}, {}, **options)
