"""Zero-call capacity plan and explicitly hypothetical token-volume comparison."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def calculate(ledger, specs, cases=4, case_cap="0.03", batch_cap="0.36"):
    if type(cases) is not int or cases < 1 or cases > 100:
        raise ValueError("Use 1–100 cases")
    cap, batch = Decimal(case_cap), Decimal(batch_cap)
    if not cap.is_finite() or not batch.is_finite() or cap <= 0 or batch < 0:
        raise ValueError("Use finite positive case cap and nonnegative batch cap")
    calls = ledger["calls"]
    required = cases * 3 * cap
    scenarios = {}
    for group in ("B", "C", "D"):
        chosen = [c for c in calls if group != "B" or c["stage"] in {"classify", "diagnose"}]
        total = Decimal(0)
        for call in chosen:
            role = "ultra" if group == "D" or call["stage"].startswith("diagnose") else "nano"
            spec, usage = specs[role], call["usage"]
            if usage.get("cached_tokens", 0):
                raise ValueError("This simple scenario requires uncached historical usage")
            total += (Decimal(usage["prompt_tokens"]) * Decimal(spec["input_per_million"]) +
                      Decimal(usage["completion_tokens"]) * Decimal(spec["output_per_million"])) / 1_000_000
        scenarios[group] = {"same_tokens_one_case_usd": str(total), "same_tokens_all_cases_usd": str(total * cases),
                            "historical_call_count_repriced": len(chosen), "actually_observed_group": group == "C"}
    return {"format": "fresh-eyes-budget-plan-v1", "data_origin": "planning", "model_calls_performed": 0,
            "spending_authorized": False, "case_count": cases, "model_groups": ["B", "C", "D"],
            "max_model_requests": cases * (2 + 4 + 4), "case_group_cap_usd": str(cap),
            "batch_cap_usd": str(batch), "required_configured_capacity_usd": str(required),
            "capacity_valid": batch >= required, "shortfall_usd": str(max(Decimal(0), required - batch)),
            "same_tokens_scenarios": scenarios,
            "same_tokens_total_usd": str(sum(Decimal(v["same_tokens_all_cases_usd"]) for v in scenarios.values())),
            "runner_usd": None, "billed_usd": None,
            "limitations": ["Scenario reuses one historical md2html token volume; it is not a forecast or measured B/D experiment.",
                "All-Ultra responses could use different tokens. Rates are historical references and must be rechecked before dispatch.",
                "Configured capacity is a ceiling, not a charge. Healthy baselines make no model calls.",
                "This file grants no spending authorization and creates no formal evaluation freeze."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=4)
    parser.add_argument("--case-cap", default="0.03")
    parser.add_argument("--batch-cap", default="0.36")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = calculate(json.loads(args.ledger.read_text()), json.loads(args.models.read_text()), args.cases, args.case_cap, args.batch_cap)
    result["input_sha256"] = {"ledger": hashlib.sha256(args.ledger.read_bytes()).hexdigest(),
                              "model_config": hashlib.sha256(args.models.read_bytes()).hexdigest()}
    raw = json.dumps(result, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(raw)
    print(raw)


if __name__ == "__main__":
    main()
