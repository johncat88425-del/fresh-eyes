"""Owner-run real Docker/PyPI release gate; no repository or model execution."""

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import tempfile

from .egress_probe import DENIED_TARGETS
from .runner import DockerRunner, InfrastructureError


def now():
    return datetime.now(timezone.utc).isoformat()


class TrackedRunner(DockerRunner):
    def __init__(self, *, interrupt=False, **kwargs):
        super().__init__(**kwargs)
        self.resources = {"containers": [], "networks": [], "images": []}
        self.interrupt = interrupt
        self.interrupted_running_worker = False

    def _call(self, *args, **kwargs):
        result = None
        if self.interrupt and args[0] == "start" and "--attach" in args:
            name = args[-1]
            super()._call("start", name)
            info = json.loads(super()._call("inspect", name))[0]
            self.interrupted_running_worker = info["State"]["Running"]
            raise KeyboardInterrupt
        result = super()._call(*args, **kwargs)
        if args[0] == "create":
            self.resources["containers"].append(args[args.index("--name") + 1])
        elif args[:2] == ("network", "create"):
            self.resources["networks"].append(args[-1])
        elif args[0] == "commit":
            self.resources["images"].append(result)
        return result

    def cleanup_evidence(self):
        # A failed daemon query is a failed check, never evidence of deletion.
        containers = set(self._call("ps", "-a", "--format", "{{.Names}}").splitlines())
        networks = set(self._call("network", "ls", "--format", "{{.Name}}").splitlines())
        images = set(self._call("image", "ls", "--quiet", "--no-trunc").splitlines())
        present = {"containers": sorted(containers.intersection(self.resources["containers"])),
                   "networks": sorted(networks.intersection(self.resources["networks"])),
                   "images": sorted(images.intersection(self.resources["images"]))}
        return {"passed": not any(present.values()), "owned_resources": self.resources, "remaining": present}


def valid_probe_result(result, addresses):
    probes = result.get("probes", [])
    required = {"proxy_ready", "pypi_tls_metadata", "artifact_tls_download",
                "no_ipv4_default_route", "no_ipv6_default_route"}
    required.update("proxy_denies:" + target for target in DENIED_TARGETS)
    required.update("direct_denies:" + address + ":443" for address in addresses)
    required.update({"direct_denies:169.254.169.254:80", "direct_denies:10.0.2.2:443"})
    names = [probe.get("name") for probe in probes]
    return (result.get("setup_ok") is True and required == set(names) and len(names) == len(required)
            and all(probe.get("passed") is True for probe in probes))


def verify_egress(output, *, docker="docker", image="python:3.12-slim", platform="linux/arm64"):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "data_origin": "platform_check", "started_at": now(),
              "status": "infrastructure_error", "model_calls": 0, "paid_calls": 0,
              "human_approval_created": False, "repository_executed": False,
              "checks": [], "limitations": ["This does not enable live API requests or certify arbitrary public code execution.",
                  "The interruption probe injects KeyboardInterrupt after a worker starts; there is no API cancellation endpoint."]}
    module = Path(__file__).parent
    report["code_sha256"] = {name: hashlib.sha256((module / name).read_bytes()).hexdigest()
                              for name in ("runner.py", "egress.py", "egress_probe.py", "network_gate.py")}
    base = dict(executable=docker, image=image, platform=platform, dependency_mode="pypi_live")
    try:
        runner = TrackedRunner(**base)
        report["environment"] = runner.preflight()
        report["docker_server"] = runner._call("version", "--format", "{{.Server.Version}}")
        report["docker_engine_id"] = runner._call("info", "--format", "{{.ID}}")
        answers = socket.getaddrinfo("pypi.org", 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        addresses = sorted({answer[4][0] for answer in answers})
        if not addresses or any(not ipaddress.IPv4Address(address).is_global for address in addresses):
            raise InfrastructureError("Could not independently resolve public PyPI IPv4 addresses")
        report["public_ipv4"] = addresses
        with tempfile.TemporaryDirectory(prefix="fresh-eyes-egress-", dir=output.parent) as temp:
            bundle = Path(temp)
            # docker cp preserves directory mode; the non-root worker must be
            # able to traverse the controller-owned, credential-free bundle.
            bundle.chmod(0o755)
            shutil.copyfile(module / "egress_probe.py", bundle / "worker.py")
            for mode in ("full", "timeout", "interrupt"):
                current = runner if mode == "full" else TrackedRunner(**base, timeout=1 if mode == "timeout" else 30,
                                                                      interrupt=mode == "interrupt")
                if mode != "full":
                    current._image_id = runner._image_id
                (bundle / "payload.json").write_text(json.dumps({"gate_mode": "full" if mode == "full" else "wait",
                                                                  "public_ipv4": addresses}))
                entry = {"name": mode, "passed": False}
                try:
                    result = current.run(bundle)
                    entry["result"] = result
                    entry["passed"] = mode == "full" and valid_probe_result(result, addresses)
                except InfrastructureError as exc:
                    entry["reason"] = str(exc)
                    entry["passed"] = mode == "timeout" and "TimeoutExpired" in str(exc)
                except KeyboardInterrupt:
                    if mode != "interrupt":
                        raise
                    entry["injected_interrupt_of_running_worker"] = current.interrupted_running_worker
                    entry["passed"] = current.interrupted_running_worker
                finally:
                    entry["cleanup"] = current.cleanup_evidence()
                    entry["passed"] = entry["passed"] and entry["cleanup"]["passed"]
                    report["checks"].append(entry)
                if not entry["passed"]:
                    break
        report["status"] = "passed" if len(report["checks"]) == 3 and all(entry["passed"] for entry in report["checks"]) else "failed"
    except (InfrastructureError, OSError, ValueError, KeyError) as exc:
        report["reason"] = str(exc)[:2000]
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        report["finished_at"] = now()
        output.write_text(json.dumps(report, indent=2) + "\n")
    return report
