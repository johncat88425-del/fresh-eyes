"""One owner-pinned development plan, consumed once by the loopback API.

HTTP clients cannot change a budget, model, source, review, route or output.
Attaching a plan never loads a key. Explicit owner activation is still needed.
"""
import json
from pathlib import Path

from .contracts import InvalidCase
from .model_trial import checked_plan, run_trial
from .models import money


class OneShotPlan:
    def __init__(self, project, path, *, enabled=False, env_file=None, reference_dir=None,
                 factory_builder=None, runner_factory=None):
        self.project, self.path = Path(project), Path(path)
        self.plan = checked_plan(project, path)
        self.enabled = enabled is True
        if self.enabled and (not env_file or not reference_dir or money(self.plan["inputs"]["budget_usd"]) <= 0):
            raise InvalidCase("One-shot activation needs a positive fixed budget, env file and tokenizer references")
        self.env_file, self.reference_dir = env_file, reference_dir
        self.factory_builder, self.runner_factory = factory_builder, runner_factory
        self.output = Path(self.plan["inputs"]["output_root"])
        self.claim_path = self.output.with_name(self.output.name + ".api-claim.json")

    @property
    def consumed(self):
        return self.claim_path.exists() or self.output.exists()

    @property
    def available(self):
        return self.enabled and not self.consumed

    def summary(self):
        p, inputs = self.plan, self.plan["inputs"]
        return {"case_id": p["case_id"], "acceptance_sha256": p["acceptance_sha256"],
                "plan_sha256": p["payload_sha256"], "route": inputs["route"],
                "dependency_mode": inputs["dependency_mode"], "budget_usd": inputs["budget_usd"],
                "max_patch_rounds": inputs["max_patch_rounds"], "max_model_requests": p["max_model_requests"],
                "time_limit_s": inputs["time_limit_s"], "available": self.available, "consumed": self.consumed}

    def check(self, contract, dependency_mode):
        if not self.enabled:
            raise InvalidCase("model_disabled")
        if self.consumed:
            raise InvalidCase("model_plan_consumed")
        if checked_plan(self.project, self.path) != self.plan:
            raise InvalidCase("model_plan_changed")
        p = self.plan
        if (contract.data["case_id"] != p["case_id"] or contract.acceptance_sha256 != p["acceptance_sha256"] or
                dependency_mode != p["inputs"]["dependency_mode"] or
                any(contract.data["review"].get(k) != v for k, v in p["review"].items())):
            raise InvalidCase("model_plan_mismatch")

    def claim(self, run_id):
        self.claim_path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation is shared across API data directories and restarts.
        # Even an interrupted queue item is consumed, never silently retried.
        with self.claim_path.open("x") as stream:
            json.dump({"plan_sha256": self.plan["payload_sha256"], "api_run_id": run_id,
                       "scope": "single_development_run", "consumed": True}, stream)

    def execute(self, progress):
        if not self.enabled or not self.claim_path.is_file():
            raise InvalidCase("One-shot plan has not been explicitly activated and claimed")
        run_trial(self.project, self.path, execute=True, enable_paid_models=True,
                  env_file=self.env_file, reference_dir=self.reference_dir,
                  factory_builder=self.factory_builder, runner_factory=self.runner_factory,
                  progress_callback=progress)
        return json.loads((self.output / "run-record.json").read_text())
