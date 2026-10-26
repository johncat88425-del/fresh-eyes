"""Runs inside a disposable container. No expected results or credentials here."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

LIMIT = 131072


def command(argv, cwd, env, prefix, executable_argv=None):
    start = time.monotonic()
    out, err = Path(str(prefix) + ".out"), Path(str(prefix) + ".err")
    code = 127
    try:
        with out.open("wb") as stdout, err.open("wb") as stderr:
            p = subprocess.run(executable_argv or argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, timeout=90, stdin=subprocess.DEVNULL)
            code = p.returncode
    except subprocess.TimeoutExpired:
        code = 124
    except OSError as exc:
        err.write_text(str(exc))
    return {
        "argv": argv, "executed_argv": executable_argv or argv,
        "exit_code": code, "duration_s": round(time.monotonic() - start, 3),
        "stdout": out.read_bytes()[:LIMIT].decode("utf-8", "replace") if out.exists() else "",
        "stderr": err.read_bytes()[:LIMIT].decode("utf-8", "replace") if err.exists() else "",
        "truncated": any(p.exists() and p.stat().st_size > LIMIT for p in (out, err)),
    }


def resolve_python_command(argv, venv):
    """Use the declared fresh interpreter, preserving the documented argv in logs."""
    python = str(venv / "bin/python")
    if argv[0] in {"pip", "pip3"}:
        return [python, "-I", "-m", "pip", *argv[1:]]
    if argv[0] in {"python", "python3"}:
        if argv[1:3] == ["-m", "pip"]:
            return [python, "-I", *argv[1:]]
        return [python, *argv[1:]]
    return argv


def probe_environment(venv, cwd, env, prefix):
    # Verify the actual interpreter and pip before executing any repository code.
    script = """import json,pathlib,subprocess,sys,uuid,pip
p=pathlib.Path(sys.prefix).resolve()
ok=(p==pathlib.Path(sys.argv[1]).resolve() and sys.prefix!=sys.base_prefix
    and pathlib.Path(pip.__file__).resolve().is_relative_to(p))
executable=False
if ok:
    probe=p.parent/('.fresh-eyes-exec-probe-'+uuid.uuid4().hex)
    try:
        probe.write_text('#!/bin/sh\\nexit 0\\n')
        probe.chmod(0o700)
        executable=subprocess.run([str(probe)],stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5).returncode==0
    except (OSError,subprocess.TimeoutExpired):
        executable=False
    finally:
        probe.unlink(missing_ok=True)
print(json.dumps(dict(executable=sys.executable,prefix=str(p),base_prefix=sys.base_prefix,
                     pip_module=pip.__file__,python=sys.version.split()[0],workspace_executable=executable)))
