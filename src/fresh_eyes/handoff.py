"""Build portable report files with checked relative links; never include SQLite."""

import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.targets.append(value)


def validate_links(files):
    missing = []
    for name, raw in files.items():
        if not name.endswith(".html"):
            continue
        parser = Links()
        parser.feed(raw.decode("utf-8"))
        for target in parser.targets:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
            if path.startswith("../") or path not in files:
                missing.append({"from": name, "target": target})
    if missing:
        raise ValueError("Broken report links: " + json.dumps(missing, ensure_ascii=False))


def report_files(project, mode=None):
    project = Path(project).resolve()
    reports = project / "reports"
    files, sections = {}, []

    def add(path, pilot=None):
        path = Path(path)
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(reports) or not path.is_file():
            raise ValueError("Report file missing or outside reports: " + str(path))
        if pilot is not None and not resolved.is_relative_to(pilot.resolve()):
            raise ValueError("A pilot entry references another directory")
        if path.suffix not in {".json", ".html", ".diff"}:
            raise ValueError("Unexpected report artifact")
        relative = resolved.relative_to(project).as_posix()
        files[relative] = path.read_bytes()
        return relative

    for summary_path in sorted(reports.glob("*-pilot-*/summary.json")):
        pilot = summary_path.parent
        summary = json.loads(summary_path.read_text())
        dependency_mode = summary.get("dependency_mode")
        if mode and dependency_mode != mode:
            continue
        if summary.get("data_origin") != "execution":
            raise ValueError("Pilot summary must describe recorded execution")
        add(summary_path, pilot)
        diagnosis = pilot / "environment-diagnosis.json"
        excluded = diagnosis.exists() and json.loads(diagnosis.read_text()).get("evaluation_eligible") is False
        rows = []
        for entry in summary["runs"]:
            label = entry["label"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", label):
                raise ValueError("Invalid report label")
            report = Path(entry["report"])
            report_json = report.with_suffix(".json")
            relative = add(report, pilot)
            add(report_json, pilot)
            record = pilot / (label + ".json")
            add(record, pilot)
            body = json.loads(record.read_text())
            evidence = json.loads(report_json.read_text())
            if body["run_id"] != entry["run_id"] or body["result_status"] != entry["result_status"] or evidence["status"] != entry["result_status"]:
                raise ValueError("Summary, API record and report disagree")
            rows.append("<tr><td>" + html.escape(label) + "</td><td><code>" + html.escape(entry["result_status"]) +
                        "</code></td><td><a href='" + html.escape(relative, quote=True) + "'>開啟 HTML 報告</a></td></tr>")
        for path in sorted(pilot.glob("*")):
            if path.is_file() and path.suffix in {".json", ".diff"} and path.name != "session.json":
                add(path, pilot)
        note = "環境故障紀錄，排除於成果" if excluded else "開發紀錄；不是正式保留集評估"
        sections.append("<section><h2>" + html.escape(dependency_mode or "mode not recorded") + "</h2><p>" +
                        html.escape(pilot.name + " · " + note) + "</p><table><thead><tr><th>任務</th><th>結果</th><th>報告</th></tr></thead><tbody>" +
                        "".join(rows) + "</tbody></table></section>")
    for pilot in sorted(reports.glob("model-pilot-*")):
        evidence_path, ledger_path = pilot / "report.json", pilot / "model-ledger.json"
        if not evidence_path.is_file() or not ledger_path.is_file():
            raise ValueError("Model pilot is incomplete; retain evidence before packaging")
        evidence, ledger = json.loads(evidence_path.read_text()), json.loads(ledger_path.read_text())
        if mode and evidence.get("dependency_mode") != mode:
            continue
        actual_calls = sum(call.get("data_origin") == "nebius_api" and bool(call.get("dispatch_started_at"))
                           for call in ledger.get("calls", []))
        if (not evidence.get("finished_at") or evidence.get("model_calls") != ledger.get("model_calls") or
            actual_calls != ledger.get("model_calls")):
            raise ValueError("Model report and ledger disagree")
        relative = add(pilot / "report.html", pilot)
        for name in ("report.json", "model-ledger.json", "authorization.json", "preflight.json"):
            add(pilot / name, pilot)
        for name in ("candidate.diff", "outcome-audit.json"):
            if (pilot / name).is_file():
                add(pilot / name, pilot)
        cost = ledger.get("calculated_usd")
        billed = ledger.get("billed_usd")
        sections.append("<section><h2>Nano → Ultra 單輪實測</h2><p>" + html.escape(pilot.name) +
            " · " + html.escape(evidence["status"]) + " · 模型呼叫 " + str(actual_calls) +
            "</p><p>按實際 usage 計算 USD " + html.escape(str(cost) if cost is not None else "未知") +
            "；帳戶實際扣款 " + html.escape(str(billed) if billed is not None else "尚未對帳") +
            "。這是開發案例，沒有正式保留集或上游維護者批准。</p><p><a href='" + html.escape(relative, quote=True) +
            "'>開啟模型實測 HTML 報告</a></p><p>" + html.escape(evidence.get("reason", "")) + "</p></section>")
    for binding_path in sorted(reports.glob("model-trial-*/trial-binding.json")):
        pilot = binding_path.parent
        binding = json.loads(binding_path.read_text())
        evidence = json.loads((pilot / "report.json").read_text())
        record = json.loads((pilot / "run-record.json").read_text())
        if mode and evidence.get("dependency_mode") != mode:
            continue
        if (binding.get("scope") != "development" or binding.get("evaluation_eligible") is not False or
            binding.get("run_state") not in {"completed", "failed"} or record.get("data_origin") != "execution" or
            evidence.get("data_origin") == "engineering_test" or evidence.get("environment", {}).get("test_double") or
            record.get("run_id") != evidence.get("run_id") or record.get("result_status") != evidence.get("status") or
            binding.get("run_id") != evidence.get("run_id") or record.get("evidence") != evidence or
            record.get("run_state") != binding.get("run_state") or record.get("evaluation_eligible") is not False or
            binding.get("result_status") != evidence.get("status") or
            hashlib.sha256((pilot / "report.json").read_bytes()).hexdigest() != binding.get("report_sha256")):
            raise ValueError("Development trial report, record and binding disagree or contain engineering data")
        ledger_path = pilot / "model-ledger.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"calls": [], "model_calls": 0, "calculated_usd": "0", "billed_usd": "0"}
        actual_calls = sum(call.get("data_origin") == "nebius_api" and bool(call.get("dispatch_started_at")) for call in ledger["calls"])
        if (actual_calls != ledger.get("model_calls") or evidence.get("model_calls") != actual_calls or
            any(call.get("data_origin") != "nebius_api" for call in ledger["calls"]) or
            binding.get("model_calls") != actual_calls or record.get("costs", {}).get("model_calls") != ledger["calls"] or
            (ledger_path.exists() and hashlib.sha256(ledger_path.read_bytes()).hexdigest() != binding.get("ledger_sha256"))):
            raise ValueError("Development trial model ledger differs from its report")
        relative = add(pilot / "report.html", pilot)
        for name in ("report.json", "run-record.json", "trial-plan.json", "trial-binding.json"):
            add(pilot / name, pilot)
        for name in ("model-ledger.json", "model-access-preflight.json", "authorization.json", "outcome-audit.json",
                     "offline-context-regression.json", "candidate.diff"):
            if (pilot / name).is_file():
                add(pilot / name, pilot)
        cost = ledger.get("calculated_usd")
        sections.append("<section><h2>模型開發試跑 · " + html.escape(evidence.get("repair_route", "route unknown")) +
            "</h2><p>" + html.escape(pilot.name) + " · " + html.escape(evidence["status"]) + " · 模型呼叫 " + str(actual_calls) +
            " · 最多 " + str(evidence.get("max_patch_rounds", "?")) + " 輪</p><p>按 usage 計算 USD " +
            html.escape(str(cost) if cost is not None else "未知") + "；實際扣款 " +
            html.escape(str(ledger["billed_usd"]) if ledger.get("billed_usd") is not None else "尚未對帳") +
            "。這是開發紀錄，不計正式評估成績。</p><p><a href='" + html.escape(relative, quote=True) +
            "'>開啟完整模型開發報告</a></p><p>" + html.escape(evidence.get("reason", "")) + "</p></section>")
    for record_path in sorted(reports.glob("live-json2yaml-*/run-record.json")):
        pilot = record_path.parent
        record = json.loads(record_path.read_text())
        evidence = json.loads((pilot / "report.json").read_text())
        if mode and evidence.get("dependency_mode") != mode:
            continue
        if (record.get("data_origin") != "execution" or record.get("run_state") != "completed" or
            record.get("evaluation_eligible") is not False or record.get("evidence") != evidence or
            record.get("run_id") != evidence.get("run_id") or record.get("result_status") != evidence.get("status") or
            record.get("case_id") != "json2yaml-yaml2json" or evidence.get("model_calls") != 0 or
            evidence.get("environment", {}).get("test_double") or not evidence.get("finished_at")):
            raise ValueError("json2yaml live report and record disagree or contain engineering data")
        relative = add(pilot / "report.html", pilot)
        add(record_path, pilot)
        add(pilot / "report.json", pilot)
        sections.append("<section><h2>json2yaml 即時 PyPI 開發試跑</h2><p>" + html.escape(pilot.name) +
            " · " + html.escape(evidence["status"]) + " · 模型呼叫 0。沿用既有團隊驗收；不計正式保留集成績。</p>" +
            "<p><a href='" + html.escape(relative, quote=True) + "'>開啟 json2yaml live 報告</a></p></section>")
    if not sections:
        raise ValueError("No complete recorded pilot reports were found")
    diagnostics = []
    for path in (sorted(reports.glob("egress-gate-*.json")) +
                 sorted(reports.glob("platform-release-gate-*.json")) +
                 sorted(reports.glob("json2yaml-wheel-preparation*.json")) +
                 sorted(reports.glob("inference-preflight-*.json"))):
        relative = add(path)
        diagnostics.append("<li><a href='" + html.escape(relative, quote=True) + "'>" + html.escape(path.name) + "</a></li>")
    index = """<!doctype html><html lang='zh-Hant'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
<title>Fresh Eyes 真實報告</title><style>body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#17222a}section{margin:28px 0;padding:20px;border:1px solid #d5dedc;border-radius:12px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}code{overflow-wrap:anywhere}a{color:#076952}p{max-width:90ch}</style>
<h1>Fresh Eyes 真實執行報告</h1><p>這個頁面可以直接雙擊開啟，不需要 API、Docker、Python 或網路。先解壓整個資料夾，再開 OPEN_REPORTS.html。</p>
<p>這些是已保存的真實紀錄；開啟報告不會重新執行。開發 repo 是 mistletoe、md2html、json2yaml 三個，離線與即時 PyPI 重跑不增加 repo 數。先前人工候選與獨立模型實測分別標示；未通過的模型候選不算 AI 修復成功。這些紀錄沒有證明已發生依賴漂移。</p>""" + "".join(sections) + "<section><h2>出站驗證與套件建置</h2><ul>" + "".join(diagnostics) + "</ul><p>JSON 是原始資料；主要功能結果請從上面的 HTML 報告閱讀。套件建置只代表 artifact 準備；功能是否通過，請看上方獨立試跑結果。</p></section></html>"
    files["OPEN_REPORTS.html"] = index.encode()
    files["READ_ME_FIRST.txt"] = ("解壓整個資料夾後，雙擊 OPEN_REPORTS.html。\n不要只把入口 HTML 單獨拖出來。\n不需要啟動 API；這是已保存的真實報告，不會執行 repo 或呼叫付費 API。\n此包不含 SQLite、憑證、VM、wheel 或第三方 checkout。\n").encode()
    validate_links(files)
    return files


def write_archive(output, files, *, prefix="fresh-eyes-report-review", kind="fresh-eyes-portable-reports-v1"):
    validate_links(files)
    manifest = {"format": kind, "paid_calls_enabled": False,
                "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or path.suffix in {".db", ".sqlite", ".sqlite3", ".whl"}:
                raise ValueError("Unexpected archive path")
            archive.writestr(prefix + "/" + name, raw)
        archive.writestr(prefix + "/manifest.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise ValueError("Archive integrity check failed")
        for name, expected in manifest["files"].items():
            if hashlib.sha256(archive.read(prefix + "/" + name)).hexdigest() != expected:
                raise ValueError("Archive manifest mismatch")
    return {"archive": str(output), "entries": len(files) + 1, "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
