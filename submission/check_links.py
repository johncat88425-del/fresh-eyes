"""Check every link and run ID cited in the submission texts against what is really public (B, 2026-10-26).

    python3 submission/check_links.py DEVPOST.md JUDGE_TESTING_INSTRUCTIONS.md README.md ...

- Pages links to #/runs/<id>, #/answer/<id> and #/cases/<id>: the ID must be in the published single-file build.
- Shortened run IDs in the text (`5c225204…`): the prefix must be in the published build, or be listed with --local-ok.
- GitHub Actions runs: must exist; the conclusion is shown. Issues: state is shown. YouTube: oEmbed metadata must be public (playback/captions still need a browser check).
- Every other link: must answer HTTP 2xx/3xx.
Read-only: GET requests only, anonymous. Exit code 1 when anything fails.
"""
import argparse, json, re, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

PAGES = "https://johncat88425-del.github.io/fresh-eyes/"
URL = re.compile(r"https?://[^\s<>()\"'`\]]+")
SHORT_ID = re.compile(r"`([0-9a-f]{8,12})…")


def get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "fresh-eyes-link-check", **({"Accept": accept} if accept else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(8_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, type(e).__name__


def build_index(html):
    """Route IDs come from the actual recording reader, never a substring in code/logs."""
    match = re.search(r'<script\b[^>]*\bid=[\"\']fresh-eyes-recordings[\"\'][^>]*>(.*?)</script>', html, re.S | re.I)
    if not match:
        raise ValueError("Published site has no embedded recording inventory")
    assets = json.loads(match[1])
    read = lambda name: json.loads(assets[name])
    runs, cases, commits = set(), set(), set()
    for row in read("data/runs.json")["items"]:
        run_id = row.get("run_id", "")
        name = "data/runs/" + run_id + ".json"
        if re.fullmatch(r"[0-9a-f]{32}", run_id) and name in assets:
            detail = read(name)
            if detail.get("run_id") != run_id:
                raise ValueError("Published run index and detail disagree")
            runs.add(run_id)
            if detail.get("commit_sha"): commits.add(detail["commit_sha"])
    for row in read("data/cases.json")["items"]:
        cases.add(row["case_id"])
        if row.get("commit_sha"): commits.add(row["commit_sha"])
    return {"runs": runs, "cases": cases, "commits": commits}


def published_build():
    status, html = get(PAGES)
    if status != 200:
        sys.exit(f"Cannot read the published site ({status}).")
    try:
        return build_index(html)
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(f"Cannot validate the published recording inventory ({type(exc).__name__}).")


def check(url, build):
    url = url.rstrip(".,;:*")
    m = re.match(re.escape(PAGES) + r"(?:\?[^#]*)?#/(runs|answer|cases)/([\w-]+)", url)
    if m:
        found = m[2] in build["cases" if m[1] == "cases" else "runs"]
        return (found, f"{m[1]} {m[2][:12]} {'in' if found else 'NOT in'} the published route inventory")
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)/actions/runs/(\d+)", url)
    if m:
        status, body = get(f"https://api.github.com/repos/{m[1]}/{m[2]}/actions/runs/{m[3]}", "application/vnd.github+json")
        if status != 200:
            return False, f"Actions run not found ({status})"
        run = json.loads(body)
        return True, f"Actions run: {run.get('name')} · {run.get('status')} · {run.get('conclusion')}"
    m = re.match(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)", url)
    if m:
        status, body = get(f"https://api.github.com/repos/{m[1]}/{m[2]}/issues/{m[3]}", "application/vnd.github+json")
        if status != 200:
            return False, f"issue not found ({status})"
        issue = json.loads(body)
        return True, f"issue {issue['state']}, {issue['comments']} comments"
    if re.match(r"https://(youtu\.be|(www\.)?youtube\.com)/", url):
        status, body = get("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""))
        return (status == 200, f"YouTube public metadata: {json.loads(body).get('title')} (check playback/captions separately)" if status == 200 else f"YouTube metadata unavailable ({status})")
    status, _ = get(url)
    return (status is not None and 200 <= status < 400, f"HTTP {status}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--local-ok", nargs="*", default=[], help="short run IDs that are cited as local-only on purpose")
    args = p.parse_args(argv)
    build = published_build()
    failures, seen = 0, set()
    for path in args.files:
        text = path.read_text(encoding="utf-8")
        print(f"\n## {path}")
        for url in URL.findall(text):
            url = url.rstrip(".,;:*")
            if url in seen or "localhost" in url or "127.0.0.1" in url:
                continue
            seen.add(url)
            ok, note = check(url, build)
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'}  {url}\n      {note}")
        for short in sorted(set(SHORT_ID.findall(text))):
            if short in seen:
                continue
            seen.add(short)
            published_run = any(run.startswith(short) for run in build["runs"])
            published_commit = any(commit.startswith(short) for commit in build["commits"])
            ok = published_run or published_commit or short in args.local_ok
            failures += not ok
            label = 'published run' if published_run else 'published repository commit' if published_commit else 'local only (allowed)' if ok else 'NOT in the published route inventory'
            print(f"{'ok  ' if ok else 'FAIL'}  ID {short}…  {label}")
    print(f"\n{failures} problem(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