sys.exit(0 if ok and executable else 1)
"""
    return command([str(venv / "bin/python"), "-I", "-c", script, str(venv)], cwd, env, prefix)


def execution_environment(root, bootstrap, mode):
    venv = root / "venv"
    env = {"PATH": f"{venv}/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(root / "home"), "LANG": "C.UTF-8",
           "VIRTUAL_ENV": str(venv), "PIP_REQUIRE_VIRTUALENV": "1", "PIP_USER": "0", "PIP_CONFIG_FILE": os.devnull,
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if mode == "offline_snapshot":
        env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(bootstrap / "wheels"))
    elif mode == "pypi_live":
        proxy = "http://pypi-proxy:8080"
        env.update(PIP_INDEX_URL="https://pypi.org/simple", PIP_PROXY=proxy, HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                   PIP_NO_CACHE_DIR="1", PIP_DEFAULT_TIMEOUT="20", PIP_RETRIES="1")
    else:
        raise ValueError("Unknown dependency mode")
    return env


def check_proxy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        try:
            with opener.open("http://pypi-proxy:8080/health", timeout=2) as response:
                data = json.loads(response.read(4096))
            if data.get("ok") is True and set(data.get("allowed_hosts", [])) == {"pypi.org", "files.pythonhosted.org"}:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return False


def main(bootstrap=Path("/bootstrap"), root=Path("/work")):
    payload = json.loads((bootstrap / "payload.json").read_text())
    repo = root / "repo"
    shutil.copytree(bootstrap / "repo", repo)
    inputs = root / "inputs"
    inputs.mkdir()
    venv = root / "venv"
    mode = payload.get("dependency_mode", "offline_snapshot")
    env = execution_environment(root, bootstrap, mode)
    if mode == "pypi_live" and not check_proxy():
        return {"setup_ok": False, "steps": [], "outputs": [], "pip_freeze": None,
                "infrastructure_error": "The required PyPI-only proxy is not ready; no repository setup was executed."}
    steps = [command([sys.executable, "-m", "venv", str(venv)], repo, env, root / "venv-log")]
    setup_ok = steps[0]["exit_code"] == 0
    if setup_ok:
        steps.append(probe_environment(venv, repo, env, root / "environment-log"))
        setup_ok = steps[-1]["exit_code"] == 0 and not steps[-1]["truncated"]
    if not setup_ok:
        return {"setup_ok": False, "steps": steps, "outputs": [], "pip_freeze": None,
                "infrastructure_error": "Fresh Python/pip environment or workspace execution could not be verified; repository setup was not executed."}
    infrastructure_error = None
    if setup_ok:
        for i, step in enumerate(payload["setup"]):
            pip_report = root / f"pip-install-{i}.json"
            step_env = {**env, "PIP_REPORT": str(pip_report)}
            result = command(step["argv"], repo, step_env, root / f"setup-{i}", resolve_python_command(step["argv"], venv))
            if pip_report.is_file() and not pip_report.is_symlink() and pip_report.stat().st_size <= 1024 * 1024:
                try:
                    result["pip_install_report"] = json.loads(pip_report.read_text())
                except (OSError, ValueError):
                    result["pip_install_report_error"] = "Installation report could not be read"
            steps.append(result)
            if result["exit_code"] != 0 or result["truncated"]:
                setup_ok = False
                diagnostic = (result.get("stderr", "") + "\n" + result.get("stdout", "")).lower()
                if mode == "offline_snapshot" and any(marker in diagnostic for marker in
                    ("no matching distribution found", "could not find a version that satisfies the requirement")):
                    infrastructure_error = "The offline snapshot cannot supply a requested package or build dependency. This does not establish a README fault; prepare artifacts inside isolation or validate with controlled live PyPI."
                break
    outputs = []
    if setup_ok:
        for i, fixture in enumerate(payload["fixtures"]):
            input_path = inputs / fixture["name"]
            input_path.parent.mkdir(parents=True, exist_ok=True)
            input_path.write_text(fixture["input"], encoding="utf-8")
            output_path = repo / payload["workflow"].get("output", "stdout")
            if payload["workflow"].get("output", "stdout") != "stdout" and output_path.exists():
                output_path.unlink()
            argv = [part.replace("{input}", str(input_path)) for part in payload["workflow"]["argv"]]
            result = command(argv, repo, env, root / f"workflow-{i}", resolve_python_command(argv, venv))
            content, truncated = result["stdout"], result["truncated"]
            if payload["workflow"].get("output", "stdout") != "stdout":
                if output_path.is_symlink() or not output_path.resolve().is_relative_to(repo.resolve()) or not output_path.is_file():
                    content = ""
                else:
                    truncated = truncated or output_path.stat().st_size > LIMIT
                    content = output_path.read_bytes()[:LIMIT].decode("utf-8", "replace")
            outputs.append({"name": fixture["name"], "exit_code": result["exit_code"], "content": content,
                            "truncated": truncated, "process": result})
    freeze = command(["pip", "freeze", "--all"], repo, env, root / "freeze",
                     resolve_python_command(["pip", "freeze", "--all"], venv))
    evidence = {"setup_ok": setup_ok, "steps": steps, "outputs": outputs, "pip_freeze": freeze,
            "dependency_mode": mode,
            "environment_policy": "Fresh venv; documented Python/pip aliases resolve to that interpreter; no user-site install."}
    if infrastructure_error:
        evidence["infrastructure_error"] = infrastructure_error
    return evidence


if __name__ == "__main__":
    print(json.dumps(main()))
