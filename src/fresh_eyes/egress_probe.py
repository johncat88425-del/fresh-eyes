"""Trusted platform probe, copied into the normal runner instead of repo code."""

import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import socket
import time
import urllib.request
from urllib.parse import urlsplit


DENIED_TARGETS = (
    "example.com:443", "169.254.169.254:443", "127.0.0.1:443",
    "localhost:443", "10.0.2.2:443", "[::1]:443", "pypi.org:80",
    "pypi.org.example.com:443",
)


def fetch(opener, url, limit):
    with opener.open(url, timeout=15) as response:
        raw = response.read(limit + 1)
        if response.status != 200 or len(raw) > limit:
            raise ValueError("Unexpected status or oversized PyPI response")
        return raw


def direct_connection(host, port):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=2):
            return {"passed": False, "connected": True}
    except OSError as exc:
        return {"passed": True, "connected": False, "error": type(exc).__name__,
                "errno": exc.errno, "duration_s": round(time.monotonic() - started, 3)}


def main():
    payload = json.loads(Path("/bootstrap/payload.json").read_text())
    if payload.get("gate_mode") == "wait":
        time.sleep(60)
        return {"setup_ok": True, "outputs": [], "probes": []}
    probes = []
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ready = False
    for _ in range(30):
        try:
            with direct.open("http://pypi-proxy:8080/health", timeout=2) as response:
                health = json.loads(response.read(4096))
            ready = health.get("ok") is True and set(health.get("allowed_hosts", [])) == {"pypi.org", "files.pythonhosted.org"}
            if ready:
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    probes.append({"name": "proxy_ready", "passed": ready})
    if not ready:
        return {"setup_ok": False, "outputs": [], "probes": probes,
                "infrastructure_error": "PyPI-only proxy did not become ready"}

    # A positive control must succeed before denied connections mean anything.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://pypi-proxy:8080"}))
    try:
        raw = fetch(opener, "https://pypi.org/pypi/packaging/json", 2 * 1024 * 1024)
        metadata = json.loads(raw)
        wheel = next(item for item in metadata["urls"] if item["filename"].endswith("-py3-none-any.whl"))
        parsed = urlsplit(wheel["url"])
        if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org" or parsed.username or parsed.password:
            raise ValueError("Unexpected package artifact destination")
        probes.append({"name": "pypi_tls_metadata", "passed": True,
                       "version": metadata["info"]["version"], "response_sha256": hashlib.sha256(raw).hexdigest()})
        artifact = fetch(opener, wheel["url"], 2 * 1024 * 1024)
        actual = hashlib.sha256(artifact).hexdigest()
        probes.append({"name": "artifact_tls_download", "passed": actual == wheel["digests"]["sha256"],
                       "filename": wheel["filename"], "bytes": len(artifact), "sha256": actual,
                       "expected_sha256": wheel["digests"]["sha256"]})
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        probes.append({"name": "pypi_positive_control", "passed": False,
                       "error": type(exc).__name__, "detail": str(exc)[:500]})

    for destination in DENIED_TARGETS:
        connection = http.client.HTTPConnection("pypi-proxy", 8080, timeout=5)
        try:
            connection.request("CONNECT", destination)
            response = connection.getresponse()
            probes.append({"name": "proxy_denies:" + destination, "passed": response.status == 403,
                           "http_status": response.status})
            response.read(4096)
        except OSError as exc:
            # An unreachable proxy is not proof of destination enforcement.
            probes.append({"name": "proxy_denies:" + destination, "passed": False,
                           "error": type(exc).__name__})
        finally:
            connection.close()

    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    defaults = [row for row in routes if row.split()[1] == "00000000"]
    probes.append({"name": "no_ipv4_default_route", "passed": not defaults, "routes": routes})
    ipv6 = Path("/proc/net/ipv6_route").read_text().splitlines()
    defaults6 = [row for row in ipv6 if row.split()[0] == "0" * 32 and row.split()[1] == "00" and row.split()[-1] != "lo"]
    probes.append({"name": "no_ipv6_default_route", "passed": not defaults6})
    addresses = payload["public_ipv4"]
    if not addresses or any(not ipaddress.IPv4Address(ip).is_global for ip in addresses):
        raise ValueError("Gate requires independently resolved public PyPI addresses")
    for host, port in [(ip, 443) for ip in addresses] + [("169.254.169.254", 80), ("10.0.2.2", 443)]:
        probes.append({"name": "direct_denies:" + host + ":" + str(port), **direct_connection(host, port)})
    return {"setup_ok": all(probe["passed"] for probe in probes), "outputs": [], "probes": probes,
            "scope": "Sampled TCP destinations and proxy denials, with actual PyPI TLS positive controls; not all protocols or public-service hardening."}


if __name__ == "__main__":
    print(json.dumps(main()))
