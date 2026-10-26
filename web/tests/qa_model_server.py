"""Free, repeatable browser QA for the single-use model UI (B18). SYNTHETIC ONLY.

Runs the real API Service with A's test doubles from tests/test_model_trial.py: a self-made git repo, scripted runner
verdicts and scripted Nano/Ultra replies. It never reads .env, never calls a provider, never touches the formal data
directory, and deletes its temporary folder on exit.

  python3.12 web/tests/qa_model_server.py --scenario success        # round 1 fails its check, round 2 verified
  python3.12 web/tests/qa_model_server.py --scenario no-candidate   # round 1 rejected (no match), round 2 no patch
  python3.12 web/tests/qa_model_server.py --disabled                # plan attached, paid models not enabled

API on http://127.0.0.1:8771 (frontend: python3 web/serve.py 3000, then http://localhost:3000/?api=http://127.0.0.1:8771).
`kill -HUP <pid>` restarts the service on the same data directory. A consumed plan must be refused at restart (A's guard
against re-arming); the script then starts a normal API without the plan, as the host would, so history stays readable.
"""
import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from fresh_eyes.api import Service, make_server  # noqa: E402
from fresh_eyes.diagnostics import RepairPlanner  # noqa: E402
from fresh_eyes.models import ModelCaller, ModelLedger  # noqa: E402
from fresh_eyes.protocols import PROTOCOLS  # noqa: E402
import shutil  # noqa: E402
import test_model_trial as trial_fixture  # noqa: E402
from test_core import ScriptedRunner  # noqa: E402
from test_models import SyntheticTransport  # noqa: E402

CLASSIFY = {"category": "documentation", "reason": "SYNTHETIC failure for UI QA"}
SCENARIOS = {
    # Runner verdicts per attempt, and the scripted model replies in call order.
    "success": ((False, False, True, True), [
        CLASSIFY, {"explanation": "SYNTHETIC round 1 proposal", "patch": {"edits": [{"path": "README.md", "old": "old", "new": "new"}]}},
        CLASSIFY, {"explanation": "SYNTHETIC round 2 proposal", "patch": {"edits": [{"path": "README.md", "old": "old", "new": "new"}]}}]),
    "no-candidate": ((False,), [
        CLASSIFY, {"explanation": "SYNTHETIC round 1 proposal that quotes text not in the README",
                   "patch": {"edits": [{"path": "README.md", "old": "text that is not there", "new": "new"}]}},
        CLASSIFY, {"explanation": "SYNTHETIC round 2: cannot see enough to propose an exact edit", "patch": None}]),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="success")
    parser.add_argument("--disabled", action="store_true", help="attach the plan without enabling paid models")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    verdicts, answers = SCENARIOS[args.scenario]

    fixture = trial_fixture.ModelTrialTests()
    fixture.setUp()  # temporary self-made project, git source, synthetic approval and model specs
    shutil.copytree(ROOT / "api", fixture.project / "api")
    plan = fixture.plan(max_patch_rounds=2)

    def builder(specs, env_file, reference_dir, *, enabled):
        owner = Mock()
        owner.preflight = {"synthetic": True, "metadata_only": True}
        def create(contract, location, **options):
            ledger = ModelLedger(location / "private-ledger.sqlite3", options["run_id"], options["max_usd"])
            return RepairPlanner(ModelCaller(SyntheticTransport(list(answers)), ledger, deadline=options["deadline"]),
                                 specs["nano"], specs["ultra"], route=PROTOCOLS[options["group"]]["route"])
        owner.side_effect = create
        return owner

    def make_service(with_plan=True):
        if not with_plan:
            return Service(fixture.project, fixture.root / "state", enable_runner=True)
        service = Service(fixture.project, fixture.root / "state", enable_runner=True, model_plan=fixture.plan_path,
                          enable_paid_models=not args.disabled, env_file=fixture.root / "NEVER_READ.env",
                          reference_dir=fixture.root / "NEVER_READ-refs", model_factory_builder=builder,
                          model_runner_factory=lambda c: ScriptedRunner(list(verdicts)))
        if not service.store.approval(fixture.contract.data["case_id"], fixture.contract.acceptance_sha256):
            service.store.approve(fixture.contract.data["case_id"], fixture.contract.acceptance_sha256, plan["review"])
        return service

    state = {"service": make_service()}
    state["server"] = make_server(state["service"], port=args.port)

    def restart(*_):
        print("SIGHUP: restarting the service on the same data directory", flush=True)
        server, service = state["server"], state["service"]
        threading.Thread(target=server.shutdown).start()
    signal.signal(signal.SIGHUP, restart)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"SYNTHETIC model UI QA: scenario={args.scenario} paid_models={'off' if args.disabled else 'on (test double)'} "
          f"case={fixture.contract.data['case_id']} api=http://127.0.0.1:{args.port} pid={os.getpid()}", flush=True)
    try:
        while True:
            state["server"].serve_forever(poll_interval=0.05)
            state["server"].server_close()
            state["service"].close()
            try:
                state["service"] = make_service()
                print("restart: same plan accepted (it was not consumed)", flush=True)
            except Exception as exc:  # the expected guard once the plan's output exists
                print(f"restart: plan refused as designed ({type(exc).__name__}: {exc}); starting a normal API without it", flush=True)
                state["service"] = make_service(with_plan=False)
            state["server"] = make_server(state["service"], port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        state["server"].server_close()
        state["service"].close()
        fixture.tearDown()
        print("stopped; temporary data removed", flush=True)


if __name__ == "__main__":
    main()
