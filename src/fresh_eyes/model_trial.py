"""One reviewed development trial, separate from the frozen holdout evaluation.

Planning and preview never load credentials or contact Docker/the provider.
Execution is explicit; model activation is delayed until a failed baseline.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

from .approval_io import contracts, load_transfer
from .contracts import Contract, InvalidCase, digest
from .diagnostics import RepairPlanner
from .engine import run_case, save_report
from .evaluation import atomic_json, code_files, now, wheels_at
from .model_runtime import OwnerModelFactory
from .models import ModelSpec, money, spec_dict
from .runner import DockerRunner

FORMAT = "fresh-eyes-development-model-trial-v1"
PUBLIC_FIELDS = {"model_id", "context_window", "max_completion_tokens", "input_per_million",
                 "output_per_million", "price_reference", "cached_input_per_million", "request_options"}


def load_public_specs(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise InvalidCase("Public model configuration exceeds 64 KiB")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise InvalidCase("Duplicate public model configuration field")
            value[key] = item
        return value
    raw = json.loads(path.read_text(), object_pairs_hook=unique)
    if (not isinstance(raw, dict) or set(raw) != {"nano", "ultra"} or
        any(not isinstance(v, dict) or set(v) - PUBLIC_FIELDS for v in raw.values())):
        raise InvalidCase("Only public Nano/Ultra ModelSpec fields are allowed; never credentials")
    try:
        return {key: ModelSpec(**value) for key, value in raw.items()}
    except TypeError:
        raise InvalidCase("Required public ModelSpec fields are missing") from None


def prepare_trial(project, *, case_id, approvals, source, model_config, output_root, runner_id,
                  wheelhouse=None, dependency_mode="offline_snapshot", route="nano_ultra",
                  max_patch_rounds=1, budget_usd="0", time_limit_s=600, docker="docker", web_research_enabled=False):
    project = Path(project).resolve()
    if type(web_research_enabled) is not bool:
        raise InvalidCase("Web research activation must be an explicit boolean")
    registered = contracts(project)  # Only cases/*.json, never cases/holdout.
    if case_id not in registered:
        raise InvalidCase("Select a registered development case; holdouts cannot enter a model trial")
    original = registered[case_id]
    rows, approval_checksum = load_transfer(project, approvals)
    selected = next((row for row in rows if row["case_id"] == case_id), None)
    if selected is None:
        raise InvalidCase("An existing attributed human acceptance approval is required")
    data = original.data
    review = {key: selected[key] for key in ("status", "reviewed_by", "approved_at", "scope", "acceptance_sha256")}
    data["review"] = review
    contract = Contract.from_dict(data)
    contract.check_review()
    if dependency_mode not in {"offline_snapshot", "pypi_live"} or not re.fullmatch(r"sha256:[a-f0-9]{64}", runner_id):
        raise InvalidCase("Select a dependency mode and an immutable sha256 runner image ID")
    if route not in {"nano_ultra", "all_ultra"} or type(max_patch_rounds) is not int or max_patch_rounds not in {1, 2}:
        raise InvalidCase("A development trial allows one or two explicit repair rounds")
    if type(time_limit_s) is not int or not 1 <= time_limit_s <= 3600:
        raise InvalidCase("Trial time limit must be between 1 and 3600 seconds")
    budget = money(budget_usd)
    if dependency_mode == "offline_snapshot" and wheelhouse is None:
        raise InvalidCase("Prepare the offline wheelhouse before planning a trial")
    if dependency_mode == "pypi_live" and wheelhouse is not None:
        raise InvalidCase("Live PyPI trials do not consume an offline wheelhouse")
    wheels = wheels_at(wheelhouse) if wheelhouse is not None else {}
    specs = load_public_specs(model_config)
    source = Path(source).resolve()
    try:
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                         text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        raise InvalidCase("The pinned development source must be prepared locally") from None
    if actual != data["repo"]["commit"]:
        raise InvalidCase("Development source HEAD differs from the registered commit")
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise InvalidCase("Trial output already exists; preserve it, never resume or overwrite it")
    # Bind file identities and local paths before the owner enables any spending.
    inputs = {"case_id": case_id, "approvals": str(Path(approvals).resolve()), "source": str(source),
              "model_config": str(Path(model_config).resolve()), "output_root": str(output_root),
              "wheelhouse": str(Path(wheelhouse).resolve()) if wheelhouse else None,
              "runner_id": runner_id, "dependency_mode": dependency_mode, "route": route,
              "max_patch_rounds": max_patch_rounds, "budget_usd": str(budget),
              "time_limit_s": time_limit_s, "docker": str(Path(docker).resolve()) if "/" in str(docker) else str(docker),
              "web_research_enabled": web_research_enabled}
    plan = {"format": FORMAT, "schema_version": 1, "scope": "development", "evaluation_eligible": False,
            "case_id": case_id, "repo": data["repo"], "contract_sha256": original.sha256,
            "execution_contract_sha256": contract.sha256, "acceptance_sha256": original.acceptance_sha256,
            "review": review, "approval_payload_sha256": approval_checksum,
            "models": {key: spec_dict(spec) for key, spec in specs.items()}, "wheelhouse_files": wheels,
            "code_files": code_files(project), "inputs": inputs, "max_model_requests": max_patch_rounds * 2,
            "executed": False, "model_calls": 0,
            "notice": "Local plan, not spending authorization or a result. Prices are rechecked only on explicit activation. Approval is an unsigned team snapshot."}
    plan["payload_sha256"] = digest(plan)
    return plan


def checked_plan(project, path):
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        raise InvalidCase("Development trial plan exceeds 1 MiB")
    saved = json.loads(path.read_text())
    if (not isinstance(saved, dict) or saved.get("format") != FORMAT or saved.get("schema_version") != 1 or
        digest({key: value for key, value in saved.items() if key != "payload_sha256"}) != saved.get("payload_sha256")):
        raise InvalidCase("Development trial plan format/checksum changed")
    try:
        current = prepare_trial(project, **saved["inputs"])
    except TypeError:
        raise InvalidCase("Unsupported development trial inputs") from None
    if current != saved:
        raise InvalidCase("Trial code, review or artifacts changed; prepare a new plan before execution")
    return current


def run_trial(project, plan_path, *, execute=False, enable_paid_models=False, env_file=None,
              reference_dir=None, factory_builder=None, runner_factory=None, progress_callback=None):
    plan = checked_plan(project, plan_path)
    if not execute:
        if enable_paid_models:
            raise InvalidCase("Paid activation also requires --execute; no key was read")
        return {"action": "preview", "plan_sha256": plan["payload_sha256"], "case_id": plan["case_id"],
                "max_model_requests": plan["max_model_requests"], "budget_usd": plan["inputs"]["budget_usd"],
                "repository_executed": False, "key_read": False, "model_calls": 0, "evaluation_eligible": False,
                "web_research_enabled": plan["inputs"]["web_research_enabled"],
                "max_research_credits": 2 if plan["inputs"]["web_research_enabled"] else 0}
    if enable_paid_models is not True or money(plan["inputs"]["budget_usd"]) <= 0 or not env_file or not reference_dir:
        raise InvalidCase("Execution needs explicit paid activation, a positive planned budget, env file and reviewed tokenizer references; no key was read")
    inputs = plan["inputs"]
    output = Path(inputs["output_root"])
    output.mkdir(parents=True, exist_ok=False)  # The exclusive claim also handles simultaneous starts.
    atomic_json(output / "trial-plan.json", plan)
    data = contracts(project)[plan["case_id"]].data
    data["review"] = plan["review"]
    contract = Contract.from_dict(data)
    trial_id = uuid.uuid4().hex
    deadline = time.monotonic() + inputs["time_limit_s"]
    binding = {"format": FORMAT, "scope": "development", "evaluation_eligible": False,
               "plan_sha256": plan["payload_sha256"], "trial_id": trial_id, "case_id": plan["case_id"],
               "run_state": "running", "started_at": now(), "model_calls": 0, "key_read": False}
    atomic_json(output / "trial-binding.json", binding)

    class LazyPlanner:
        route = inputs["route"]
        actual = None

        @property
        def control_secrets(self):
            return self.actual.control_secrets if self.actual is not None else ()

        def propose(self, *args):
            if self.actual is None:
                # A healthy baseline or unavailable runner never gets this far.
                binding.update(credential_activation_attempted=True, key_read=None)
                atomic_json(output / "trial-binding.json", binding)
                factory = (factory_builder or OwnerModelFactory)(
                    {key: ModelSpec(**value) for key, value in plan["models"].items()}, env_file, reference_dir, enabled=True,
                    **({"web_research_enabled": True} if inputs["web_research_enabled"] else {}))
                binding["key_read"] = True
                atomic_json(output / "trial-binding.json", binding)
                atomic_json(output / "model-access-preflight.json", factory.preflight)
                group = "D" if self.route == "all_ultra" else ("B" if inputs["max_patch_rounds"] == 1 else "C")
                self.actual = factory(contract, output, group=group, max_usd=inputs["budget_usd"], deadline=deadline, run_id=trial_id)
                if (not isinstance(self.actual, RepairPlanner) or self.actual.route != self.route or
                    {key: spec_dict(getattr(self.actual, key)) for key in ("nano", "ultra")} != plan["models"]):
                    raise InvalidCase("Trial planner must match the planned route and public model configuration")
                if (self.actual.researcher is not None) != inputs["web_research_enabled"]:
                    raise InvalidCase("Trial research activation must match its fixed plan")
                ledger = self.actual.caller.ledger
                if (ledger.run_id != trial_id or ledger.budget != money(inputs["budget_usd"]) or
                    not ledger.path.resolve().is_relative_to(output) or ledger.summary()["calls"]):
                    raise InvalidCase("A trial requires a fresh private ledger bound to this output and budget")
            return self.actual.propose(*args)

    planner = LazyPlanner()
    partial = {}
    report = None
    snapshot = None
    def progress(value):
        partial.clear()
        partial.update(value)
        binding["run_id"] = value["run_id"]
        binding["phase"] = value["current_phase"]
        atomic_json(output / "trial-binding.json", binding)
        if progress_callback is not None:
            progress_callback(value)
    try:
        runner = runner_factory(contract) if runner_factory else DockerRunner(
            image=inputs["runner_id"], platform=data["environment"]["platform"],
            dependency_mode=inputs["dependency_mode"], executable=inputs["docker"])
        if getattr(runner, "dependency_mode", "offline_snapshot") != inputs["dependency_mode"]:
            raise InvalidCase("Trial runner dependency mode differs from the plan")
        report = run_case(contract, Path(inputs["source"]), Path(inputs["wheelhouse"]) if inputs["wheelhouse"] else None,
                          output, runner=runner, repairer=planner, max_patch_rounds=inputs["max_patch_rounds"],
                          # Keep mutable execution scratch out of the report/source
                          # tree, which may be watched by desktop sync tools.
                          time_limit_s=inputs["time_limit_s"], temp_root=None, progress=progress)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Never expose provider messages or credentials in generic error output.
        report = dict(partial)
        report.update(schema_version=1, run_id=partial.get("run_id", trial_id), case_id=plan["case_id"], goal=data["goal"],
                      repo=data["repo"], review=plan["review"], contract_sha256=contract.sha256,
                      acceptance_sha256=contract.acceptance_sha256, status="infrastructure_error",
                      reason="Controller or model activation failed; preserve this trial before starting another.",
                      started_at=partial.get("started_at", binding["started_at"]), finished_at=now(),
                      dependency_mode=inputs["dependency_mode"], attempts=partial.get("attempts", []),
                      max_patch_rounds=inputs["max_patch_rounds"], repair_route=inputs["route"], time_limit_s=inputs["time_limit_s"])
        binding["run_state"] = "failed"
    finally:
        if planner.actual is not None:
            snapshot = planner.actual.caller.ledger.summary()
            atomic_json(output / "model-ledger.json", snapshot)
            binding.update(model_calls=snapshot["model_calls"], ledger_run_id=trial_id,
                           ledger_sha256=hashlib.sha256((output / "model-ledger.json").read_bytes()).hexdigest())
        if report is None:
            binding.update(run_state="interrupted", reason="No final result; do not resume or reset this trial.", finished_at=now())
        atomic_json(output / "trial-binding.json", binding)
    report.update(evaluation_eligible=False, trial_plan_sha256=plan["payload_sha256"],
                  model_calls=snapshot["model_calls"] if snapshot else 0)
    if snapshot:
        report["model_ledger"] = snapshot
        if snapshot["engineering_test_calls"]:
            report["data_origin"] = "engineering_test"
    save_report(report, output)
    origin = "engineering_test" if report.get("data_origin") == "engineering_test" or report.get("environment", {}).get("test_double") else "execution"
    if origin == "engineering_test":
        report["data_origin"] = origin
        save_report(report, output)
    record = {"schema_version": 1, "data_origin": origin, "executed": True, "record_source": "owner_local_engine",
              "api_persisted": False, "evaluation_eligible": False, "run_id": report["run_id"],
              "case_id": plan["case_id"], "goal": data["goal"], "verification_level": "functional", "repair_mode": "model",
              "repo_url": data["repo"]["url"], "commit_sha": data["repo"]["commit"], "review": plan["review"],
              "repair_author": report.get("repair_author"), "phase": "finished", "created_at": binding["started_at"],
              "run_state": "failed" if binding["run_state"] == "failed" else "completed", "result_status": report["status"],
              "acceptance_sha256": contract.acceptance_sha256, "dependency_mode": inputs["dependency_mode"],
              "started_at": report["started_at"], "finished_at": report["finished_at"], "reason": report.get("reason"),
              "evidence": report, "patch_available": bool(report.get("patch")),
              "costs": {"currency": "USD", "model_usd": float(snapshot["calculated_usd"]) if snapshot and snapshot["calculated_usd"] is not None else (0 if not snapshot else None),
                        "model_calls": snapshot["calls"] if snapshot else [], "paid_calls_enabled": False,
                        "billing_status": "engineering_test" if origin == "engineering_test" else ("usage_calculated_pending_reconciliation" if snapshot and snapshot["calculated_usd"] is not None and snapshot["calls"] else ("unknown" if snapshot and snapshot["calls"] else "local_compute_not_metered")),
                        "billed_usd": None if snapshot and snapshot["calls"] else 0, "runner_usd": None, "total_usd": None}}
    atomic_json(output / "run-record.json", record)
    binding.update(run_state=record["run_state"], result_status=report["status"], finished_at=now(),
                   report_sha256=hashlib.sha256((output / "report.json").read_bytes()).hexdigest())
    if snapshot and any(c.get("dispatch_started_at") and c.get("calculated_usd") is None for c in snapshot["calls"]):
        binding["stop_reason"] = "billing_unresolved"
    atomic_json(output / "trial-binding.json", binding)
    if report.get("patch"):
        (output / "candidate.diff").write_text(report["patch"]["diff"])
    from .handoff import write_archive
    files = {name: (output / name).read_bytes() for name in (
        "report.html", "report.json", "run-record.json", "trial-plan.json", "trial-binding.json",
        "model-ledger.json", "model-access-preflight.json", "candidate.diff") if (output / name).is_file()}
    files["OPEN_REPORTS.html"] = ("<!doctype html><meta charset='utf-8'><title>Fresh Eyes development trial</title>"
        "<h1>Fresh Eyes development trial</h1><p>Recorded development evidence; not a holdout evaluation. "
        "Opening this file does not execute a repository or call a model.</p>"
        "<p>Data origin: " + origin + "</p><a href='report.html'>Open full report</a>").encode()
    write_archive(output / "report-package.zip", files, kind="fresh-eyes-development-trial-reports-v1")
    return {"action": "executed", "scope": "development", "evaluation_eligible": False, "status": report["status"],
            "case_id": plan["case_id"], "model_calls": report["model_calls"], "key_read": binding["key_read"],
            "stop_reason": binding.get("stop_reason"), "report": str(output / "report.html"),
            "record": str(output / "run-record.json"), "package": str(output / "report-package.zip")}
