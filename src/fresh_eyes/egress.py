"""PyPI-only CONNECT proxy, run in a separate trusted container, never on the host."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import select
import socket
import threading
import time
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
MAX_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 300


def target(value):
    parsed = urlsplit("//" + value)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS or parsed.port != 443 or parsed.username or parsed.password:
        raise ValueError("Only PyPI HTTPS endpoints are allowed")
    if parsed.path or parsed.query or parsed.fragment or value.lower() != host + ":443":
        raise ValueError("Invalid CONNECT target")
    return host


def open_public(host, port=443):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private, loopback, link-local and reserved destinations are blocked")
    error = None
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(10)
            # Connect to the already-validated numeric address, with no second DNS lookup.
            connection.connect(address)
            return connection
        except OSError as exc:
            connection.close()
            error = exc
    raise OSError("PyPI connection unavailable") from error


def make_proxy(address=("0.0.0.0", 8080), connector=open_public):
    slots = threading.BoundedSemaphore(8)

    class Handler(BaseHTTPRequestHandler):
        # Unbuffered input ensures TLS bytes following CONNECT are not read ahead.
        rbufsize = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/health":
                self.send_error(403, "CONNECT to PyPI HTTPS only")
                return
            raw = json.dumps({"ok": True, "allowed_hosts": sorted(ALLOWED_HOSTS)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_CONNECT(self):
            try:
                host = target(self.path)
            except ValueError:
                self.send_error(403, "Destination denied")
                return
            if not slots.acquire(blocking=False):
                self.send_error(503, "Too many package connections")
                return
            upstream = None
            try:
                try:
                    upstream = connector(host)
                except ValueError:
                    self.send_error(403, "Destination address denied")
                    return
                except OSError:
                    self.send_error(502, "Package server unavailable")
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.wfile.flush()
                sockets = [self.connection, upstream]
                for connection in sockets:
                    connection.settimeout(10)
                deadline, transferred = time.monotonic() + MAX_SECONDS, 0
                while time.monotonic() < deadline and transferred < MAX_BYTES:
                    readable, _, _ = select.select(sockets, [], [], 1)
                    for source in readable:
                        data = source.recv(min(65536, MAX_BYTES - transferred))
                        if not data:
                            return
                        transferred += len(data)
                        (upstream if source is self.connection else self.connection).sendall(data)
            except OSError:
                pass
            finally:
                if upstream:
                    upstream.close()
                slots.release()

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    make_proxy().serve_forever(poll_interval=0.2)
