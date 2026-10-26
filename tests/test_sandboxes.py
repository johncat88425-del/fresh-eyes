"""Access diagnostics with synthetic credentials; no external API requests."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fresh_eyes.sandboxes import (ACCESS_PATH, BASE_URL, IMAGES_PATH, MAX_RESPONSE,
                                 NoRedirect, ReadOnlySandboxClient, SandboxConfig,
                                 SandboxDiagnosticError, diagnose)


SECRET = "synthetic-secret-do-not-display"
IMAGE = "12345678-9abc-baba-deda-0123456789ab"


class FakeMetadata:
    def __init__(self, permissions=None, error=None):
        self.paths = []
        self.permissions = permissions if permissions is not None else {"spawn": True, "list": True}
        self.error = error

    def get(self, path):
        self.paths.append(path)
        if self.error:
            raise self.error
        if path == ACCESS_PATH:
            return {"permissions": self.permissions, "token_uuid": SECRET,
                    "limits": {"instance_max_timeout": 300, "unknown": SECRET}, "statistics": {"cost": 0}}
        return {"images": [{"uuid": IMAGE, "tag": SECRET}, {"uuid": SECRET}]}


class SandboxAccessChecks(unittest.TestCase):
    def config(self):
        return SandboxConfig(SECRET, "project-test")

    def test_missing_configuration_never_contacts_remote(self):
        client = FakeMetadata()
        result = diagnose(SandboxConfig(SECRET, ""), remote=True, client=client)
        self.assertEqual(result["missing_variables"], ["NEBIUS_PROJECT_ID"])
        self.assertEqual(result["metadata_requests"], 0)
        self.assertFalse(result["remote_checked"])
        self.assertEqual(client.paths, [])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_local_check_does_not_query_even_with_complete_credentials(self):
        client = FakeMetadata()
        self.assertEqual(diagnose(self.config(), client=client)["status"], "configuration_ready")
        self.assertEqual(client.paths, [])

    def test_success_is_access_evidence_only_and_filters_unknown_data(self):
        client = FakeMetadata()
        result = diagnose(self.config(), remote=True, client=client)
        self.assertEqual(client.paths, [ACCESS_PATH, IMAGES_PATH])
        self.assertEqual(result["status"], "access_verified")
        self.assertEqual(result["image_uuids"], [IMAGE])
        self.assertEqual(result["limits"], {"instance_max_timeout": 300})
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["runner_integrated"])
        self.assertFalse(result["operation_submission_enabled"])
        self.assertEqual(result["operations_submitted"], 0)
        self.assertIsNone(result["runner_usd"])
        self.assertNotIn(SECRET, repr(self.config()))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_permission_does_not_imply_project_inactive_or_attempt_execution(self):
        client = FakeMetadata({"spawn": False})
        result = diagnose(self.config(), remote=True, client=client)
        self.assertEqual(result["status"], "permission_missing")
        self.assertEqual(client.paths, [ACCESS_PATH])
        self.assertIn("caller", result["reason"])

    def test_permission_schema_is_not_truthiness_coerced(self):
        result = diagnose(self.config(), remote=True, client=FakeMetadata({"spawn": "true"}))
        self.assertEqual(result["status"], "invalid_response")

    def test_read_dotenv_without_shell_or_interpolation_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("NEBIUS_API_KEY='" + SECRET + "' # comment\n"
                            "export NEBIUS_PROJECT_ID=project-file\n"
                            "UNRELATED=$(touch impossible)\n")
            config = SandboxConfig.load(path, {"NEBIUS_PROJECT_ID": "project-process"})
            self.assertEqual(config.api_key, SECRET)
            self.assertEqual(config.project_id, "project-process")
            path.write_text("NEBIUS_PROJECT_ID='unterminated-secret\n")
            with self.assertRaises(SandboxDiagnosticError) as raised:
                SandboxConfig.load(path, {})
            self.assertNotIn("unterminated-secret", str(raised.exception))

    def test_invalid_headers_rejected_without_echo(self):
        for key, value in [("NEBIUS_API_KEY", SECRET + "\r\nHeader: evil"),
                           ("NEBIUS_PROJECT_ID", "$(touch sentinel)")]:
            with self.assertRaises(SandboxDiagnosticError) as raised:
                SandboxConfig.load(environ={key: value})
            self.assertNotIn(value, str(raised.exception))

    def test_only_two_fixed_get_endpoints_and_correct_project_header(self):
        client = ReadOnlySandboxClient(self.config())
        client.opener.open = lambda *a, **k: io.BytesIO(b'{"permissions":{"spawn":true}}')
        with patch.object(client.opener, "open", wraps=client.opener.open) as opened:
            client.get(ACCESS_PATH)
        req = opened.call_args.args[0]
        self.assertEqual(req.full_url, BASE_URL + ACCESS_PATH)
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Project"), "project-test")
        for path in ["/instances", "/files", "/images/import", "https://example.invalid", "/whoami?redirect=evil"]:
            with self.assertRaises(SandboxDiagnosticError):
                client.get(path)

    def test_http_error_body_and_network_error_do_not_expose_secrets(self):
        for status, expected in [(401, "unauthorized"), (403, "permission_denied"), (302, "redirect_refused"), (429, "rate_limited")]:
            client = ReadOnlySandboxClient(self.config())
            body = io.BytesIO(SECRET.encode())
            error = HTTPError(BASE_URL + SECRET, status, SECRET, {}, body)
            with patch.object(client.opener, "open", side_effect=error):
                result = diagnose(self.config(), remote=True, client=client)
            self.assertEqual(result["status"], expected)
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertTrue(body.closed)
        client = ReadOnlySandboxClient(self.config())
        with patch.object(client.opener, "open", side_effect=URLError(SECRET)):
            result = diagnose(self.config(), remote=True, client=client)
        self.assertEqual(result["status"], "network_error")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_redirect_handler_never_forwards_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_large_and_non_json_responses_are_rejected(self):
        for body in [b"[1]", b"not-json", b"x" * (MAX_RESPONSE + 1)]:
            client = ReadOnlySandboxClient(self.config())
            with patch.object(client.opener, "open", return_value=io.BytesIO(body)):
                result = diagnose(self.config(), remote=True, client=client)
            self.assertEqual(result["status"], "invalid_response")


if __name__ == "__main__":
    unittest.main()
