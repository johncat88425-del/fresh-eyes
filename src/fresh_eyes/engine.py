from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import html
import io
import json
from pathlib import Path
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

from .contracts import Contract, InvalidCase
from .patches import apply_patch, PatchMismatch
from .runner import DockerRunner, InfrastructureError
from .validators import evaluate


def export_source(source: Path, sha: str, destination: Path):
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != sha:
        raise InvalidCase("Source HEAD does not match the registered commit")
    archive = subprocess.check_output(["git", "-C", str(source), "archive", "--format=tar", sha])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        indexed = {}
        for member in members:
            path = Path(member.name)
            name = member.name.rstrip("/")
            if (not name or path.is_absolute() or ".." in path.parts or posixpath.normpath(name) != name or
                name in indexed or not (member.isdir() or member.isfile() or member.issym())):
                raise InvalidCase("Source archive contains unsupported links or paths")
            indexed[name] = member
        links = []
        for name, member in indexed.items():
            # A file link may not also act as an archive directory.
            if any(indexed.get(parent) and not indexed[parent].isdir() for parent in
                   (str(p) for p in Path(name).parents if str(p) != ".")):
                raise InvalidCase("Source archive places a file beneath a non-directory")
            if not member.issym():
                continue
            target, seen = name, set()
            while indexed.get(target) is not None and indexed[target].issym():
                if target in seen or len(seen) >= 32:
                    raise InvalidCase("Source archive contains a cyclic or excessive file link")
                seen.add(target)
                link = indexed[target].linkname
                if not link or posixpath.isabs(link):
                    raise InvalidCase("Source file link must stay inside the repository")
                target = posixpath.normpath(posixpath.join(posixpath.dirname(target), link))
                if target == ".." or target.startswith("../"):
                    raise InvalidCase("Source file link escapes the repository")
            resolved = indexed.get(target)
            if resolved is None or not resolved.isfile():
                raise InvalidCase("Source file link must resolve to an archived regular file")
            links.append((name, target, resolved))
        destination.mkdir(parents=True)
        # Never create a host symlink. Read targets only from this Git archive.
        tar.extractall(destination, members=[m for m in members if not m.issym()], filter="data")
        materialized = []
        for name, target, resolved in links:
            with tar.extractfile(resolved) as stream:
                content = stream.read()
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            output.chmod(0o755 if resolved.mode & 0o111 else 0o644)
            materialized.append({"path": name, "target": target, "sha256": hashlib.sha256(content).hexdigest()})
    return {"commit_sha": sha, "materialized_file_links": materialized,
            "notice": "Internal file links are materialized from archived target bytes; no host symlink is created."}


