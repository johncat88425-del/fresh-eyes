#!/usr/bin/env python3
"""Start the already-downloaded local VM from a normal terminal.

This does not install Homebrew packages, mount host directories in the VM,
change the global Docker context, or read model credentials.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

def start_command(lima: Path, home: Path) -> list[str]:
    instance = home / "fresh-eyes"
    if (instance / "lima.yaml").is_file():
        # A template argument always requests creation. Existing instances must
        # instead be selected by name, including after an interrupted startup.
        return [str(lima), "start", "--tty=false", "--timeout=5m", "fresh-eyes"]
    if instance.exists():
        raise ValueError("The fresh-eyes instance directory has no lima.yaml. It was preserved; inspect it before retrying.")
    return [str(lima), "start", "--name=fresh-eyes", "--vm-type=vz", "--mount-none", "--cpus=2",
            "--memory=2", "--disk=12", "--tty=false", "--timeout=5m", "template:docker"]


def choose_home(recorded: Path, persistent: Path) -> Path:
    # Reuse valid or partial existing instances; never move or delete their disks.
    if (recorded / "fresh-eyes").exists():
        return recorded
    return persistent


def main(argv=None, runtime_root=None, persistent_home=None):
    project = Path(__file__).resolve().parents[1]
    runtime = Path(runtime_root) if runtime_root else project.parents[1] / "work" / "runtime"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Stop the VM without deleting its disk")
    parser.add_argument("--state-dir", type=Path, help="Persistent Lima directory for a new VM; existing recorded instances are reused")
    args = parser.parse_args(argv)
    lima = runtime / "lima/bin/limactl"
    docker = runtime / "docker/docker"
    state = runtime / "lima-home.txt"
    if not all(p.exists() for p in (lima, state)) or (not args.stop and not docker.exists()):
        parser.error("Prepared runtime files are missing. See START_HERE.md in the parent outputs folder.")
    recorded = Path(state.read_text().strip())
    persistent = args.state_dir or persistent_home or Path.home() / ".local/share/fresh-eyes/lima"
    home = (recorded if args.stop else choose_home(recorded, Path(persistent))).expanduser().resolve()
    if args.stop and not (home / "fresh-eyes/lima.yaml").is_file():
        parser.error("There is no recorded VM to stop. No files were removed.")
    if not args.stop:
        home.mkdir(parents=True, exist_ok=True)
        state.write_text(str(home))
        print(f"VM state: {home} (persistent; no automatic deletion)", flush=True)
    env = os.environ.copy()
    env["LIMA_HOME"] = str(home)
    env["XDG_CACHE_HOME"] = str(runtime / "cache")
    if args.stop:
        return subprocess.call([str(lima), "stop", "--tty=false", "fresh-eyes"], env=env)
    try:
        cmd = start_command(lima, home)
    except ValueError as exc:
        parser.error(str(exc))
    if cmd[-1] == "fresh-eyes":
        print("Starting existing Fresh Eyes VM; keeping its configuration and disk.", flush=True)
    else:
        print("Creating Fresh Eyes VM: 2 CPUs, 2 GiB RAM, 12 GiB disk; no host directory mounts.", flush=True)
    started = subprocess.call(cmd, env=env)
    if started:
        print("VM did not start. No third-party project has been executed.", flush=True)
        return started
    settings = {"docker_executable": str(docker), "DOCKER_HOST": "unix://" + str(home / "fresh-eyes/sock/docker.sock"),
                "DOCKER_CONFIG": str(runtime / "docker-config")}
    Path(settings["DOCKER_CONFIG"]).mkdir(exist_ok=True)
    (runtime / "docker-env.json").write_text(json.dumps(settings, indent=2) + "\n")
    env.update({k: settings[k] for k in ("DOCKER_HOST", "DOCKER_CONFIG")})
    print("Pulling the local-development Linux arm64 Python image (network needed only for this preparation).", flush=True)
    try:
        subprocess.run([str(docker), "pull", "--platform", "linux/arm64", "python:3.12-slim"], env=env, check=True)
    except subprocess.CalledProcessError as exc:
        print("VM started, but the Python image could not be pulled. Retry this script to resume.", flush=True)
        return exc.returncode
    print("Runtime ready. The runner resolves this image to an immutable local image ID before executing cases.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
