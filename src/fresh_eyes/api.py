"""Loopback teammate API; models disabled unless one owner-pinned plan is activated."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import tempfile
from urllib.parse import urlsplit
import uuid

from .contracts import Contract, InvalidCase
from .engine import run_case
from .runner import DockerRunner, InfrastructureError
from . import approval_history


def now():
    return datetime.now(timezone.utc).isoformat()


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class StateLock:
    """Do not let a second API process recover the first process's active jobs."""
    def __init__(self, path):
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, 2)
                if not self.file.tell():
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("Another Fresh Eyes API owns this data directory. Stop it or choose a different --data-dir.") from None

    def close(self):
        self.file.close()


class Store:
    """Small durable queue/history; each operation has its own SQLite connection."""

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "runs.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS approvals (case_id TEXT, acceptance TEXT, body TEXT NOT NULL, PRIMARY KEY(case_id, acceptance))")
            approval_history.initialize(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, record):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                       (record["run_id"], record["created_at"], json.dumps(record, ensure_ascii=False)))

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise APIError(404, "run_not_found", "No real run exists with that ID. Examples use /api/examples.")
        return json.loads(row[0])

    def records(self):
        with self.connection() as db:
            rows = db.execute("SELECT body FROM runs ORDER BY created DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def approval(self, case_id, acceptance):
        with self.connection() as db:
            row = db.execute("SELECT body FROM approvals WHERE case_id=? AND acceptance=?", (case_id, acceptance)).fetchone()
        return json.loads(row[0]) if row else None

    def approve(self, case_id, acceptance, record):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM approvals WHERE case_id=? AND acceptance=?", (case_id, acceptance)).fetchone()
            if row:
                old = json.loads(row[0])
                if old.get("reviewed_by") != record.get("reviewed_by"):
                    raise APIError(409, "approval_exists", "An approval already exists. Use the explicit correction endpoint with a reason and current revision.")
                return old
            return approval_history.create(db, case_id, acceptance, record)

    def approval_events(self, case_id, acceptance):
        with self.connection() as db:
            return approval_history.history(db, case_id, acceptance)

    def correct_approval(self, case_id, acceptance, *, reviewer, actor, reason, expected_revision):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM approvals WHERE case_id=? AND acceptance=?", (case_id, acceptance)).fetchone()
            if not row:
                raise APIError(404, "approval_missing", "There is no approval to correct.")
            old = json.loads(row[0])
            if old.get("review_revision") != expected_revision:
                raise APIError(409, "approval_changed", "Approval changed. Reload its history before correcting it.")
            if old.get("reviewed_by") == reviewer:
                raise APIError(400, "correction_unchanged", "The reviewer is unchanged; no correction was recorded.")
            record = {**old, "reviewed_by": reviewer, "review_revision": expected_revision + 1,
                      "correction": {"reason": reason, "corrected_by": actor, "corrected_at": now(),
                                     "supersedes_revision": expected_revision}}
            approval_history.append(db, case_id, acceptance, record, "correction")
            db.execute("UPDATE approvals SET body=? WHERE case_id=? AND acceptance=?", (json.dumps(record), case_id, acceptance))
            return record

    def recover(self):
        for record in self.records():
            if record["run_state"] in {"queued", "running"}:
                record.update(run_state="failed", result_status="infrastructure_error", phase="interrupted",
                              finished_at=now(), reason="The local API stopped before this run completed; rerun explicitly.")
                self.save(record)


class Service:
    def __init__(self, project, data_dir, *, enable_runner=False, development=False, source_root=None,
                 wheelhouse=None, docker="docker", runner_factory=None, enable_live_pypi=False,
                 model_plan=None, enable_paid_models=False, env_file=None, reference_dir=None,
                 model_factory_builder=None, model_runner_factory=None, enable_github_import=False,
                 enable_startup_checks=False, startup_inspector=None, startup_runner_factory=None):
        self.project = Path(project).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.model_plan = None
        if enable_startup_checks and not (enable_github_import and enable_runner and enable_live_pypi):
            raise InvalidCase("Startup checks require --enable-github-import, --enable-runner and --enable-live-pypi")
        self.enable_github_import, self.enable_startup_checks = enable_github_import, enable_startup_checks
        from .startup import inspect_repo
        self.startup_inspector = startup_inspector or inspect_repo
        self.startup_runner_factory = startup_runner_factory or (lambda: DockerRunner(
            image="python:3.12-slim", platform="linux/arm64", executable=self.docker, dependency_mode="pypi_live"))
        if enable_paid_models and (not model_plan or not enable_runner):
            raise InvalidCase("Paid API activation needs an owner-pinned model plan and enabled runner")
        if model_plan:
            from .one_shot import OneShotPlan
            self.model_plan = OneShotPlan(self.project, model_plan, enabled=enable_paid_models,
                env_file=env_file, reference_dir=reference_dir, factory_builder=model_factory_builder,
                runner_factory=model_runner_factory)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_lock = StateLock(self.data_dir / "instance.lock")
        self.store = Store(self.data_dir)
        self.store.recover()
        self.temp = self.data_dir / "tmp"
        self.temp.mkdir(exist_ok=True)
        self.enable_runner, self.development = enable_runner, development
        self.enable_live_pypi = enable_live_pypi
        self.source_root = Path(source_root).resolve() if source_root else None
        self.wheelhouse = Path(wheelhouse).resolve() if wheelhouse else None
        self.docker = str(Path(docker).resolve()) if "/" in str(docker) else str(docker)
        self.runner_factory = runner_factory or self._docker_runner
        self.contracts = {}
        for path in sorted((self.project / "cases").glob("*.json")):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "fixtures" in data and "case_id" in data:
                contract = Contract.from_dict(data)
                self.contracts[data["case_id"]] = contract
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fresh-eyes-run")
        self.submission_lock = threading.Lock()
        self.stopping = threading.Event()

    def _docker_runner(self, contract):
        env = contract.data["environment"]
        return DockerRunner(image=env.get("image", "python:3.12-slim"), platform=env["platform"], executable=self.docker)

    def capabilities(self):
        model_available = bool(self.enable_runner and self.model_plan and self.model_plan.available)
        return {"schema_version": 1, "api_version": "0.2.1", "service": "fresh-eyes-local-api", "display_timezone": "Europe/London",
                "execution_enabled": self.enable_runner, "reviewed_cases_only": not self.enable_startup_checks,
                "functional_reviews_required": True,
                "development_review_allowed": self.development, "arbitrary_repo_execution": self.enable_startup_checks,
                "github_import": self.enable_github_import,
                "startup_checks": self.enable_startup_checks, "startup_scope": "small_public_github_python_cli_help_only",
                "functional_checks": True, "manual_candidate_patches": True,
                "autonomous_repair": model_available, "nightly_checks": False, "sandboxes": False,
                "paid_calls_enabled": model_available, "model_calls_enabled": model_available, "max_pending_runs": 4,
                "model_trial_plan": self.model_plan.summary() if self.model_plan else None,
                "approval_history": True, "approval_corrections": True,
                "dependency_modes": ["offline_snapshot", "pypi_live"], "live_pypi_enabled": self.enable_live_pypi,
                "live_pypi_platform_verified": False,
                "run_states": ["queued", "running", "completed", "failed"],
                "result_statuses": ["already_passes", "verified_fix", "startup_only", "needs_review", "needs_input",
                                    "unsupported", "unresolved", "infrastructure_error"]}

    def contract(self, case_id):
        if case_id not in self.contracts:
            raise APIError(404, "case_not_found", "Choose a registered case from GET /api/cases.")
        return self.contracts[case_id]

    def case_view(self, case_id):
        contract = self.contract(case_id)
        d = contract.data
        approval = self.store.approval(case_id, contract.acceptance_sha256)
        return {"case_id": case_id, "data_origin": "registered_contract", "repo_url": d["repo"]["url"],
                "commit_sha": d["repo"]["commit"], "goal": d["goal"], "readme_source": d["readme_source"],
                "environment": d["environment"], "acceptance_sha256": contract.acceptance_sha256,
                "contract_sha256": contract.sha256, "review": approval or d["review"],
                "upstream_maintainer_approval": False, "fixtures": d["fixtures"],
                "manual_candidate_available": self.patch_path(contract).is_file(),
                "execution_enabled": self.enable_runner,
                "can_run": self.enable_runner and (bool(approval) or d["review"].get("status") == "approved" or
                             (self.development and d["review"].get("status") == "development_reviewed"))}

    def patch_path(self, contract):
        slug = urlsplit(contract.data["repo"]["url"]).path.rstrip("/").split("/")[-1]
        return self.project / "cases" / f"{slug}-candidate-patch.json"

    def approve(self, case_id, body):
        self.only_fields(body, {"acceptance_sha256", "reviewed_by"})
        contract = self.contract(case_id)
        if body.get("acceptance_sha256") != contract.acceptance_sha256:
            raise APIError(409, "acceptance_changed", "Reload the case and review the current acceptance contract.")
        reviewer = body.get("reviewed_by")
        if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 100:
            raise APIError(400, "reviewer_required", "Provide the name of the person who reviewed the goal and both fixtures.")
        approval = {"status": "approved", "reviewed_by": reviewer.strip(), "approved_at": now(),
                    "scope": "team_acceptance_review", "acceptance_sha256": contract.acceptance_sha256}
        self.store.approve(case_id, contract.acceptance_sha256, approval)
        return self.case_view(case_id)

    def approval_events(self, case_id):
        contract = self.contract(case_id)
        return {"case_id": case_id, "acceptance_sha256": contract.acceptance_sha256,
                "items": self.store.approval_events(case_id, contract.acceptance_sha256),
                "notice": "Legacy snapshots preserve the observed current value; earlier overwritten history cannot be reconstructed."}

    def correct_approval(self, case_id, body):
        self.only_fields(body, {"acceptance_sha256", "reviewed_by", "corrected_by", "reason", "expected_revision"})
        contract = self.contract(case_id)
        if body.get("acceptance_sha256") != contract.acceptance_sha256:
            raise APIError(409, "acceptance_changed", "Reload and review the current acceptance contract.")
        for key, limit in (("reviewed_by", 100), ("corrected_by", 100), ("reason", 1000)):
            if not isinstance(body.get(key), str) or not 1 <= len(body[key].strip()) <= limit:
                raise APIError(400, "correction_required", "Provide reviewer, correction author and a bounded correction reason.")
        revision = body.get("expected_revision")
        if type(revision) is not int or revision < 1:
            raise APIError(400, "correction_required", "Provide the current positive approval revision.")
        self.store.correct_approval(case_id, contract.acceptance_sha256, reviewer=body["reviewed_by"].strip(),
            actor=body["corrected_by"].strip(), reason=body["reason"].strip(), expected_revision=revision)
        return self.case_view(case_id)

    @staticmethod
    def only_fields(body, allowed):
        if not isinstance(body, dict):
            raise APIError(400, "invalid_body", "The JSON body must be an object.")
        if set(body) - allowed:
            raise APIError(400, "unknown_fields", "Unsupported request fields: " + ", ".join(sorted(set(body) - allowed)))

    def submit(self, body):
        self.only_fields(body, {"case_id", "repo_url", "ref", "verification_level", "repair_mode", "dependency_mode"})
        dependency_mode = body.get("dependency_mode", "offline_snapshot")
        if not isinstance(dependency_mode, str) or dependency_mode not in {"offline_snapshot", "pypi_live"}:
            raise APIError(400, "invalid_dependency_mode", "Use offline_snapshot or pypi_live.")
        if dependency_mode == "pypi_live" and not self.enable_live_pypi:
            raise APIError(403, "live_pypi_disabled", "The backend owner must enable --enable-live-pypi after checking the execution platform.")
        level = body.get("verification_level", "functional")
        if level == "startup":
            if not isinstance(body.get("repo_url"), str) or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*/?", body["repo_url"]):
                raise APIError(400, "invalid_repo_url", "Use an HTTPS GitHub owner/repository URL without credentials, query or fragment.")
            if "ref" in body and (not isinstance(body["ref"], str) or not re.fullmatch(r"[a-f0-9]{40}", body["ref"])):
                raise APIError(400, "invalid_ref", "Use a full lowercase 40-character commit SHA.")
            if not self.enable_startup_checks:
                raise APIError(501, "startup_not_enabled", "The owner has not enabled bounded GitHub startup checks. Registered functional cases remain available.")
            return self.submit_startup(body, execute=True)
        if level != "functional":
            raise APIError(400, "invalid_level", "verification_level must be startup or functional.")
        case_id = body.get("case_id")
        if not isinstance(case_id, str):
            raise APIError(400, "case_required", "Functional execution requires a registered case_id.")
        contract = self.contract(case_id)
        d = contract.data
        if body.get("repo_url", d["repo"]["url"]) != d["repo"]["url"] or body.get("ref", d["repo"]["commit"]) != d["repo"]["commit"]:
            raise APIError(409, "source_mismatch", "repo_url/ref must match the registered immutable case.")
        mode = body.get("repair_mode", "none")
        if not isinstance(mode, str) or mode not in {"none", "manual_candidate", "model"}:
            raise APIError(400, "invalid_repair_mode", "Use none, manual_candidate or an explicitly activated owner model plan.")
        if mode == "manual_candidate" and not self.patch_path(contract).is_file():
            raise APIError(409, "candidate_unavailable", "This case has no prepared manual candidate patch.")
        approval = self.store.approval(case_id, contract.acceptance_sha256)
        if approval:
            d["review"] = approval
            contract = Contract.from_dict(d)
        try:
            contract.check_review(self.development)
        except InvalidCase:
            raise APIError(409, "needs_review", "Review the goal and fixtures, then POST the acceptance hash and reviewer to the approval endpoint.") from None
        if not self.enable_runner:
            raise APIError(503, "runner_disabled", "This checkout serves real contracts and labelled examples. Start with --enable-runner to execute reviewed local cases.")
        if mode == "model":
            return self._submit_model(contract, dependency_mode)
        if not self.source_root or (dependency_mode == "offline_snapshot" and (not self.wheelhouse or not self.wheelhouse.is_dir())):
            raise APIError(503, "assets_unavailable", "Configure --source-root and --wheelhouse with prepared local assets.")
        slug = urlsplit(d["repo"]["url"]).path.rstrip("/").split("/")[-1]
        source = self.source_root / slug
        if not (source / ".git").exists():
            raise APIError(503, "source_unavailable", "The pinned source checkout is not prepared locally.")
        with self.submission_lock:
            if self.stopping.is_set():
                raise APIError(503, "server_stopping", "Wait for the local API to restart.")
            if sum(r["run_state"] in {"queued", "running"} for r in self.store.records()) >= 4:
                raise APIError(429, "queue_full", "At most four active/queued runs are allowed; wait for a run to finish.")
            record = {"schema_version": 1, "run_id": uuid.uuid4().hex, "data_origin": "execution",
                      "case_id": case_id, "repo_url": d["repo"]["url"], "commit_sha": d["repo"]["commit"],
                      "verification_level": "functional", "acceptance_sha256": contract.acceptance_sha256,
                      "review": d["review"], "repair_mode": mode, "repair_author": "manual" if mode == "manual_candidate" else None,
                      "dependency_mode": dependency_mode,
                      "run_state": "queued", "result_status": None, "phase": "queued", "created_at": now(),
                      "started_at": None, "finished_at": None, "reason": None, "patch_available": False,
                      "costs": {"currency": "USD", "model_calls": [], "model_usd": 0.0, "runner_usd": None,
                                "billing_status": "local_compute_not_metered", "paid_calls_enabled": False},
                      "evidence": None}
            self.store.save(record)
            self.pool.submit(self._execute, record["run_id"], contract, source)
        return record

    def submit_startup(self, body, *, execute=False):
        from .github_source import github_repo, commit_ref, SourceError
        self.only_fields(body, {"repo_url", "ref", "verification_level", "repair_mode", "dependency_mode", "case_id"})
        try:
            github_repo(body.get("repo_url"))
            commit_ref(body.get("ref"))
        except SourceError as exc:
            raise APIError(400, exc.code, str(exc)) from None
        if body.get("repair_mode", "none") != "none":
            raise APIError(400, "startup_no_repair", "README inspection/startup cannot call a model or apply a patch.")
        if not self.enable_github_import:
            raise APIError(503, "github_import_disabled", "The owner must enable --enable-github-import for public source downloads.")
        if execute and body.get("dependency_mode", "pypi_live") != "pypi_live":
            raise APIError(400, "startup_requires_live_pypi", "New repositories require the controlled live PyPI runner, never an assumed offline snapshot.")
        with self.submission_lock:
            if self.stopping.is_set():
                raise APIError(503, "server_stopping", "Wait for the API to restart.")
            if sum(r["run_state"] in {"queued", "running"} for r in self.store.records()) >= 4:
                raise APIError(429, "queue_full", "Wait for an active check to finish.")
            record = {"schema_version":1, "run_id":uuid.uuid4().hex,
                "data_origin":"execution" if execute else "inspection", "case_id":None, "case_available":False,
                "repo_url":body["repo_url"].rstrip('/'), "commit_sha":body.get("ref"), "requested_ref":body.get("ref"),
                "verification_level":"startup", "acceptance_sha256":None, "review":{"status":"draft"},
                "repair_mode":"none", "repair_author":None, "dependency_mode":"pypi_live" if execute else None,
                "execution_requested":execute, "evaluation_eligible":False,
                "run_state":"queued", "result_status":None, "phase":"queued", "created_at":now(),
                "started_at":None, "finished_at":None, "reason":None, "patch_available":False, "evidence":None,
                "costs":{"currency":"USD", "model_calls":[], "model_usd":0.0, "runner_usd":None,
                         "billing_status":"local_compute_not_metered", "paid_calls_enabled":False}}
            self.store.save(record)
            self.pool.submit(self._execute_startup, record['run_id'])
            return record

    def _execute_startup(self, run_id):
        from .github_source import SourceError
        from .startup import run_startup
        record = self.store.get(run_id)
        record.update(run_state="running", phase="download_source", started_at=now())
        self.store.save(record)
        try:
            with tempfile.TemporaryDirectory(prefix='github-inspect-', dir=self.temp) as directory:
                source = Path(directory)/'repo'
                inspection = self.startup_inspector(record['repo_url'], record['requested_ref'], source)
                record.update(commit_sha=inspection['source']['commit_sha'], phase='inspect_readme', evidence=inspection)
                self.store.save(record)
                if self.stopping.is_set():
                    raise InfrastructureError('The API is stopping; repository execution did not start.')
                if record['execution_requested']:
                    record.update(phase='original_readme')
                    self.store.save(record)
                    report = run_startup(inspection, source, self.data_dir/'reports'/run_id,
                                         runner=self.startup_runner_factory(), temp_root=None)
                    record.update(result_status=report['status'], evidence=report, reason=report['reason'])
                else:
                    plan = inspection['plan']
                    record.update(result_status='needs_input' if plan['status']=='ready' else plan['status'],
                        reason='README plan saved. No repository command ran; enable startup execution separately.' if plan['status']=='ready' else plan['reason'])
                record.update(run_state='completed', phase='finished', finished_at=now())
        except SourceError as exc:
            infrastructure = exc.code in {'source_unavailable','source_timeout','source_http_error'}
            record.update(run_state='failed' if infrastructure else 'completed',
                result_status='infrastructure_error' if infrastructure else 'unsupported', phase='finished',
                reason=f'{exc.code}: {exc}', finished_at=now())
        except (OSError, ValueError, KeyError, InfrastructureError):
            record.update(run_state='failed', result_status='infrastructure_error', phase='failed',
                          reason='The bounded source/startup worker stopped. No retry or model call was made.', finished_at=now())
        finally:
            if record['run_state']=='running':
                record.update(run_state='failed', result_status='infrastructure_error', phase='failed', finished_at=now())
            self.store.save(record)

    def _submit_model(self, contract, dependency_mode):
        with self.submission_lock:
            if self.stopping.is_set():
                raise APIError(503, "server_stopping", "Wait for the local API to restart.")
            if not self.model_plan:
                raise APIError(403, "model_disabled", "The owner must activate a new single-use model plan locally.")
            if sum(r["run_state"] in {"queued", "running"} for r in self.store.records()) >= 4:
                raise APIError(429, "queue_full", "Wait for a run to finish before claiming the owner plan.")
            try:
                self.model_plan.check(contract, dependency_mode)
            except (InvalidCase, OSError, KeyError):
                code = "model_plan_consumed" if self.model_plan.consumed else (
                    "model_disabled" if not self.model_plan.enabled else "model_plan_mismatch")
                raise APIError(409 if code != "model_disabled" else 403, code,
                               "The fixed owner plan is unavailable or differs from this case, review or runtime; no model was called.") from None
            record = {"schema_version": 1, "run_id": uuid.uuid4().hex, "data_origin": "execution",
                "case_id": contract.data["case_id"], "repo_url": contract.data["repo"]["url"],
                "commit_sha": contract.data["repo"]["commit"], "verification_level": "functional",
                "acceptance_sha256": contract.acceptance_sha256, "review": contract.data["review"],
                "repair_mode": "model", "repair_author": None, "dependency_mode": dependency_mode,
                "model_plan_sha256": self.model_plan.plan["payload_sha256"], "evaluation_eligible": False,
                "run_state": "queued", "result_status": None, "phase": "queued", "created_at": now(),
                "started_at": None, "finished_at": None, "reason": None, "patch_available": False, "evidence": None,
                "costs": {"currency": "USD", "model_calls": [], "model_usd": None, "billed_usd": None,
                          "runner_usd": None, "paid_calls_enabled": False, "billing_status": "not_dispatched"}}
            try:
                self.model_plan.claim(record["run_id"])
            except FileExistsError:
                raise APIError(409, "model_plan_consumed", "This one-shot plan is already consumed.") from None
            self.store.save(record)
            self.pool.submit(self._execute_model, record["run_id"])
            return record

    def _execute_model(self, run_id):
        record = self.store.get(run_id)
        record.update(run_state="running", phase="prepare_environment", started_at=now())
        self.store.save(record)
        def progress(report):
            if self.stopping.is_set() and report["current_phase"] != "finished":
                raise InfrastructureError("The API is stopping; no further model stages will start.")
            record.update(phase=report["current_phase"], evidence=report)
            self.store.save(record)
        try:
            result = self.model_plan.execute(progress)
            result.pop("run_id", None)  # Preserve the API job ID and original engine ID in evidence.
            record.update(result, api_persisted=True, record_source="local_api_model_trial")
        except (InvalidCase, OSError, ValueError, KeyError, RuntimeError):
            record.update(run_state="failed", result_status="infrastructure_error", phase="failed",
                          reason="One-shot worker stopped; preserve its evidence and consumed plan. No automatic retry.",
                          finished_at=now())
        finally:
            if record["run_state"] == "running":
                record.update(run_state="failed", result_status="infrastructure_error", phase="failed", finished_at=now())
            self.store.save(record)

    def _execute(self, run_id, contract, source):
        record = self.store.get(run_id)
        record.update(run_state="running", phase="prepare_environment", started_at=now())
        self.store.save(record)

        def progress(report):
            if self.stopping.is_set() and report["current_phase"] != "finished":
                raise InfrastructureError("The API is stopping; no further attempts will be started.")
            record.update(phase=report["current_phase"], evidence=report)
            self.store.save(record)

        try:
            patch = json.loads(self.patch_path(contract).read_text()) if record["repair_mode"] == "manual_candidate" else None
            runner = self.runner_factory(contract)
            runner.dependency_mode = record["dependency_mode"]
            report = run_case(contract, source, self.wheelhouse, self.data_dir / "reports" / run_id,
                              runner=runner, patch=patch, development=self.development,
                              progress=progress, temp_root=None)
            record.update(run_state="completed", result_status=report["status"], phase="finished", evidence=report,
                          reason=report.get("reason"), patch_available=bool(report.get("patch")), finished_at=now())
        except (InvalidCase, OSError, ValueError, KeyError, InfrastructureError) as exc:
            record.update(run_state="failed", result_status="infrastructure_error", phase="failed",
                          reason=str(exc)[:2000], finished_at=now())
        finally:
            if record["run_state"] == "running":
                record.update(run_state="failed", result_status="infrastructure_error", phase="failed",
                              reason="Unexpected worker failure; inspect local diagnostics.", finished_at=now())
            self.store.save(record)

    def examples(self):
        return {p.stem: json.loads(p.read_text()) for p in sorted((self.project / "api" / "examples").glob("*.json"))}

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.recover()
        self.state_lock.close()


def make_server(service, port=8765, origins=None):
    allowed_origins = {"http://localhost:5173", "http://127.0.0.1:5173",
                       "http://localhost:3000", "http://127.0.0.1:3000"}
    allowed_origins.update(origins or [])

    class Handler(BaseHTTPRequestHandler):
        server_version = "FreshEyesLocal/0.1"

        def log_message(self, *args):
            pass  # URLs and request contents are not written to the console.

        def check_request(self):
            try:
                host = urlsplit("http://" + self.headers.get("Host", ""))
                valid = host.hostname in {"localhost", "127.0.0.1"} and host.port == self.server.server_port
            except ValueError:
                valid = False
            if not valid:
                raise APIError(403, "invalid_host", "This development API only accepts its loopback address.")
            origin = self.headers.get("Origin")
            same = {f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}
            if origin and origin not in allowed_origins | same:
                raise APIError(403, "origin_not_allowed", "Add the exact frontend origin with --allow-origin.")

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            raw = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith("application/json") else data.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin")
            same = {f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}
            if origin in allowed_origins | same:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            if self.command == "OPTIONS" and status == 204:
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise APIError(415, "json_required", "Send Content-Type: application/json.")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise APIError(400, "invalid_length", "Invalid Content-Length.") from None
            if size < 1 or size > 65536:
                raise APIError(413, "body_size", "The request must contain 1–65536 bytes of JSON.")
            try:
                return json.loads(self.rfile.read(size))
            except (ValueError, UnicodeDecodeError):
                raise APIError(400, "invalid_json", "The request body is not valid UTF-8 JSON.") from None

        def dispatch(self):
            try:
                self.connection.settimeout(10)
                self.check_request()
                path = urlsplit(self.path).path
                if self.command == "OPTIONS":
                    return self.reply(204, "", "text/plain")
                if self.command == "GET":
                    if path == "/":
                        return self.reply(200, "Fresh Eyes teammate API\nGET /api/health\nGET /api/cases\nGET /api/examples\nGET /api/runs\nGET /api/openapi.json\n", "text/plain; charset=utf-8")
                    if path == "/api/health":
                        return self.reply(200, service.capabilities())
                    if path == "/api/cases":
                        return self.reply(200, {"schema_version": 1, "items": [service.case_view(k) for k in service.contracts]})
                    match = re.fullmatch(r"/api/cases/([a-z0-9-]+)/approval/history", path)
                    if match:
                        return self.reply(200, service.approval_events(match[1]))
                    if path == "/api/runs":
                        return self.reply(200, {"schema_version": 1, "items": [{k: v for k, v in r.items() if k != "evidence"} for r in service.store.records()]})
                    if path == "/api/examples":
                        return self.reply(200, {"data_origin": "example", "executed": False, "items": service.examples()})
                    if path == "/api/openapi.json":
                        return self.reply(200, json.loads((service.project / "api/openapi.json").read_text()))
                    match = re.fullmatch(r"/api/examples/([a-z0-9-]+)", path)
                    if match:
                        data = service.examples().get(match[1])
                        if data:
                            return self.reply(200, data)
                        raise APIError(404, "example_not_found", "See GET /api/examples.")
                    match = re.fullmatch(r"/api/runs/([a-f0-9]{32})(/patch)?", path)
                    if match:
                        record = service.store.get(match[1])
                        if match[2]:
                            patch = (record.get("evidence") or {}).get("patch")
                            if not patch:
                                raise APIError(404, "patch_unavailable", "No actual patch is available for this run.")
                            return self.reply(200, patch["diff"], "text/x-diff; charset=utf-8")
                        return self.reply(200, record)
                if self.command == "POST":
                    if path == "/api/repo-inspections":
                        return self.reply(202, service.submit_startup(self.body(), execute=False))
                    if path == "/api/runs":
                        return self.reply(202, service.submit(self.body()))
                    match = re.fullmatch(r"/api/cases/([a-z0-9-]+)/approval/correction", path)
                    if match:
                        return self.reply(200, service.correct_approval(match[1], self.body()))
                    match = re.fullmatch(r"/api/cases/([a-z0-9-]+)/approval", path)
                    if match:
                        return self.reply(200, service.approve(match[1], self.body()))
                raise APIError(404, "not_found", "No such API endpoint.")
            except APIError as exc:
                self.reply(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self.reply(500, {"error": {"code": "internal_error", "message": "Local API error; inspect configuration and try again."}})

        do_GET = do_POST = do_OPTIONS = dispatch

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(project, data_dir, *, port=8765, origins=None, **kwargs):
    service = Service(project, data_dir, **kwargs)
    try:
        server = make_server(service, port, origins)
        activation = "one-shot owner plan armed" if service.capabilities()["paid_calls_enabled"] else "paid calls disabled"
        print(f"Fresh Eyes API: http://127.0.0.1:{server.server_port} ({activation})", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        service.close()
