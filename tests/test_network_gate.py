"""Gate bookkeeping tests use explicit doubles, not real network evidence."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fresh_eyes.egress_probe import DENIED_TARGETS
from fresh_eyes.network_gate import TrackedRunner, valid_probe_result, verify_egress
from fresh_eyes.runner import InfrastructureError


class GateEvidence(unittest.TestCase):
    def result(self):
        addresses = ["151.101.0.223"]
        names = ["proxy_ready", "pypi_tls_metadata", "artifact_tls_download",
                 "no_ipv4_default_route", "no_ipv6_default_route",
                 "direct_denies:151.101.0.223:443", "direct_denies:169.254.169.254:80", "direct_denies:10.0.2.2:443"]
        names += ["proxy_denies:" + target for target in DENIED_TARGETS]
        return addresses, {"setup_ok": True, "probes": [{"name": name, "passed": True} for name in names]}

    def test_every_positive_and_negative_probe_is_required_exactly_once(self):
        addresses, result = self.result()
        self.assertTrue(valid_probe_result(result, addresses))
        result["probes"].pop(1)  # Missing positive control cannot be called isolation.
        self.assertFalse(valid_probe_result(result, addresses))
        addresses, result = self.result()
        result["probes"].append(result["probes"][0])
        self.assertFalse(valid_probe_result(result, addresses))
        addresses, result = self.result()
        result["probes"][-1]["passed"] = False
        self.assertFalse(valid_probe_result(result, addresses))

    def test_leftover_owned_resources_fail_cleanup_without_reporting_unrelated_resources(self):
        runner = TrackedRunner()
        runner.resources = {"containers": ["owned-worker"], "networks": ["owned-network"], "images": ["sha256:owned"]}
        with patch.object(runner, "_call", side_effect=["other-worker\nowned-worker", "other-network", "sha256:other"]):
            evidence = runner.cleanup_evidence()
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["remaining"], {"containers": ["owned-worker"], "networks": [], "images": []})

    def test_unavailable_daemon_is_not_treated_as_successful_cleanup(self):
        runner = TrackedRunner()
        with patch.object(runner, "_call", side_effect=InfrastructureError("not connected")):
            with self.assertRaises(InfrastructureError):
                runner.cleanup_evidence()

    def test_failed_preflight_saves_failure_and_does_not_query_pypi_or_run_code(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(TrackedRunner, "preflight", side_effect=InfrastructureError("Synthetic unavailable runtime")), \
             patch("fresh_eyes.network_gate.socket.getaddrinfo") as dns, patch.object(TrackedRunner, "run") as run:
            path = Path(temp) / "gate.json"
            report = verify_egress(path)
            self.assertEqual(report["status"], "infrastructure_error")
            self.assertFalse(report["repository_executed"])
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(json.loads(path.read_text()), report)
            dns.assert_not_called()
            run.assert_not_called()

    def test_probe_bundle_is_readable_by_nonroot_worker_and_failed_probe_stops_the_gate(self):
        calls = []
        def failed_probe(runner, bundle):
            calls.append(bundle.name)
            self.assertEqual(bundle.stat().st_mode & 0o005, 0o005)
            self.assertTrue((bundle / "worker.py").stat().st_mode & 0o004)
            self.assertFalse((bundle / "repo").exists())
            return {"setup_ok": False, "outputs": [], "probes": []}
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(TrackedRunner, "preflight", return_value={"image_id": "synthetic"}), \
             patch.object(TrackedRunner, "_call", return_value="synthetic"), \
             patch.object(TrackedRunner, "cleanup_evidence", return_value={"passed": True}), \
             patch("fresh_eyes.network_gate.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("151.101.0.223", 443))]), \
             patch.object(TrackedRunner, "run", failed_probe):
            report = verify_egress(Path(temp) / "gate.json")
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual([check["name"] for check in report["checks"]], ["full"])


if __name__ == "__main__":
    unittest.main()
