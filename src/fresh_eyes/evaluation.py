"""Deliberate frozen holdout baseline runner and evidence-bound result export.

No implicit key loading. Model stages require an explicitly supplied owner
factory; the normal development API never loads holdouts.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from .approval_io import timestamp
from .case_sets import verify_holdout
from .contracts import Contract, InvalidCase, digest
from .engine import run_case, save_report
from .runner import DockerRunner
from .models import ModelSpec, money, spec_dict, usage_and_cost
from .diagnostics import RepairPlanner
from .protocols import PROTOCOLS

FORMAT = "fresh-eyes-evaluation-freeze-v2"
GROUPS = ("A", "B", "C", "D")


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def code_files(project):
    project = Path(project)
    paths = list((project / "src").rglob("*.py"))
    paths += [project / name for name in ("web/evaluation.js", "api/openapi.json") if (project / name).is_file()]
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file() and not p.is_symlink()}


def wheels_at(root):
    root = Path(root)
    if not root.is_dir():
        raise InvalidCase("The frozen offline wheelhouse must be prepared before execution")
    files = {}
    for path in sorted(root.glob("*.whl")):
        if path.is_symlink() or path.stat().st_size > 100 * 1024 * 1024:
            raise InvalidCase("Unsupported frozen wheel artifact")
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def freeze(project, manifest, output, *, runner_id, dependency_mode, reviews_template=None, wheelhouse=None,
           model_specs=None, case_budget_usd="0", batch_budget_usd="0", time_limit_s=600):
    project, manifest, output = Path(project).resolve(), Path(manifest).resolve(), Path(output)
    verified = verify_holdout(manifest)
    if dependency_mode not in {"offline_snapshot", "pypi_live"} or not re.fullmatch(r"sha256:[a-f0-9]{64}", runner_id):
        raise InvalidCase("Freeze requires an explicit dependency mode and immutable sha256 runner image ID")
    if reviews_template and output.resolve() == Path(reviews_template).resolve():
        raise InvalidCase("Freeze and review template must use different files")
    if output.exists() or (reviews_template and Path(reviews_template).exists()):
        raise InvalidCase("Freeze/review template already exists; preserve it and choose a new version")
    inventory = json.loads(manifest.read_text())
    cases = []
    for row in inventory["cases"]:
        contract = Contract.load(manifest.parent / row["file"])
        cases.append({"case_id": row["case_id"], "file": row["file"], "set": "holdout",
                      "repo_url": contract.data["repo"]["url"], "commit_sha": contract.data["repo"]["commit"],
                      "acceptance_sha256": row["acceptance_sha256"], "contract_sha256": row["contract_sha256"],
                      "file_sha256": row["file_sha256"], "expected_state": None})
    files = code_files(project)
    if not files:
        raise InvalidCase("No source code inventory was found to freeze")
    prompts = {k: v for k, v in files.items() if k.endswith(("diagnostics.py", "prompt_budget.py", "models.py"))}
    if dependency_mode == "offline_snapshot" and wheelhouse is None:
        raise InvalidCase("Offline evaluation freeze requires --wheelhouse so artifact bytes are fixed")
    wheels = wheels_at(wheelhouse) if dependency_mode == "offline_snapshot" else {}
    if type(time_limit_s) is not int or not 1 <= time_limit_s <= 3600:
        raise InvalidCase("The same positive time limit is required for every comparison group")
    case_budget, batch_budget = money(case_budget_usd), money(batch_budget_usd)
    models = None
    if model_specs is not None:
        if not isinstance(model_specs, dict) or set(model_specs) != {"nano", "ultra"} or any(not isinstance(s, ModelSpec) for s in model_specs.values()):
            raise InvalidCase("Freeze only explicit public Nano/Ultra ModelSpec configurations, never credentials")
        models = {k: spec_dict(v) for k,v in model_specs.items()}
    body = {"format": FORMAT, "schema_version": 2, "frozen_at": now(), "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "freeze": {"code_version": digest(files), "prompts_sha256": digest(prompts), "runner": runner_id,
                       "dependency_mode": dependency_mode, "wheelhouse_sha256": digest(wheels)},
            "wheelhouse_files": wheels, "code_files": files, "cases": cases,
            "supported_groups": list(GROUPS), "protocols": PROTOCOLS, "models": models,
            "limits": {"case_model_usd": str(case_budget), "batch_model_usd": str(batch_budget), "time_limit_s": time_limit_s},
            "model_activation": "Owner factory required; CLI needs explicit paid flag, local env file and reviewed references. Web remains disabled.",
            "group_protocol": {"A": "original README, no patch", "B": "one Nano/Ultra patch round",
                "C": "up to two Nano/Ultra patch rounds", "D": "same as C, all Ultra"},
            "unavailable_groups": {g: "No execution recorded; explicit owner model activation and budget are required" for g in ("B", "C", "D")},
            "executed": False, "human_approval_created": False, "model_calls": 0}
    body["payload_sha256"] = digest(body)
    atomic_json(output, body)
    if reviews_template:
        atomic_json(reviews_template, {"schema_version": 1, "scope": "holdout_team_acceptance_review", "freeze_sha256": body["payload_sha256"],
            "approvals": [{"case_id": c["case_id"], "acceptance_sha256": c["acceptance_sha256"], "status": "draft",
                           "reviewed_by": None, "approved_at": None} for c in cases]})
    return {"freeze": str(output.resolve()), "payload_sha256": body["payload_sha256"], "case_count": verified["case_count"],
            "executed": False, "model_calls": 0, "supported_groups": list(GROUPS), "paid_cli_enabled": False}


def load_freeze(project, manifest, source, *, execution=False):
    if Path(source).stat().st_size > 1024 * 1024:
        raise InvalidCase("Freeze exceeds 1 MiB")
    data = json.loads(Path(source).read_text())
    saved = data.get("payload_sha256")
    if data.get("format") != FORMAT or data.get("schema_version") != 2 or digest({k:v for k,v in data.items() if k != "payload_sha256"}) != saved:
        raise InvalidCase("Evaluation freeze checksum or format changed")
    if data.get("protocols") != PROTOCOLS or data.get("supported_groups") != list(GROUPS):
        raise InvalidCase("Evaluation group protocol differs from this frozen implementation")
    verify_holdout(manifest)
    if hashlib.sha256(Path(manifest).read_bytes()).hexdigest() != data["manifest_sha256"]:
        raise InvalidCase("Holdout manifest changed after evaluation freeze")
    inventory = json.loads(Path(manifest).read_text())["cases"]
    if len(data["cases"]) != len(inventory):
        raise InvalidCase("Frozen evaluation cases differ from the holdout inventory")
    for frozen, original in zip(data["cases"], inventory):
        if any(frozen[k] != original[k] for k in ("file", "case_id", "file_sha256", "contract_sha256", "acceptance_sha256")):
            raise InvalidCase("Evaluation case identity changed")
        contract = Contract.load(Path(manifest).parent / original["file"])
        if frozen.get("repo_url") != contract.data["repo"]["url"] or frozen.get("commit_sha") != contract.data["repo"]["commit"] or frozen.get("set") != "holdout":
            raise InvalidCase("Evaluation repo, commit or set changed")
    if execution and code_files(project) != data["code_files"]:
        raise InvalidCase("Code/prompts changed after freeze; create a new labelled evaluation version")
    return data


def reviews_for(frozen, source):
    if Path(source).stat().st_size > 262144:
        raise InvalidCase("Holdout reviews exceed 256 KiB")
    body = json.loads(Path(source).read_text())
    if body.get("schema_version") != 1 or body.get("scope") != "holdout_team_acceptance_review" or body.get("freeze_sha256") != frozen["payload_sha256"]:
        raise InvalidCase("Human holdout reviews do not match this freeze")
    cases = {c["case_id"]: c for c in frozen["cases"]}
    approved = {}
    for row in body.get("approvals", []):
        case_id = row.get("case_id")
        if (case_id not in cases or case_id in approved or row.get("acceptance_sha256") != cases[case_id]["acceptance_sha256"] or
            row.get("status") != "approved" or not isinstance(row.get("reviewed_by"), str) or not 1 <= len(row["reviewed_by"].strip()) <= 100):
            raise InvalidCase("Every frozen case needs a matching attributed human approval; draft/duplicates cannot execute")
        timestamp(row.get("approved_at"))
        approved[case_id] = {"status": "approved", "reviewed_by": row["reviewed_by"].strip(), "approved_at": row["approved_at"],
                             "scope": "holdout_team_acceptance_review", "acceptance_sha256": row["acceptance_sha256"]}
    if set(approved) != set(cases):
        raise InvalidCase("All frozen holdouts require human review before any repository executes")
    return approved


def run_baselines(project, manifest, frozen_path, reviews, *, source_root, output_root, wheelhouse=None,
                  docker="docker", group="A", groups=None, runner_factory=None, repairer_factory=None):
    frozen = load_freeze(project, manifest, frozen_path, execution=True)
    selected = list(groups) if groups is not None else [group]
    if not selected or len(set(selected)) != len(selected) or any(g not in GROUPS for g in selected):
        raise InvalidCase("Choose distinct A/B/C/D groups")
    model_groups = [g for g in selected if g != "A"]
    case_budget = money(frozen["limits"]["case_model_usd"])
    batch_budget = money(frozen["limits"]["batch_model_usd"])
    if model_groups and (repairer_factory is None or frozen.get("models") is None or case_budget <= 0):
        raise InvalidCase("Owner planner plus frozen model/rate/budget configuration is required for B/C/D; CLI paid calls remain disabled")
    if model_groups and case_budget * len(model_groups) * len(frozen["cases"]) > batch_budget:
        raise InvalidCase("The batch cannot reserve every selected model slot within its frozen total budget")
    approvals = reviews_for(frozen, reviews)
    if frozen["freeze"]["dependency_mode"] == "offline_snapshot" and (wheelhouse is None or wheels_at(wheelhouse) != frozen["wheelhouse_files"]):
        raise InvalidCase("Offline wheelhouse differs from the frozen artifact inventory")
    output_root = Path(output_root)
    if output_root.exists():
        raise InvalidCase("Evaluation output already exists; never overwrite or automatically retry a previous batch")
    output_root.mkdir(parents=True)
    atomic_json(output_root / "freeze.json", frozen)
    if repairer_factory is not None and hasattr(repairer_factory, "preflight"):
        atomic_json(output_root / "model-access-preflight.json", repairer_factory.preflight)
    total_calls = 0
    # Case outermost: selected groups are interleaved on the same pinned case.
    for case in frozen["cases"]:
        original = Contract.load(Path(manifest).parent / case["file"])
        data = original.data
        data["review"] = approvals[case["case_id"]]
        contract = Contract.from_dict(data)
        for selected_group in selected:
            protocol = PROTOCOLS[selected_group]
            env = data["environment"]
            runner = runner_factory(contract) if runner_factory else DockerRunner(image=frozen["freeze"]["runner"], platform=env["platform"], executable=docker,
                                      dependency_mode=frozen["freeze"]["dependency_mode"])
            if getattr(runner, "dependency_mode", "offline_snapshot") != frozen["freeze"]["dependency_mode"]:
                raise InvalidCase("Runner dependency mode differs from freeze")
            location = output_root / (case["case_id"] + "-" + selected_group)
            location.mkdir()
            deadline = time.monotonic() + frozen["limits"]["time_limit_s"]
            binding = {"schema_version": 1, "group": selected_group, "freeze_sha256": frozen["payload_sha256"], "case_id": case["case_id"],
                       "acceptance_sha256": case["acceptance_sha256"], "frozen_contract_sha256": case["contract_sha256"],
                       "execution_contract_sha256": contract.sha256, "review": approvals[case["case_id"]],
                       "run_state": "running", "started_at": now(), "run_id": uuid.uuid4().hex,
                       "max_patch_rounds": protocol["max_patch_rounds"], "repair_route": protocol["route"], "model_calls": 0}
            atomic_json(location / "binding.json", binding)
            planner = None
            if selected_group != "A":
                planner = repairer_factory(contract, location, group=selected_group, max_usd=str(case_budget),
                                          deadline=deadline, run_id=binding["run_id"])
                if not isinstance(planner, RepairPlanner) or planner.route != protocol["route"]:
                    raise InvalidCase("Owner planner route differs from the frozen group protocol")
                if planner.researcher is not None:
                    raise InvalidCase("Web research is not enabled in the frozen evaluation protocol; prepare a separately reviewed protocol before comparing research-enabled groups")
                if {k: spec_dict(getattr(planner, k)) for k in ("nano", "ultra")} != frozen["models"]:
                    raise InvalidCase("Owner model IDs, limits or prices differ from the frozen configuration")
                ledger = planner.caller.ledger
                if ledger.budget != case_budget or not ledger.path.resolve().is_relative_to(location.resolve()) or ledger.summary()["calls"]:
                    raise InvalidCase("Each comparison slot requires a fresh local ledger with the identical frozen per-case budget")
                if ledger.run_id != binding["run_id"]:
                    raise InvalidCase("The model ledger must be bound to the evaluation job identity")
                planner.caller.deadline = deadline
                binding["ledger_run_id"] = ledger.run_id
                atomic_json(location / "binding.json", binding)
            def progress(report):
                binding["run_id"] = report["run_id"]
                atomic_json(location / "binding.json", binding)
            slug = urlsplit(data["repo"]["url"]).path.rstrip("/").split("/")[-1]
            try:
                report = run_case(contract, Path(source_root) / slug, Path(wheelhouse) if wheelhouse else None,
                    location, runner=runner, progress=progress, temp_root=None, repairer=planner,
                    max_patch_rounds=protocol["max_patch_rounds"], time_limit_s=frozen["limits"]["time_limit_s"])
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                summary = planner.caller.ledger.summary() if planner else None
                report = {"schema_version": 1, "run_id": binding["run_id"], "case_id": case["case_id"], "goal": data["goal"],
                    "repo": data["repo"], "review": data["review"], "contract_sha256": contract.sha256,
                    "acceptance_sha256": contract.acceptance_sha256, "status": "infrastructure_error", "reason": str(exc)[:2000],
                    "started_at": binding["started_at"], "finished_at": now(), "attempts": [],
                    "model_calls": summary["model_calls"] if summary else 0, "max_patch_rounds": protocol["max_patch_rounds"],
                    "repair_route": protocol["route"], "dependency_mode": frozen["freeze"]["dependency_mode"]}
                if summary:
                    report["diagnosis"] = {"route": protocol["route"], "ledger": summary}
                save_report(report, location)
            finally:
                if planner:
                    snapshot = planner.caller.ledger.summary()
                    atomic_json(location / "model-ledger.json", snapshot)
                    binding["ledger_sha256"] = hashlib.sha256((location / "model-ledger.json").read_bytes()).hexdigest()
                    binding["model_calls"] = snapshot["model_calls"]
                    atomic_json(location / "binding.json", binding)
            binding.update(run_state="completed", result_status=report["status"], run_id=report["run_id"], finished_at=now(),
                           report_sha256=hashlib.sha256((location / "report.json").read_bytes()).hexdigest())
            atomic_json(location / "binding.json", binding)
            total_calls += report["model_calls"]
            if planner and any(c.get("dispatch_started_at") and c.get("calculated_usd") is None for c in snapshot["calls"]):
                atomic_json(output_root / "batch-stop.json", {"reason": "billing_unresolved", "case_id": case["case_id"],
                    "group": selected_group, "stopped_at": now(), "notice": "No next comparison slot was started; unknown charge remains reserved."})
                raise InvalidCase("A model charge is unresolved; the batch stopped before starting the next slot. Preserve and export this failure before any new authorization")
    return {"output_root": str(output_root.resolve()), "executed_cases": len(frozen["cases"]),
            "executed_slots": len(frozen["cases"]) * len(selected), "groups": selected, "model_calls": total_calls}


def checked_ledger(location, binding, frozen):
    path = location / "model-ledger.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise InvalidCase("Unsupported model evaluation ledger")
    raw = path.read_bytes()
    ledger = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != binding.get("ledger_sha256") or
        ledger.get("run_id") != binding.get("ledger_run_id") or
        money(ledger.get("max_usd")) != money(frozen["limits"]["case_model_usd"])):
        raise InvalidCase("Model ledger identity, hash or budget differs from the frozen slot")
    protocol = PROTOCOLS[binding["group"]]
    calls = ledger.get("calls", [])
    allowed = ["classify", "diagnose", "classify_r2", "diagnose_r2"][:2 * protocol["max_patch_rounds"]]
    if not isinstance(calls, list) or [c.get("stage") for c in calls] != allowed[:len(calls)] or len(calls) > len(allowed):
        raise InvalidCase("Model stages exceed or differ from the frozen round protocol")
    actual_calls = 0
    for call in calls:
        role = "ultra" if protocol["route"] == "all_ultra" or call["stage"].startswith("diagnose") else "nano"
        spec = ModelSpec(**frozen["models"][role])
        if (call.get("data_origin") != "nebius_api" or call.get("run_id") != ledger["run_id"] or
            call.get("requested_model") != spec.model_id or call.get("price_reference") != spec.price_reference or
            money(call.get("input_per_million")) != money(spec.input_per_million) or
            money(call.get("output_per_million")) != money(spec.output_per_million) or
            call.get("cached_input_per_million") != spec.cached_input_per_million or
            call.get("request_options") != spec.request_options):
            raise InvalidCase("Model ledger origin, role, identity or price differs from the freeze")
        actual_calls += bool(call.get("dispatch_started_at"))
        if call.get("calculated_usd") is not None and call.get("dispatch_started_at"):
            usage = dict(call.get("usage") or {})
            if "cached_tokens" in usage:
                usage["prompt_tokens_details"] = {"cached_tokens": usage["cached_tokens"]}
            if "reasoning_tokens" in usage:
                usage["completion_tokens_details"] = {"reasoning_tokens": usage["reasoning_tokens"]}
            _, amount, _ = usage_and_cost({"usage": usage}, spec)
            if call.get("returned_model") != spec.model_id or amount is None or money(amount) != money(call["calculated_usd"]):
                raise InvalidCase("Calculated model cost cannot be reproduced from frozen rates and usage")
    expected = sum((money(c["calculated_usd"]) for c in calls), money("0")) if all(c.get("calculated_usd") is not None for c in calls) else None
    if (ledger.get("model_calls") != actual_calls or binding.get("model_calls") != actual_calls or
        (expected is None) != (ledger.get("calculated_usd") is None) or
        (expected is not None and money(ledger["calculated_usd"]) != expected) or
        money(ledger["held_usd"]) > money(frozen["limits"]["case_model_usd"])):
        raise InvalidCase("Model ledger totals differ from the frozen budget or individual calls")
    return ledger


def export_results(project, manifest, frozen_path, records, output, *, replace=False, human_reviews=None, review_template=None):
    frozen = load_freeze(project, manifest, frozen_path)
    roots = [Path(p) for p in records] if isinstance(records, (list, tuple)) else [Path(records)]
    output = Path(output)
    if review_template and (Path(review_template).exists() or Path(review_template).resolve() == output.resolve()):
        raise InvalidCase("Preserve existing review templates and keep the template separate from results")
    paths, batch_stops = [], []
    for root in roots:
        if not root.is_dir() or json.loads((root / "freeze.json").read_text()).get("payload_sha256") != frozen["payload_sha256"]:
            raise InvalidCase("Evaluation records directory does not match this freeze")
        paths.extend(sorted(root.glob("*/binding.json")))
        if (root / "batch-stop.json").is_file():
            batch_stops.append(json.loads((root / "batch-stop.json").read_text()))
    if output.exists() and not replace:
        raise InvalidCase("Results already exist; use --replace to publish an updated snapshot while retaining a backup")
    cases = {c["case_id"]: c for c in frozen["cases"]}
    runs, seen = [], set()
    total_held = money("0")
    for path in paths:
        if path.is_symlink() or path.stat().st_size > 262144:
            raise InvalidCase("Unsupported evaluation binding")
        b = json.loads(path.read_text())
        key = b.get("case_id"), b.get("group")
        if (key in seen or key[0] not in cases or key[1] not in GROUPS or b.get("freeze_sha256") != frozen["payload_sha256"] or
            b.get("acceptance_sha256") != cases[key[0]]["acceptance_sha256"] or b.get("frozen_contract_sha256") != cases[key[0]]["contract_sha256"]):
            raise InvalidCase("Duplicate or mismatched evaluation binding")
        protocol = PROTOCOLS[key[1]]
        if b.get("max_patch_rounds") != protocol["max_patch_rounds"] or b.get("repair_route") != protocol["route"]:
            raise InvalidCase("Evaluation slot was relabelled to a different round/route protocol")
        seen.add(key)
        ledger = None
        if key[1] != "A" and b.get("ledger_sha256"):
            ledger = checked_ledger(path.parent, b, frozen)
            total_held += money(ledger["held_usd"])
        row = {"case_id": key[0], "group": key[1], "run_id": b["run_id"], "run_state": b["run_state"],
               "result_status": None, "acceptance_sha256": b["acceptance_sha256"], "started_at": b["started_at"],
               "duration_s": None, "model_calls": ledger["model_calls"] if ledger else (0 if key[1] == "A" else None),
               "cost_usd": None, "cost_status": "unknown", "model_usd": float(ledger["calculated_usd"]) if ledger and ledger.get("calculated_usd") is not None else (0 if key[1] == "A" else None),
               "model_cost_status": "usage_calculated_pending_reconciliation" if ledger and ledger.get("calculated_usd") is not None and ledger["model_calls"] else ("no_model_calls" if key[1] == "A" or ledger and not ledger["model_calls"] else "unknown"),
               "runner_usd": None, "human_review": None, "report_sha256": b.get("report_sha256")}
        if b["run_state"] == "completed":
            report_path = path.parent / "report.json"
            if report_path.is_symlink() or report_path.stat().st_size > 4 * 1024 * 1024:
                raise InvalidCase("Unsupported evaluation report")
            raw = report_path.read_bytes()
            r = json.loads(raw)
            if r.get("status") not in {"already_passes", "verified_fix", "unresolved", "needs_review", "infrastructure_error"}:
                raise InvalidCase("Unknown functional evaluation result")
            if (hashlib.sha256(raw).hexdigest() != b.get("report_sha256") or r.get("run_id") != b["run_id"] or
                r.get("case_id") != key[0] or r.get("acceptance_sha256") != row["acceptance_sha256"] or
                r.get("contract_sha256") != b.get("execution_contract_sha256") or r.get("status") != b.get("result_status") or
                r.get("data_origin") in {"example", "engineering_test"} or r.get("environment", {}).get("test_double")):
                raise InvalidCase("Evaluation report identity, hash or origin does not match the recorded run")
            original = Contract.load(Path(manifest).parent / cases[key[0]]["file"])
            reviewed = original.data
            reviewed["review"] = b["review"]
            if Contract.from_dict(reviewed).sha256 != b["execution_contract_sha256"]:
                raise InvalidCase("Evaluation execution contract differs from the frozen case plus review")
            if r.get("max_patch_rounds") != protocol["max_patch_rounds"] or r.get("repair_route") != protocol["route"]:
                raise InvalidCase("Run round/route protocol differs from its evaluation group")
            attempts = r.get("attempts", [])
            if any(a.get("acceptance_sha256") != row["acceptance_sha256"] for a in attempts):
                raise InvalidCase("An evaluation attempt changed the fixed acceptance")
            if key[1] == "A":
                if r.get("model_calls") != 0 or r.get("patch") or r.get("repair_rounds") or r["status"] not in {"already_passes", "unresolved", "infrastructure_error"}:
                    raise InvalidCase("Original-README group contains repair or model evidence")
            else:
                if ledger is None or r.get("model_calls") != ledger["model_calls"] or len(r.get("repair_rounds", [])) > protocol["max_patch_rounds"]:
                    raise InvalidCase("Model group is missing its matching budget ledger or exceeds its round limit")
                if r.get("diagnosis", {}).get("ledger", ledger) != ledger:
                    raise InvalidCase("Engine and standalone model ledgers disagree")
                if r["status"] == "already_passes" and (ledger["model_calls"] or r.get("patch") or r.get("repair_rounds")):
                    raise InvalidCase("Healthy original workflows must not have model repair evidence")
                if r["status"] == "verified_fix" and (r.get("repair_author") != "model" or len(attempts) < 3 or
                    [a.get("phase") for a in attempts[-2:]] != ["candidate_patch", "fresh_verification"] or
                    any(a.get("status") != "passed" for a in attempts[-2:]) or
                    attempts[-1].get("round") != attempts[-2].get("round")):
                    raise InvalidCase("Model verified_fix needs candidate and independent final verification in the same round")
            if r.get("environment", {}).get("image_id", frozen["freeze"]["runner"]) != frozen["freeze"]["runner"]:
                raise InvalidCase("Evaluation runner image differs from freeze")
            timestamp(r["started_at"])
            timestamp(r["finished_at"])
            duration = (datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00")) - datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))).total_seconds()
            if duration < 0:
                raise InvalidCase("Evaluation finish time precedes start time")
            row.update(result_status=r["status"], duration_s=duration)
        elif b["run_state"] != "running":
            raise InvalidCase("Unsupported evaluation run state")
        runs.append(row)
    if total_held > money(frozen["limits"]["batch_model_usd"]):
        raise InvalidCase("Combined model holds exceed the frozen evaluation batch budget")
    result = {"schema_version": 1, "data_origin": "evaluation", "frozen_at": frozen["frozen_at"], "freeze": frozen["freeze"],
              "freeze_sha256": frozen["payload_sha256"], "protocols": frozen["protocols"], "limits": frozen["limits"],
              "cases": [{k:v for k,v in c.items() if k not in {"file","file_sha256","contract_sha256"}} for c in frozen["cases"]],
              "runs": runs, "batch_stops": batch_stops,
              "unrun": [{"case_id": c, "group": g, "reason": "Batch stopped with an unresolved charge; see batch_stops" if batch_stops else frozen["unavailable_groups"].get(g, "not run")}
                                      for c in cases for g in GROUPS if (c,g) not in seen],
              "notice": "Unknown local runner costs remain null. Model subtotals are separate. Unrun groups are explicitly listed; no repair success is implied."}
    from .result_reviews import apply_reviews, template
    if human_reviews:
        apply_reviews(result, human_reviews)
    if review_template:
        atomic_json(review_template, template(result))
    if output.exists():
        backup = output.with_name(output.stem + ".previous-" + uuid.uuid4().hex[:8] + output.suffix)
        backup.write_bytes(output.read_bytes())
    atomic_json(output, result)
    return {"output": str(output.resolve()), "runs": len(runs), "unrun": len(result["unrun"]),
            "model_calls": sum(r["model_calls"] for r in runs) if all(r["model_calls"] is not None for r in runs) else None,
            "unknown_call_counts": sum(r["model_calls"] is None for r in runs)}
