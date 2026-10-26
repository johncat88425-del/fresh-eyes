import argparse
import json
from pathlib import Path
import sys

from .contracts import Contract, InvalidCase
from .engine import run_case
from .runner import DockerRunner, doctor


def optional_model_task(parser, args):
    """One Nemotron call outside the repair loop: a maintainer note or an acceptance draft. Both stay drafts."""
    import time
    import uuid
    from .model_trial import load_public_specs
    from .models import money
    spec = load_public_specs(args.model_config)[args.role]
    if args.output.exists():
        parser.error("use a new output directory; older drafts and ledgers are preserved")
    if args.command == "maintainer-note":
        from .maintainer_note import SYSTEM, MaintainerNoteWriter, evidence_for
        if args.report.stat().st_size > 4 * 1024 * 1024:
            parser.error("report exceeds 4 MiB")
        raw = args.report.read_bytes()
        report = json.loads(raw)
        if isinstance(report.get("evidence"), dict) and "attempts" in report["evidence"]:
            report = report["evidence"]  # an API run record wraps the engine report
        size = len(SYSTEM.encode()) + len(json.dumps(evidence_for(report), ensure_ascii=False).encode())  # refuses non-fixes before any key is read
    else:
        from .acceptance_drafts import SYSTEM, AcceptanceDraftPlanner
        inspection = json.loads((args.inspection / "inspection.json").read_text())
        if inspection["plan"]["status"] != "ready":
            parser.error("the inspection found no ready README startup plan; a draft would have nothing to cite")
        size = len(SYSTEM.encode()) + 16384 * 2  # numbered README (at most 16 KiB) plus setup and startup
    estimate = spec.reserve_for_input(min(spec.context_window - spec.max_completion_tokens, size + 2048))
    if not args.enable_paid_models:
        print(json.dumps({"action": "preview", "task": args.command, "model": spec.model_id, "model_calls": 0, "key_read": False,
                          "max_completion_tokens": spec.max_completion_tokens, "estimated_reservation_usd": str(estimate),
                          "budget_usd": args.budget_usd}, indent=2))
        return 0
    if money(args.budget_usd) < estimate or not args.env_file or not args.tokenizer_references:
        parser.error("paid activation needs --env-file, --tokenizer-references and a budget covering the reservation; no key was read")
    from .model_runtime import OwnerModelFactory
    factory = OwnerModelFactory({args.role: spec}, args.env_file, args.tokenizer_references, enabled=True)
    args.output.mkdir(parents=True)
    (args.output / "catalog-preflight.json").write_text(json.dumps(factory.preflight, indent=2) + "\n")
    caller = factory.caller(args.output / "private-ledger.sqlite3", uuid.uuid4().hex, args.budget_usd, deadline=time.monotonic() + 120)
    try:
        if args.command == "maintainer-note":
            result = MaintainerNoteWriter(caller, spec).write(report, raw)
            (args.output / "note.md").write_text(result["markdown"])
        else:
            result = AcceptanceDraftPlanner(caller, spec).propose(inspection, args.inspection / "source",
                                                                 environment={"platform": args.platform, "image": args.image})
            if result.get("contract"):
                (args.output / "draft-contract.json").write_text(json.dumps(result["contract"], indent=2, ensure_ascii=False) + "\n")
    finally:
        (args.output / "ledger.json").write_text(json.dumps(caller.ledger.summary(), indent=2) + "\n")
    (args.output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"action": "executed", "task": args.command, "status": result.get("status"), "output": str(args.output.resolve()),
                      "calculated_usd": result["ledger"]["calculated_usd"], "human_review_required": True}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="fresh-eyes", description="Verify a documented CLI workflow using fixed functional fixtures.")
    sub = parser.add_subparsers(dest="command", required=True)
    diagnostic = sub.add_parser("doctor", help="Check local prerequisites without exposing keys")
    diagnostic.add_argument("--docker", default="docker")
    egress = sub.add_parser("verify-egress", help="Test real Docker PyPI-only egress and cleanup; no repo, models or credentials")
    egress.add_argument("--docker", default="docker")
    egress.add_argument("--image", default="python:3.12-slim")
    egress.add_argument("--platform", default="linux/arm64")
    egress.add_argument("--output", type=Path, required=True, help="Save platform-check evidence as JSON")
    sandbox = sub.add_parser("sandboxes-doctor", help="Check Sandboxes configuration; optional read-only access queries, never execute code")
    sandbox.add_argument("--env-file", type=Path, help="Read only NEBIUS_API_KEY and NEBIUS_PROJECT_ID from this local file")
    sandbox.add_argument("--check-remote", action="store_true", help="GET account permissions and image metadata; never submit an operation")
    web = sub.add_parser("serve", help="Serve loopback API; models disabled unless one owner plan is explicitly armed")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--data-dir", type=Path, default=Path(".fresh-eyes/api"))
    web.add_argument("--enable-runner", action="store_true", help="Allow reviewed local Docker runs; GitHub startup has additional explicit flags")
    web.add_argument("--enable-github-import", action="store_true", help="Allow bounded, credential-free public GitHub README inspections")
    web.add_argument("--enable-startup-checks", action="store_true", help="Allow README-only GitHub startup checks; also requires runner, live PyPI and GitHub import")
    web.add_argument("--enable-live-pypi", action="store_true", help="Allow opting into isolated PyPI-only execution; requires a capable Docker engine")
    web.add_argument("--development", action="store_true", help="Allow existing development-reviewed cases without claiming human approval")
    web.add_argument("--source-root", type=Path)
    web.add_argument("--wheelhouse", type=Path)
    web.add_argument("--docker", default="docker")
    web.add_argument("--allow-origin", action="append", help="Add an exact frontend origin to default localhost ports 3000/5173; repeat to add more")
    web.add_argument("--model-plan", type=Path, help="Attach one fixed development plan; attaching alone never enables models")
    web.add_argument("--enable-paid-models", action="store_true", help="Owner-only: arm the attached plan once; requires new spending authorization")
    web.add_argument("--env-file", type=Path, help="Local credentials; loaded only after the fixed baseline fails")
    web.add_argument("--tokenizer-references", type=Path)
    check = sub.add_parser("validate", help="Validate a case without executing repository code")
    check.add_argument("case", type=Path)
    repo_inspect = sub.add_parser("repo-inspect", help="Download bounded public GitHub source and save a README startup plan; never executes code or calls a model")
    repo_inspect.add_argument("repo_url")
    repo_inspect.add_argument("--ref", help="Full commit SHA; omitted means resolve the default branch once")
    repo_inspect.add_argument("--output", type=Path, required=True, help="New directory containing source, inspection.json and an HTML report")
    plan = sub.add_parser("repair-plan", help="Plan Nano/Ultra diagnosis from existing evidence; no keys, network or execution")
    plan.add_argument("case", type=Path)
    plan.add_argument("--evidence", type=Path, required=True, help="The engine report.json, not a UI example")
    plan.add_argument("--route", choices=["nano_ultra", "all_ultra"], default="nano_ultra")
    plan.add_argument("--max-patch-rounds", type=int, choices=[1, 2], default=1)
    trial_plan = sub.add_parser("model-trial-plan", help="Prepare one human-approved development trial; no keys, Docker or network")
    trial_plan.add_argument("--case-id", required=True)
    trial_plan.add_argument("--approvals", type=Path, required=True, help="Existing approvals-export file; never creates an approval")
    trial_plan.add_argument("--source", type=Path, required=True)
    trial_plan.add_argument("--model-config", type=Path, required=True, help="Public Nano/Ultra ModelSpec fields only")
    trial_plan.add_argument("--runner-id", required=True)
    trial_plan.add_argument("--dependency-mode", choices=["offline_snapshot", "pypi_live"], required=True)
    trial_plan.add_argument("--wheelhouse", type=Path)
    trial_plan.add_argument("--route", choices=["nano_ultra", "all_ultra"], default="nano_ultra")
    trial_plan.add_argument("--max-patch-rounds", type=int, choices=[1, 2], default=1)
    trial_plan.add_argument("--enable-web-research", action="store_true", help="Bind optional Tavily research to this development plan; at most 2 basic credits and prepaid policy still required")
    trial_plan.add_argument("--budget-usd", default="0")
    trial_plan.add_argument("--time-limit-s", type=int, default=600)
    trial_plan.add_argument("--docker", default="docker")
    trial_plan.add_argument("--output-root", type=Path, required=True, help="New, unused trial directory; preview does not create it")
    trial_plan.add_argument("--output", type=Path, required=True, help="Save the public plan without overwriting an older version")
    trial_run = sub.add_parser("model-trial-run", help="Preview a saved development plan; explicit flags are required for execution")
    trial_run.add_argument("--plan", type=Path, required=True)
    trial_run.add_argument("--execute", action="store_true")
    trial_run.add_argument("--enable-paid-models", action="store_true")
    trial_run.add_argument("--env-file", type=Path)
    trial_run.add_argument("--tokenizer-references", type=Path)
    note = sub.add_parser("maintainer-note", help="Draft a note to the maintainer from a verified_fix report with one Nemotron call; never posts")
    note.add_argument("--report", type=Path, required=True, help="The engine report.json of a verified fix")
    draft = sub.add_parser("draft-acceptance", help="Ask Nemotron to propose a functional check for an inspected repo; never approves or runs it")
    draft.add_argument("--inspection", type=Path, required=True, help="A directory written by repo-inspect")
    draft.add_argument("--image", default="python:3.12-slim")
    draft.add_argument("--platform", default="linux/arm64")
    for task, role in ((note, "nano"), (draft, "ultra")):
        task.add_argument("--model-config", type=Path, required=True, help="Public Nano/Ultra ModelSpec fields only")
        task.add_argument("--role", choices=["nano", "ultra"], default=role)
        task.add_argument("--budget-usd", default="0")
        task.add_argument("--output", type=Path, required=True, help="New directory for the draft and its private ledger")
        task.add_argument("--enable-paid-models", action="store_true", help="Owner-only: send the one call; without it, only a preview")
        task.add_argument("--env-file", type=Path)
        task.add_argument("--tokenizer-references", type=Path)
    holdout = sub.add_parser("verify-holdout", help="Check frozen case hashes without execution or human approval")
    holdout.add_argument("--manifest", type=Path, default=Path("cases/holdout/freeze.json"))
    eval_freeze = sub.add_parser("evaluation-freeze", help="Freeze code, prompts, cases and runner identity; never execute holdouts")
    eval_freeze.add_argument("--manifest", type=Path, default=Path("cases/holdout/freeze.json"))
    eval_freeze.add_argument("--output", type=Path, required=True)
    eval_freeze.add_argument("--runner-id", required=True, help="Immutable sha256 Docker image ID, not a mutable tag")
    eval_freeze.add_argument("--dependency-mode", choices=["offline_snapshot", "pypi_live"], required=True)
    eval_freeze.add_argument("--reviews-template", type=Path)
    eval_freeze.add_argument("--wheelhouse", type=Path, help="Required for offline freeze; fixes every wheel's SHA-256")
    eval_freeze.add_argument("--model-config", type=Path, help="Optional public Nano/Ultra ModelSpec JSON; never credentials")
    eval_freeze.add_argument("--case-budget-usd", default="0", help="Same model cap for every B/C/D slot; zero disables model slots")
    eval_freeze.add_argument("--batch-budget-usd", default="0", help="Total model reservation cap for the selected batch")
    eval_freeze.add_argument("--time-limit-s", type=int, default=600)
    eval_run = sub.add_parser("evaluation-run", help="Run frozen reviewed evaluation groups; models require explicit local paid activation")
    eval_run.add_argument("--manifest", type=Path, default=Path("cases/holdout/freeze.json"))
    eval_run.add_argument("--freeze", type=Path, required=True)
    eval_run.add_argument("--reviews", type=Path, required=True)
    eval_run.add_argument("--source-root", type=Path, required=True)
    eval_run.add_argument("--output-root", type=Path, required=True)
    eval_run.add_argument("--wheelhouse", type=Path)
    eval_run.add_argument("--docker", default="docker")
    selection = eval_run.add_mutually_exclusive_group()
    selection.add_argument("--group", choices=["A", "B", "C", "D"], default="A")
    selection.add_argument("--groups", nargs="+", choices=["A", "B", "C", "D"])
    eval_run.add_argument("--execute", action="store_true", required=True, help="Explicitly enter the formal holdout execution path")
    eval_run.add_argument("--enable-paid-models", action="store_true", help="Explicitly activate the frozen model budget; default never reads a key")
    eval_run.add_argument("--env-file", type=Path, help="Explicit local key file; required with paid activation")
    eval_run.add_argument("--tokenizer-references", type=Path, help="Reviewed local NVIDIA tokenizer/template references; required with paid activation")
    eval_export = sub.add_parser("evaluation-export", help="Publish verified frozen run evidence to the frontend results format")
    eval_export.add_argument("--manifest", type=Path, default=Path("cases/holdout/freeze.json"))
    eval_export.add_argument("--freeze", type=Path, required=True)
    eval_export.add_argument("--records", type=Path, required=True, action="append", help="Repeat to merge distinct groups from the same freeze")
    eval_export.add_argument("--output", type=Path, default=Path("evaluation/results.json"))
    eval_export.add_argument("--replace", action="store_true")
    eval_export.add_argument("--human-reviews", type=Path, help="Merge attributed post-run patch reviews bound to exact report hashes")
    eval_export.add_argument("--review-template", type=Path, help="Write a new draft post-run review template; never approves results")
    approval_export = sub.add_parser("approvals-export", help="Export recorded human team reviews; never creates approvals from draft/development review")
    approval_export.add_argument("--data-dir", type=Path, default=Path(".fresh-eyes/api"))
    approval_export.add_argument("--output", type=Path, required=True)
    approval_import = sub.add_parser("approvals-import", help="Preview a team review transfer; --apply imports matching records atomically")
    approval_import.add_argument("input", type=Path)
    approval_import.add_argument("--data-dir", type=Path, default=Path(".fresh-eyes/api"))
    approval_import.add_argument("--apply", action="store_true", help="Apply the reviewed unsigned team record; preserves the original reviewer/time")
    run = sub.add_parser("run", help="Run a reviewed case in new offline or PyPI-only Docker containers")
    run.add_argument("case", type=Path)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--wheelhouse", type=Path)
    run.add_argument("--dependency-mode", choices=["offline_snapshot", "pypi_live"], default="offline_snapshot")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--patch", type=Path)
    run.add_argument("--development", action="store_true")
    run.add_argument("--docker", default="docker")
    args = parser.parse_args()
    try:
        if args.command == "repo-inspect":
            from .startup import inspect_repo, save
            if args.output.exists():
                parser.error("use a new inspection directory; older source and evidence are preserved")
            info = inspect_repo(args.repo_url, args.ref, args.output/'source')
            (args.output/'inspection.json').write_text(json.dumps(info, indent=2, ensure_ascii=False)+'\n')
            save(info, args.output/'report')
            print(json.dumps({'status':info['plan']['status'], 'commit_sha':info['source']['commit_sha'],
                              'file_count':info['source']['file_count'], 'executed':False, 'model_calls':0,
                              'inspection':str(args.output/'inspection.json')}, indent=2))
            return 0
        if args.command in {"maintainer-note", "draft-acceptance"}:
            return optional_model_task(parser, args)
        if args.command == "sandboxes-doctor":
            from .sandboxes import SandboxConfig, SandboxDiagnosticError, diagnose
            try:
                info = diagnose(SandboxConfig.load(args.env_file), remote=args.check_remote)
            except SandboxDiagnosticError as exc:
                info = {"status": exc.code, "reason": str(exc), "operations_submitted": 0, "model_calls": 0}
            print(json.dumps(info, indent=2))
            return 0 if info["status"] in {"configuration_ready", "access_verified"} else 2
        if args.command == "doctor":
            info = doctor(args.docker)
            print(json.dumps(info, indent=2))
            return 2 if "runtime_error" in info else 0
        if args.command == "verify-egress":
            from .network_gate import verify_egress
            info = verify_egress(args.output, docker=args.docker, image=args.image, platform=args.platform)
            print(json.dumps({"status": info["status"], "report": str(args.output.resolve()),
                              "reason": info.get("reason"), "model_calls": 0, "repository_executed": False}, indent=2))
            return 0 if info["status"] == "passed" else 2
        if args.command == "serve":
            from .api import serve
            if not 1 <= args.port <= 65535:
                parser.error("port must be between 1 and 65535")
            serve(Path(__file__).resolve().parents[2], args.data_dir, port=args.port,
                  origins=args.allow_origin, enable_runner=args.enable_runner, development=args.development,
                  source_root=args.source_root, wheelhouse=args.wheelhouse, docker=args.docker,
                  enable_live_pypi=args.enable_live_pypi, model_plan=args.model_plan,
                  enable_paid_models=args.enable_paid_models, env_file=args.env_file,
                  reference_dir=args.tokenizer_references, enable_github_import=args.enable_github_import,
                  enable_startup_checks=args.enable_startup_checks)
            return 0
        if args.command == "verify-holdout":
            from .case_sets import verify_holdout
            print(json.dumps(verify_holdout(args.manifest), indent=2))
            return 0
        if args.command in {"model-trial-plan", "model-trial-run"}:
            from .model_trial import prepare_trial, run_trial
            project = Path(__file__).resolve().parents[2]
            if args.command == "model-trial-plan":
                if args.output.exists():
                    parser.error("preserve the existing plan; choose a new version")
                info = prepare_trial(project, case_id=args.case_id, approvals=args.approvals, source=args.source,
                    model_config=args.model_config, output_root=args.output_root, runner_id=args.runner_id,
                    wheelhouse=args.wheelhouse, dependency_mode=args.dependency_mode, route=args.route,
                    max_patch_rounds=args.max_patch_rounds, budget_usd=args.budget_usd,
                    time_limit_s=args.time_limit_s, docker=args.docker, web_research_enabled=args.enable_web_research)
                if args.output.resolve() == args.output_root.resolve() or args.output_root.resolve() in args.output.resolve().parents:
                    parser.error("save the plan outside the unused trial output directory")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as stream:
                    stream.write(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
                info = {"plan": str(args.output.resolve()), "payload_sha256": info["payload_sha256"],
                        "case_id": info["case_id"], "max_model_requests": info["max_model_requests"],
                        "budget_usd": info["inputs"]["budget_usd"], "repository_executed": False, "model_calls": 0}
            else:
                info = run_trial(project, args.plan, execute=args.execute, enable_paid_models=args.enable_paid_models,
                                 env_file=args.env_file, reference_dir=args.tokenizer_references)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 2 if info.get("action") == "executed" and info.get("status") not in {"already_passes", "verified_fix"} else 0
        if args.command.startswith("evaluation-"):
            from .evaluation import freeze, run_baselines, export_results
            project = Path(__file__).resolve().parents[2]
            if args.command == "evaluation-freeze":
                specs = None
                if args.model_config:
                    from .models import ModelSpec
                    if args.model_config.stat().st_size > 65536:
                        parser.error("model config exceeds 64 KiB")
                    raw = json.loads(args.model_config.read_text())
                    allowed = {"model_id", "context_window", "max_completion_tokens", "input_per_million", "output_per_million", "price_reference", "cached_input_per_million", "request_options"}
                    if not isinstance(raw, dict) or set(raw) != {"nano", "ultra"} or any(not isinstance(v, dict) or set(v) - allowed for v in raw.values()):
                        parser.error("model config must contain only public Nano/Ultra ModelSpec fields")
                    try:
                        specs = {k: ModelSpec(**v) for k,v in raw.items()}
                    except TypeError:
                        parser.error("model config is missing required public ModelSpec fields")
                info = freeze(project, args.manifest, args.output, runner_id=args.runner_id,
                              dependency_mode=args.dependency_mode, reviews_template=args.reviews_template, wheelhouse=args.wheelhouse,
                              model_specs=specs, case_budget_usd=args.case_budget_usd, batch_budget_usd=args.batch_budget_usd,
                              time_limit_s=args.time_limit_s)
            elif args.command == "evaluation-run":
                factory = None
                if args.enable_paid_models:
                    from .evaluation import load_freeze, reviews_for, wheels_at
                    from .models import ModelSpec, money
                    from .model_runtime import OwnerModelFactory
                    frozen = load_freeze(project, args.manifest, args.freeze, execution=True)
                    chosen = args.groups or [args.group]
                    reviews_for(frozen, args.reviews)
                    model_slots = sum(g != "A" for g in chosen) * len(frozen["cases"])
                    if (not model_slots or len(set(chosen)) != len(chosen) or not frozen.get("models") or
                        money(frozen["limits"]["case_model_usd"]) <= 0 or
                        money(frozen["limits"]["case_model_usd"]) * model_slots > money(frozen["limits"]["batch_model_usd"])):
                        parser.error("paid activation needs distinct model groups and matching frozen public configuration/budgets")
                    if args.output_root.exists():
                        parser.error("preserve the existing output; do not repeat or reset its paid run")
                    if frozen["freeze"]["dependency_mode"] == "offline_snapshot" and (args.wheelhouse is None or wheels_at(args.wheelhouse) != frozen["wheelhouse_files"]):
                        parser.error("wheelhouse differs from freeze; no key was read")
                    if not args.env_file or not args.tokenizer_references:
                        parser.error("explicit --env-file and --tokenizer-references are required; no key was read")
                    specs = {k: ModelSpec(**v) for k,v in frozen["models"].items()}
                    factory = OwnerModelFactory(specs, args.env_file, args.tokenizer_references, enabled=True)
                info = run_baselines(project, args.manifest, args.freeze, args.reviews, source_root=args.source_root,
                    output_root=args.output_root, wheelhouse=args.wheelhouse, docker=args.docker, group=args.group, groups=args.groups,
                    repairer_factory=factory)
            else:
                info = export_results(project, args.manifest, args.freeze, args.records, args.output, replace=args.replace,
                                      human_reviews=args.human_reviews, review_template=args.review_template)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"approvals-export", "approvals-import"}:
            from .approval_io import export_approvals, import_approvals
            project = Path(__file__).resolve().parents[2]
            if args.command == "approvals-export":
                info = export_approvals(project, args.data_dir, args.output)
            else:
                info = import_approvals(project, args.data_dir, args.input, apply=args.apply)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0
        contract = Contract.load(args.case)
        if args.command == "repair-plan":
            from .diagnostics import repair_plan
            if args.evidence.stat().st_size > 2 * 1024 * 1024:
                parser.error("evidence exceeds 2 MiB")
            evidence = json.loads(args.evidence.read_text())
            if not isinstance(evidence, dict) or evidence.get("data_origin") == "example":
                parser.error("use an engine evidence object, not a UI example")
            print(json.dumps(repair_plan(contract, evidence, route=args.route, max_patch_rounds=args.max_patch_rounds), indent=2))
            return 0
        if args.command == "validate":
            print(json.dumps({"case_id": contract.data["case_id"], "contract_sha256": contract.sha256,
                              "acceptance_sha256": contract.acceptance_sha256, "review": contract.data["review"], "executed": False}, indent=2))
            return 0
        d = contract.data
        runner = DockerRunner(image=d["environment"].get("image", "python:3.12-slim"), platform=d["environment"]["platform"], executable=args.docker,
                              dependency_mode=args.dependency_mode)
        report = run_case(contract, args.source.resolve(), args.wheelhouse.resolve() if args.wheelhouse else None, args.output.resolve(), runner=runner,
                          patch=json.loads(args.patch.read_text()) if args.patch else None, development=args.development)
        print(json.dumps({"status": report["status"], "case_id": report["case_id"], "report": str(args.output.resolve() / "report.html")}, indent=2))
        return 0 if report["status"] in {"already_passes", "verified_fix"} else 2
    except (InvalidCase, ValueError, KeyError, OSError) as exc:
        print(f"fresh-eyes: {exc}", file=sys.stderr)
        return 1
