"""Serve the frontend on 127.0.0.1 only.

Exposes web/, ../api/client.js as /client.js and ../evaluation/results.json as
/evaluation.json, and nothing else, so the project root (and any .env in it) is
never reachable from the browser.
Usage: python3 web/serve.py [port] [--evaluation FILE]
       (default port 5173, an origin the API allows; --evaluation overrides the results file for testing)
"""
import argparse
import http.server
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
CLIENT = ROOT.parent / "api" / "client.js"
EVALUATION = ROOT.parent / "evaluation" / "results.json"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_file(self, path, content_type):
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/client.js":
            return self.send_file(CLIENT, "text/javascript; charset=utf-8")
        if route == "/evaluation.json":
            return self.send_file(EVALUATION, "application/json; charset=utf-8")
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", nargs="?", type=int, default=5173)
    parser.add_argument("--evaluation", type=pathlib.Path, help="results file to serve at /evaluation.json")
    args = parser.parse_args()
    if args.evaluation:
        EVALUATION = args.evaluation.resolve()
    print(f"Fresh Eyes frontend: http://localhost:{args.port}/  (API default http://127.0.0.1:8765, override with ?api=)")
    print(f"Evaluation results: {EVALUATION} ({'found' if EVALUATION.is_file() else 'not found yet'})")
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
