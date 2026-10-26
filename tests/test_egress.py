"""Egress policy tests use loopback/socket pairs and explicit Docker doubles."""

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fresh_eyes.egress import make_proxy, open_public, target
from fresh_eyes.runner import DockerRunner, InfrastructureError
from test_core import result


class ProxyPolicy(unittest.TestCase):
    def test_targets_are_exact_https_hosts_without_credentials_or_paths(self):
        for name in ["pypi.org", "files.pythonhosted.org"]:
            self.assertEqual(target(name + ":443"), name)
        for value in ["localhost:443", "169.254.169.254:443", "pypi.org.evil.invalid:443", "evil@pypi.org:443",
                      "pypi.org:80", "pypi.org:443/x", "pypi.org:443?x", "pypi.org:443#x", "pypi.org:bad"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                target(value)

    def test_private_or_rebound_dns_is_rejected_before_connect(self):
        for address in ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.subTest(address=address), patch("fresh_eyes.egress.socket.getaddrinfo", return_value=answer), \
                 patch("fresh_eyes.egress.socket.socket") as constructor:
                with self.assertRaises(ValueError):
                    open_public("pypi.org")
                constructor.assert_not_called()

    def test_connection_uses_validated_numeric_address_without_second_lookup(self):
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        connection = Mock()
        with patch("fresh_eyes.egress.socket.getaddrinfo", return_value=answer) as lookup, \
             patch("fresh_eyes.egress.socket.socket", return_value=connection):
            self.assertIs(open_public("pypi.org"), connection)
        self.assertEqual(lookup.call_count, 1)
        connection.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_http_proxy_denies_other_destinations_and_tunnels_allowed_stream(self):
        upstream, peer = socket.socketpair()
        received = []
        def exchange():
            try:
                received.append(peer.recv(4))
                if received[-1]:
                    peer.sendall(b"PONG")
            finally:
                peer.close()
        exchange_thread = threading.Thread(target=exchange, daemon=True)
        exchange_thread.start()
        connector = Mock(return_value=upstream)
        server = make_proxy(("127.0.0.1", 0), connector)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        server_thread.start()
        try:
            health_client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            health_client.request("GET", "/health")
            response = health_client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(len(json.loads(response.read())["allowed_hosts"]), 2)
            health_client.close()
            denied = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            denied.request("CONNECT", "169.254.169.254:443")
            response = denied.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            denied.close()
            connector.assert_not_called()
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=3) as client:
                client.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
                headers = b""
                while not headers.endswith(b"\r\n\r\n"):
                    chunk = client.recv(1)
                    self.assertTrue(chunk, "Proxy closed before completing CONNECT")
                    headers += chunk
                self.assertIn(b"200 Connection Established", headers)
                client.sendall(b"PING")
                self.assertEqual(client.recv(4), b"PONG")
            exchange_thread.join(timeout=3)
            self.assertEqual(received, [b"PING"])
            connector.assert_called_once_with("pypi.org")
        finally:
            upstream.close()
            peer.close()
            server.shutdown()
            server_thread.join(timeout=3)
            server.server_close()


class LiveDockerNetwork(unittest.TestCase):
    def execute(self, *, valid_network=True, timeout=False):
        runner = DockerRunner(dependency_mode="pypi_live")
        runner._image_id = "sha256:base"
        calls = []
        def call(*args, **kwargs):
            calls.append(args)
            if args[0] == "commit":
                return "sha256:prepared"
            if args[:2] == ("network", "inspect"):
                return json.dumps([{"Id": "test-network", "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                                    "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"} if valid_network else {}}])
            if args[0] == "start" and "--attach" in args:
                if timeout:
                    raise InfrastructureError("Synthetic execution timeout")
                return json.dumps(result())
            return ""
        runner._call = call
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            (bundle / "payload.json").write_text(json.dumps({"dependency_mode": "untrusted-input"}))
            try:
                report = runner.run(bundle)
                payload = json.loads((bundle / "payload.json").read_text())
            except InfrastructureError as error:
                return calls, error, None
        return calls, report, payload

    def test_repo_has_only_internal_isolated_network_and_proxy_has_external_link(self):
        calls, report, payload = self.execute()
        self.assertIsInstance(report, dict)
        self.assertEqual(payload["dependency_mode"], "pypi_live")
        active = next(c for c in calls if c[0] == "create" and "/bootstrap/worker.py" in c)
        proxy = next(c for c in calls if c[0] == "create" and "/bootstrap/egress.py" in c)
        self.assertEqual(active[active.index("--network") + 1], proxy[proxy.index("--network") + 1])
        self.assertNotEqual(active[active.index("--network") + 1], "bridge")
        self.assertIn("--dns", active)
        self.assertIn("127.0.0.1", active)
        self.assertEqual([c for c in calls if c[:2] == ("network", "connect")], [("network", "connect", "bridge", proxy[proxy.index("--name") + 1])])
        self.assertTrue(report["network_policy"]["configuration_verified"])
        self.assertFalse(report["network_policy"]["runtime_egress_verified"])
        self.assertTrue(any(c[:2] == ("network", "rm") for c in calls))

    def test_missing_isolated_network_mode_does_not_fall_back_to_unrestricted_network(self):
        calls, error, _ = self.execute(valid_network=False)
        self.assertIsInstance(error, InfrastructureError)
        self.assertFalse(any(c[0] == "start" for c in calls))
        self.assertTrue(any(c[:2] == ("network", "rm") for c in calls))

    def test_timeout_cleans_execution_proxy_staging_network_and_image(self):
        calls, error, _ = self.execute(timeout=True)
        self.assertIsInstance(error, InfrastructureError)
        self.assertEqual(sum(c[0] == "rm" for c in calls), 3)
        self.assertTrue(any(c[:2] == ("network", "rm") for c in calls))
        self.assertIn(("image", "rm", "--no-prune", "sha256:prepared"), calls)


if __name__ == "__main__":
    unittest.main()
