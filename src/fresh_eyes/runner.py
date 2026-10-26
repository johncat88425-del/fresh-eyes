"""Fresh Docker execution: offline snapshot or isolated PyPI-only proxy network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


class InfrastructureError(RuntimeError):
    pass


class DockerRunner:
    def __init__(self, image="python:3.12-slim", platform="linux/arm64", executable="docker", timeout=300,
                 dependency_mode="offline_snapshot"):
        if dependency_mode not in {"offline_snapshot", "pypi_live"}:
            raise ValueError("Unsupported dependency mode")
        self.image = image
        self.platform = platform
        self.executable = executable
        self.timeout = timeout
        self._image_id = None
        self.dependency_mode = dependency_mode

    def _call(self, *args, timeout=30):
        try:
            p = subprocess.run([self.executable, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfrastructureError(f"Container runtime unavailable: {type(exc).__name__}") from None
        if p.returncode:
            raise InfrastructureError(p.stderr.strip()[:2000] or "Container command failed")
        return p.stdout.strip()

    def preflight(self):
        self._call("version", "--format", "{{.Server.Version}}")
        info = json.loads(self._call("image", "inspect", self.image))[0]
        actual = "linux/" + {"aarch64": "arm64", "x86_64": "amd64"}.get(info["Architecture"], info["Architecture"])
        if actual != self.platform or info["Os"] != "linux":
            raise InfrastructureError(f"Image platform is {actual}; case requires {self.platform}")
        self._image_id = info["Id"]
        live = self.dependency_mode == "pypi_live"
        return {"requested_image": self.image, "image_id": self._image_id, "repo_digests": info.get("RepoDigests", []),
                "platform": actual, "dependency_mode": self.dependency_mode,
                "network": "pypi_proxy_isolated" if live else "none", "offline_wheelhouse": not live,
                "egress_allowlist": ["pypi.org:443", "files.pythonhosted.org:443"] if live else []}

    def run(self, bundle: Path):
        if self._image_id is None:
            self.preflight()
        name = "fresh-eyes-" + uuid.uuid4().hex[:16]
        staging = name + "-staging"
        created = False
        staging_created = False
        execution_image = None
        network = name + "-packages"
        proxy = name + "-proxy"
        network_created = proxy_created = False
        live = self.dependency_mode == "pypi_live"
        try:
            # Controller-owned settings override any repository-supplied network values.
            payload_path = bundle / "payload.json"
            if payload_path.is_file():
                payload = json.loads(payload_path.read_text())
                payload["dependency_mode"] = self.dependency_mode
                payload_path.write_text(json.dumps(payload))
            if live:
                shutil.copyfile(Path(__file__).with_name("egress.py"), bundle / "egress.py")
            # docker cp cannot write into a read-only root filesystem. Prepare
            # a stopped staging container, snapshot it, then execute only the
            # snapshot in a separate read-only/non-root container. Staging is
            # never started and contains no expected results or credentials.
            self._call("create", "--name", staging, "--network", "none", self._image_id, "true")
            staging_created = True
            self._call("cp", str(bundle) + "/.", staging + ":/bootstrap")
            execution_image = self._call("commit", staging)
            if not execution_image.startswith("sha256:"):
                raise InfrastructureError("Staging did not produce an immutable image ID")
            policy = {"mode": "none", "configuration_verified": True}
            if live:
                self._call("network", "create", "--driver", "bridge", "--internal", "--ipv6=false",
                           "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated", network)
                network_created = True
                info = json.loads(self._call("network", "inspect", network))[0]
                if (not info.get("Internal") or info.get("EnableIPv6") or
                    info.get("Driver") != "bridge" or
                    info.get("Options", {}).get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"):
                    raise InfrastructureError("Docker did not confirm the required isolated internal network; live execution denied")
                self._call("create", "--name", proxy, "--network", network, "--network-alias", "pypi-proxy",
                           "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
                           "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "128m",
                           "--memory-swap", "128m", "--cpus", "0.25", "--log-driver", "none",
                           execution_image, "python", "-I", "/bootstrap/egress.py")
                proxy_created = True
                self._call("network", "connect", "bridge", proxy)
                self._call("start", proxy)
                policy = {"mode": "pypi_proxy_isolated", "network_id": info["Id"], "configuration_verified": True,
                          "runtime_egress_verified": False,
                          "allowed_hosts": ["pypi.org:443", "files.pythonhosted.org:443"],
                          "verification_scope": "Docker network configuration; actual egress probes are a separate release gate"}
            network_args = ["--network", network, "--dns", "127.0.0.1"] if live else ["--network", "none"]
            self._call("create", "--name", name, *network_args, "--user", "65532:65532",
                       "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                       "--pids-limit", "96", "--memory", "768m", "--memory-swap", "768m", "--cpus", "1",
                       "--tmpfs", "/work:rw,exec,nosuid,nodev,size=536870912,uid=65532,gid=65532,mode=0700",
                       "--tmpfs", "/tmp:rw,nosuid,nodev,size=67108864,mode=1777", "--log-driver", "none",
                       "--platform", self.platform, execution_image, "python", "-I", "/bootstrap/worker.py")
            created = True
            raw = self._call("start", "--attach", name, timeout=self.timeout)
            if len(raw.encode()) > 4 * 1024 * 1024:
                raise InfrastructureError("Worker result exceeded protocol limit")
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("setup_ok"), bool) or not isinstance(result.get("outputs"), list):
                raise InfrastructureError("Worker result has an invalid schema")
            result["container_id"] = name
            result["execution_image_id"] = execution_image
            result["dependency_mode"] = self.dependency_mode
            result["network_policy"] = policy
            return result
        except json.JSONDecodeError:
            raise InfrastructureError("Worker did not return a valid result") from None
        finally:
            for target, exists in [(name, created), (proxy, proxy_created), (staging, staging_created)]:
                if not exists:
                    continue
                try:
                    self._call("rm", "--force", target)
                except InfrastructureError:
                    pass
            if network_created:
                try:
                    self._call("network", "rm", network)
                except InfrastructureError:
                    pass
            if execution_image:
                try:
                    # Docker otherwise recursively removes untagged parents,
                    # including the pinned base needed by the next clean run.
                    self._call("image", "rm", "--no-prune", execution_image)
                except InfrastructureError:
                    pass


def doctor(executable="docker"):
    runtime = shutil.which(executable)
    result = {"python": os.sys.version.split()[0], "docker_cli": runtime,
              "nebius_key_configured": bool(os.environ.get("NEBIUS_API_KEY")), "model_calls_performed": 0}
    if runtime:
        try:
            result["docker_server"] = DockerRunner(executable=runtime)._call("version", "--format", "{{.Server.Version}}")
        except InfrastructureError as exc:
            result["runtime_error"] = str(exc)
    else:
        result["runtime_error"] = "Docker CLI is not installed. No repository code was executed."
    return result
