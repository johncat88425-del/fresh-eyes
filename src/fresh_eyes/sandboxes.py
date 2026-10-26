"""Read-only Sandboxes access diagnostics. No operation/upload/import endpoints.

Credentials stay in this control process. This module is deliberately separate
from both the public teammate API and the Docker execution interface.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


BASE_URL = "https://api.tokenfactory.nebius.com/sandboxes/v1"
ACCESS_PATH = "/whoami"
IMAGES_PATH = "/images?limit=20&tagged=1"
MAX_RESPONSE = 1024 * 1024
VARIABLES = ("NEBIUS_API_KEY", "NEBIUS_PROJECT_ID")


class SandboxDiagnosticError(Exception):
    """Only fixed, credential-free messages may leave this boundary."""

    def __init__(self, code, message, http_status=None):
        super().__init__(message)
        self.code, self.http_status = code, http_status


@dataclass(frozen=True)
class SandboxConfig:
    api_key: str = field(default="", repr=False)
    project_id: str = field(default="", repr=False)

    @classmethod
    def load(cls, env_file=None, environ=None):
        """Read just two dotenv values as data; never execute or interpolate them."""
        values = {}
        if env_file is not None:
            try:
                path = Path(env_file)
                if path.stat().st_size > 65536:
                    raise SandboxDiagnosticError("invalid_config", "The configuration file exceeds 64 KiB.")
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            except (OSError, UnicodeError):
                raise SandboxDiagnosticError("invalid_config", "Cannot read the configuration file.") from None
            for line in lines:
                line = line.strip().removeprefix("export ").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name, value = name.strip(), value.strip()
                if name not in VARIABLES:
                    continue
                if value.startswith(("'", '"')):
                    match = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
                    if not match:
                        raise SandboxDiagnosticError("invalid_config", "Invalid quoting for " + name + ".")
                    value = match.group(2)
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                values[name] = value
        environment = os.environ if environ is None else environ
        for name in VARIABLES:
            if name in environment:
                values[name] = environment[name].strip()
        key, project = (values.get(name, "") for name in VARIABLES)
        if key and (len(key) > 8192 or not re.fullmatch(r"[!-~]+", key)):
            raise SandboxDiagnosticError("invalid_config", "Invalid NEBIUS_API_KEY header value.")
        if project and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", project):
            raise SandboxDiagnosticError("invalid_config", "Invalid NEBIUS_PROJECT_ID header value.")
        return cls(key, project)

    def missing(self):
        return [name for name, value in zip(VARIABLES, (self.api_key, self.project_id)) if not value]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReadOnlySandboxClient:
    def __init__(self, config):
        self.config = config
        # No inherited HTTP proxy or redirect can receive the authorization key.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(self, path):
        if path not in {ACCESS_PATH, IMAGES_PATH}:
            raise SandboxDiagnosticError("endpoint_disabled", "Only access and public-image metadata queries are enabled.")
        if self.config.missing():
            raise SandboxDiagnosticError("missing_configuration", "Configure the API key and project ID locally first.")
        request = Request(BASE_URL + path, method="GET", headers={
            "Authorization": "Bearer " + self.config.api_key,
            "Project": self.config.project_id,
            "Accept": "application/json",
            "User-Agent": "fresh-eyes-access-check/1",
        })
        try:
            with self.opener.open(request, timeout=15) as response:
                body = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()  # Never echo an upstream error body, URL or headers.
            code = {401: "unauthorized", 403: "permission_denied", 429: "rate_limited"}.get(status, "http_error")
            if 300 <= status < 400:
                code = "redirect_refused"
            raise SandboxDiagnosticError(code, "Sandboxes metadata request was rejected.", status) from None
        except (TimeoutError, URLError, OSError):
            raise SandboxDiagnosticError("network_error", "Sandboxes metadata request did not complete.") from None
        if len(body) > MAX_RESPONSE:
            raise SandboxDiagnosticError("invalid_response", "Sandboxes metadata response exceeds 1 MiB.")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise SandboxDiagnosticError("invalid_response", "Sandboxes returned invalid JSON metadata.") from None
        if not isinstance(result, dict):
            raise SandboxDiagnosticError("invalid_response", "Sandboxes metadata must be a JSON object.")
        return result


def diagnose(config, *, remote=False, client=None):
    result = {"service": "nebius-sandboxes", "api_base": BASE_URL,
              "status": "missing_configuration" if config.missing() else "configuration_ready",
              "missing_variables": config.missing(), "remote_checked": False,
              "metadata_requests": 0, "operations_submitted": 0,
              "execution_verified": False, "runner_integrated": False,
              "operation_submission_enabled": False, "model_calls": 0,
              "billing_verified": False, "runner_usd": None}
    if config.missing() or not remote:
        return result
    transport = client or ReadOnlySandboxClient(config)
    try:
        result["metadata_requests"] += 1
        identity = transport.get(ACCESS_PATH)
        permissions = identity.get("permissions")
        if not isinstance(permissions, dict) or any(type(v) is not bool for v in permissions.values()):
            raise SandboxDiagnosticError("invalid_response", "Sandboxes permissions must be a boolean map.")
        result["remote_checked"] = True
        result["permissions"] = {key: permissions[key] for key in
                                 ("spawn", "spawn_disposable", "list", "import", "cancel") if key in permissions}
        limits = identity.get("limits", {})
        if isinstance(limits, dict):
            result["limits"] = {key: limits[key] for key in ("instance_max_timeout", "instance_max_concurrency")
                                if key in limits and type(limits[key]) is int and limits[key] >= 0}
        if permissions.get("spawn") is not True:
            result.update(status="permission_missing", reason="Check both project activation and caller permissions.")
            return result
        result["metadata_requests"] += 1
        images = transport.get(IMAGES_PATH).get("images")
        if not isinstance(images, list) or any(not isinstance(image, dict) for image in images):
            raise SandboxDiagnosticError("invalid_response", "Sandboxes images must be a JSON array of objects.")
        # Retain only UUIDs; no unknown server fields or tags enter shared output.
        uuids = [image.get("uuid") for image in images]
        result["image_uuids"] = [value for value in uuids if isinstance(value, str) and
                                 re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value)]
        result["status"] = "access_verified" if result["image_uuids"] else "no_base_image"
    except SandboxDiagnosticError as exc:
        result.update(status=exc.code, reason=str(exc))
        if exc.http_status is not None:
            result["http_status"] = exc.http_status
    return result