def save_report(report: dict, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    timeline = "".join(f'<li><strong>{html.escape(t["phase"])}</strong> — {html.escape(t["status"])}</li>' for t in report.get("attempts", []))
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Fresh Eyes — {html.escape(report['case_id'])}</title><style>
body{{font:16px/1.6 system-ui;background:#101922;color:#e5edf4;max-width:1000px;margin:50px auto;padding:0 24px}}
h1{{letter-spacing:-1.5px;font-size:42px}}.status{{color:#a4dec7;font-size:24px}}pre{{background:#192735;padding:24px;overflow:auto;border-radius:12px;white-space:pre-wrap}}small{{color:#a9bac9}}
</style><small>FRESH EYES / {html.escape(report.get('data_origin', 'local_execution'))} / MODEL CALLS: {report.get('model_calls', 0)}</small><h1>{html.escape(report['goal'])}</h1>
<p class="status">{html.escape(report['status'])}</p><p>{html.escape(report.get('reason',''))}</p>
<p>Acceptance SHA-256: <code>{report['acceptance_sha256']}</code></p><ol>{timeline}</ol>
<h2>Evidence</h2><pre>{html.escape(json.dumps(report, indent=2, ensure_ascii=False))}</pre></html>'''
    (output / "report.html").write_text(page, encoding="utf-8")
    return output / "report.html"


def run_case(contract: Contract, source: Path, wheelhouse: Path, output: Path, runner=None, patch=None, development=False,
             progress=None, temp_root=None, repairer=None, max_patch_rounds=1, time_limit_s=None):
    if patch is not None and repairer is not None:
        raise InvalidCase("Select one repair source: manual patch or diagnostic planner")
    if type(max_patch_rounds) is not int or max_patch_rounds not in {0, 1, 2} or (repairer is not None and max_patch_rounds == 0):
        raise InvalidCase("Diagnostic repair requires one or two explicit candidate rounds")
    if patch is not None and max_patch_rounds != 1:
        raise InvalidCase("Manual candidates have exactly one round, never a model evaluation protocol")
    if time_limit_s is not None and (type(time_limit_s) not in {int, float} or not 0 < time_limit_s <= 3600):
        raise InvalidCase("Configure a finite run time limit between 0 and 3600 seconds")
    deadline = time.monotonic() + time_limit_s if time_limit_s is not None else None
    if repairer is not None and deadline is not None and hasattr(repairer, "caller"):
        old_deadline = getattr(repairer.caller, "deadline", None)
        deadline = min(deadline, old_deadline) if old_deadline is not None else deadline
        repairer.caller.deadline = deadline
    contract.check_review(development)
    d = contract.data
    runner = runner or DockerRunner(platform=d["environment"]["platform"], image=d["environment"].get("image", "python:3.12-slim"))
    dependency_mode = getattr(runner, "dependency_mode", "offline_snapshot")
    report = {"schema_version": 1, "run_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
              "case_id": d["case_id"], "goal": d["goal"], "contract_sha256": contract.sha256,
              "acceptance_sha256": contract.acceptance_sha256, "repo": d["repo"], "review": d["review"],
              "status": "infrastructure_error", "attempts": [], "model_calls": 0, "repair_author": "manual" if patch else None,
              "dependency_mode": dependency_mode,
              "max_patch_rounds": max_patch_rounds if repairer is not None else (1 if patch else 0),
              "repair_route": getattr(repairer, "route", None), "repair_rounds": [], "time_limit_s": time_limit_s,
              "limitations": [("Live PyPI resolution; package versions may change between attempts."
                               if dependency_mode == "pypi_live" else "Offline snapshot; cannot observe new upstream package releases."),
                              "Development cases have not been approved by upstream maintainers.",
                              "Passing fixtures only verifies the registered workflow."]}
    def notify(phase):
        report["current_phase"] = phase
        if progress:
            progress(copy.deepcopy(report))
    def remaining():
        if deadline is None:
            return None
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("The run time limit was exhausted; no next stage was started.")
        return value
    try:
        remaining()
        notify("prepare_environment")
        report["environment"] = runner.preflight()
        with tempfile.TemporaryDirectory(prefix="fresh-eyes-", dir=temp_root) as temp:
            bundle = Path(temp).resolve() / "bootstrap"
            bundle.mkdir()
            report["source_export"] = export_source(source, d["repo"]["commit"], bundle / "repo")
            contract.check_cwd_files(bundle / "repo")
            original_repo = Path(temp).resolve() / "original_repo"
            shutil.copytree(bundle / "repo", original_repo)
            shutil.copyfile(Path(__file__).with_name("worker.py"), bundle / "worker.py")
            wheels = bundle / "wheels"
            wheels.mkdir()
            manifest = []
            if dependency_mode == "offline_snapshot" and (wheelhouse is None or not wheelhouse.is_dir()):
                raise InfrastructureError("The offline snapshot wheelhouse is unavailable")
            for path in sorted(wheelhouse.glob("*.whl")) if dependency_mode == "offline_snapshot" else []:
                if path.is_symlink() or path.stat().st_size > 100 * 1024 * 1024:
                    raise InvalidCase("Unsupported wheel artifact")
                shutil.copyfile(path, wheels / path.name)
                manifest.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            report["wheelhouse"] = manifest
            def attempt(phase, setup=None, round_index=None):
                available = remaining()
                notify(phase)
                (bundle / "payload.json").write_text(json.dumps(contract.execution_payload(setup)), encoding="utf-8")
                original_timeout = getattr(runner, "timeout", None)
                try:
                    if available is not None and original_timeout is not None:
                        runner.timeout = min(original_timeout, available)
                    result = runner.run(bundle)
                finally:
                    if original_timeout is not None:
                        runner.timeout = original_timeout
                if result.get("infrastructure_error"):
                    report["attempts"].append({"phase": phase, "round": round_index, "status": "infrastructure_error",
                                               "acceptance_sha256": contract.acceptance_sha256, "result": result})
                    raise InfrastructureError(result["infrastructure_error"])
                verdict = evaluate(contract, result)
                report["attempts"].append({"phase": phase, "round": round_index, "status": "passed" if verdict["passed"] else "failed",
                                           "acceptance_sha256": contract.acceptance_sha256, "result": result, "validation": verdict})
                notify(phase)
                return verdict["passed"]
            passed = attempt("original_readme")
            if passed:
                report["status"] = "already_passes"
            elif patch is not None or repairer is not None:
                report["status"] = "unresolved"
                rounds = max_patch_rounds if repairer is not None else 1
                for index in range(1, rounds + 1):
                    remaining()
                    # Every proposal is against the original commit. No rejected
                    # or failed candidate silently becomes the next patch base.
                    shutil.rmtree(bundle / "repo")
                    shutil.copytree(original_repo, bundle / "repo")
                    report.pop("patch", None)
                    report.pop("candidate_patch", None)
                    report["repair_author"] = None
                    if repairer is not None:
                        report["repair_round"] = index
                        notify("diagnose" if index == 1 else "diagnose_round_2")
                        diagnosis_input = copy.deepcopy(report)
                        diagnosis_input["status"] = "unresolved"
                        diagnosis = repairer.propose(contract, diagnosis_input, bundle / "repo")
                        report["diagnosis"] = {k: v for k, v in diagnosis.items() if k != "patch"}
                        report["model_calls"] = diagnosis.get("ledger", {}).get("model_calls", 0)
                        if diagnosis.get("data_origin") == "engineering_test":
                            report["data_origin"] = "engineering_test"
                        patch = diagnosis.get("patch")
                    round_record = {"round": index, "diagnosis": copy.deepcopy(report.get("diagnosis")), "status": "no_candidate"}
                    report["repair_rounds"].append(round_record)
                    if patch is None:
                        report["status"] = "needs_review" if diagnosis.get("status") == "needs_review" else "unresolved"
                        report["reason"] = diagnosis.get("reason", "Diagnosis did not yield an admissible candidate patch.")
                        break
                    report["repair_author"] = ("engineering_test" if report.get("data_origin") == "engineering_test" else "model") if repairer else "manual"
                    proposal = {"status": "proposed", "patch": copy.deepcopy(patch)}
                    report["candidate_patch"] = proposal
                    round_record["candidate_patch"] = proposal
                    try:
                        diff = apply_patch(bundle / "repo", patch,
                            secrets=getattr(repairer, "control_secrets", ()),
                            protected_paths={name for f in d["fixtures"] for name in f.get("cwd_files", {})})
                    except InvalidCase as exc:
                        proposal.update(status="rejected", reason=str(exc))
                        round_record.update(status="rejected", reason=str(exc))
                        report.update(status="needs_review", reason=f"Candidate patch rejected: {exc}")
                        if isinstance(exc, PatchMismatch) and index < rounds:
                            continue
                        break
                    proposal["status"] = "accepted_for_validation"
                    round_record["status"] = "testing"
                    report["patch"] = {"diff": diff, "sha256": hashlib.sha256(diff.encode()).hexdigest(), "round": index}
                    round_record["patch"] = copy.deepcopy(report["patch"])
                    setup = patch.get("setup")
                    trial = attempt("candidate_patch", setup, index)
                    final = attempt("fresh_verification", setup, index) if trial else False
                    if trial and final:
                        round_record["status"] = "verified_fix"
                        report["status"] = "verified_fix"
                        report.pop("reason", None)
                        break
                    round_record["status"] = "validation_failed"
                    report.update(status="unresolved", reason="Candidate or independent clean verification failed.")
            else:
                report["status"] = "unresolved"
                report["reason"] = "Original workflow failed; no repair was proposed."
    except InfrastructureError as exc:
        report.update(status="infrastructure_error", reason=str(exc))
        if report["repair_rounds"] and report["repair_rounds"][-1]["status"] == "testing":
            report["repair_rounds"][-1].update(status="infrastructure_error", reason=str(exc))
    except TimeoutError as exc:
        report.update(status="unresolved" if report.get("attempts") else "infrastructure_error",
                      reason=str(exc), stop_reason="time_limit_exceeded")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    notify("finished")
    save_report(report, output)
    return report
